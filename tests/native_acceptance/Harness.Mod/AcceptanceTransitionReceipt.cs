using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;
using RimWorld;
using RimWorld.Planet;
using Verse;

namespace Rwsi.NativeAcceptance
{
    internal static class AcceptanceTransitionReceipt
    {
        internal static TransitionResult? LastTransitionResult;
        private static string? _pendingFailedNativeActionId;
        internal static TransitionResult RunExternal(string family, string actionKey, string nativeApi,
            string subjectThingId, string subjectUniqueLoadId, string expectedTransition, Func<object?> captureBefore,
            Func<object?> invoke, Func<object?> captureAfter, Func<object?, object?, object?, bool> verify,
            Func<object?, object?, NativeSubjectReference[]>? identifyAffectedSubjects = null)
        {
            if (!AcceptanceRuntime.TryAuthorize(out var guardError))
                throw new InvalidOperationException("Native action authorization failed: " + guardError);
            var actionId = Guid.NewGuid().ToString("D");
            var directory = Path.Combine(GenFilePaths.SaveDataFolderPath, "NativeAcceptance", "transitions");
            Directory.CreateDirectory(directory);
            var stage = new TransitionStage
            {
                Schema = "rwsi-native-transition-stage/1", ActionId = actionId, Family = family,
                ActionKey = actionKey, NativeApi = nativeApi, SubjectThingId = subjectThingId,
                SubjectUniqueLoadId = subjectUniqueLoadId, ExpectedTransition = expectedTransition,
                StartedUtc = DateTime.UtcNow.ToString("O"), SavedataRoot = GenFilePaths.SaveDataFolderPath,
                WorldSeed = Find.World?.info?.seedString, PersistentRandomValue = Find.World?.info?.persistentRandomValue,
                NativeBuild = VersionControl.CurrentVersionStringWithRev, BeforeClock = Clock(), Before = captureBefore()
            };
            WriteStage(directory, actionId, "before", stage);
            object? result = null; Exception? failure = null;
            try { result = invoke(); } catch (Exception exception) { failure = exception; }
            stage.ActionUtc = DateTime.UtcNow.ToString("O"); stage.ActionClock = Clock();
            stage.ApiReturnValue = result == null ? null : Convert.ToString(result, System.Globalization.CultureInfo.InvariantCulture);
            stage.ExceptionType = failure?.GetType().FullName; stage.ExceptionMessage = failure?.Message; stage.ExceptionStack = failure?.ToString();
            stage.After = captureAfter(); stage.CompletionPredicate = expectedTransition;
            try
            {
                stage.Completed = failure == null && verify(stage.Before, stage.After, result);
                if (stage.Completed == true)
                {
                    stage.AffectedSubjects = identifyAffectedSubjects?.Invoke(stage.Before, stage.After) ??
                        new[] { new NativeSubjectReference { ThingId = subjectThingId, UniqueLoadId = subjectUniqueLoadId } };
                    if (stage.AffectedSubjects.Length == 0) stage.Completed = false;
                }
            }
            catch (Exception exception) { failure = failure ?? exception; stage.ExceptionType = failure.GetType().FullName; stage.ExceptionMessage = failure.Message; stage.ExceptionStack = failure.ToString(); stage.Completed = false; }
            stage.ExpectedEventCount = TransitionOracle.ExpectedEventCount(stage.Completed == true, false, stage.AffectedSubjects.Length);
            WriteStage(directory, actionId, "action", stage);
            stage.AfterClock = Clock(); stage.FinishedUtc = DateTime.UtcNow.ToString("O");
            WriteStage(directory, actionId, "after", stage);
            var transitionResult = new TransitionResult { ActionId = actionId, Family = family, Completed = stage.Completed == true,
                ExpectedEventCount = stage.ExpectedEventCount ?? 0, ReturnValue = stage.ApiReturnValue, AffectedSubjects = stage.AffectedSubjects,
                ControlPassed = stage.ControlPassed,
                ExceptionType = stage.ExceptionType, BeforePath = Path.Combine(directory, actionId + ".before.json"),
                ActionPath = Path.Combine(directory, actionId + ".action.json"), AfterPath = Path.Combine(directory, actionId + ".after.json") };
            LastTransitionResult = transitionResult;
            return transitionResult;
        }

        internal static TransitionResult Run(string family, string actionKey, string nativeApi, Pawn subject,
            Pawn? related, string expectedTransition, Func<object?> invoke, Func<NativePawnSnapshot, NativePawnSnapshot, object?, bool> verify,
            bool controlOnly = false, bool expectedExceptionNoChange = false, string? expectedExceptionType = null,
            Func<string?>? captureAdditionalObservation = null)
        {
            if (!AcceptanceRuntime.TryAuthorize(out var guardError))
                throw new InvalidOperationException("Native action authorization failed: " + guardError);
            var actionId = Guid.NewGuid().ToString("D");
            var startedUtc = DateTime.UtcNow.ToString("O");
            var before = NativePawnSnapshot.Capture(subject);
            var relatedBefore = related == null ? null : NativePawnSnapshot.Capture(related);
            var beforeClock = Clock();
            var root = GenFilePaths.SaveDataFolderPath;
            var directory = Path.Combine(root, "NativeAcceptance", "transitions");
            Directory.CreateDirectory(directory);
            var common = new TransitionStage
            {
                Schema = "rwsi-native-transition-stage/1", ActionId = actionId, Family = family,
                ActionKey = actionKey, NativeApi = nativeApi, SubjectThingId = subject.ThingID,
                SubjectUniqueLoadId = subject.GetUniqueLoadID(), RelatedThingId = related?.ThingID,
                RelatedUniqueLoadId = related?.GetUniqueLoadID(), ExpectedTransition = expectedTransition,
                StartedUtc = startedUtc, SavedataRoot = root, WorldSeed = Find.World?.info?.seedString,
                PersistentRandomValue = Find.World?.info?.persistentRandomValue,
                NativeBuild = VersionControl.CurrentVersionStringWithRev, BeforeClock = beforeClock,
                Before = before, RelatedBefore = relatedBefore,
                PreviousFailedNativeActionId = _pendingFailedNativeActionId
            };
            WriteStage(directory, actionId, "before", common);

            object? returnValue = null;
            Exception? failure = null;
            try { returnValue = invoke(); }
            catch (Exception exception) { failure = exception; }

            var actionClock = Clock();
            var after = NativePawnSnapshot.Capture(subject);
            var relatedAfter = related == null ? null : NativePawnSnapshot.Capture(related);
            bool predicatePassed;
            try { predicatePassed = verify(before, after, returnValue); }
            catch (Exception exception) { failure = failure ?? exception; predicatePassed = false; }
            var completed = predicatePassed && failure == null && !controlOnly;
            common.ActionUtc = DateTime.UtcNow.ToString("O");
            common.ActionClock = actionClock;
            common.ApiReturnValue = returnValue == null ? null : Convert.ToString(returnValue, System.Globalization.CultureInfo.InvariantCulture);
            common.ExceptionType = failure?.GetType().FullName;
            common.ExceptionMessage = failure?.Message;
            common.ExceptionStack = failure?.ToString();
            try { common.AdditionalObservation = captureAdditionalObservation?.Invoke(); }
            catch (Exception exception) { common.AdditionalObservation = "observation_failed:" + exception.GetType().FullName + ":" + exception.Message; predicatePassed = false; }
            common.After = after;
            common.RelatedAfter = relatedAfter;
            common.CompletionPredicate = expectedTransition;
            common.ControlOnly = controlOnly;
            common.ExpectedExceptionNoChange = expectedExceptionNoChange;
            common.ExpectedExceptionType = expectedExceptionType;
            common.ControlPassed = controlOnly ? (bool?)(predicatePassed &&
                (expectedExceptionNoChange
                    ? TransitionOracle.ExpectedExceptionMatches(expectedExceptionType, failure?.GetType().FullName)
                    : failure == null)) : null;
            common.Completed = completed;
            common.SuccessAfterFailedNativeAction = !string.IsNullOrEmpty(common.PreviousFailedNativeActionId) && completed;
            common.ExpectedEventCount = TransitionOracle.ExpectedEventCount(completed, controlOnly);
            WriteStage(directory, actionId, "action", common);

            common.AfterClock = Clock();
            common.FinishedUtc = DateTime.UtcNow.ToString("O");
            WriteStage(directory, actionId, "after", common);
            var transitionResult = new TransitionResult { ActionId = actionId, Family = family, Completed = completed,
                ExpectedEventCount = common.ExpectedEventCount ?? 0, ReturnValue = common.ApiReturnValue,
                ControlPassed = common.ControlPassed,
                PreviousFailedNativeActionId = common.PreviousFailedNativeActionId,
                SuccessAfterFailedNativeAction = common.SuccessAfterFailedNativeAction,
                AdditionalObservation = common.AdditionalObservation,
                ExceptionType = common.ExceptionType, BeforePath = Path.Combine(directory, actionId + ".before.json"),
                ActionPath = Path.Combine(directory, actionId + ".action.json"), AfterPath = Path.Combine(directory, actionId + ".after.json") };
            LastTransitionResult = transitionResult;
            if (expectedExceptionNoChange && transitionResult.ControlPassed == true) _pendingFailedNativeActionId = actionId;
            else if (transitionResult.SuccessAfterFailedNativeAction) _pendingFailedNativeActionId = null;
            return transitionResult;
        }

        private static ClockReceipt Clock() => new ClockReceipt
        {
            Utc = DateTime.UtcNow.ToString("O"), GameTicks = Find.TickManager?.TicksGame,
            AbsoluteTicks = Find.TickManager?.TicksAbs
        };

        private static void WriteStage(string directory, string actionId, string stage, TransitionStage payload)
        {
            var json = JsonConvert.SerializeObject(payload, Formatting.Indented);
            var bytes = Encoding.UTF8.GetBytes(json);
            string payloadHash;
            using (var sha = SHA256.Create())
                payloadHash = BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
            var output = JsonConvert.SerializeObject(new TransitionEnvelope { PayloadSha256 = payloadHash, Payload = payload }, Formatting.Indented);
            var path = Path.Combine(directory, actionId + "." + stage + ".json");
            using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
            {
                var outputBytes = Encoding.UTF8.GetBytes(output);
                stream.Write(outputBytes, 0, outputBytes.Length);
                stream.Flush(true);
            }
        }
    }

    internal sealed class TransitionResult
    {
        internal string ActionId = "";
        internal string Family = "";
        internal bool Completed;
        internal bool? ControlPassed;
        internal int ExpectedEventCount;
        internal string? ReturnValue;
        internal string? ExceptionType;
        internal string? PreviousFailedNativeActionId;
        internal bool SuccessAfterFailedNativeAction;
        internal string? AdditionalObservation;
        internal NativeSubjectReference[] AffectedSubjects = Array.Empty<NativeSubjectReference>();
        internal string BeforePath = "";
        internal string ActionPath = "";
        internal string AfterPath = "";
    }

    internal sealed class ClockReceipt
    {
        [JsonProperty("utc")] public string Utc = "";
        [JsonProperty("game_ticks")] public int? GameTicks;
        [JsonProperty("absolute_ticks")] public int? AbsoluteTicks;
    }

    internal sealed class NativeSubjectReference
    {
        [JsonProperty("thing_id")] public string ThingId = "";
        [JsonProperty("unique_load_id")] public string UniqueLoadId = "";
    }

    internal sealed class NativePawnSnapshot
    {
        [JsonProperty("thing_id")] public string ThingId = "";
        [JsonProperty("unique_load_id")] public string UniqueLoadId = "";
        [JsonProperty("dead")] public bool Dead;
        [JsonProperty("spawned")] public bool Spawned;
        [JsonProperty("downed")] public bool Downed;
        [JsonProperty("map_id")] public int? MapId;
        [JsonProperty("world_pool")] public string WorldPool = "";
        [JsonProperty("holder_chain")] public string[] HolderChain = Array.Empty<string>();
        [JsonProperty("holder_chain_diagnostic")] public string? HolderChainDiagnostic;
        [JsonProperty("position")] public string? Position;
        [JsonProperty("faction_id")] public string? FactionId;
        [JsonProperty("guest")] public NativeGuestSnapshot Guest = new NativeGuestSnapshot();
        [JsonProperty("hediffs")] public NativeHediffSnapshot[] Hediffs = Array.Empty<NativeHediffSnapshot>();
        [JsonProperty("relations")] public NativeRelationSnapshot[] Relations = Array.Empty<NativeRelationSnapshot>();
        [JsonProperty("xenotype")] public string? Xenotype;
        [JsonProperty("genes")] public NativeGeneSnapshot[] Genes = Array.Empty<NativeGeneSnapshot>();
        [JsonProperty("roles")] public object[] Roles = Array.Empty<object>();

        internal static NativePawnSnapshot Capture(Pawn pawn)
        {
            var snapshot = new NativePawnSnapshot
            {
                ThingId = pawn.ThingID, UniqueLoadId = pawn.GetUniqueLoadID(), Dead = pawn.Dead,
                Spawned = pawn.Spawned, Downed = pawn.Downed, MapId = pawn.Map?.uniqueID,
                WorldPool = FindPool(pawn),
                HolderChain = CaptureHolderChain(pawn, out var holderDiagnostic),
                HolderChainDiagnostic = holderDiagnostic,
                Position = pawn.PositionHeld.IsValid ? pawn.PositionHeld.ToString() : null,
                FactionId = pawn.Faction?.GetUniqueLoadID(), Xenotype = pawn.genes?.Xenotype?.defName
            };
            snapshot.Hediffs = pawn.health?.hediffSet?.hediffs?.Select(hediff => new NativeHediffSnapshot
            {
                LoadId = hediff.loadID, Def = hediff.def?.defName, BodyPart = hediff.Part?.def?.defName,
                BodyPartIndex = hediff.Part == null ? (int?)null : pawn.RaceProps.body.AllParts.IndexOf(hediff.Part),
                Severity = hediff.Severity
            }).ToArray() ?? Array.Empty<NativeHediffSnapshot>();
            snapshot.Relations = pawn.relations?.DirectRelations?.Select(relation => new NativeRelationSnapshot
            {
                Kind = relation.def?.defName, TargetUniqueLoadId = relation.otherPawn?.GetUniqueLoadID()
            }).ToArray() ?? Array.Empty<NativeRelationSnapshot>();
            snapshot.Genes = pawn.genes?.GenesListForReading?.Select(gene => new NativeGeneSnapshot
            {
                LoadId = gene.loadID, Def = gene.def?.defName, Xenogene = pawn.genes.IsXenogene(gene)
            }).ToArray() ?? Array.Empty<NativeGeneSnapshot>();
            snapshot.Guest = CaptureGuest(pawn);
            snapshot.Roles = CaptureRoles(pawn);
            return snapshot;
        }

        private static string[] CaptureHolderChain(Pawn pawn, out string? diagnostic)
        {
            const int maximumDepth = 64;
            diagnostic = null;
            var chain = new System.Collections.Generic.List<string>();
            var visited = new System.Collections.Generic.HashSet<IThingHolder>(ReferenceComparer<IThingHolder>.Instance);
            IThingHolder? current;
            try { current = ((IThingHolder)pawn).ParentHolder; }
            catch (Exception exception)
            {
                diagnostic = "parent_holder_read_failed:" + exception.GetType().FullName;
                return chain.ToArray();
            }
            while (current != null)
            {
                if (chain.Count >= maximumDepth)
                {
                    diagnostic = "depth_limit:" + maximumDepth;
                    break;
                }
                var identity = HolderIdentity(current);
                if (identity == null)
                {
                    diagnostic = "unstable_holder_identity:" + current.GetType().FullName;
                    break;
                }
                if (!visited.Add(current))
                {
                    diagnostic = "cycle:" + identity;
                    break;
                }
                chain.Add(identity);
                try { current = current.ParentHolder; }
                catch (Exception exception)
                {
                    diagnostic = "parent_holder_read_failed:" + exception.GetType().FullName;
                    break;
                }
            }
            return chain.ToArray();
        }

        private static string FindPool(Pawn pawn)
        {
            var pools = Find.WorldPawns;
            if (pools == null) return "unavailable";
            const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
            foreach (var tuple in new[] { Tuple.Create("pawnsAlive", "world_alive"), Tuple.Create("pawnsMothballed", "world_mothballed"), Tuple.Create("pawnsDead", "world_dead") })
            {
                var values = pools.GetType().GetField(tuple.Item1, flags)?.GetValue(pools) as System.Collections.IEnumerable;
                if (values == null) continue;
                foreach (var value in values) if (ReferenceEquals(value, pawn)) return tuple.Item2;
            }
            return "not_in_world_pool";
        }

        private static string? HolderIdentity(IThingHolder holder)
        {
            return AcceptanceReceiptWriter.StableHolderIdentity(holder);
        }

        private sealed class ReferenceComparer<T> : System.Collections.Generic.IEqualityComparer<T> where T : class
        {
            internal static readonly ReferenceComparer<T> Instance = new ReferenceComparer<T>();
            public bool Equals(T? x, T? y) => ReferenceEquals(x, y);
            public int GetHashCode(T value) => System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(value);
        }

        private static NativeGuestSnapshot CaptureGuest(Pawn pawn)
        {
            var guest = pawn.guest;
            return new NativeGuestSnapshot
            {
                Status = guest?.GuestStatus.ToString(), HostFactionId = guest?.HostFaction?.GetUniqueLoadID(),
                Released = guest?.Released ?? false, PrisonerOfColony = pawn.IsPrisonerOfColony,
                SlaveOfColony = pawn.IsSlaveOfColony
            };
        }

        private static object[] CaptureRoles(Pawn pawn)
        {
            var result = new System.Collections.Generic.List<object>();
            try
            {
                var ideology = pawn.ideo?.Ideo;
                var role = ideology?.GetRole(pawn);
                if (role?.def?.defName != null) result.Add(new { kind = "ideology_role", def = role.def.defName });
            }
            catch { }
            try
            {
                var title = pawn.royalty?.MostSeniorTitle;
                if (title?.def?.defName != null) result.Add(new { kind = "royal_title", def = title.def.defName });
            }
            catch { }
            return result.ToArray();
        }
    }

    internal sealed class NativeHediffSnapshot
    {
        [JsonProperty("load_id")] public int LoadId;
        [JsonProperty("def")] public string? Def;
        [JsonProperty("body_part_def")] public string? BodyPart;
        [JsonProperty("body_part_index")] public int? BodyPartIndex;
        [JsonProperty("severity")] public float Severity;
    }

    internal sealed class NativeGuestSnapshot
    {
        [JsonProperty("status")] public string? Status;
        [JsonProperty("host_faction_id")] public string? HostFactionId;
        [JsonProperty("released")] public bool Released;
        [JsonProperty("prisoner_of_colony")] public bool PrisonerOfColony;
        [JsonProperty("slave_of_colony")] public bool SlaveOfColony;
    }

    internal sealed class NativeGeneSnapshot
    {
        [JsonProperty("load_id")] public int LoadId;
        [JsonProperty("def")] public string? Def;
        [JsonProperty("xenogene")] public bool Xenogene;
    }

    internal sealed class TransitionEnvelope
    {
        [JsonProperty("payload_sha256")] public string PayloadSha256 = "";
        [JsonProperty("payload")] public TransitionStage Payload = new TransitionStage();
    }

    internal sealed class NativeRelationSnapshot
    {
        [JsonProperty("kind")] public string? Kind;
        [JsonProperty("target_unique_load_id")] public string? TargetUniqueLoadId;
    }

    internal sealed class TransitionStage
    {
        [JsonProperty("schema")] public string Schema = "";
        [JsonProperty("action_id")] public string ActionId = "";
        [JsonProperty("family")] public string Family = "";
        [JsonProperty("action_key")] public string ActionKey = "";
        [JsonProperty("native_api")] public string NativeApi = "";
        [JsonProperty("subject_thing_id")] public string SubjectThingId = "";
        [JsonProperty("subject_unique_load_id")] public string SubjectUniqueLoadId = "";
        [JsonProperty("related_thing_id")] public string? RelatedThingId;
        [JsonProperty("related_unique_load_id")] public string? RelatedUniqueLoadId;
        [JsonProperty("expected_transition")] public string ExpectedTransition = "";
        [JsonProperty("completion_predicate")] public string? CompletionPredicate;
        [JsonProperty("completed")] public bool? Completed;
        [JsonProperty("control_only")] public bool ControlOnly;
        [JsonProperty("expected_exception_no_change")] public bool ExpectedExceptionNoChange;
        [JsonProperty("expected_exception_type")] public string? ExpectedExceptionType;
        [JsonProperty("previous_failed_native_action_id")] public string? PreviousFailedNativeActionId;
        [JsonProperty("success_after_failed_native_action")] public bool SuccessAfterFailedNativeAction;
        [JsonProperty("additional_observation")] public string? AdditionalObservation;
        [JsonProperty("affected_subjects")] public NativeSubjectReference[] AffectedSubjects = Array.Empty<NativeSubjectReference>();
        [JsonProperty("control_passed")] public bool? ControlPassed;
        [JsonProperty("expected_event_count")] public int? ExpectedEventCount;
        [JsonProperty("started_utc")] public string StartedUtc = "";
        [JsonProperty("action_utc")] public string? ActionUtc;
        [JsonProperty("finished_utc")] public string? FinishedUtc;
        [JsonProperty("savedata_root")] public string SavedataRoot = "";
        [JsonProperty("world_seed")] public string? WorldSeed;
        [JsonProperty("persistent_random_value")] public int? PersistentRandomValue;
        [JsonProperty("native_build")] public string? NativeBuild;
        [JsonProperty("before_clock")] public ClockReceipt BeforeClock = new ClockReceipt();
        [JsonProperty("action_clock")] public ClockReceipt? ActionClock;
        [JsonProperty("after_clock")] public ClockReceipt? AfterClock;
        [JsonProperty("api_return_value")] public string? ApiReturnValue;
        [JsonProperty("exception_type")] public string? ExceptionType;
        [JsonProperty("exception_message")] public string? ExceptionMessage;
        [JsonProperty("exception_stack")] public string? ExceptionStack;
        [JsonProperty("before")] public object? Before = new NativePawnSnapshot();
        [JsonProperty("related_before")] public NativePawnSnapshot? RelatedBefore;
        [JsonProperty("after")] public object? After;
        [JsonProperty("related_after")] public NativePawnSnapshot? RelatedAfter;
    }
}
