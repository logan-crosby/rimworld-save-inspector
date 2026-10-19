using System;
using System.Collections.Generic;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json.Linq;

namespace RimWorld.LifeRecorder.Core
{
    public enum BirthObserveResult { Staged, Merged, LateObservation, LateConflict, Overflow, CapacityExhausted }

    /// <summary>One immutable fact observed by a birth-specific native callback.</summary>
    public sealed class BirthObservation
    {
        public string SubjectId { get; }
        public string Source { get; }
        public string Kind { get; }
        public string? MotherId { get; }
        public string? FatherId { get; }
        public string? BirtherId { get; }
        public RecorderTimeBasis TimeBasis { get; }

        public BirthObservation(string subjectId, string source, string kind, string? motherId, string? fatherId,
            string? birtherId, RecorderTimeBasis timeBasis)
        {
            Require(subjectId, nameof(subjectId)); Require(source, nameof(source));
            if (kind != "unknown" && kind != "biological_birth" && kind != "hatching")
                throw new ArgumentException("Birth kind must be unknown, biological_birth, or hatching.", nameof(kind));
            SubjectId = subjectId; Source = source; Kind = kind; MotherId = Clean(motherId); FatherId = Clean(fatherId);
            BirtherId = Clean(birtherId); TimeBasis = timeBasis ?? throw new ArgumentNullException(nameof(timeBasis));
        }

        public JObject ToJson() => new JObject
        {
            ["source"] = Source,
            ["birth_kind"] = Kind,
            ["mother_id"] = MotherId == null ? JValue.CreateNull() : new JValue(MotherId),
            ["father_id"] = FatherId == null ? JValue.CreateNull() : new JValue(FatherId),
            ["birther_id"] = BirtherId == null ? JValue.CreateNull() : new JValue(BirtherId),
            ["ticks_game"] = Nullable(TimeBasis.TicksGame),
            ["ticks_abs"] = Nullable(TimeBasis.TicksAbs),
            ["game_start_abs_tick"] = Nullable(TimeBasis.GameStartAbsTick),
            ["starting_year"] = TimeBasis.StartingYear.HasValue ? new JValue(TimeBasis.StartingYear.Value) : JValue.CreateNull()
        };

        private static JValue Nullable(long? value) => value.HasValue ? new JValue(value.Value) : JValue.CreateNull();
        private static string? Clean(string? value) => String.IsNullOrWhiteSpace(value) ? null : value;
        private static void Require(string value, string name) { if (String.IsNullOrWhiteSpace(value)) throw new ArgumentException("Value is required.", name); }
    }

    public sealed class PendingBirth
    {
        public string SubjectId { get; }
        public RecorderTimeBasis FirstObservedTime { get; }
        public IReadOnlyList<string> RelatedIds { get; }
        public JObject Payload { get; }
        public long ReservedBytes { get; }
        internal PendingBirth(string subjectId, RecorderTimeBasis firstObservedTime, string[] relatedIds, JObject payload, long reservedBytes)
        { SubjectId = subjectId; FirstObservedTime = firstObservedTime; RelatedIds = relatedIds; Payload = payload; ReservedBytes = reservedBytes; }
    }

    /// <summary>
    /// Coalesces synchronous/asynchronous sibling birth callbacks within one recorder session. Pending event bytes are
    /// reserved against the same queue limit, and completed IDs remain session-scoped to suppress late duplicates.
    /// </summary>
    public sealed class BirthObservationBuffer
    {
        private sealed class Staged
        {
            internal readonly List<BirthObservation> Observations = new List<BirthObservation>();
            internal long ReservedBytes;
        }

        private readonly string _sessionId;
        private readonly RecorderQueue _queue;
        private readonly int _maxPendingSubjects;
        private readonly int _maxSeenSubjects;
        private readonly Dictionary<string, Staged> _pending = new Dictionary<string, Staged>(StringComparer.Ordinal);
        private readonly List<string> _pendingOrder = new List<string>();
        private readonly Dictionary<string, string> _seenFirstObservationDigest = new Dictionary<string, string>(StringComparer.Ordinal);

        public long StagedBytes => _pending.Values.Sum(x => x.ReservedBytes);
        public int PendingCount => _pending.Count;
        public int CompletedSubjectCount => _seenFirstObservationDigest.Count;

        public BirthObservationBuffer(string sessionId, RecorderQueue queue, int maxPendingSubjects = 1024, int maxSeenSubjects = 100000)
        {
            if (String.IsNullOrWhiteSpace(sessionId)) throw new ArgumentException("Session ID is required.", nameof(sessionId));
            _sessionId = sessionId; _queue = queue ?? throw new ArgumentNullException(nameof(queue));
            if (!String.Equals(_sessionId, _queue.SessionId, StringComparison.Ordinal)) throw new ArgumentException("Birth buffer session must match its recorder queue.", nameof(sessionId));
            if (maxPendingSubjects < 1 || maxSeenSubjects < 1) throw new ArgumentOutOfRangeException("Birth buffer bounds must be positive.");
            _maxPendingSubjects = maxPendingSubjects; _maxSeenSubjects = maxSeenSubjects;
        }

        public BirthObserveResult Observe(BirthObservation observation)
        {
            if (observation == null) throw new ArgumentNullException(nameof(observation));
            if (_seenFirstObservationDigest.TryGetValue(observation.SubjectId, out var firstDigest))
                return String.Equals(firstDigest, Digest(observation), StringComparison.Ordinal) ? BirthObserveResult.LateObservation : BirthObserveResult.LateConflict;
            if (!_pending.TryGetValue(observation.SubjectId, out var staged))
            {
                if (_seenFirstObservationDigest.Count >= _maxSeenSubjects) return BirthObserveResult.CapacityExhausted;
                if (_pending.Count >= _maxPendingSubjects) return BirthObserveResult.Overflow;
                var created = new Staged(); created.Observations.Add(observation);
                var preview = CreatePending(observation.SubjectId, created.Observations, 0);
                var bytes = PreviewEvent(preview).SerializedBytes;
                if (!_queue.TryChangeReservedBytes(bytes)) return BirthObserveResult.Overflow;
                created.ReservedBytes = bytes; _pending.Add(observation.SubjectId, created); _pendingOrder.Add(observation.SubjectId);
                return BirthObserveResult.Staged;
            }

            var merged = new List<BirthObservation>(staged.Observations) { observation };
            var pending = CreatePending(observation.SubjectId, merged, 0);
            var newBytes = PreviewEvent(pending).SerializedBytes;
            var delta = newBytes - staged.ReservedBytes;
            if (!_queue.TryChangeReservedBytes(delta)) return BirthObserveResult.Overflow;
            staged.Observations.Add(observation); staged.ReservedBytes = newBytes;
            return BirthObserveResult.Merged;
        }

        /// <summary>Promotes staged births in observation order; call on GameComponentUpdate, before save, and before close.</summary>
        public int Flush()
        {
            var completed = 0;
            foreach (var subject in _pendingOrder.ToArray())
            {
                if (!_pending.TryGetValue(subject, out var staged)) continue;
                var item = CreatePending(subject, staged.Observations, staged.ReservedBytes);
                var accepted = _queue.TryEnqueueReserved(staged.ReservedBytes, sequence => CreateEvent(sequence, item), out _);
                if (!accepted) continue;
                _pending.Remove(subject); _pendingOrder.Remove(subject);
                _seenFirstObservationDigest.Add(subject, Digest(staged.Observations[0])); completed++;
            }
            return completed;
        }

        public PendingBirth? GetPending(string subjectId) => _pending.TryGetValue(subjectId, out var staged)
            ? CreatePending(subjectId, staged.Observations, staged.ReservedBytes) : null;

        public static JObject LateObservationPayload(BirthObservation observation, bool differingFacts = false) => new JObject
        {
            ["family"] = "pawn_born", ["status"] = "late_observation_after_completed_event",
            ["subject_id"] = observation.SubjectId, ["observation"] = observation.ToJson(),
            ["differs_from_first_observation"] = differingFacts,
            ["limitations"] = "A completed pawn_born event already exists in this recorder session; this late callback is retained as coverage evidence and never emits a second completed birth."
        };

        private JournalEvent PreviewEvent(PendingBirth item) => CreateEvent(long.MaxValue, item);
        private JournalEvent CreateEvent(long sequence, PendingBirth item) => new JournalEvent(_queue.RecorderVersion, _queue.GameVersion, _queue.CampaignId, _sessionId,
            sequence, "pawn_born", item.FirstObservedTime.TicksGame, item.FirstObservedTime.TicksAbs, item.FirstObservedTime.GameStartAbsTick,
            item.SubjectId, item.RelatedIds, null, null, "pawn_born", "recorded_event", item.Payload);

        private static PendingBirth CreatePending(string subjectId, IReadOnlyList<BirthObservation> observations, long reservedBytes)
        {
            var kinds = observations.Select(x => x.Kind).Where(x => x != "unknown").Distinct(StringComparer.Ordinal).ToArray();
            var conflict = kinds.Length > 1;
            var kind = conflict || kinds.Length == 0 ? "unknown" : kinds[0];
            var mothers = Unique(observations.Select(x => x.MotherId));
            var fathers = Unique(observations.Select(x => x.FatherId));
            var birthers = Unique(observations.Select(x => x.BirtherId));
            var related = Unique(observations.SelectMany(x => new[] { x.MotherId, x.FatherId, x.BirtherId })).ToArray();
            var payload = new JObject
            {
                ["birth_kind"] = kind,
                ["classification_conflict"] = conflict,
                ["mother_id"] = mothers.Length == 1 ? new JValue(mothers[0]) : JValue.CreateNull(),
                ["father_id"] = fathers.Length == 1 ? new JValue(fathers[0]) : JValue.CreateNull(),
                ["birther_id"] = birthers.Length == 1 ? new JValue(birthers[0]) : JValue.CreateNull(),
                ["parent_observation_conflict"] = mothers.Length > 1 || fathers.Length > 1 || birthers.Length > 1,
                ["observations"] = new JArray(observations.Select(x => x.ToJson()))
            };
            return new PendingBirth(subjectId, observations[0].TimeBasis, related, payload, reservedBytes);
        }

        private static string[] Unique(IEnumerable<string?> values) => values.Where(x => !String.IsNullOrWhiteSpace(x)).Cast<string>().Distinct(StringComparer.Ordinal).OrderBy(x => x, StringComparer.Ordinal).ToArray();
        private static string Digest(BirthObservation observation)
        {
            using (var sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(observation.ToJson().ToString(Newtonsoft.Json.Formatting.None)))).Replace("-", "").ToLowerInvariant();
        }
    }
}
