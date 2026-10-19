using System;
using System.Collections.Generic;
using System.Runtime.CompilerServices;
using Newtonsoft.Json.Linq;
using RimWorld.LifeRecorder.Core;
using RimWorld.Planet;
using Verse;

namespace RimWorld.LifeRecorder
{
    internal sealed class HolderBefore
    {
        internal string PawnId = "";
        internal JArray Chain = new JArray();
        internal string? MapId;
        internal string? WorldPoolStatus;
        internal bool ChainComplete = true;
        internal IDisposable? CorrelationScope;
    }

    internal sealed class PendingHolderState
    {
        internal HolderBefore Before = null!;
        internal ThingOwner? HoldingOwner;
        internal bool ContainsResult;
        internal long CapturedAt;
    }

    internal static class HolderCollector
    {
        private sealed class IdentityComparer<T> : IEqualityComparer<T> where T : class
        {
            internal static readonly IdentityComparer<T> Instance = new IdentityComparer<T>();
            public bool Equals(T? left, T? right) => ReferenceEquals(left, right);
            public int GetHashCode(T value) => RuntimeHelpers.GetHashCode(value);
        }

        [ThreadStatic] private static Dictionary<ThingOwner, Dictionary<Pawn, PendingHolderState>>? _pending;
        [ThreadStatic] private static int _pendingCount;
        [ThreadStatic] private static int _storesSincePrune;
        private const int MaxPendingSnapshots = 4096;
        private const int SnapshotLifetimeSeconds = 10;

        internal static int PendingSnapshotCount => _pendingCount;

        private static void PrunePending(long now)
        {
            var pending = _pending;
            if (pending == null || ++_storesSincePrune < 128) return;
            _storesSincePrune = 0;
            var expiredBefore = now - System.Diagnostics.Stopwatch.Frequency * SnapshotLifetimeSeconds;
            foreach (var owner in new List<ThingOwner>(pending.Keys))
            {
                var pawns = pending[owner];
                foreach (var pawn in new List<Pawn>(pawns.Keys))
                    if (pawns[pawn].CapturedAt < expiredBefore) { pawns.Remove(pawn); _pendingCount--; }
                if (pawns.Count == 0) pending.Remove(owner);
            }
        }

        internal static HolderBefore Capture(Thing? thing)
        {
            var result = new HolderBefore();
            if (!(thing is Pawn pawn)) return result;
            result.PawnId = CollectorUtil.Id(pawn) ?? "";
            result.MapId = pawn.MapHeld != null ? "Map_" + pawn.MapHeld.uniqueID.ToString(System.Globalization.CultureInfo.InvariantCulture) : null;
            var seen = new HashSet<IThingHolder>();
            IThingHolder? holder = pawn.ParentHolder;
            var depth = 0;
            while (holder != null)
            {
                if (depth >= 32) { result.ChainComplete = false; result.Chain.Add(new JObject { ["kind"] = "unknown", ["reason"] = "depth_limit" }); break; }
                if (!seen.Add(holder)) { result.ChainComplete = false; result.Chain.Add(new JObject { ["kind"] = "unknown", ["reason"] = "cycle" }); break; }
                result.Chain.Add(Describe(holder));
                depth++;
                try { holder = holder.ParentHolder; }
                catch { result.ChainComplete = false; result.Chain.Add(new JObject { ["kind"] = "unknown", ["reason"] = "parent_read_failed" }); break; }
            }
            result.WorldPoolStatus = WorldPawnCollector.Classify(pawn);
            if (result.WorldPoolStatus != "not_in_world")
                result.Chain.Add(new JObject { ["kind"] = result.WorldPoolStatus, ["id"] = String.IsNullOrEmpty(result.PawnId) ? JValue.CreateNull() : new JValue(result.PawnId) });
            return result;
        }

        private static HolderBefore CaptureScoped(Thing? thing, string source)
        {
            var scope = TransitionCorrelation.Begin(source);
            try { var result = Capture(thing); result.CorrelationScope = scope; return result; }
            catch { scope.Dispose(); throw; }
        }

        private static JObject Describe(IThingHolder holder)
        {
            if (holder is Map map) return new JObject { ["kind"] = "map", ["id"] = "Map_" + map.uniqueID.ToString(System.Globalization.CultureInfo.InvariantCulture) };
            if (holder is Caravan caravan) return new JObject { ["kind"] = "caravan", ["id"] = caravan.ID.ToString(System.Globalization.CultureInfo.InvariantCulture) };
            var holderName = holder.GetType().Name;
            if (holderName == "Pawn_CarryTracker") return new JObject { ["kind"] = "carried", ["id"] = JValue.CreateNull() };
            if (holderName == "Pawn_InventoryTracker") return new JObject { ["kind"] = "inventory", ["id"] = JValue.CreateNull() };
            if (holderName == "Building_CryptosleepCasket" || holderName == "Building_AncientCryptosleepCasket") return new JObject { ["kind"] = "casket", ["id"] = holder is Thing casket ? CollectorUtil.EntityId(casket) : null };
            if (holderName == "Building_GrowthVat") return new JObject { ["kind"] = "growth_vat", ["id"] = holder is Thing vat ? CollectorUtil.EntityId(vat) : null };
            if (holderName == "Corpse" || holderName == "UnnaturalCorpse") return new JObject { ["kind"] = "corpse", ["id"] = holder is Thing corpse ? CollectorUtil.EntityId(corpse) : null };
            if (holderName == "ActiveTransporter" || holderName == "TravellingTransporters" || holderName == "CompTransporter") return new JObject { ["kind"] = "transport", ["id"] = holder is Thing transport ? CollectorUtil.EntityId(transport) : null };
            if (holder is Thing thing) return new JObject { ["kind"] = "thing", ["id"] = CollectorUtil.EntityId(thing), ["def"] = thing.def?.defName };
            if (holder is ThingOwner) return new JObject { ["kind"] = "thing_owner", ["type"] = holder.GetType().FullName ?? holderName, ["id"] = JValue.CreateNull() };
            return new JObject { ["kind"] = "holder", ["type"] = holder.GetType().FullName ?? holder.GetType().Name, ["id"] = JValue.CreateNull() };
        }

        internal static void Emit(Thing? thing, HolderBefore? before)
        {
            try
            {
                if (!(thing is Pawn pawn) || before == null) return;
                var after = Capture(pawn);
                if (!Changed(before, after)) return;
                if (TransitionCorrelation.IsWithinSource("CaravanMaker.MakeCaravan"))
                {
                    // MakeCaravan calls DeSpawn, owner/caravan mutation, then PassToWorld
                    // as siblings. The first changed callback owns the true pre-state;
                    // capture the final state only when the native call has completed.
                    var firstBefore = before;
                    var source = TransitionCorrelation.CurrentSource ?? "pawn_holder_changed";
                    TransitionCorrelation.SubmitFirstForObject(pawn, source, sources =>
                    {
                        try { EmitDifference(pawn, firstBefore, Capture(pawn), sources); }
                        catch (Exception ex) { Log.Warning("[Life Recorder] Caravan holder observation was isolated after " + ex.GetType().Name); }
                    });
                    return;
                }
                EmitDifference(pawn, before, after, null);
            }
            finally { before?.CorrelationScope?.Dispose(); }
        }

        private static bool Changed(HolderBefore before, HolderBefore after) =>
            before.Chain.ToString(Newtonsoft.Json.Formatting.None) != after.Chain.ToString(Newtonsoft.Json.Formatting.None) ||
            before.MapId != after.MapId || before.WorldPoolStatus != after.WorldPoolStatus;

        private static void EmitDifference(Pawn pawn, HolderBefore before, HolderBefore after, IReadOnlyList<string>? sources)
        {
            if (!Changed(before, after)) return;
            var afterId = CollectorUtil.Id(pawn);
            var identityPreserved = SavedIdentity.IsPreserved(before.PawnId, afterId);
            var payload = new JObject
                {
                    ["before_holder_chain"] = before.Chain.DeepClone(),
                    ["after_holder_chain"] = after.Chain,
                    ["before_map_id"] = before.MapId == null ? JValue.CreateNull() : new JValue(before.MapId),
                    ["after_map_id"] = after.MapId == null ? JValue.CreateNull() : new JValue(after.MapId),
                    ["before_world_pool_status"] = before.WorldPoolStatus == null ? JValue.CreateNull() : new JValue(before.WorldPoolStatus),
                    ["after_world_pool_status"] = after.WorldPoolStatus == null ? JValue.CreateNull() : new JValue(after.WorldPoolStatus),
                    ["before_chain_complete"] = before.ChainComplete,
                    ["after_chain_complete"] = after.ChainComplete,
                    ["identity_preserved"] = identityPreserved.HasValue ? new JValue(identityPreserved.Value) : JValue.CreateNull(),
                    ["identity_evidence"] = identityPreserved.HasValue ? "saved_id_comparison" : "saved_id_unavailable",
                    ["coverage"] = "observed ThingOwner add/remove, map/caravan entry, world pool methods, and mothball processing; unknown holder types remain explicit"
                };
            if (String.IsNullOrWhiteSpace(afterId))
            {
                // A pawn event needs a saved subject ID. Preserve the observed
                // change as an explicit coverage gap instead of inventing one.
                payload["scope"] = "pawn_holder_changed";
                payload["complete"] = false;
                payload["reason"] = "saved_pawn_id_unavailable";
                RecorderBootstrap.Emit("pawn_holder_changed", "coverage", null, null, payload,
                    "coverage", mapId: after.MapId, hookSources: sources);
                return;
            }
            RecorderBootstrap.Emit("pawn_holder_changed", "pawn_holder_changed", afterId, null, payload,
                "observed_difference", mapId: after.MapId, hookSources: sources);
        }

        // Native ThingOwner<T>.TryAdd and Remove both call this non-generic method
        // before changing holdingOwner. Their successful paths later call the
        // non-generic NotifyAdded/NotifyRemoved methods, respectively.
        internal static PendingHolderState? CaptureContains(ThingOwner owner, Thing thing)
        {
            if (!(thing is Pawn pawn) || owner.Owner is Map) return null;
            try
            {
                return new PendingHolderState { Before = Capture(pawn), HoldingOwner = pawn.holdingOwner,
                    CapturedAt = System.Diagnostics.Stopwatch.GetTimestamp() };
            }
            catch (Exception ex) { Log.Warning("[Life Recorder] Owner pre-mutation snapshot failed: " + ex.GetType().Name); return null; }
        }

        internal static void StoreContains(ThingOwner owner, Thing thing, PendingHolderState? state, bool contains)
        {
            if (!(thing is Pawn pawn) || state == null) return;
            try
            {
                state.ContainsResult = contains;
                var pending = _pending ?? (_pending = new Dictionary<ThingOwner, Dictionary<Pawn, PendingHolderState>>(IdentityComparer<ThingOwner>.Instance));
                PrunePending(state.CapturedAt);
                if (_pendingCount >= MaxPendingSnapshots) { pending.Clear(); _pendingCount = 0; }
                if (!pending.TryGetValue(owner, out var pawns))
                    pending[owner] = pawns = new Dictionary<Pawn, PendingHolderState>(IdentityComparer<Pawn>.Instance);
                if (!pawns.ContainsKey(pawn)) _pendingCount++;
                pawns[pawn] = state;
            }
            catch (Exception ex) { Log.Warning("[Life Recorder] Owner pre-mutation cache failed: " + ex.GetType().Name); }
        }

        private static HolderBefore? BeginNotification(ThingOwner owner, Thing thing, bool added)
        {
            if (!(thing is Pawn pawn) || owner.Owner is Map) return null;
            PendingHolderState? state = null;
            var pending = _pending;
            if (pending != null && pending.TryGetValue(owner, out var pawns) && pawns.TryGetValue(pawn, out state))
            {
                pawns.Remove(pawn);
                _pendingCount--;
                if (pawns.Count == 0) pending.Remove(owner);
            }
            var recent = state != null && System.Diagnostics.Stopwatch.GetTimestamp() - state.CapturedAt <= System.Diagnostics.Stopwatch.Frequency * SnapshotLifetimeSeconds;
            var valid = state != null && recent && state.ContainsResult != added &&
                (added ? state.HoldingOwner == null : ReferenceEquals(state.HoldingOwner, owner));
            if (!valid)
            {
                RecorderBootstrap.Emit("pawn_holder_changed", "coverage", CollectorUtil.Id(pawn), null,
                    new JObject
                    {
                        ["scope"] = "thing_owner_notification",
                        ["operation"] = added ? "add" : "remove",
                        ["complete"] = false,
                        ["reason"] = state == null ? "pre_mutation_snapshot_missing" : !recent ? "pre_mutation_snapshot_stale" :
                            state.ContainsResult == added ? "pre_mutation_contains_result_mismatch" : "pre_mutation_holding_owner_mismatch"
                    }, "coverage");
                return null;
            }
            var before = state!.Before;
            before.CorrelationScope = TransitionCorrelation.Begin(added ? "ThingOwner.NotifyAdded" : "ThingOwner.NotifyRemoved");
            return before;
        }

        internal static HolderBefore? AddedPrefix(ThingOwner owner, Thing thing)
        {
            try { return BeginNotification(owner, thing, true); }
            catch (Exception ex) { Log.Warning("[Life Recorder] Owner add observation failed: " + ex.GetType().Name); return null; }
        }
        internal static HolderBefore? RemovedPrefix(ThingOwner owner, Thing thing)
        {
            try { return BeginNotification(owner, thing, false); }
            catch (Exception ex) { Log.Warning("[Life Recorder] Owner remove observation failed: " + ex.GetType().Name); return null; }
        }
        internal static void NotificationPostfix(Thing thing, HolderBefore? before)
        {
            try { Emit(thing, before); }
            catch (Exception ex) { before?.CorrelationScope?.Dispose(); Log.Warning("[Life Recorder] Owner notification was isolated after " + ex.GetType().Name); }
        }
        internal static void ThingPrefix(Thing __instance, out HolderBefore? __state) => __state = __instance is Pawn ? CaptureScoped(__instance, "Thing.DeSpawn") : null;
        internal static void ThingPostfix(Thing __instance, HolderBefore? __state) => Emit(__instance, __state);
        internal static void PawnSpawnPrefix(Pawn __instance, out HolderBefore __state) => __state = CaptureScoped(__instance, "Pawn.SpawnSetup");
        internal static void PawnSpawnPostfix(Pawn __instance, HolderBefore __state) => Emit(__instance, __state);
        internal static void CaravanPrefix(Caravan __instance, Pawn __0, out HolderBefore __state) => __state = CaptureScoped(__0, "Caravan.AddOrRemovePawn");
        internal static void CaravanPostfix(Caravan __instance, Pawn __0, HolderBefore __state) => Emit(__0, __state);

        internal static void WorldPawnPrefix(Pawn __0, out HolderBefore __state) => __state = CaptureScoped(__0, "WorldPawns.PoolMutation");
        internal static void WorldPawnPostfix(Pawn __0, HolderBefore __state) => Emit(__0, __state);

    }

    internal static class HolderOwnerContainsCollector
    {
        public static void Prefix(ThingOwner __instance, Thing __0, out PendingHolderState? __state) => __state = HolderCollector.CaptureContains(__instance, __0);
        public static void Postfix(ThingOwner __instance, Thing __0, bool __result, PendingHolderState? __state) => HolderCollector.StoreContains(__instance, __0, __state, __result);
    }

    internal static class HolderOwnerAddedCollector
    {
        public static void Prefix(ThingOwner __instance, Thing __0, out HolderBefore? __state) => __state = HolderCollector.AddedPrefix(__instance, __0);
        public static void Postfix(Thing __0, HolderBefore? __state) => HolderCollector.NotificationPostfix(__0, __state);
        public static Exception? Finalizer(Exception? __exception, HolderBefore? __state) { __state?.CorrelationScope?.Dispose(); return __exception; }
    }

    internal static class HolderOwnerRemovedCollector
    {
        public static void Prefix(ThingOwner __instance, Thing __0, out HolderBefore? __state) => __state = HolderCollector.RemovedPrefix(__instance, __0);
        public static void Postfix(Thing __0, HolderBefore? __state) => HolderCollector.NotificationPostfix(__0, __state);
        public static Exception? Finalizer(Exception? __exception, HolderBefore? __state) { __state?.CorrelationScope?.Dispose(); return __exception; }
    }

    internal static class HolderThingDespawnCollector
    {
        public static void Prefix(Thing __instance, out HolderBefore? __state) => HolderCollector.ThingPrefix(__instance, out __state);
        public static void Postfix(Thing __instance, HolderBefore? __state) => HolderCollector.ThingPostfix(__instance, __state);
        public static Exception? Finalizer(Exception? __exception, HolderBefore? __state) { __state?.CorrelationScope?.Dispose(); return __exception; }
    }

    internal static class HolderPawnSpawnCollector
    {
        public static void Prefix(Pawn __instance, out HolderBefore __state) => HolderCollector.PawnSpawnPrefix(__instance, out __state);
        public static void Postfix(Pawn __instance, HolderBefore __state) => HolderCollector.PawnSpawnPostfix(__instance, __state);
        public static Exception? Finalizer(Exception? __exception, HolderBefore? __state) { __state?.CorrelationScope?.Dispose(); return __exception; }
    }

    internal static class HolderCaravanCollector
    {
        public static void Prefix(Caravan __instance, Pawn __0, out HolderBefore __state) => HolderCollector.CaravanPrefix(__instance, __0, out __state);
        public static void Postfix(Caravan __instance, Pawn __0, HolderBefore __state) => HolderCollector.CaravanPostfix(__instance, __0, __state);
        public static Exception? Finalizer(Exception? __exception, HolderBefore? __state) { __state?.CorrelationScope?.Dispose(); return __exception; }
    }

    internal static class HolderMakeCaravanCollector
    {
        public static void Prefix(out IDisposable __state) => __state = TransitionCorrelation.Begin("CaravanMaker.MakeCaravan");
        public static void Postfix(IDisposable __state) => __state.Dispose();
        public static Exception? Finalizer(Exception? __exception, IDisposable? __state) { __state?.Dispose(); return __exception; }
    }
}
