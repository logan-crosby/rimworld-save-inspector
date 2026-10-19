using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using LudeonTK;
using Newtonsoft.Json;
using RimWorld;
using Verse;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Resumable bounded batch: at most two real calls per family per invocation.</summary>
    public static class AcceptanceBatchActions
    {
        private const int TargetPerFamily = 100;
        private const int MaxRoundsPerCall = 2;
        private const long CallBudgetMilliseconds = 15000;
        private static readonly string[] Families = { "pawn_died", "pawn_resurrected", "pawn_health_changed", "pawn_relation_changed", "pawn_faction_changed", "pawn_guest_status_changed", "pawn_genes_changed", "pawn_role_changed", "pawn_holder_changed", "pawn_born" };
        private static readonly Dictionary<string, int> Completed = new Dictionary<string, int>(StringComparer.Ordinal);
        private static readonly Dictionary<string, string> Blocked = new Dictionary<string, string>(StringComparer.Ordinal);
        private static readonly Dictionary<string, List<string>> ActionIdsByFamily = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        private static readonly NativeBatchActionLedger Ledger = new NativeBatchActionLedger();
        private static readonly Stopwatch Budget = new Stopwatch();
        private static string _sessionId = Guid.NewGuid().ToString("D");
        private static string? _baselineReceiptPath;
        private static string? _baselinePayloadSha256;
        private static string? _scopeIdentity;

        [DebugAction("RWSI Native Acceptance", "Batch: pin baseline and reset progress", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void PinBaselineAndReset()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error)) { Log.Error("[RWSI Native Acceptance] Batch baseline rejected: " + error); return; }
            var baseline = AcceptanceReceiptWriter.Write();
            if (!baseline.CensusComplete) { Log.Error("[RWSI Native Acceptance] Batch baseline census is incomplete; progress remains unchanged. Receipt=" + baseline.Path); return; }
            ResetCounters();
            _sessionId = Guid.NewGuid().ToString("D");
            _baselineReceiptPath = baseline.Path;
            _baselinePayloadSha256 = baseline.PayloadSha256;
            _scopeIdentity = CurrentScopeIdentity();
            Log.Message("[RWSI Native Acceptance] Batch baseline pinned session_id=" + _sessionId +
                " baseline_state_sha256=" + _baselinePayloadSha256 + " receipt=" + _baselineReceiptPath +
                " scope=" + _scopeIdentity + "; counts are native action receipt counts, not recorder journal acceptance.");
        }

        internal static void ResetForLifecycle(string lifecycle)
        {
            ResetCounters();
            _sessionId = Guid.NewGuid().ToString("D");
            _baselineReceiptPath = null;
            _baselinePayloadSha256 = null;
            _scopeIdentity = null;
            Log.Message("[RWSI Native Acceptance] Batch counters invalidated on game lifecycle=" + lifecycle +
                " session_id=" + _sessionId + "; repin the frozen baseline before continuing.");
        }

        private static void ResetCounters()
        {
            Completed.Clear(); Blocked.Clear(); ActionIdsByFamily.Clear(); Ledger.Clear();
            foreach (var family in Families)
            { Completed[family] = 0; ActionIdsByFamily[family] = new List<string>(); }
        }

        private static string CurrentScopeIdentity() => string.Join("|", GenFilePaths.SaveDataFolderPath ?? "",
            Find.World?.info?.seedString ?? "", (Find.World?.info?.persistentRandomValue ?? 0).ToString(System.Globalization.CultureInfo.InvariantCulture),
            VersionControl.CurrentVersionStringWithRev ?? "");

        [DebugAction("RWSI Native Acceptance", "Batch: continue bounded 10-family run", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void Continue()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error)) { Log.Error("[RWSI Native Acceptance] Batch rejected: " + error); return; }
            if (string.IsNullOrEmpty(_baselinePayloadSha256) || string.IsNullOrEmpty(_scopeIdentity))
            { Log.Error("[RWSI Native Acceptance] Batch has no pinned baseline for this loaded game; pin it first."); return; }
            if (!string.Equals(_scopeIdentity, CurrentScopeIdentity(), StringComparison.Ordinal))
            { ResetForLifecycle("world_profile_or_build_identity_changed"); Log.Error("[RWSI Native Acceptance] Batch identity changed; old progress was cleared."); return; }
            if (Families.Any(family => !Completed.ContainsKey(family)))
            { ResetForLifecycle("uninitialized_batch_state"); Log.Error("[RWSI Native Acceptance] Batch state was incomplete and has been reset; pin baseline again."); return; }
            Budget.Restart();
            var actions = 0;
            for (var round = 0; round < MaxRoundsPerCall && Budget.ElapsedMilliseconds < CallBudgetMilliseconds; round++)
            {
                foreach (var family in Families)
                {
                    if (Completed[family] >= TargetPerFamily || Blocked.ContainsKey(family)) continue;
                    if (Budget.ElapsedMilliseconds >= CallBudgetMilliseconds) break;
                    var succeeded = ExecuteNext(family);
                    actions++;
                    if (!succeeded && !Blocked.ContainsKey(family))
                        Blocked[family] = "no completed transition receipt was produced; inspect the family action log/receipt";
                }
            }
            Budget.Stop();
            WriteProgress(actions, Budget.ElapsedMilliseconds);
        }

        private static bool ExecuteNext(string family)
        {
            AcceptanceTransitionReceipt.LastTransitionResult = null;
            var parity = Completed[family] & 1;
            switch (family)
            {
                case "pawn_died": AcceptanceTransitionActions.KillLiveHuman(); break;
                case "pawn_resurrected": AcceptanceTransitionActions.ResurrectDeadPawn(); break;
                case "pawn_health_changed": if (parity == 0) AcceptanceTransitionActions.AddNonfatalInjury(); else AcceptanceTransitionActions.RemoveExactInjury(); break;
                case "pawn_relation_changed": if (parity == 0) AcceptanceTransitionActions.AddDirectRelation(); else AcceptanceTransitionActions.RemoveDirectRelation(); break;
                case "pawn_faction_changed": if (parity == 0) AcceptanceSecondaryTransitions.ChangeFaction(); else AcceptanceSecondaryTransitions.RestoreFaction(); break;
                case "pawn_guest_status_changed": if (parity == 0) AcceptanceSecondaryTransitions.SetGuestStatus(); else AcceptanceSecondaryTransitions.ToggleGuestHost(); break;
                case "pawn_genes_changed": if (parity == 0) AcceptanceSecondaryTransitions.AddGene(); else AcceptanceSecondaryTransitions.RemoveExactGene(); break;
                case "pawn_role_changed": if (parity == 0) AcceptanceSecondaryTransitions.AssignIdeologyRole(); else AcceptanceSecondaryTransitions.UnassignIdeologyRole(); break;
                case "pawn_holder_changed": if (parity == 0) AcceptanceHolderBirthActions.MovePawnToCaravan(); else AcceptanceHolderBirthActions.RestorePawnFromCaravan(); break;
                case "pawn_born": AcceptanceHolderBirthActions.HatchFertilizedEgg(); break;
            }
            var result = AcceptanceTransitionReceipt.LastTransitionResult;
            if (result == null || result.Family != family || !result.Completed || result.ExpectedEventCount < 1)
            {
                Blocked[family] = result?.ExceptionType ?? "no eligible native state or completion predicate did not pass";
                return false;
            }
            if (!Ledger.TryRecord(result.ActionId, result.Completed, result.ExpectedEventCount))
            { Blocked[family] = "duplicate completed action_id was rejected: " + result.ActionId; return false; }
            ActionIdsByFamily[family].Add(result.ActionId);
            Completed[family] += result.ExpectedEventCount;
            return true;
        }

        private static void WriteProgress(int actions, long elapsedMilliseconds)
        {
            var id = Guid.NewGuid().ToString("D");
            var payload = new BatchReceipt { Schema = "rwsi-native-batch-progress/2", ActionId = id,
                CreatedUtc = DateTime.UtcNow.ToString("O"), GameTicks = Find.TickManager?.TicksGame,
                AbsoluteTicks = Find.TickManager?.TicksAbs, TargetPerFamily = TargetPerFamily,
                MaxRoundsPerCall = MaxRoundsPerCall, ActionsThisCall = actions, ElapsedMilliseconds = elapsedMilliseconds,
                Completed = new Dictionary<string, int>(Completed), Blocked = new Dictionary<string, string>(Blocked),
                SessionId = _sessionId, BaselineReceiptPath = _baselineReceiptPath ?? "", BaselinePayloadSha256 = _baselinePayloadSha256 ?? "",
                ScopeIdentity = _scopeIdentity ?? "", ActionIdsByFamily = ActionIdsByFamily.ToDictionary(pair => pair.Key, pair => pair.Value.ToArray(), StringComparer.Ordinal) };
            var payloadJson = JsonConvert.SerializeObject(payload, Formatting.Indented);
            var payloadBytes = Encoding.UTF8.GetBytes(payloadJson);
            string hash;
            using (var sha = SHA256.Create()) hash = BitConverter.ToString(sha.ComputeHash(payloadBytes)).Replace("-", "").ToLowerInvariant();
            var bytes = Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(new { payload_sha256 = hash, payload }, Formatting.Indented));
            var path = Path.Combine(GenFilePaths.SaveDataFolderPath, "NativeAcceptance", "batches", id + ".json");
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
            { stream.Write(bytes, 0, bytes.Length); stream.Flush(true); }
            Log.Message("[RWSI Native Acceptance] Batch action_id=" + id + " actions=" + actions +
                " elapsed_ms=" + elapsedMilliseconds + " native_completed_by_family=" + JsonConvert.SerializeObject(Completed) +
                " blocked=" + JsonConvert.SerializeObject(Blocked) + " sha256=" + hash + " receipt=" + path);
        }

        private sealed class BatchReceipt
        {
            [JsonProperty("schema")] public string Schema = "";
            [JsonProperty("action_id")] public string ActionId = "";
            [JsonProperty("created_utc")] public string CreatedUtc = "";
            [JsonProperty("game_ticks")] public int? GameTicks;
            [JsonProperty("absolute_ticks")] public int? AbsoluteTicks;
            [JsonProperty("target_per_family")] public int TargetPerFamily;
            [JsonProperty("max_rounds_per_call")] public int MaxRoundsPerCall;
            [JsonProperty("actions_this_call")] public int ActionsThisCall;
            [JsonProperty("elapsed_milliseconds")] public long ElapsedMilliseconds;
            [JsonProperty("completed_by_family")] public Dictionary<string, int> Completed = new Dictionary<string, int>();
            [JsonProperty("blocked_by_family")] public Dictionary<string, string> Blocked = new Dictionary<string, string>();
            [JsonProperty("game_session_id")] public string SessionId = "";
            [JsonProperty("baseline_receipt_path")] public string BaselineReceiptPath = "";
            [JsonProperty("baseline_payload_sha256")] public string BaselinePayloadSha256 = "";
            [JsonProperty("scope_identity")] public string ScopeIdentity = "";
            [JsonProperty("completed_action_ids_by_family")] public Dictionary<string, string[]> ActionIdsByFamily = new Dictionary<string, string[]>();
        }
    }
}
