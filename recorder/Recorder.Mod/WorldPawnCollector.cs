using System;
using System.Collections.Generic;
using System.Reflection;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using RimWorld.LifeRecorder.Core;
using RimWorld.Planet;
using Verse;

namespace RimWorld.LifeRecorder
{
    internal sealed class MothballSnapshot
    {
        internal HashSet<Pawn> Alive = new HashSet<Pawn>();
        internal bool Complete;
        internal IDisposable? CorrelationScope;
        internal long MetricsStarted;
    }

    /// <summary>Uses cached native world-pool references so per-pawn holder capture does not reflect.</summary>
    internal static class WorldPawnCollector
    {
        private const int MaxMothballSnapshotPawns = 100000;
        private static HashSet<Pawn>? _alive;
        private static HashSet<Pawn>? _mothballed;
        private static HashSet<Pawn>? _dead;
        private static string _initializationStatus = "missing";

        internal static string InitializationStatus => _initializationStatus;

        internal static void Initialize()
        {
            try
            {
                var world = Find.WorldPawns;
                if (world == null) { _initializationStatus = "missing"; return; }
                const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
                var type = typeof(WorldPawns);
                _alive = type.GetField("pawnsAlive", flags)?.GetValue(world) as HashSet<Pawn>;
                _mothballed = type.GetField("pawnsMothballed", flags)?.GetValue(world) as HashSet<Pawn>;
                _dead = type.GetField("pawnsDead", flags)?.GetValue(world) as HashSet<Pawn>;
                _initializationStatus = _alive != null && _mothballed != null && _dead != null ? "installed" : "missing";
            }
            catch (Exception ex)
            {
                _alive = _mothballed = _dead = null;
                _initializationStatus = "failed:" + ex.GetType().Name;
            }
        }

        internal static string Classify(Pawn pawn)
        {
            try
            {
                if (_dead?.Contains(pawn) == true) return "world_dead";
                if (_mothballed?.Contains(pawn) == true) return "world_mothballed";
                if (_alive?.Contains(pawn) == true) return "world_alive";
                return "not_in_world";
            }
            catch { return "world_pool_unknown"; }
        }

        internal static int InstallMothballProcessing(Harmony harmony)
        {
            var method = typeof(WorldPawns).GetMethod("DoMothballProcessing", BindingFlags.Instance | BindingFlags.NonPublic, null, Type.EmptyTypes, null);
            if (method == null) return 0;
            harmony.Patch(method,
                new HarmonyMethod(typeof(WorldPawnCollector).GetMethod(nameof(MothballPrefix), BindingFlags.Static | BindingFlags.NonPublic)),
                new HarmonyMethod(typeof(WorldPawnCollector).GetMethod(nameof(MothballPostfix), BindingFlags.Static | BindingFlags.NonPublic)));
            return 1;
        }

        internal static void MothballPrefix(WorldPawns __instance, out MothballSnapshot __state)
        {
            __state = new MothballSnapshot
            {
                CorrelationScope = TransitionCorrelation.Begin("WorldPawns.DoMothballProcessing"),
                MetricsStarted = RecorderDeveloperMetrics.Enabled ? System.Diagnostics.Stopwatch.GetTimestamp() : 0
            };
            try
            {
                if (_alive == null || _alive.Count > MaxMothballSnapshotPawns) return;
                __state.Alive = new HashSet<Pawn>(_alive);
                __state.Complete = true;
            }
            catch { __state.Complete = false; }
        }

        internal static void MothballPostfix(WorldPawns __instance, MothballSnapshot __state)
        {
            try
            {
                if (!__state.Complete || _mothballed == null)
                {
                    RecorderBootstrap.Emit("pawn_holder_changed", "coverage", null, null, new JObject
                    {
                        ["scope"] = "world_mothball_reconciliation",
                        ["complete"] = false,
                        ["reason"] = "world_alive_pool_snapshot_unavailable_or_over_limit",
                        ["snapshot_limit"] = MaxMothballSnapshotPawns
                    }, "coverage");
                    return;
                }
                foreach (var pawn in __state.Alive)
                {
                    try
                    {
                        if (!_mothballed.Contains(pawn)) continue;
                        var id = CollectorUtil.Id(pawn);
                        var before = new HolderBefore { PawnId = id ?? "", WorldPoolStatus = "world_alive" };
                        if (id != null) before.Chain.Add(new JObject { ["kind"] = "world_alive", ["id"] = id });
                        HolderCollector.Emit(pawn, before);
                    }
                    catch (Exception ex)
                    {
                        RecorderBootstrap.Emit("pawn_holder_changed", "coverage", null, null, new JObject
                        {
                            ["scope"] = "world_mothball_reconciliation",
                            ["complete"] = false,
                            ["failure"] = ex.GetType().Name
                        }, "coverage");
                        break;
                    }
                }
            }
            finally
            {
                __state.CorrelationScope?.Dispose();
                if (__state.MetricsStarted != 0) RecorderDeveloperMetrics.RecordOperation("world_pool_reconciliation", System.Diagnostics.Stopwatch.GetTimestamp() - __state.MetricsStarted);
            }
        }

        public static void Prefix(WorldPawns __instance, out MothballSnapshot __state) => MothballPrefix(__instance, out __state);
        public static void Postfix(WorldPawns __instance, MothballSnapshot __state) => MothballPostfix(__instance, __state);
        public static Exception? Finalizer(Exception? __exception, MothballSnapshot? __state) { __state?.CorrelationScope?.Dispose(); return __exception; }
    }

    internal static class HolderWorldPawnCollector
    {
        public static void Prefix(Pawn __0, out HolderBefore __state) => HolderCollector.WorldPawnPrefix(__0, out __state);
        public static void Postfix(Pawn __0, HolderBefore __state) => HolderCollector.Emit(__0, __state);
        public static Exception? Finalizer(Exception? __exception, HolderBefore? __state) { __state?.CorrelationScope?.Dispose(); return __exception; }
    }
}
