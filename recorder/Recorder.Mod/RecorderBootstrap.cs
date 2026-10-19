using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using RimWorld;
using RimWorld.Planet;
using RimWorld.LifeRecorder.Core;
using Verse;

namespace RimWorld.LifeRecorder
{
    internal sealed class CollectorStatus
    {
        public string Family { get; set; } = "";
        public string Status { get; set; } = "missing";
        public string Target { get; set; } = "";
        public string Limitations { get; set; } = "";
        public string CollectorVersion { get; set; } = "0.1.0";
        internal bool HasMissingTarget;
    }

    internal static class RecorderBootstrap
    {
        private static readonly List<CollectorStatus> Collectors = new List<CollectorStatus>();
        private static bool _initialized;
        private static Harmony? _harmony;
        internal static IReadOnlyList<CollectorStatus> Coverage => Collectors;

        internal static void Initialize()
        {
            if (_initialized) return;
            _initialized = true;
            _harmony = new Harmony("logan.crosby.rimworld.life-recorder");
            TryPatch("pawn_died", typeof(Pawn), "Kill", new[] { typeof(DamageInfo?), typeof(Hediff) }, typeof(DeathCollector), "Pawn.Kill(Nullable<DamageInfo>, Hediff)", "Postfix records only a verified dead-state transition and supplied damage fields.");
            TryPatch("pawn_resurrected", typeof(Pawn_HealthTracker), "Notify_Resurrected", new[] { typeof(bool), typeof(float) }, typeof(ResurrectionCollector), "Pawn_HealthTracker.Notify_Resurrected(bool, float)", "Only observed dead-to-alive transition; path completeness is unverified.");
            TryPatch("pawn_faction_changed", typeof(Pawn), "SetFaction", new[] { typeof(Faction), typeof(Pawn) }, typeof(FactionCollector), "Pawn.SetFaction(Faction, Pawn)", "Records changed before/after faction IDs; does not infer recruitment intent.");
            TryPatch("pawn_born", typeof(QuestManager), "Notify_PawnBorn", new[] { typeof(Thing), typeof(Thing), typeof(Pawn), typeof(Pawn) }, typeof(QuestBirthCollector), "QuestManager.Notify_PawnBorn(Thing, Thing, Pawn, Pawn)", "Emits only from the native birth notification; non-pawn birthers are classified gestator_or_nonbiological_birth.");
            TryPatch("pawn_born", typeof(TaleRecorder), "RecordTale", new[] { typeof(TaleDef), typeof(object[]) }, typeof(AnimalBirthTaleCollector), "TaleRecorder.RecordTale(TaleDef, Object[])", "Records only TaleDefOf.GaveBirth with validated mother and newborn pawns; hatching is a separate kind.");
            TryPatch("pawn_born", typeof(CompHatcher), "Hatch", Type.EmptyTypes, typeof(HatchingCollector), "CompHatcher.Hatch()", "Opens a thread-local egg scope; newborn is recorded only when TrySpawnHatchedOrBornPawn returns true for that egg.");
            TryPatch("pawn_born", typeof(PawnUtility), "TrySpawnHatchedOrBornPawn", new[] { typeof(Pawn), typeof(Thing), typeof(IntVec3?) }, typeof(HatchedSpawnCollector), "PawnUtility.TrySpawnHatchedOrBornPawn(Pawn, Thing, Nullable<IntVec3>)", "Postfix is gated by active CompHatcher.Hatch scope and true result.");
            TryPatch("pawn_guest_status_changed", typeof(Pawn_GuestTracker), "SetGuestStatus", new[] { typeof(Faction), typeof(GuestStatus) }, typeof(GuestSetStatusCollector), "Pawn_GuestTracker.SetGuestStatus(Faction, GuestStatus)", "Captures changed host/status/released/prisoner/slave state; other direct-field changes remain outside exact-event coverage.");
            TryPatch("pawn_guest_status_changed", typeof(Pawn_GuestTracker), "CapturedBy", new[] { typeof(Faction), typeof(Pawn) }, typeof(GuestCapturedByCollector), "Pawn_GuestTracker.CapturedBy(Faction, Pawn)", "Captures resulting guest/faction context; nested SetGuestStatus callbacks are filtered by before/after equality.");
            TryPatch("pawn_relation_changed", typeof(Pawn_RelationsTracker), "AddDirectRelation", new[] { typeof(PawnRelationDef), typeof(Pawn) }, typeof(RelationMutationCollector), "Pawn_RelationsTracker.AddDirectRelation(PawnRelationDef, Pawn)", "Directional direct-relation before/after; nested callbacks collapse when no state changes.");
            TryPatch("pawn_relation_changed", typeof(Pawn_RelationsTracker), "RemoveDirectRelation", new[] { typeof(PawnRelationDef), typeof(Pawn) }, typeof(RelationMutationCollector), "Pawn_RelationsTracker.RemoveDirectRelation(PawnRelationDef, Pawn)", "Directional direct-relation before/after.");
            TryPatch("pawn_relation_changed", typeof(Pawn_RelationsTracker), "RemoveDirectRelation", new[] { typeof(DirectPawnRelation) }, typeof(RelationMutationCollector), "Pawn_RelationsTracker.RemoveDirectRelation(DirectPawnRelation)", "Directional direct-relation before/after.");
            TryPatch("pawn_relation_changed", typeof(Pawn_RelationsTracker), "RemoveRelation", new[] { typeof(RelationshipRecord) }, typeof(RelationMutationCollector), "Pawn_RelationsTracker.RemoveRelation(RelationshipRecord)", "Records only verified direct-relation differences; virtual relation semantics are not represented.");
            TryPatch("pawn_relation_changed", typeof(Pawn_RelationsTracker), "TryRemoveDirectRelation", new[] { typeof(PawnRelationDef), typeof(Pawn) }, typeof(RelationMutationCollector), "Pawn_RelationsTracker.TryRemoveDirectRelation(PawnRelationDef, Pawn)", "Directional direct-relation before/after.");
            TryPatch("pawn_genes_changed", typeof(Pawn_GeneTracker), "AddGene", new[] { typeof(GeneDef), typeof(bool) }, typeof(GeneMutationCollector), "Pawn_GeneTracker.AddGene(GeneDef, Boolean)", "Snapshots gene set and xenotype; direct-field/custom-mod changes remain a coverage gap.");
            TryPatch("pawn_genes_changed", typeof(Pawn_GeneTracker), "RemoveGene", new[] { typeof(Gene) }, typeof(GeneMutationCollector), "Pawn_GeneTracker.RemoveGene(Gene)", "Snapshots gene set and xenotype.");
            TryPatch("pawn_genes_changed", typeof(Pawn_GeneTracker), "ClearXenogenes", Type.EmptyTypes, typeof(GeneMutationCollector), "Pawn_GeneTracker.ClearXenogenes()", "Snapshots gene set and xenotype.");
            TryPatch("pawn_genes_changed", typeof(Pawn_GeneTracker), "SetXenotype", new[] { typeof(XenotypeDef) }, typeof(GeneMutationCollector), "Pawn_GeneTracker.SetXenotype(XenotypeDef)", "Snapshots gene set and xenotype.");
            TryPatch("pawn_genes_changed", typeof(Pawn_GeneTracker), "SetXenotypeDirect", new[] { typeof(XenotypeDef) }, typeof(GeneMutationCollector), "Pawn_GeneTracker.SetXenotypeDirect(XenotypeDef)", "Snapshots gene set and xenotype; direct-field/custom-mod changes remain a coverage gap.");
            TryPatch("pawn_role_changed", typeof(Precept_RoleSingle), "Assign", new[] { typeof(Pawn), typeof(bool) }, typeof(IdeologyRoleCollector), "Precept_RoleSingle.Assign(Pawn, Boolean)", "Records only changed ChosenPawns membership before/after assignment.");
            TryPatch("pawn_role_changed", typeof(Precept_RoleSingle), "Unassign", new[] { typeof(Pawn), typeof(bool) }, typeof(IdeologyRoleCollector), "Precept_RoleSingle.Unassign(Pawn, Boolean)", "Records only changed ChosenPawns membership before/after unassignment.");
            TryPatch("pawn_role_changed", typeof(Precept_RoleMulti), "Assign", new[] { typeof(Pawn), typeof(bool) }, typeof(IdeologyRoleCollector), "Precept_RoleMulti.Assign(Pawn, Boolean)", "Records only changed ChosenPawns membership before/after assignment.");
            TryPatch("pawn_role_changed", typeof(Precept_RoleMulti), "Unassign", new[] { typeof(Pawn), typeof(bool) }, typeof(IdeologyRoleCollector), "Precept_RoleMulti.Unassign(Pawn, Boolean)", "Records only changed ChosenPawns membership before/after unassignment.");
            TryPatch("pawn_role_changed", typeof(Pawn_RoyaltyTracker), "SetTitle", new[] { typeof(Faction), typeof(RoyalTitleDef), typeof(bool), typeof(bool), typeof(bool) }, typeof(RoyalTitleCollector), "Pawn_RoyaltyTracker.SetTitle(Faction, RoyalTitleDef, Boolean, Boolean, Boolean)", "Royal title event requires effective before/after title difference; mod-specific role fields remain outside coverage.");
            TryPatch("pawn_holder_changed", typeof(Pawn), "SpawnSetup", new[] { typeof(Map), typeof(bool) }, typeof(HolderPawnSpawnCollector), "Pawn.SpawnSetup(Map, Boolean)", "Records identity-preserving map transitions from exact before/after holder chains.");
            TryPatch("pawn_holder_changed", typeof(Thing), "DeSpawn", new[] { typeof(DestroyMode) }, typeof(HolderThingDespawnCollector), "Thing.DeSpawn(DestroyMode)", "Records pawn holder/map changes; non-pawn things are ignored.");
            TryPatch("pawn_holder_changed", typeof(Caravan), "AddPawn", new[] { typeof(Pawn), typeof(bool) }, typeof(HolderCaravanCollector), "Caravan.AddPawn(Pawn, Boolean)", "Records pawn caravan entry when holder chain changes.");
            TryPatch("pawn_holder_changed", typeof(CaravanMaker), "MakeCaravan", new[] { typeof(IEnumerable<Pawn>), typeof(Faction), typeof(PlanetTile), typeof(bool) }, typeof(HolderMakeCaravanCollector), "CaravanMaker.MakeCaravan(IEnumerable<Pawn>, Faction, PlanetTile, Boolean)", "Correlates synchronous map, owner, caravan and world-pool callbacks into one final holder transition per pawn; no timer-based merging.");
            TryPatch("pawn_holder_changed", typeof(Caravan), "RemovePawn", new[] { typeof(Pawn) }, typeof(HolderCaravanCollector), "Caravan.RemovePawn(Pawn)", "Records pawn caravan exit when holder chain changes.");
            TryPatch("pawn_holder_changed", typeof(WorldPawns), "PassToWorld", new[] { typeof(Pawn), typeof(PawnDiscardDecideMode) }, typeof(HolderWorldPawnCollector), "WorldPawns.PassToWorld(Pawn, PawnDiscardDecideMode)", "Records verified world-pool entry when the before/after membership or holder chain changes.");
            TryPatch("pawn_holder_changed", typeof(WorldPawns), "RemovePawn", new[] { typeof(Pawn) }, typeof(HolderWorldPawnCollector), "WorldPawns.RemovePawn(Pawn)", "Records verified world-pool exit when the before/after membership or holder chain changes.");
            TryPatch("pawn_holder_changed", typeof(WorldPawns), "Notify_PawnDestroyed", new[] { typeof(Pawn) }, typeof(HolderWorldPawnCollector), "WorldPawns.Notify_PawnDestroyed(Pawn)", "Records alive/mothballed to dead pool transition after native mutation.");
            TryPatch("pawn_holder_changed", typeof(WorldPawns), "DoMothballProcessing", Type.EmptyTypes, typeof(WorldPawnCollector), "WorldPawns.DoMothballProcessing()", "Reconciles cached alive pool members against mothballed pool after native batch processing; bounded to 100000 members, otherwise emits incomplete coverage.");
            TryPatch("pawn_holder_changed", typeof(ThingOwner), "Contains", new[] { typeof(Thing) }, typeof(HolderOwnerContainsCollector), "ThingOwner.Contains(Thing)", "Captures exact pre-mutation Pawn state for native ThingOwner<T>.TryAdd/Remove; callbacks without a matching capture emit incomplete coverage.");
            TryPatch("pawn_holder_changed", typeof(ThingOwner), "NotifyAdded", new[] { typeof(Thing) }, typeof(HolderOwnerAddedCollector), "ThingOwner.NotifyAdded(Thing)", "Observes successful non-map holder addition through the native generic override's base callback; map entry remains covered by Pawn.SpawnSetup.");
            TryPatch("pawn_holder_changed", typeof(ThingOwner), "NotifyRemoved", new[] { typeof(Thing) }, typeof(HolderOwnerRemovedCollector), "ThingOwner.NotifyRemoved(Thing)", "Observes successful non-map holder removal through the native generic override's base callback; map exit remains covered by Thing.DeSpawn.");
            TryPatch("save_checkpoint_lifecycle", typeof(SafeSaver), "Save", new[] { typeof(string), typeof(string), typeof(Action), typeof(bool) }, typeof(SaveLifecycleCollector), "SafeSaver.Save(String, String, Action, Boolean)", "Records savegame attempts and reports success only after bounded read-only output marker/hash verification; failure exceptions are rethrown unchanged.");
            TryHealthPatch("pawn_health_changed", typeof(HediffDef), "AddHediff", new[] { typeof(HediffDef), typeof(BodyPartRecord), typeof(DamageInfo?), typeof(DamageWorker.DamageResult) }, typeof(HealthAddDefCollector), "Pawn_HealthTracker.AddHediff(HediffDef, BodyPartRecord, Nullable<DamageInfo>, DamageWorker.DamageResult)");
            TryHealthPatch("pawn_health_changed", typeof(Hediff), "AddHediff", new[] { typeof(Hediff), typeof(BodyPartRecord), typeof(DamageInfo?), typeof(DamageWorker.DamageResult) }, typeof(HealthAddCollector), "Pawn_HealthTracker.AddHediff(Hediff, BodyPartRecord, Nullable<DamageInfo>, DamageWorker.DamageResult)");
            TryHealthPatch("pawn_health_changed", typeof(Hediff), "RemoveHediff", new[] { typeof(Hediff) }, typeof(HealthRemoveCollector), "Pawn_HealthTracker.RemoveHediff(Hediff)");
            foreach (var item in Collectors.Where(c => c.Status != "installed"))
                Log.Warning("[Life Recorder] Collector " + item.Family + " is " + item.Status + ": " + item.Limitations);
        }

        private static void TryPatch(string family, Type owner, string methodName, Type[] parameters, Type patchType, string target, string limitations)
        {
            var status = new CollectorStatus { Family = family, Target = target, Limitations = limitations };
            try
            {
                var targetMethod = owner.GetMethod(methodName, BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic, null, parameters, null);
                if (targetMethod == null) { status.Status = "missing"; status.Limitations += " Exact target signature was not found in this runtime."; }
                else if (targetMethod.IsAbstract || targetMethod.ContainsGenericParameters) { status.Status = "missing"; status.Limitations += " Exact target is abstract or open generic."; }
                else { ApplyPatch(targetMethod, patchType, family); status.Status = "installed"; }
            }
            catch (Exception ex) { status.Status = "failed"; status.Limitations += " Binding exception: " + FormatBindingException(ex); }
            Collectors.Add(status);
        }

        private static void TryHealthPatch(string family, Type originalParameter, string methodName, Type[] parameters, Type patchType, string target)
        {
            var status = Collectors.LastOrDefault(c => c.Family == family);
            if (status == null) { status = new CollectorStatus { Family = family, Target = target, Limitations = "Condition add/remove only; surgery, implants and complete health semantics remain unverified." }; Collectors.Add(status); }
            try
            {
                var targetMethod = typeof(Pawn_HealthTracker).GetMethod(methodName, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic, null, parameters, null);
                if (targetMethod == null) { status.Status = "missing"; status.HasMissingTarget = true; status.Limitations += " Exact target signature was not found: " + target; }
                else { ApplyPatch(targetMethod, patchType, family); if (status.Status != "failed" && !status.HasMissingTarget) status.Status = "installed"; status.Target = status.Target + "; " + target; }
            }
            catch (Exception ex) { status.Status = "failed"; status.Limitations += " Binding exception: " + FormatBindingException(ex); }
        }

        private static string FormatBindingException(Exception ex)
        {
            var parts = new List<string>();
            for (Exception? current = ex; current != null; current = current.InnerException)
                parts.Add(current.GetType().FullName + ": " + current.Message);
            if (ex is ReflectionTypeLoadException load && load.LoaderExceptions != null)
                parts.AddRange(load.LoaderExceptions.Where(e => e != null).Select(e => e!.GetType().FullName + ": " + e.Message));
            return String.Join(" -> ", parts);
        }

        private static void ApplyPatch(MethodBase target, Type patchType, string family)
        {
            var prefix = patchType.GetMethod("Prefix", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            var postfix = patchType.GetMethod("Postfix", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            var finalizer = patchType.GetMethod("Finalizer", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            if (prefix == null && postfix == null) throw new MissingMethodException(patchType.FullName, "Prefix/Postfix");
            try
            {
                CollectorMetricInstrumentation.Install(_harmony!, target, family);
                Collectors.Add(new CollectorStatus { Family = "developer_metrics", Status = "installed", Target = target.DeclaringType?.FullName + "." + target.Name, Limitations = "Opt-in callback timing only; disabled by default." });
            }
            catch (Exception ex)
            {
                Collectors.Add(new CollectorStatus { Family = "developer_metrics", Status = "failed", Target = target.DeclaringType?.FullName + "." + target.Name, Limitations = "Opt-in timing hook failed: " + ex.GetType().Name });
            }
            _harmony!.Patch(target, prefix == null ? null : new HarmonyMethod(prefix), postfix == null ? null : new HarmonyMethod(postfix), null,
                finalizer == null ? null : new HarmonyMethod(finalizer));
        }
        private static void AddMissing(string family, string limitations) => Collectors.Add(new CollectorStatus { Family = family, Status = "missing", Target = "unresolved", Limitations = limitations });

        internal static void Emit(string family, string eventType, string? subject, IEnumerable<string>? relatedIds, object payload, string semantics = "recorded_event", string? mapId = null, IReadOnlyList<string>? hookSources = null)
        {
            try
            {
                var frozenRelated = (relatedIds ?? Enumerable.Empty<string>()).Distinct(StringComparer.Ordinal).OrderBy(id => id, StringComparer.Ordinal).ToArray();
                var frozenPayload = payload is JObject jo ? (JObject)jo.DeepClone() : JObject.FromObject(payload);
                var key = family + "|" + eventType + "|" + (subject ?? "");
                if (eventType == "pawn_relation_changed")
                    key += "|" + (string?)frozenPayload["change"] + "|" + (string?)frozenPayload["other_pawn_id"] + "|" + (string?)frozenPayload["relation_def"];
                else if (eventType == "pawn_health_changed")
                    key += "|" + (string?)frozenPayload["change"] + "|" + (string?)frozenPayload["hediff_def"];
                else if (eventType == "pawn_role_changed")
                    key += "|" + (string?)frozenPayload["role_kind"] + "|" + (string?)frozenPayload["role_def"] + "|" + (string?)frozenPayload["faction_id"];
                else if (eventType == "pawn_holder_changed")
                    key += "|" + frozenPayload.ToString(Newtonsoft.Json.Formatting.None);
                else if (eventType == "coverage" || eventType.EndsWith("_baseline", StringComparison.Ordinal))
                    key += "|" + frozenPayload.ToString(Newtonsoft.Json.Formatting.None);
                var source = TransitionCorrelation.CurrentSource ?? family;
                void Publish(IReadOnlyList<string> sources)
                {
                    var correlatedPayload = (JObject)frozenPayload.DeepClone();
                    correlatedPayload["hook_sources"] = new JArray(sources);
                    if (semantics != "coverage") RecorderDeveloperMetrics.NoteTransition(family);
                    RecorderSessionComponent.Current?.Record(family, eventType, subject, frozenRelated, correlatedPayload, semantics, eventMapId: mapId);
                }
                if (hookSources != null) Publish(hookSources);
                else TransitionCorrelation.Submit(key, source, Publish);
            }
            catch (Exception ex) { Log.Warning("[Life Recorder] Collector callback was isolated after " + ex.GetType().Name + "."); }
        }
    }
}
