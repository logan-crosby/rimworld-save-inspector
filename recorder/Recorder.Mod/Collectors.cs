using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Threading;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using RimWorld.LifeRecorder.Core;
using RimWorld;
using Verse;

namespace RimWorld.LifeRecorder
{
    internal static class CollectorUtil
    {
        internal static string? Id(Pawn? pawn) { try { return pawn?.GetUniqueLoadID(); } catch { return null; } }
        internal static string? EntityId(Thing? thing) { try { return thing?.GetUniqueLoadID(); } catch { return null; } }
        internal static string? FactionId(Faction? faction) { try { return faction?.GetUniqueLoadID(); } catch { return null; } }
        internal static JObject Payload(params (string Key, object? Value)[] fields)
        {
            var result = new JObject();
            foreach (var field in fields) result[field.Key] = field.Value == null ? JValue.CreateNull() : JToken.FromObject(field.Value);
            return result;
        }
    }

    [HarmonyPatch]
    internal static class DeathCollector
    {
        public static void Prefix(Pawn __instance, out bool __state) => __state = __instance.Dead;
        public static void Postfix(Pawn __instance, DamageInfo? __0, bool __state)
        {
            if (__state || !__instance.Dead) return;
            string? instigator = null;
            string? damageDef = null;
            try { if (__0.HasValue) { instigator = CollectorUtil.EntityId(__0.Value.Instigator); damageDef = __0.Value.Def?.defName; } } catch { }
            RecorderBootstrap.Emit("pawn_died", "pawn_died", CollectorUtil.Id(__instance), instigator == null ? null : new[] { instigator },
                CollectorUtil.Payload(("damage_def", damageDef), ("instigator_id", instigator), ("cause_supplied", __0.HasValue)));
        }
    }

    internal static class ResurrectionCollector
    {
        public static void Prefix(Pawn_HealthTracker __instance, out bool __state) => __state = HealthCollector.PawnFor(__instance)?.Dead ?? false;
        public static void Postfix(Pawn_HealthTracker __instance, bool __state)
        {
            var pawn = HealthCollector.PawnFor(__instance);
            if (__state && pawn != null && !pawn.Dead)
                RecorderBootstrap.Emit("pawn_resurrected", "pawn_resurrected", CollectorUtil.Id(pawn), null, new JObject());
        }
    }

    internal sealed class FactionBefore
    {
        internal string? OldId;
        internal Pawn? Recruiter;
    }
    internal static class FactionCollector
    {
        public static void Prefix(Pawn __instance, Pawn? __1, out FactionBefore __state) => __state = new FactionBefore { OldId = CollectorUtil.FactionId(__instance.Faction), Recruiter = __1 };
        public static void Postfix(Pawn __instance, Faction? __0, FactionBefore __state)
        {
            var newId = CollectorUtil.FactionId(__instance.Faction);
            if (String.Equals(__state.OldId, newId, StringComparison.Ordinal)) return;
            var recruiterId = CollectorUtil.Id(__state.Recruiter);
            RecorderBootstrap.Emit("pawn_faction_changed", "pawn_faction_changed", CollectorUtil.Id(__instance), recruiterId == null ? null : new[] { recruiterId },
                CollectorUtil.Payload(("before_faction_id", __state.OldId), ("after_faction_id", newId), ("recruiter_id", recruiterId)));
        }
    }

    internal static class HealthCollector
    {
        private static readonly System.Reflection.FieldInfo? PawnField = typeof(Pawn_HealthTracker).GetField("pawn", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic);
        private static readonly System.Reflection.PropertyInfo? PawnProperty = typeof(Pawn_HealthTracker).GetProperty("pawn", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic);
        internal static Pawn? PawnFor(Pawn_HealthTracker tracker) { try { return (PawnField?.GetValue(tracker) ?? PawnProperty?.GetValue(tracker, null)) as Pawn; } catch { return null; } }
        internal static int Count(Pawn_HealthTracker tracker, HediffDef def)
        {
            try { return tracker.hediffSet.hediffs.FindAll(h => h.def == def).Count; } catch { return -1; }
        }
        internal static bool Contains(Pawn_HealthTracker tracker, Hediff hediff)
        {
            try { return tracker.hediffSet.hediffs.Contains(hediff); } catch { return false; }
        }
        internal static void EmitAdd(Pawn_HealthTracker tracker, HediffDef? def, Hediff? hediff, bool added)
        {
            if (!added) return;
            var pawn = PawnFor(tracker);
            if (pawn == null) return;
            RecorderBootstrap.Emit("pawn_health_changed", "pawn_health_changed", CollectorUtil.Id(pawn), null,
                CollectorUtil.Payload(("change", "condition_added"), ("hediff_def", def?.defName ?? hediff?.def.defName), ("coverage", "condition additions only for hooked overload")), "observed_difference");
        }
    }

    internal sealed class HealthMutationState
    {
        internal int Count;
        internal bool Contains;
        internal IDisposable? Scope;
    }

    internal static class HealthAddDefCollector
    {
        public static void Prefix(Pawn_HealthTracker __instance, HediffDef __0, out HealthMutationState __state) { __state = new HealthMutationState { Scope = TransitionCorrelation.Begin("Pawn_HealthTracker.AddHediff(HediffDef)") }; __state.Count = HealthCollector.Count(__instance, __0); }
        public static void Postfix(Pawn_HealthTracker __instance, HediffDef __0, HealthMutationState __state) { try { HealthCollector.EmitAdd(__instance, __0, null, HealthCollector.Count(__instance, __0) > __state.Count); } finally { __state.Scope?.Dispose(); } }
        public static Exception? Finalizer(Exception? __exception, HealthMutationState? __state) { __state?.Scope?.Dispose(); return __exception; }
    }
    internal static class HealthAddCollector
    {
        public static void Prefix(Pawn_HealthTracker __instance, Hediff __0, out HealthMutationState __state) { __state = new HealthMutationState { Scope = TransitionCorrelation.Begin("Pawn_HealthTracker.AddHediff(Hediff)") }; __state.Contains = HealthCollector.Contains(__instance, __0); }
        public static void Postfix(Pawn_HealthTracker __instance, Hediff __0, HealthMutationState __state) { try { HealthCollector.EmitAdd(__instance, __0.def, __0, !__state.Contains && HealthCollector.Contains(__instance, __0)); } finally { __state.Scope?.Dispose(); } }
        public static Exception? Finalizer(Exception? __exception, HealthMutationState? __state) { __state?.Scope?.Dispose(); return __exception; }
    }
    internal static class HealthRemoveCollector
    {
        public static void Prefix(Pawn_HealthTracker __instance, Hediff __0, out HealthMutationState __state) { __state = new HealthMutationState { Scope = TransitionCorrelation.Begin("Pawn_HealthTracker.RemoveHediff") }; __state.Contains = HealthCollector.Contains(__instance, __0); }
        public static void Postfix(Pawn_HealthTracker __instance, Hediff __0, HealthMutationState __state)
        {
            try
            {
                if (__state.Contains && !HealthCollector.Contains(__instance, __0))
                    RecorderBootstrap.Emit("pawn_health_changed", "pawn_health_changed", CollectorUtil.Id(HealthCollector.PawnFor(__instance)), null,
                        CollectorUtil.Payload(("change", "condition_removed"), ("hediff_def", __0.def.defName), ("coverage", "condition removals only for hooked overload")), "observed_difference");
            }
            finally { __state.Scope?.Dispose(); }
        }
        public static Exception? Finalizer(Exception? __exception, HealthMutationState? __state) { __state?.Scope?.Dispose(); return __exception; }
    }

    internal sealed class GuestState
    {
        internal string? Status;
        internal string? HostFactionId;
        internal bool Released;
        internal bool Prisoner;
        internal bool Slave;
        internal IDisposable? CorrelationScope;
    }

    internal static class GuestCollector
    {
        internal static GuestState Capture(Pawn_GuestTracker tracker, Pawn pawn) => new GuestState
        {
            Status = tracker.GuestStatus.ToString(),
            HostFactionId = CollectorUtil.FactionId(tracker.HostFaction),
            Released = tracker.Released,
            Prisoner = pawn.IsPrisonerOfColony,
            Slave = pawn.IsSlaveOfColony
        };

        internal static void Emit(Pawn pawn, GuestState before, GuestState after)
        {
            if (before.Status == after.Status && before.HostFactionId == after.HostFactionId && before.Released == after.Released &&
                before.Prisoner == after.Prisoner && before.Slave == after.Slave) return;
            RecorderBootstrap.Emit("pawn_guest_status_changed", "pawn_guest_status_changed", CollectorUtil.Id(pawn), null,
                CollectorUtil.Payload(("before_guest_status", before.Status), ("after_guest_status", after.Status),
                    ("before_host_faction_id", before.HostFactionId), ("after_host_faction_id", after.HostFactionId),
                    ("before_released", before.Released), ("after_released", after.Released),
                    ("before_prisoner", before.Prisoner), ("after_prisoner", after.Prisoner),
                    ("before_slave", before.Slave), ("after_slave", after.Slave)), "observed_difference");
        }
    }

    internal static class GuestSetStatusCollector
    {
        public static void Prefix(Pawn_GuestTracker __instance, Pawn ___pawn, out GuestState __state) { __state = GuestCollector.Capture(__instance, ___pawn); __state.CorrelationScope = TransitionCorrelation.Begin("Pawn_GuestTracker.SetGuestStatus"); }
        public static void Postfix(Pawn_GuestTracker __instance, Pawn ___pawn, GuestState __state) { try { GuestCollector.Emit(___pawn, __state, GuestCollector.Capture(__instance, ___pawn)); } finally { __state.CorrelationScope?.Dispose(); } }
        public static Exception? Finalizer(Exception? __exception, GuestState? __state) { __state?.CorrelationScope?.Dispose(); return __exception; }
    }
    internal static class GuestCapturedByCollector
    {
        public static void Prefix(Pawn_GuestTracker __instance, Pawn ___pawn, out GuestState __state) { __state = GuestCollector.Capture(__instance, ___pawn); __state.CorrelationScope = TransitionCorrelation.Begin("Pawn_GuestTracker.CapturedBy"); }
        public static void Postfix(Pawn_GuestTracker __instance, Pawn ___pawn, GuestState __state) { try { GuestCollector.Emit(___pawn, __state, GuestCollector.Capture(__instance, ___pawn)); } finally { __state.CorrelationScope?.Dispose(); } }
        public static Exception? Finalizer(Exception? __exception, GuestState? __state) { __state?.CorrelationScope?.Dispose(); return __exception; }
    }

    internal sealed class RelationSnapshot
    {
        internal readonly string OtherPawnId;
        internal readonly string RelationDef;
        internal RelationSnapshot(string otherPawnId, string relationDef) { OtherPawnId = otherPawnId; RelationDef = relationDef; }
        internal string Key => OtherPawnId + "\u001f" + RelationDef;
    }

    internal static class RelationCollector
    {
        internal static RelationSnapshot[] Capture(Pawn_RelationsTracker tracker)
        {
            try
            {
                return tracker.DirectRelations
                    .Where(r => r?.otherPawn != null && r.def != null)
                    .Select(r => new RelationSnapshot(CollectorUtil.Id(r.otherPawn)!, r.def.defName))
                    .Where(r => !String.IsNullOrWhiteSpace(r.OtherPawnId) && !String.IsNullOrWhiteSpace(r.RelationDef))
                    .OrderBy(r => r.Key, StringComparer.Ordinal).ToArray();
            }
            catch { return Array.Empty<RelationSnapshot>(); }
        }

        internal static void Emit(Pawn pawn, RelationSnapshot[] before, RelationSnapshot[] after)
        {
            var old = new HashSet<string>(before.Select(x => x.Key), StringComparer.Ordinal);
            var current = new HashSet<string>(after.Select(x => x.Key), StringComparer.Ordinal);
            foreach (var row in after.Where(x => !old.Contains(x.Key))) EmitOne(pawn, row, "added", before, after);
            foreach (var row in before.Where(x => !current.Contains(x.Key))) EmitOne(pawn, row, "removed", before, after);
        }

        private static void EmitOne(Pawn pawn, RelationSnapshot changed, string change, RelationSnapshot[] before, RelationSnapshot[] after)
        {
            RecorderBootstrap.Emit("pawn_relation_changed", "pawn_relation_changed", CollectorUtil.Id(pawn), new[] { changed.OtherPawnId },
                new JObject
                {
                    ["change"] = change,
                    ["subject_pawn_id"] = CollectorUtil.Id(pawn),
                    ["other_pawn_id"] = changed.OtherPawnId,
                    ["relation_def"] = changed.RelationDef,
                    ["direction"] = "subject_to_other",
                    ["before_direct_relations"] = new JArray(before.Select(x => new JObject { ["other_pawn_id"] = x.OtherPawnId, ["relation_def"] = x.RelationDef })),
                    ["after_direct_relations"] = new JArray(after.Select(x => new JObject { ["other_pawn_id"] = x.OtherPawnId, ["relation_def"] = x.RelationDef }))
                }, "observed_difference");
        }
    }

    internal static class RelationMutationCollector
    {
        internal sealed class State { internal RelationSnapshot[] Before = Array.Empty<RelationSnapshot>(); internal IDisposable? Scope; }
        public static void Prefix(Pawn_RelationsTracker __instance, Pawn ___pawn, out State __state) { __state = new State { Scope = TransitionCorrelation.Begin("Pawn_RelationsTracker." + __instance.GetType().Name) }; __state.Before = RelationCollector.Capture(__instance); }
        public static void Postfix(Pawn_RelationsTracker __instance, Pawn ___pawn, State __state) { try { RelationCollector.Emit(___pawn, __state.Before, RelationCollector.Capture(__instance)); } finally { __state.Scope?.Dispose(); } }
        public static Exception? Finalizer(Exception? __exception, State? __state) { __state?.Scope?.Dispose(); return __exception; }
    }

    internal sealed class GeneSnapshot
    {
        internal string Xenotype = "";
        internal string[] Genes = Array.Empty<string>();
        internal IDisposable? CorrelationScope;
    }

    internal static class GeneCollector
    {
        internal static GeneSnapshot Capture(Pawn_GeneTracker tracker)
        {
            try
            {
                return new GeneSnapshot
                {
                    Xenotype = tracker.Xenotype?.defName ?? "",
                    Genes = tracker.GenesListForReading.Select(g => (tracker.IsXenogene(g) ? "xeno:" : "endo:") + (g.def?.defName ?? ""))
                        .Where(s => !s.EndsWith(":" , StringComparison.Ordinal)).OrderBy(s => s, StringComparer.Ordinal).ToArray()
                };
            }
            catch { return new GeneSnapshot(); }
        }

        internal static void Emit(Pawn pawn, GeneSnapshot before, GeneSnapshot after)
        {
            if (before.Xenotype == after.Xenotype && before.Genes.SequenceEqual(after.Genes, StringComparer.Ordinal)) return;
            RecorderBootstrap.Emit("pawn_genes_changed", "pawn_genes_changed", CollectorUtil.Id(pawn), null,
                new JObject
                {
                    ["before_xenotype_def"] = before.Xenotype,
                    ["after_xenotype_def"] = after.Xenotype,
                    ["before_genes"] = new JArray(before.Genes),
                    ["after_genes"] = new JArray(after.Genes),
                    ["coverage"] = "gene_add_remove_and_xenotype_mutators; runtime callback not yet validated"
                }, "observed_difference");
        }
    }

    internal static class GeneMutationCollector
    {
        public static void Prefix(Pawn_GeneTracker __instance, Pawn ___pawn, out GeneSnapshot __state) { __state = GeneCollector.Capture(__instance); __state.CorrelationScope = TransitionCorrelation.Begin("Pawn_GeneTracker." + __instance.GetType().Name); }
        public static void Postfix(Pawn_GeneTracker __instance, Pawn ___pawn, GeneSnapshot __state) { try { GeneCollector.Emit(___pawn, __state, GeneCollector.Capture(__instance)); } finally { __state.CorrelationScope?.Dispose(); } }
        public static Exception? Finalizer(Exception? __exception, GeneSnapshot? __state) { __state?.CorrelationScope?.Dispose(); return __exception; }
    }

    internal static class BirthCollector
    {
        internal static void Emit(Pawn baby, Pawn? mother, Pawn? father, string birthKind, string collector, string? birtherId = null)
        {
            var babyId = CollectorUtil.Id(baby);
            if (String.IsNullOrWhiteSpace(babyId)) return;
            RecorderSessionComponent.Current?.ObserveBirth(new BirthObservation(babyId!, collector, birthKind,
                CollectorUtil.Id(mother), CollectorUtil.Id(father), birtherId, RecorderClockCapture.Capture()));
        }
    }

    internal static class QuestBirthCollector
    {
        public static void Postfix(Thing __0, Thing __1, Pawn? __2, Pawn? __3)
        {
            var baby = __0 as Pawn ?? (__0 as Corpse)?.InnerPawn;
            if (baby == null) return;
            var birthKind = __1 == __2 ? "biological_birth" : "unknown";
            BirthCollector.Emit(baby, __2, __3, birthKind, "QuestManager.Notify_PawnBorn", CollectorUtil.Id(__1 as Pawn));
        }
    }

    internal static class AnimalBirthTaleCollector
    {
        public static void Postfix(TaleDef __0, object[] __1)
        {
            if (__0 != TaleDefOf.GaveBirth || __1 == null || __1.Length < 2) return;
            var mother = __1[0] as Pawn;
            var baby = __1[1] as Pawn ?? (__1[1] as Corpse)?.InnerPawn;
            if (mother == null || baby == null) return;
            BirthCollector.Emit(baby, mother, null, "biological_birth", "TaleRecorder.RecordTale(GaveBirth)");
        }
    }

    internal sealed class HatchScope
    {
        internal string EggId = "";
        internal string? MotherId;
        internal string? FatherId;
        internal IDisposable? CorrelationScope;
        [ThreadStatic] private static Stack<HatchScope>? _stack;
        internal static HatchScope? Current => _stack != null && _stack.Count > 0 ? _stack.Peek() : null;
        internal static void Push(HatchScope scope) { (_stack ?? (_stack = new Stack<HatchScope>())).Push(scope); }
        internal static void Pop(HatchScope? scope)
        {
            if (scope == null || _stack == null || _stack.Count == 0) return;
            if (ReferenceEquals(_stack.Peek(), scope)) _stack.Pop();
            else _stack.Clear();
        }
    }

    internal static class HatchingCollector
    {
        public static void Prefix(CompHatcher __instance, Thing ___parent, Pawn? ___hatcheeParent, Pawn? ___otherParent, out HatchScope __state)
        {
            __state = new HatchScope
            {
                EggId = CollectorUtil.EntityId(___parent) ?? "",
                MotherId = CollectorUtil.Id(___hatcheeParent),
                FatherId = CollectorUtil.Id(___otherParent),
                CorrelationScope = TransitionCorrelation.Begin("CompHatcher.Hatch")
            };
            HatchScope.Push(__state);
        }

        public static Exception? Finalizer(Exception? __exception, HatchScope? __state)
        {
            HatchScope.Pop(__state);
            __state?.CorrelationScope?.Dispose();
            return __exception;
        }

        public static void SpawnPostfix(Pawn __0, Thing __1, bool __result)
        {
            var scope = HatchScope.Current;
            if (!__result || scope == null || !String.Equals(scope.EggId, CollectorUtil.EntityId(__1), StringComparison.Ordinal)) return;
            var id = CollectorUtil.Id(__0);
            if (!String.IsNullOrWhiteSpace(id))
                RecorderSessionComponent.Current?.ObserveBirth(new BirthObservation(id!, "CompHatcher.Hatch", "hatching",
                    scope.MotherId, scope.FatherId, scope.EggId, RecorderClockCapture.Capture()));
        }
    }

    internal static class HatchedSpawnCollector
    {
        public static void Postfix(Pawn __0, Thing __1, bool __result) => HatchingCollector.SpawnPostfix(__0, __1, __result);
    }

    internal sealed class RoleAssignmentSnapshot
    {
        internal HashSet<Pawn>? AssignedPawns;
        internal IDisposable? CorrelationScope;
    }

    internal static class IdeologyRoleCollector
    {
        private static HashSet<Pawn>? Capture(Precept_Role role)
        {
            try { return new HashSet<Pawn>(role.ChosenPawns().Where(p => p != null)); }
            catch (Exception ex)
            {
                Log.Warning("[Life Recorder] Role membership snapshot was unavailable: " + ex.GetType().Name + ": " + ex.Message);
                return null;
            }
        }

        public static void Prefix(Precept_Role __instance, MethodBase __originalMethod, out RoleAssignmentSnapshot __state)
        {
            __state = new RoleAssignmentSnapshot
            {
                CorrelationScope = TransitionCorrelation.Begin((__originalMethod.DeclaringType?.Name ?? "Precept_Role") + "." + __originalMethod.Name),
                AssignedPawns = Capture(__instance)
            };
        }

        public static void Postfix(Precept_Role __instance, RoleAssignmentSnapshot __state)
        {
            try
            {
                var before = __state.AssignedPawns;
                var after = Capture(__instance);
                if (before == null || after == null) return;
                foreach (var pawn in before.Except(after)) EmitChange(__instance, pawn, false);
                foreach (var pawn in after.Except(before)) EmitChange(__instance, pawn, true);
            }
            finally { __state.CorrelationScope?.Dispose(); }
        }

        public static Exception? Finalizer(Exception? __exception, RoleAssignmentSnapshot? __state)
        {
            __state?.CorrelationScope?.Dispose();
            return __exception;
        }

        private static void EmitChange(Precept_Role role, Pawn pawn, bool assigned)
        {
            var pawnId = CollectorUtil.Id(pawn);
            if (String.IsNullOrWhiteSpace(pawnId)) return;
            RecorderBootstrap.Emit("pawn_role_changed", "pawn_role_changed", pawnId, null,
                CollectorUtil.Payload(("role_kind", "ideology_role"), ("role_def", role.def?.defName),
                    ("change", assigned ? "assigned" : "unassigned"), ("before_assigned", !assigned),
                    ("after_assigned", assigned), ("assignment_verified", true)), "observed_difference");
        }
    }

    internal static class RoyalTitleCollector
    {
        private static string? CurrentTitle(Pawn pawn, Faction faction)
        {
            try { return pawn.royalty?.GetCurrentTitle(faction)?.defName; } catch { return null; }
        }

        public static void Prefix(Pawn_RoyaltyTracker __instance, Pawn ___pawn, Faction __0, out string? __state) => __state = CurrentTitle(___pawn, __0);
        public static void Postfix(Pawn_RoyaltyTracker __instance, Pawn ___pawn, Faction __0, RoyalTitleDef __1, string? __state)
        {
            var current = CurrentTitle(___pawn, __0);
            if (String.Equals(__state, current, StringComparison.Ordinal)) return;
            RecorderBootstrap.Emit("pawn_role_changed", "pawn_role_changed", CollectorUtil.Id(___pawn), null,
                CollectorUtil.Payload(("role_kind", "royal_title"), ("faction_id", CollectorUtil.FactionId(__0)),
                    ("before_title_def", __state), ("after_title_def", current),
                    ("requested_title_def", __1?.defName), ("title_transition_verified", true)), "observed_difference");
        }
    }
}
