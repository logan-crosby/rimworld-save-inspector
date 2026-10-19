using System;
using System.Linq;
using LudeonTK;
using RimWorld;
using Verse;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Native-only debug actions. They call game APIs and never create recorder journal entries.</summary>
    public static class AcceptanceTransitionActions
    {
        private const string HumanKind = "humanlike";
        private static Pawn? _healthPawn;
        private static Hediff? _healthHediff;
        private static Pawn? _relationPawn;
        private static Pawn? _relationOther;
        private static PawnRelationDef? _relationDef;

        [DebugAction("RWSI Native Acceptance", "Transition: kill live human", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void KillLiveHuman()
        {
            var pawn = SelectLiveHuman();
            Run("pawn_died", "death.kill_live_subject", "Verse.Pawn.Kill(Nullable<DamageInfo>, Hediff)", pawn, null,
                "same subject changes alive -> dead", () => { pawn?.Kill(null, null); return "void"; },
                (before, after, _) => TransitionOracle.BooleanEdge(before.Dead, after.Dead, true));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: resurrect dead pawn", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void ResurrectDeadPawn()
        {
            var corpse = CurrentMapCorpses().FirstOrDefault(candidate => candidate.InnerPawn?.Dead == true);
            var pawn = corpse?.InnerPawn;
            if (pawn == null) { Log.Error("[RWSI Native Acceptance] No current-map dead pawn corpse is available."); return; }
            Run("pawn_resurrected", "resurrection.try_resurrect_dead_subject",
                "RimWorld.ResurrectionUtility.TryResurrect(Pawn, ResurrectionParams = default) -> bool", pawn, null,
                "native API returns true and same subject changes dead -> alive",
                () => ResurrectionUtility.TryResurrect(pawn),
                (before, after, result) => TransitionOracle.BooleanEdgeAndReturn(before.Dead, after.Dead, false, result, true));
        }

        [DebugAction("RWSI Native Acceptance", "Control: kill already-dead pawn", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void KillAlreadyDeadControl()
        {
            var corpse = CurrentMapCorpses().FirstOrDefault(candidate => candidate.InnerPawn?.Dead == true);
            var pawn = corpse?.InnerPawn;
            if (pawn == null) { Log.Error("[RWSI Native Acceptance] No dead pawn corpse is available for the death no-op control."); return; }
            Run("pawn_died", "control.death.kill_already_dead_subject", "Verse.Pawn.Kill(Nullable<DamageInfo>, Hediff)", pawn, null,
                "already-dead subject remains dead", () => { pawn.Kill(null, null); return "void"; },
                (before, after, _) => before.Dead && after.Dead, true);
        }

        [DebugAction("RWSI Native Acceptance", "Control: resurrect living pawn", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void ResurrectLivingControl()
        {
            var pawn = SelectLiveHuman();
            Run("pawn_resurrected", "control.resurrection.try_resurrect_living_subject",
                "RimWorld.ResurrectionUtility.TryResurrect(Pawn, ResurrectionParams = default) -> bool", pawn, null,
                "living subject remains alive and native API returns false",
                () => ResurrectionUtility.TryResurrect(pawn),
                (before, after, result) => before.Dead == false && after.Dead == false && Equals(result, false), true);
        }

        [DebugAction("RWSI Native Acceptance", "Transition: add nonfatal injury", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void AddNonfatalInjury()
        {
            var pawn = SelectLiveHuman();
            if (pawn == null) { Log.Error("[RWSI Native Acceptance] No live human is available for the injury transition."); return; }
            var def = DefDatabase<HediffDef>.AllDefsListForReading
                .Where(candidate => candidate?.hediffClass != null && typeof(Hediff_Injury).IsAssignableFrom(candidate.hediffClass))
                .OrderBy(candidate => candidate.defName, StringComparer.Ordinal).FirstOrDefault();
            if (def == null) { Log.Error("[RWSI Native Acceptance] No native Hediff_Injury definition is available."); return; }
            Hediff? created = null;
            Run("pawn_health_changed", "health.add_nonfatal_hediff",
                "Verse.Pawn_HealthTracker.AddHediff(Hediff, BodyPartRecord, Nullable<DamageInfo>, DamageResult)", pawn, null,
                "one exact nonfatal Hediff instance is added to this pawn", () =>
                {
                    created = HediffMaker.MakeHediff(def, pawn);
                    if (created == null) throw new InvalidOperationException("HediffMaker.MakeHediff returned null.");
                    created.Severity = 0.1f;
                    pawn.health.AddHediff(created, null, null, null);
                    _healthPawn = pawn;
                    _healthHediff = created;
                    return created.loadID;
                }, (before, after, _) => created?.loadID != null &&
                    !before.Hediffs.Any(item => item.LoadId == created.loadID) &&
                    after.Hediffs.Any(item => item.LoadId == created.loadID && item.Def == def.defName));
        }

        [DebugAction("RWSI Native Acceptance", "Control: injected nested health failure then success", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void InjectedNestedHealthFailureThenSuccessControl()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var guardError))
            { Log.Error("[RWSI Native Acceptance] Nested health fault control rejected: " + guardError); return; }
            var pawn = SelectLiveHuman();
            var def = DefDatabase<HediffDef>.AllDefsListForReading
                .Where(candidate => candidate?.hediffClass != null && typeof(Hediff_Injury).IsAssignableFrom(candidate.hediffClass))
                .OrderBy(candidate => candidate.defName, StringComparer.Ordinal).FirstOrDefault();
            if (pawn == null || def == null)
            { Log.Error("[RWSI Native Acceptance] Nested health fault control requires a live synthetic human and native injury definition."); return; }

            try
            {
                var failed = Run("pawn_health_changed", "control.health.throw_in_nested_add_then_success",
                    "outer Pawn_HealthTracker.AddHediff(HediffDef,...), inner AddHediff(Hediff,...); harness Harmony prefix throws after recorder prefix, before native mutation",
                    pawn, null, "outer and inner native add methods are entered; injected exception is captured; exact health snapshot remains unchanged",
                    () =>
                    {
                        if (!AcceptanceHealthFaultProbe.TryArm(pawn, def, out var armError)) throw new InvalidOperationException(armError);
                        try { pawn.health.AddHediff(def, null, null, null); return "unexpected_native_return"; }
                        finally { AcceptanceHealthFaultProbe.Disarm(); }
                    }, (before, after, _) => AcceptanceHealthFaultProbe.Verified && SameHediffState(before, after),
                    controlOnly: true, expectedExceptionNoChange: true,
                    expectedExceptionType: AcceptanceHealthFaultProbe.ExpectedInjectedExceptionType.FullName,
                    captureAdditionalObservation: () => AcceptanceHealthFaultProbe.Trace);
                if (failed?.ControlPassed != true)
                { Log.Error("[RWSI Native Acceptance] Nested health fault was not proven; no success follow-up was run. action_id=" + (failed?.ActionId ?? "unavailable")); return; }

                AcceptanceTransitionReceipt.LastTransitionResult = null;
                AddNonfatalInjury();
                var success = AcceptanceTransitionReceipt.LastTransitionResult;
                Log.Message("[RWSI Native Acceptance] Nested health failure action_id=" + failed.ActionId +
                    " expected_events=" + failed.ExpectedEventCount + " exception=" + (failed.ExceptionType ?? "none") +
                    " followup_action_id=" + (success?.ActionId ?? "unavailable") +
                    " followup_completed=" + (success?.Completed ?? false) + " followup_expected_events=" + (success?.ExpectedEventCount ?? 0) +
                    " followup_linked=" + (success?.SuccessAfterFailedNativeAction ?? false) +
                    " previous_failed_action_id=" + (success?.PreviousFailedNativeActionId ?? "unavailable"));
            }
            finally { AcceptanceHealthFaultProbe.Disarm(); }
        }

        [DebugAction("RWSI Native Acceptance", "Transition: remove exact added injury", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RemoveExactInjury()
        {
            var pawn = _healthPawn;
            var hediff = _healthHediff;
            if (pawn == null || hediff == null || !pawn.health.hediffSet.hediffs.Contains(hediff))
            { Log.Error("[RWSI Native Acceptance] No still-present harness injury exists; use Add nonfatal injury first."); return; }
            Run("pawn_health_changed", "health.remove_exact_hediff",
                "Verse.Pawn_HealthTracker.RemoveHediff(Hediff)", pawn, null,
                "the exact previously added Hediff load ID is absent after removal", () => { pawn.health.RemoveHediff(hediff); return "void"; },
                (before, after, _) => before.Hediffs.Any(item => item.LoadId == hediff.loadID) &&
                    !after.Hediffs.Any(item => item.LoadId == hediff.loadID));
            _healthPawn = null;
            _healthHediff = null;
        }

        [DebugAction("RWSI Native Acceptance", "Control: remove absent injury", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RemoveAbsentInjuryControl()
        {
            var pawn = SelectLiveHuman();
            if (pawn == null) { Log.Error("[RWSI Native Acceptance] No live human is available for the absent-injury control."); return; }
            var def = DefDatabase<HediffDef>.AllDefsListForReading
                .Where(candidate => candidate?.hediffClass != null && typeof(Hediff_Injury).IsAssignableFrom(candidate.hediffClass))
                .OrderBy(candidate => candidate.defName, StringComparer.Ordinal).FirstOrDefault();
            if (def == null) { Log.Error("[RWSI Native Acceptance] No native Hediff_Injury definition is available."); return; }
            var absent = HediffMaker.MakeHediff(def, pawn);
            if (absent == null) { Log.Error("[RWSI Native Acceptance] HediffMaker.MakeHediff returned null; absent-injury control was not called."); return; }
            Run("pawn_health_changed", "control.health.remove_absent_hediff",
                "Verse.Pawn_HealthTracker.RemoveHediff(Hediff)", pawn, null,
                "the absent Hediff remains absent; native state is unchanged", () => { pawn.health.RemoveHediff(absent); return "void"; },
                (before, after, _) => before.Hediffs.Length == after.Hediffs.Length &&
                    !after.Hediffs.Any(item => item.LoadId == absent.loadID), true);
        }

        [DebugAction("RWSI Native Acceptance", "Transition: add direct acquaintance", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void AddDirectRelation()
        {
            var relation = DefDatabase<PawnRelationDef>.GetNamedSilentFail("Acquaintance");
            var pair = SelectUnrelatedPair(relation);
            if (pair.Item1 == null || pair.Item2 == null || relation == null)
            { Log.Error("[RWSI Native Acceptance] No eligible pair with no Acquaintance relation is available."); return; }
            var actor = pair.Item1;
            var other = pair.Item2;
            Run("pawn_relation_changed", "relation.add_direct_relation",
                "RimWorld.Pawn_RelationsTracker.AddDirectRelation(PawnRelationDef, Pawn)", actor, other,
                "directed Acquaintance relation to the exact other pawn is present", () =>
                {
                    actor.relations.AddDirectRelation(relation, other);
                    _relationPawn = actor; _relationOther = other; _relationDef = relation;
                    return "void";
                }, (before, after, _) =>
                    !before.Relations.Any(item => item.Kind == relation.defName && item.TargetUniqueLoadId == other.GetUniqueLoadID()) &&
                    after.Relations.Any(item => item.Kind == relation.defName && item.TargetUniqueLoadId == other.GetUniqueLoadID()));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: remove direct acquaintance", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RemoveDirectRelation()
        {
            var actor = _relationPawn; var other = _relationOther; var relation = _relationDef;
            if (actor == null || other == null || relation == null || !HasRelation(actor, relation, other))
            { Log.Error("[RWSI Native Acceptance] No recorded Acquaintance relation exists; add it first."); return; }
            Run("pawn_relation_changed", "relation.remove_direct_relation",
                "RimWorld.Pawn_RelationsTracker.RemoveDirectRelation(PawnRelationDef, Pawn)", actor, other,
                "the exact directed Acquaintance relation is absent", () =>
                { actor.relations.RemoveDirectRelation(relation, other); return "void"; },
                (before, after, _) => before.Relations.Any(item => item.Kind == relation.defName && item.TargetUniqueLoadId == other.GetUniqueLoadID()) &&
                    !after.Relations.Any(item => item.Kind == relation.defName && item.TargetUniqueLoadId == other.GetUniqueLoadID()));
            _relationPawn = null; _relationOther = null; _relationDef = null;
        }

        [DebugAction("RWSI Native Acceptance", "Control: remove absent acquaintance", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RemoveAbsentRelationControl()
        {
            var relation = DefDatabase<PawnRelationDef>.GetNamedSilentFail("Acquaintance");
            var pair = SelectUnrelatedPair(relation);
            if (pair.Item1 == null || pair.Item2 == null || relation == null)
            { Log.Error("[RWSI Native Acceptance] No eligible unrelated pair exists for the negative control."); return; }
            var actor = pair.Item1; var other = pair.Item2;
            Run("pawn_relation_changed", "control.relation.remove_absent_relation",
                "RimWorld.Pawn_RelationsTracker.TryRemoveDirectRelation(PawnRelationDef, Pawn) -> bool", actor, other,
                "absent relation remains absent and native API returns false", () => actor.relations.TryRemoveDirectRelation(relation, other),
                (before, after, result) => Equals(result, false) && before.Relations.Length == after.Relations.Length &&
                    !after.Relations.Any(item => item.Kind == relation.defName && item.TargetUniqueLoadId == other.GetUniqueLoadID()), true);
        }

        private static TransitionResult? Run(string family, string actionKey, string nativeApi, Pawn? subject, Pawn? related,
            string expectation, Func<object?> action, Func<NativePawnSnapshot, NativePawnSnapshot, object?, bool> predicate,
            bool controlOnly = false, bool expectedExceptionNoChange = false, string? expectedExceptionType = null,
            Func<string?>? captureAdditionalObservation = null)
        {
            if (subject == null) { Log.Error("[RWSI Native Acceptance] " + actionKey + " has no eligible subject; no call was made."); return null; }
            try
            {
                var result = AcceptanceTransitionReceipt.Run(family, actionKey, nativeApi, subject, related, expectation,
                    action, predicate, controlOnly, expectedExceptionNoChange, expectedExceptionType, captureAdditionalObservation);
                Log.Message("[RWSI Native Acceptance] " + actionKey + " action_id=" + result.ActionId +
                    " completed=" + result.Completed + " expected_events=" + result.ExpectedEventCount +
                    " control_passed=" + result.ControlPassed +
                    " previous_failed_action_id=" + (result.PreviousFailedNativeActionId ?? "none") +
                    " success_after_failed_action=" + result.SuccessAfterFailedNativeAction +
                    " before=" + result.BeforePath + " action=" + result.ActionPath + " after=" + result.AfterPath +
                    " exception=" + (result.ExceptionType ?? "none"));
                return result;
            }
            catch (Exception exception)
            { Log.Error("[RWSI Native Acceptance] " + actionKey + " receipt/action failed: " + exception); return null; }
        }

        private static bool SameHediffState(NativePawnSnapshot before, NativePawnSnapshot after)
        {
            if (before.Hediffs.Length != after.Hediffs.Length) return false;
            var left = before.Hediffs.OrderBy(item => item.LoadId).ThenBy(item => item.Def, StringComparer.Ordinal).ToArray();
            var right = after.Hediffs.OrderBy(item => item.LoadId).ThenBy(item => item.Def, StringComparer.Ordinal).ToArray();
            for (var index = 0; index < left.Length; index++)
                if (left[index].LoadId != right[index].LoadId || left[index].Def != right[index].Def ||
                    left[index].BodyPart != right[index].BodyPart || left[index].BodyPartIndex != right[index].BodyPartIndex ||
                    left[index].Severity != right[index].Severity) return false;
            return true;
        }

        private static Pawn? SelectLiveHuman() => Find.CurrentMap?.mapPawns.AllPawnsSpawned
            .Where(pawn => pawn.Faction == Faction.OfPlayer && pawn.RaceProps.Humanlike && !pawn.Dead)
            .OrderBy(pawn => pawn.ThingID, StringComparer.Ordinal).FirstOrDefault();

        private static System.Collections.Generic.IEnumerable<Corpse> CurrentMapCorpses() =>
            Find.CurrentMap?.listerThings.ThingsInGroup(ThingRequestGroup.Corpse).OfType<Corpse>() ?? Enumerable.Empty<Corpse>();

        private static Tuple<Pawn?, Pawn?> SelectUnrelatedPair(PawnRelationDef? relation)
        {
            if (relation == null) return Tuple.Create<Pawn?, Pawn?>(null, null);
            var eligible = Find.CurrentMap?.mapPawns.AllPawnsSpawned
                .Where(pawn => pawn.Faction == Faction.OfPlayer && pawn.RaceProps.Humanlike && !pawn.Dead)
                .OrderBy(pawn => pawn.ThingID, StringComparer.Ordinal).ToArray() ?? Array.Empty<Pawn>();
            for (var i = 0; i < eligible.Length; i++)
                for (var j = 0; j < eligible.Length; j++)
                    if (i != j && !HasRelation(eligible[i], relation, eligible[j])) return Tuple.Create<Pawn?, Pawn?>(eligible[i], eligible[j]);
            return Tuple.Create<Pawn?, Pawn?>(null, null);
        }

        private static bool HasRelation(Pawn pawn, PawnRelationDef relation, Pawn other) =>
            pawn.relations?.DirectRelations?.Any(item => item.def == relation && item.otherPawn == other) == true;
    }
}
