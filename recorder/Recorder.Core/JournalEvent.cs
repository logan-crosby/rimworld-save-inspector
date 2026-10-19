using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace RimWorld.LifeRecorder.Core
{
    public static class RecorderLimits
    {
        public const int MaxLineBytes = 64 * 1024;
        public const int DefaultQueueRecords = 4096;
        public const long DefaultQueueBytes = 16L * 1024 * 1024;
        public const long DefaultSegmentBytes = 16L * 1024 * 1024;
        public const long DefaultCampaignBytes = 512L * 1024 * 1024;
        public const int MaxGapRanges = 32;
    }

    /// <summary>An immutable snapshot of one recorder envelope. Payload and extensions are cloned at both boundaries.</summary>
    public sealed class JournalEvent
    {
        private readonly JObject _payload;
        private readonly JObject? _extensions;
        private readonly byte[] _jsonLine;

        public string SchemaVersion { get; }
        public string RecorderVersion { get; }
        public string GameVersion { get; }
        public string CampaignId { get; }
        public string SessionId { get; }
        public long Sequence { get; }
        public string EventId { get; }
        public string EventType { get; }
        public long? TicksGame { get; }
        public long? TicksAbs { get; }
        public long? GameStartAbsTick { get; }
        public string? SubjectId { get; }
        public IReadOnlyList<string> RelatedIds { get; }
        public string? MapId { get; }
        public string? CheckpointId { get; }
        public string Collector { get; }
        public string Semantics { get; }
        public JObject Payload => (JObject)_payload.DeepClone();
        public JObject? Extensions => _extensions == null ? null : (JObject)_extensions.DeepClone();
        public int SerializedBytes => _jsonLine.Length;
        public byte[] ToUtf8JsonLine() => (byte[])_jsonLine.Clone();

        public JournalEvent(string recorderVersion, string gameVersion, string campaignId, string sessionId,
            long sequence, string eventType, long? ticksGame, long? ticksAbs, long? gameStartAbsTick,
            string? subjectId, IEnumerable<string>? relatedIds, string? mapId, string? checkpointId,
            string collector, string semantics, JObject payload, JObject? extensions = null)
        {
            RequireText(recorderVersion, nameof(recorderVersion));
            RequireText(gameVersion, nameof(gameVersion));
            RequireText(campaignId, nameof(campaignId));
            RequireText(sessionId, nameof(sessionId));
            RequireText(eventType, nameof(eventType));
            RequireText(collector, nameof(collector));
            if (sequence <= 0) throw new ArgumentOutOfRangeException(nameof(sequence));
            if (subjectId != null) RequireText(subjectId, nameof(subjectId));
            if (mapId != null) RequireText(mapId, nameof(mapId));
            if (checkpointId != null) RequireText(checkpointId, nameof(checkpointId));
            if (semantics != "recorded_event" && semantics != "state_observation" && semantics != "observed_difference" && semantics != "coverage")
                throw new ArgumentException("Unsupported source semantics.", nameof(semantics));
            if (ticksGame.HasValue && gameStartAbsTick.HasValue && ticksAbs.HasValue && ticksAbs.Value != checked(ticksGame.Value + gameStartAbsTick.Value))
                throw new ArgumentException("Known absolute tick must equal game tick plus game start tick.");
            SchemaVersion = "1.0";
            RecorderVersion = recorderVersion;
            GameVersion = gameVersion;
            CampaignId = campaignId;
            SessionId = sessionId;
            Sequence = sequence;
            EventId = sessionId + ":" + sequence.ToString(System.Globalization.CultureInfo.InvariantCulture);
            EventType = eventType;
            TicksGame = ticksGame;
            TicksAbs = ticksAbs;
            GameStartAbsTick = gameStartAbsTick;
            SubjectId = subjectId;
            var ids = (relatedIds ?? Enumerable.Empty<string>()).ToArray();
            if (ids.Any(string.IsNullOrWhiteSpace) || ids.Distinct(StringComparer.Ordinal).Count() != ids.Length)
                throw new ArgumentException("Related IDs must be nonempty and distinct.", nameof(relatedIds));
            RelatedIds = new ReadOnlyCollection<string>(ids);
            MapId = mapId;
            CheckpointId = checkpointId;
            Collector = collector;
            Semantics = semantics;
            _payload = payload == null ? throw new ArgumentNullException(nameof(payload)) : (JObject)payload.DeepClone();
            _extensions = extensions == null ? null : (JObject)extensions.DeepClone();
            _jsonLine = EncodingWithoutBom.GetBytes(ToJson());
            if (_jsonLine.Length > RecorderLimits.MaxLineBytes)
                throw new InvalidOperationException("Serialized event exceeds the 64 KiB line limit.");
        }

        public string ToJson()
        {
            var envelope = new JObject
            {
                ["schema_version"] = SchemaVersion,
                ["recorder_version"] = RecorderVersion,
                ["game_version"] = GameVersion,
                ["campaign_id"] = CampaignId,
                ["session_id"] = SessionId,
                ["sequence"] = Sequence,
                ["event_id"] = EventId,
                ["event_type"] = EventType,
                ["ticks_game"] = JToken.FromObject(TicksGame as object ?? JValue.CreateNull()),
                ["ticks_abs"] = JToken.FromObject(TicksAbs as object ?? JValue.CreateNull()),
                ["game_start_abs_tick"] = JToken.FromObject(GameStartAbsTick as object ?? JValue.CreateNull()),
                ["subject_id"] = SubjectId == null ? JValue.CreateNull() : new JValue(SubjectId),
                ["related_ids"] = new JArray(RelatedIds),
                ["map_id"] = MapId == null ? JValue.CreateNull() : new JValue(MapId),
                ["checkpoint_id"] = CheckpointId == null ? JValue.CreateNull() : new JValue(CheckpointId),
                ["source"] = new JObject { ["collector"] = Collector, ["semantics"] = Semantics },
                ["payload"] = _payload.DeepClone()
            };
            if (_extensions != null) envelope["extensions"] = _extensions.DeepClone();
            return envelope.ToString(Formatting.None);
        }

        public static JournalEvent FromJson(string json)
        {
            var token = JToken.Parse(json, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error, LineInfoHandling = LineInfoHandling.Ignore });
            var obj = token as JObject ?? throw new JsonSerializationException("Event line must be a JSON object.");
            var allowed = new HashSet<string>(new[] { "schema_version", "recorder_version", "game_version", "campaign_id", "session_id", "sequence", "event_id", "event_type", "ticks_game", "ticks_abs", "game_start_abs_tick", "subject_id", "related_ids", "map_id", "checkpoint_id", "source", "payload", "extensions" }, StringComparer.Ordinal);
            if (obj.Properties().Any(p => !allowed.Contains(p.Name))) throw new JsonSerializationException("Unexpected top-level envelope field; extensions belong inside extensions.");
            if (allowed.Where(k => k != "extensions").Any(k => !obj.ContainsKey(k))) throw new JsonSerializationException("Envelope is missing a required field.");
            if ((string?)obj["schema_version"] != "1.0") throw new JsonSerializationException("Unsupported schema version.");
            var source = (JObject?)obj["source"] ?? throw new JsonSerializationException("Missing source object.");
            var result = new JournalEvent(
                RequiredString(obj, "recorder_version"), RequiredString(obj, "game_version"), RequiredString(obj, "campaign_id"), RequiredString(obj, "session_id"),
                RequiredLong(obj, "sequence"), RequiredString(obj, "event_type"), NullableLong(obj, "ticks_game"), NullableLong(obj, "ticks_abs"), NullableLong(obj, "game_start_abs_tick"),
                NullableString(obj, "subject_id"), ((JArray?)obj["related_ids"] ?? new JArray()).Select(x => (string?)x ?? string.Empty), NullableString(obj, "map_id"), NullableString(obj, "checkpoint_id"),
                RequiredString(source, "collector"), RequiredString(source, "semantics"), (JObject?)obj["payload"] ?? new JObject(), (JObject?)obj["extensions"]);
            if ((string?)obj["event_id"] != result.EventId) throw new JsonSerializationException("event_id must equal session_id:sequence.");
            if (!(obj["related_ids"] is JArray) || !(obj["payload"] is JObject)) throw new JsonSerializationException("related_ids and payload must be JSON array/object values.");
            return result;
        }

        private static string RequiredString(JObject obj, string name) => (string?)obj[name] is string s && !string.IsNullOrWhiteSpace(s) ? s : throw new JsonSerializationException("Invalid " + name + ".");
        private static long RequiredLong(JObject obj, string name) => (long?)obj[name] ?? throw new JsonSerializationException("Invalid " + name + ".");
        private static long? NullableLong(JObject obj, string name) => obj[name]?.Type == JTokenType.Null ? null : (long?)obj[name];
        private static string? NullableString(JObject obj, string name) => obj[name]?.Type == JTokenType.Null ? null : (string?)obj[name];
        private static void RequireText(string value, string name) { if (string.IsNullOrWhiteSpace(value)) throw new ArgumentException("Value must be nonempty.", name); }
        private static class EncodingWithoutBom { public static readonly System.Text.UTF8Encoding Value = new System.Text.UTF8Encoding(false, true); public static byte[] GetBytes(string text) => Value.GetBytes(text); }
    }
}
