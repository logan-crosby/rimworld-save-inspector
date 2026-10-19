using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Collections.Concurrent;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using RimWorld.LifeRecorder.Core;
using UnityEngine;
using Verse;

namespace RimWorld.LifeRecorder
{
    /// <summary>Persists only opaque campaign/checkpoint metadata. Journal payloads stay in local JSONL files.</summary>
    public sealed class RecorderSessionComponent : GameComponent
    {
        private const string RecorderVersion = "0.1.0-candidate";
        private const int MaxBaselinePawns = 1024;
        private string campaignId = "";
        private string lastCheckpointId = "";
        private string checkpointProducerSessionId = "";
        private string _verifiedCheckpointId = "";
        private string _verifiedCheckpointSessionId = "";
        private string _sessionId = "";
        private RecorderSaveAttempt? _pendingSaveAttempt;
        private RecorderQueue? _queue;
        private BirthObservationBuffer? _birthBuffer;
        private JournalWriter? _writer;
        private RecorderStatus? _lastStatusReported;
        private bool _shutdown;
        internal static RecorderSessionComponent? Current { get; private set; }
        internal long QueuedBytes => _queue?.RetainedBytes ?? 0;
        internal long QueueSerializedBytes => _queue?.QueuedBytes ?? 0;
        internal long PendingBirthBytes => _birthBuffer?.StagedBytes ?? 0;

        public RecorderSessionComponent(Game game) : base() { }

        public override void FinalizeInit()
        {
            base.FinalizeInit();
            try
            {
                FlushPendingBirths("session_reinitialize");
                if (String.IsNullOrWhiteSpace(campaignId)) campaignId = NewOpaqueId();
                _sessionId = NewOpaqueId();
                _verifiedCheckpointId = lastCheckpointId;
                _verifiedCheckpointSessionId = checkpointProducerSessionId;
                Current = this;
                WorldPawnCollector.Initialize();
                var gameVersion = VersionControl.CurrentVersionString;
                _queue = new RecorderQueue(RecorderVersion, gameVersion, campaignId, _sessionId);
                _birthBuffer = new BirthObservationBuffer(_sessionId, _queue);
                var root = RecorderStoragePaths.ResolveCampaignDirectory(GenFilePaths.SaveDataFolderPath, campaignId);
                _writer = new JournalWriter(_queue, root, _sessionId,
                    diagnosticsEnabled: () => RecorderDeveloperMetrics.Enabled,
                    flushDiagnostic: elapsed => RecorderDeveloperMetrics.RecordOperation("flush", elapsed));
                UnityEngine.Application.quitting += OnApplicationQuitting;
                WriteSessionStarted(gameVersion);
                WriteCoverageRecords();
                Record("collector_manifest", "coverage", null, null, new JObject
                {
                    ["family"] = "pawn_holder_changed",
                    ["scope"] = "world_pawn_pool_accessors",
                    ["status"] = WorldPawnCollector.InitializationStatus,
                    ["limitations"] = "Pool membership is read from cached native alive/mothballed/dead HashSet references; initialization failure leaves world-pool status unknown."
                }, "coverage");
                WriteBaseline();
            }
            catch (Exception ex)
            {
                Log.Error("[Life Recorder] Recorder startup failed safely: " + ex.GetType().Name + ".");
                _writer?.Dispose();
                _writer = null;
                _queue = null;
            }
        }

        public override void ExposeData()
        {
            if (Scribe.mode == LoadSaveMode.Saving)
            {
                FlushPendingBirths("save_serialization");
                if (String.IsNullOrWhiteSpace(campaignId)) campaignId = NewOpaqueId();
                var attempt = _pendingSaveAttempt;
                if (attempt == null)
                {
                    attempt = new RecorderSaveAttempt("", campaignId, _sessionId, NewOpaqueId(), NewOpaqueId(),
                        String.IsNullOrWhiteSpace(_verifiedCheckpointId) ? null : _verifiedCheckpointId, false);
                    _pendingSaveAttempt = attempt;
                }
                var observed = _queue?.HighestAllocatedSequence ?? 0;
                var written = _queue?.HighestWrittenSequence ?? 0;
                var durable = _queue?.HighestDurableSequence ?? 0;
                attempt.CaptureWatermarks(observed, written, Math.Min(durable, written));
                lastCheckpointId = attempt.CheckpointId;
                checkpointProducerSessionId = attempt.ProducerSessionId;
                Record("save_lifecycle", "save_checkpoint", null, null, new JObject
                {
                    ["checkpoint_id"] = attempt.CheckpointId,
                    ["save_attempt_id"] = attempt.AttemptId,
                    ["status"] = "attempted",
                    ["parent_checkpoint_id"] = attempt.ParentCheckpointId == null ? JValue.CreateNull() : new JValue(attempt.ParentCheckpointId),
                    ["observed_sequence"] = observed,
                    ["written_sequence"] = written,
                    ["durable_sequence"] = Math.Min(durable, written)
                }, "coverage", attempt.CheckpointId);
            }
            Scribe_Values.Look(ref campaignId, "lifeRecorderCampaignId", "");
            Scribe_Values.Look(ref lastCheckpointId, "lifeRecorderCheckpointId", "");
            Scribe_Values.Look(ref checkpointProducerSessionId, "lifeRecorderCheckpointSession", "");
        }

        public override void GameComponentTick()
        {
            base.GameComponentTick();
            DrainSaveVerificationResults();
            if (_writer == null || _writer.Status == _lastStatusReported) return;
            _lastStatusReported = _writer.Status;
            if (_writer.Status == RecorderStatus.StorageLimit || _writer.Status == RecorderStatus.IoFailure || _writer.Status == RecorderStatus.UnflushedTail)
                Log.Warning("[Life Recorder] " + _writer.Status + ": " + (_writer.LastError ?? "recording coverage is interrupted; inspect the latest journal tail."));
        }

        public override void GameComponentUpdate()
        {
            base.GameComponentUpdate();
            FlushPendingBirths("game_component_update");
            DrainSaveVerificationResults();
        }

        internal RecorderSaveAttempt? BeginSave(string path)
        {
            try
            {
                if (_shutdown || _queue == null || String.IsNullOrWhiteSpace(campaignId)) return null;
                FlushPendingBirths("save_attempt");
                var attempt = new RecorderSaveAttempt(path, campaignId, _sessionId, NewOpaqueId(), NewOpaqueId(),
                    String.IsNullOrWhiteSpace(_verifiedCheckpointId) ? null : _verifiedCheckpointId, true);
                _pendingSaveAttempt = attempt;
                return attempt;
            }
            catch (Exception ex)
            {
                Log.Warning("[Life Recorder] Save attempt tracking failed safely: " + ex.GetType().Name + ".");
                return null;
            }
        }

        internal void SaveCompleted(RecorderSaveAttempt attempt)
        {
            if (!attempt.MarkerPersisted || String.IsNullOrWhiteSpace(attempt.OutputPath))
            {
                CompleteSave(new RecorderSaveVerificationResult(attempt, false, 0, null, "checkpoint_marker_not_persisted"));
            }
            else
            {
                try
                {
                    Task.Run(() =>
                    {
                        var result = SaveOutputVerifier.Verify(attempt.OutputPath, attempt.CampaignId,
                            attempt.CheckpointId, attempt.ProducerSessionId);
                        SaveVerificationResults.Enqueue(new RecorderSaveVerificationResult(attempt, result.Associated,
                            result.ByteCount, result.Sha256, result.FailureReason));
                    });
                }
                catch (Exception ex)
                {
                    CompleteSave(new RecorderSaveVerificationResult(attempt, false, 0, null, "verification_start_" + ex.GetType().Name));
                }
            }
            if (ReferenceEquals(_pendingSaveAttempt, attempt)) _pendingSaveAttempt = null;
        }

        internal void SaveFailed(RecorderSaveAttempt attempt, Exception exception)
        {
            try
            {
                CompleteSave(new RecorderSaveVerificationResult(attempt, false, 0, null,
                    "safe_save_exception_" + exception.GetType().Name));
            }
            catch (Exception ex)
            {
                Log.Warning("[Life Recorder] Failed-save reporting was isolated after " + ex.GetType().Name + ".");
            }
            if (ReferenceEquals(_pendingSaveAttempt, attempt)) _pendingSaveAttempt = null;
        }

        private void DrainSaveVerificationResults()
        {
            while (SaveVerificationResults.TryDequeue(out var result))
            {
                try { CompleteSave(result); }
                catch (Exception ex) { Log.Warning("[Life Recorder] Save verification result was isolated after " + ex.GetType().Name + "."); }
            }
        }

        private void CompleteSave(RecorderSaveVerificationResult result)
        {
            var attempt = result.Attempt;
            var observed = _queue?.HighestAllocatedSequence ?? 0;
            var written = _queue?.HighestWrittenSequence ?? 0;
            var durable = _queue?.HighestDurableSequence ?? 0;
            Record("save_lifecycle", "save_checkpoint", null, null, new JObject
            {
                ["checkpoint_id"] = attempt.CheckpointId,
                ["save_attempt_id"] = attempt.AttemptId,
                ["status"] = result.Associated ? "succeeded" : "failed",
                ["parent_checkpoint_id"] = attempt.ParentCheckpointId == null ? JValue.CreateNull() : new JValue(attempt.ParentCheckpointId),
                ["observed_sequence"] = observed,
                ["written_sequence"] = written,
                ["durable_sequence"] = Math.Min(durable, written),
                ["output_association"] = result.Associated ? "verified" : "unproven",
                ["output_bytes"] = result.ByteCount,
                ["output_sha256"] = result.Sha256 == null ? JValue.CreateNull() : new JValue(result.Sha256),
                ["failure_reason"] = result.FailureReason == null ? JValue.CreateNull() : new JValue(result.FailureReason)
            }, "coverage", attempt.CheckpointId);

            if (result.Associated)
            {
                _verifiedCheckpointId = attempt.CheckpointId;
                _verifiedCheckpointSessionId = attempt.ProducerSessionId;
            }
            else if (lastCheckpointId == attempt.CheckpointId)
            {
                lastCheckpointId = _verifiedCheckpointId;
                checkpointProducerSessionId = _verifiedCheckpointSessionId;
            }
        }

        private static readonly ConcurrentQueue<RecorderSaveVerificationResult> SaveVerificationResults = new ConcurrentQueue<RecorderSaveVerificationResult>();

        internal void Record(string collector, string eventType, string? subjectId, IEnumerable<string>? relatedIds,
            object payload, string semantics = "recorded_event", string? eventCheckpointId = null, string? eventMapId = null)
        {
            if (_queue == null || _writer == null || _shutdown) return;
            try
            {
                var frozenRelated = (relatedIds ?? Enumerable.Empty<string>()).ToArray();
                var frozenPayload = payload is JObject jo ? (JObject)jo.DeepClone() : JObject.FromObject(payload);
                var timeBasis = RecorderClockCapture.Capture();
                var checkpoint = eventCheckpointId ?? (String.IsNullOrWhiteSpace(lastCheckpointId) ? null : lastCheckpointId);
                var accepted = _queue.TryEnqueue(seq => new JournalEvent(RecorderVersion, VersionControl.CurrentVersionString,
                    campaignId, _sessionId, seq, eventType, timeBasis.TicksGame, timeBasis.TicksAbs, timeBasis.GameStartAbsTick, subjectId, frozenRelated,
                    eventMapId, checkpoint, collector, semantics, frozenPayload), out _);
                RecorderDeveloperMetrics.ObserveRetention(_queue.QueuedBytes, _birthBuffer?.StagedBytes ?? 0);
                _writer.Signal();
                if (!accepted && !_overflowWarned)
                {
                    _overflowWarned = true;
                    Log.Warning("[Life Recorder] The bounded journal queue overflowed. Dropped sequence IDs will be reported by a gap record if storage recovers.");
                }
            }
            catch (Exception ex) { Log.Warning("[Life Recorder] Event collection failed safely: " + ex.GetType().Name + "."); }
        }
        private bool _overflowWarned;

        internal BirthObserveResult ObserveBirth(BirthObservation observation)
        {
            try
            {
                if (_birthBuffer == null || _queue == null || _shutdown)
                {
                    Record("birth_collector", "coverage", observation.SubjectId,
                        new[] { observation.MotherId, observation.FatherId, observation.BirtherId }.Where(x => !String.IsNullOrWhiteSpace(x)).Cast<string>(),
                        new JObject { ["family"] = "pawn_born", ["status"] = "session_unavailable", ["observation"] = observation.ToJson() }, "coverage");
                    return BirthObserveResult.CapacityExhausted;
                }
                var result = _birthBuffer.Observe(observation);
                RecorderDeveloperMetrics.NoteBirthObservation(result);
                RecorderDeveloperMetrics.ObserveRetention(_queue.QueuedBytes, _birthBuffer.StagedBytes);
                if (result == BirthObserveResult.LateObservation || result == BirthObserveResult.LateConflict)
                {
                    Record("birth_collector", "coverage", observation.SubjectId,
                        new[] { observation.MotherId, observation.FatherId, observation.BirtherId }.Where(x => !String.IsNullOrWhiteSpace(x)).Cast<string>(),
                        BirthObservationBuffer.LateObservationPayload(observation, result == BirthObserveResult.LateConflict), "coverage");
                }
                else if (result == BirthObserveResult.Overflow || result == BirthObserveResult.CapacityExhausted)
                {
                    Record("birth_collector", "coverage", observation.SubjectId,
                        new[] { observation.MotherId, observation.FatherId, observation.BirtherId }.Where(x => !String.IsNullOrWhiteSpace(x)).Cast<string>(),
                        new JObject { ["family"] = "pawn_born", ["status"] = result == BirthObserveResult.Overflow ? "staging_or_shared_byte_budget_overflow" : "session_dedup_capacity_exhausted", ["observation"] = observation.ToJson(), ["pending_births"] = _birthBuffer.PendingCount, ["retained_bytes"] = _queue.RetainedBytes }, "coverage");
                }
                return result;
            }
            catch (Exception ex)
            {
                Log.Warning("[Life Recorder] Birth observation was isolated after " + ex.GetType().Name + ".");
                return BirthObserveResult.Overflow;
            }
        }

        private int FlushPendingBirths(string boundary)
        {
            try
            {
                var flushed = _birthBuffer?.Flush() ?? 0;
                for (var i = 0; i < flushed; i++) RecorderDeveloperMetrics.NoteTransition("pawn_born");
                if (_queue != null) RecorderDeveloperMetrics.ObserveRetention(_queue.QueuedBytes, _birthBuffer?.StagedBytes ?? 0);
                if (flushed > 0) _writer?.Signal();
                if (_birthBuffer != null && _birthBuffer.PendingCount > 0 &&
                    (boundary == "save_attempt" || boundary == "save_serialization" || boundary == "session_close" || boundary == "session_reinitialize"))
                    Log.Warning("[Life Recorder] Pending birth events could not enter the bounded journal queue at " + boundary + "; they remain staged and unflushed.");
                return flushed;
            }
            catch (Exception ex)
            {
                Log.Warning("[Life Recorder] Pending birth flush was isolated at " + boundary + " after " + ex.GetType().Name + ".");
                return 0;
            }
        }

        private void WriteSessionStarted(string gameVersion)
        {
            var loaded = String.IsNullOrWhiteSpace(lastCheckpointId) ? null : lastCheckpointId;
            var parentSession = String.IsNullOrWhiteSpace(checkpointProducerSessionId) ? null : checkpointProducerSessionId;
            var timeBasis = RecorderClockCapture.Capture();
            var collectors = new JArray(RecorderBootstrap.Coverage.Select(c => new JObject
            {
                ["family"] = c.Family, ["status"] = c.Status, ["target"] = c.Target,
                ["limitations"] = c.Limitations, ["collector_version"] = c.CollectorVersion
            }));
            Record("session_lifecycle", "session_started", null, null, new JObject
            {
                ["loaded_checkpoint_id"] = loaded == null ? JValue.CreateNull() : new JValue(loaded),
                ["parent_session_id"] = parentSession == null ? JValue.CreateNull() : new JValue(parentSession),
                ["parent_association"] = loaded == null ? "absent" : "unproven",
                ["time_basis"] = new JObject { ["starting_year"] = timeBasis.StartingYear.HasValue ? new JValue(timeBasis.StartingYear.Value) : JValue.CreateNull() },
                ["game_identity"] = new JObject { ["game_version"] = gameVersion },
                ["collectors"] = collectors
            }, "coverage");
        }

        private void WriteCoverageRecords()
        {
            foreach (var c in RecorderBootstrap.Coverage)
                Record("collector_manifest", "coverage", null, null, new JObject
                {
                    ["family"] = c.Family, ["status"] = c.Status, ["target"] = c.Target,
                    ["collector_version"] = c.CollectorVersion, ["limitations"] = c.Limitations
                }, "coverage");
        }

        private void WriteBaseline()
        {
            var metricsStarted = RecorderDeveloperMetrics.Enabled ? System.Diagnostics.Stopwatch.GetTimestamp() : 0;
            try
            {
                var seen = new HashSet<string>(StringComparer.Ordinal);
                var droppedBefore = _queue?.DroppedCount ?? 0;
                var observed = 0;
                var truncated = false;
                foreach (var map in Find.Maps)
                foreach (var pawn in map.mapPawns.AllPawns)
                {
                    if (!IsPlayerLinked(pawn)) continue;
                    var pawnId = CollectorUtil.Id(pawn);
                    if (pawnId == null || !seen.Add(pawnId)) continue;
                    if (observed >= MaxBaselinePawns) { truncated = true; continue; }
                    observed++;
                    var relations = ReadRelations(pawn);
                    var health = ReadHealth(pawn);
                    var genes = ReadGenes(pawn, out var genesStatus);
                    var roles = ReadRoles(pawn);
                    var guestState = ReadGuestState(pawn);
                    var holder = HolderCollector.Capture(pawn);
                    Record("baseline", "pawn_baseline", pawnId, relations.Select(r => (string?)r["other_pawn_id"]).Where(id => id != null).Cast<string>().Distinct(StringComparer.Ordinal), new JObject
                    {
                        ["label_at_observation"] = SafeLabel(pawn),
                        ["faction_id"] = CollectorUtil.FactionId(pawn.Faction) is string faction ? new JValue(faction) : JValue.CreateNull(),
                        ["biological_age_years"] = ReadBiologicalAge(pawn) is double age ? new JValue(age) : JValue.CreateNull(),
                        ["relations"] = relations,
                        ["health_conditions"] = health,
                        ["genes"] = genes,
                        ["roles"] = roles,
                        ["guest_state"] = guestState,
                        ["holder_chain"] = holder.Chain,
                        ["domain_coverage"] = new JObject
                        {
                            ["faction_and_guest_state"] = "observed_for_player_linked_local_pawn",
                            ["biological_age"] = "observed_or_unknown",
                            ["relations"] = "direct_relations_only",
                            ["health"] = "current_condition_defs_only",
                            ["genes"] = genesStatus,
                            ["role"] = "ideology_role_and_most_senior_royal_title_snapshot_only",
                            ["holder"] = "current_holder_chain_for_player_linked_local_pawns; world pawns outside local maps are not censused"
                        }
                    }, "state_observation", eventMapId: "Map_" + map.uniqueID.ToString(System.Globalization.CultureInfo.InvariantCulture));
                }
                var droppedBaselineRecords = Math.Max(0, (_queue?.DroppedCount ?? droppedBefore) - droppedBefore);
                Record("baseline", "coverage", null, null, new JObject
                {
                    ["scope"] = "player_linked_pawns_on_loaded_local_maps",
                    ["observed_pawn_count"] = observed,
                    ["complete"] = !truncated && droppedBaselineRecords == 0,
                    ["dropped_baseline_records"] = droppedBaselineRecords,
                    ["truncated_at_pawn_limit"] = truncated,
                    ["limitations"] = "Does not census world pawns; unavailable fields are labeled per pawn; baseline is capped at 1024 pawn records."
                }, "coverage");
            }
            catch (Exception ex)
            {
                Record("baseline", "coverage", null, null, new JObject
                {
                    ["scope"] = "player_linked_pawns_on_loaded_local_maps", ["complete"] = false,
                    ["failure"] = ex.GetType().Name
                }, "coverage");
                Log.Warning("[Life Recorder] Initial pawn baseline is incomplete: " + ex.GetType().Name + ".");
            }
            WriteWorldPawnBaseline();
            if (metricsStarted != 0) RecorderDeveloperMetrics.RecordOperation("baseline", System.Diagnostics.Stopwatch.GetTimestamp() - metricsStarted);
        }

        private void WriteWorldPawnBaseline()
        {
            var seen = new HashSet<string>(StringComparer.Ordinal);
            var observed = 0;
            var truncated = false;
            var failed = false;
            try
            {
                var world = Find.WorldPawns;
                if (world == null) throw new InvalidOperationException("WorldPawns unavailable");
                foreach (var pawn in world.AllPawnsAliveOrDead)
                {
                    if (pawn == null) continue;
                    var id = CollectorUtil.Id(pawn);
                    if (id == null || !seen.Add(id)) continue;
                    if (observed >= MaxBaselinePawns) { truncated = true; continue; }
                    observed++;
                    var holder = HolderCollector.Capture(pawn);
                    Record("baseline", "world_pawn_baseline", id, null, new JObject
                    {
                        ["world_pool_status"] = holder.WorldPoolStatus ?? "world_pool_unknown",
                        ["holder_chain"] = holder.Chain,
                        ["holder_chain_complete"] = holder.ChainComplete,
                        ["scope"] = "native_world_pawn_pools"
                    }, "state_observation");
                }
            }
            catch (Exception ex)
            {
                failed = true;
                Record("baseline", "world_pawn_baseline", null, null, new JObject
                {
                    ["scope"] = "native_world_pawn_pools",
                    ["complete"] = false,
                    ["failure"] = ex.GetType().Name
                }, "coverage");
            }
            Record("baseline", "coverage", null, null, new JObject
            {
                ["scope"] = "native_world_pawn_pools",
                ["observed_pawn_count"] = observed,
                ["complete"] = !failed && !truncated && WorldPawnCollector.InitializationStatus == "installed",
                ["truncated_at_pawn_limit"] = truncated,
                ["pool_accessor_status"] = WorldPawnCollector.InitializationStatus,
                ["limitations"] = "Pool statuses use the native alive, mothballed, and dead sets. Census is capped at 1024 pawn observations."
            }, "coverage");
        }

        private static bool IsPlayerLinked(Pawn pawn)
        {
            if (pawn.Faction == Faction.OfPlayer) return true;
            return ReadBool(pawn, "IsPrisonerOfColony") || ReadBool(pawn, "IsSlaveOfColony");
        }

        private static string SafeLabel(Pawn pawn) { try { return pawn.LabelShortCap; } catch { return ""; } }
        private static double? ReadBiologicalAge(Pawn pawn)
        {
            try { return Convert.ToDouble(Read(Read(pawn, "ageTracker"), "AgeBiologicalYearsFloat")); } catch { return null; }
        }
        private static JArray ReadRelations(Pawn pawn)
        {
            var result = new JArray();
            try
            {
                var tracker = Read(pawn, "relations");
                if (!(Read(tracker, "DirectRelations") is IEnumerable rows)) return result;
                foreach (var row in rows)
                {
                    var other = Read(row, "otherPawn") as Pawn;
                    var def = Read(row, "def");
                    var id = CollectorUtil.Id(other);
                    if (id == null) continue;
                    result.Add(new JObject { ["other_pawn_id"] = id, ["relation_def"] = Read(def, "defName")?.ToString() ?? "" });
                }
            }
            catch { }
            return result;
        }
        private static JArray ReadHealth(Pawn pawn)
        {
            var result = new JArray();
            try
            {
                var health = Read(pawn, "health");
                var set = Read(health, "hediffSet");
                if (Read(set, "hediffs") is IEnumerable rows)
                    foreach (var row in rows) { var def = Read(Read(row, "def"), "defName")?.ToString(); if (!String.IsNullOrWhiteSpace(def)) result.Add(def); }
            }
            catch { }
            return result;
        }
        private static JArray ReadGenes(Pawn pawn, out string status)
        {
            var result = new JArray();
            try
            {
                var tracker = Read(pawn, "genes");
                if (tracker == null) { status = "not_present_or_not_available"; return result; }
                if (Read(tracker, "GenesListForReading") is IEnumerable genes)
                {
                    foreach (var gene in genes) { var def = Read(Read(gene, "def"), "defName")?.ToString(); if (!String.IsNullOrWhiteSpace(def)) result.Add(def); }
                    status = "observed_snapshot_only";
                    return result;
                }
            }
            catch { }
            status = "unavailable";
            return result;
        }
        private static JArray ReadRoles(Pawn pawn)
        {
            var result = new JArray();
            try
            {
                var ideologyTracker = Read(pawn, "ideo");
                var ideology = Read(ideologyTracker, "Ideo");
                var getRole = ideology?.GetType().GetMethod("GetRole", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic, null, new[] { typeof(Pawn) }, null);
                var role = getRole?.Invoke(ideology, new object[] { pawn });
                var roleDef = Read(Read(role, "def"), "defName")?.ToString();
                if (!String.IsNullOrWhiteSpace(roleDef)) result.Add(new JObject { ["role_kind"] = "ideology_role", ["role_def"] = roleDef });
                var title = Read(Read(pawn, "royalty"), "MostSeniorTitle");
                var titleDef = Read(title, "defName")?.ToString();
                if (!String.IsNullOrWhiteSpace(titleDef)) result.Add(new JObject { ["role_kind"] = "royal_title", ["role_def"] = titleDef });
            }
            catch { }
            return result;
        }
        private static JObject ReadGuestState(Pawn pawn)
        {
            var guest = Read(pawn, "guest");
            var hostId = Read(guest, "HostFaction") is Faction faction ? CollectorUtil.FactionId(faction) : null;
            return new JObject
            {
                ["status"] = Read(guest, "GuestStatus")?.ToString() is string status ? new JValue(status) : JValue.CreateNull(),
                ["host_faction_id"] = hostId == null ? JValue.CreateNull() : new JValue(hostId),
                ["released"] = Read(guest, "Released") is bool released ? new JValue(released) : JValue.CreateNull(),
                ["prisoner_of_colony"] = pawn.IsPrisonerOfColony,
                ["slave_of_colony"] = pawn.IsSlaveOfColony
            };
        }
        private static object? Read(object? instance, string name)
        {
            if (instance == null) return null;
            const BindingFlags flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
            try { return instance.GetType().GetProperty(name, flags)?.GetValue(instance, null) ?? instance.GetType().GetField(name, flags)?.GetValue(instance); }
            catch { return null; }
        }
        private static bool ReadBool(object instance, string name) { try { return Read(instance, name) is bool value && value; } catch { return false; } }

        private void OnApplicationQuitting()
        {
            if (_shutdown) return;
            FlushPendingBirths("session_close");
            Record("session_lifecycle", "session_ended", null, null, new JObject { ["reason"] = "application_quitting", ["clean_shutdown_requested"] = true }, "coverage");
            _shutdown = true;
            if (_writer != null && !_writer.Stop(TimeSpan.FromSeconds(2)))
                Log.Warning("[Life Recorder] Shutdown exceeded 2 seconds; the remaining journal tail is not certified durable.");
            UnityEngine.Application.quitting -= OnApplicationQuitting;
        }

        private static string NewOpaqueId()
        {
            var bytes = new byte[16];
            using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(bytes);
            return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant();
        }
    }

    internal sealed class RecorderSaveAttempt
    {
        internal string OutputPath { get; }
        internal string CampaignId { get; }
        internal string ProducerSessionId { get; }
        internal string CheckpointId { get; }
        internal string AttemptId { get; }
        internal string? ParentCheckpointId { get; }
        internal bool Hooked { get; }
        internal bool MarkerPersisted { get; private set; }
        internal long ObservedSequence { get; private set; }
        internal long WrittenSequence { get; private set; }
        internal long DurableSequence { get; private set; }

        internal RecorderSaveAttempt(string outputPath, string campaignId, string producerSessionId, string checkpointId,
            string attemptId, string? parentCheckpointId, bool hooked)
        {
            OutputPath = outputPath; CampaignId = campaignId; ProducerSessionId = producerSessionId;
            CheckpointId = checkpointId; AttemptId = attemptId; ParentCheckpointId = parentCheckpointId; Hooked = hooked;
        }

        internal void CaptureWatermarks(long observed, long written, long durable)
        {
            ObservedSequence = observed;
            WrittenSequence = written;
            DurableSequence = Math.Min(durable, written);
            MarkerPersisted = Hooked;
        }
    }

    internal sealed class RecorderSaveVerificationResult
    {
        internal RecorderSaveAttempt Attempt { get; }
        internal bool Associated { get; }
        internal long ByteCount { get; }
        internal string? Sha256 { get; }
        internal string? FailureReason { get; }
        internal RecorderSaveVerificationResult(RecorderSaveAttempt attempt, bool associated, long byteCount, string? sha256, string? failureReason)
        { Attempt = attempt; Associated = associated; ByteCount = byteCount; Sha256 = sha256; FailureReason = failureReason; }
    }
}
