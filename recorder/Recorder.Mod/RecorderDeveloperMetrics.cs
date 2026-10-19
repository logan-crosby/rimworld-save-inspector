using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Reflection;
using System.Threading;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using RimWorld.LifeRecorder.Core;

namespace RimWorld.LifeRecorder
{
    /// <summary>Opt-in, bounded local developer measurements. Disabled by default.</summary>
    public static class RecorderDeveloperMetrics
    {
        private const int SamplesPerFamily = 2048;
        private static readonly string[] Families = { "pawn_died", "pawn_resurrected", "pawn_born", "pawn_faction_changed", "pawn_guest_status_changed", "pawn_relation_changed", "pawn_health_changed", "pawn_genes_changed", "pawn_role_changed", "pawn_holder_changed" };
        private sealed class Ring
        {
            internal readonly long[] ElapsedTicks = new long[SamplesPerFamily];
            internal int Count;
            internal int Next;
            internal long OverflowCount;
            internal long CompletedTransitions;
            internal long QueueBytesPeak;
            internal long PendingBirthBytesPeak;
            internal long RetainedSerializedBytesPeak;
        }
        private static readonly Dictionary<string, Ring> Rings = Families.ToDictionary(f => f, _ => new Ring(), StringComparer.Ordinal);
        private static readonly Dictionary<string, List<long>> Operations = new Dictionary<string, List<long>>(StringComparer.Ordinal)
        {
            ["baseline"] = new List<long>(32), ["flush"] = new List<long>(512), ["world_pool_reconciliation"] = new List<long>(64)
        };
        private static readonly Dictionary<string, long> OperationDrops = Operations.Keys.ToDictionary(k => k, _ => 0L, StringComparer.Ordinal);
        private static int _enabled;
        private static long _queueBytesPeak;
        private static long _pendingBirthBytesPeak;
        private static long _retainedSerializedBytesPeak;
        private static long _lateBirthObservationCount;
        private static long _lateBirthConflictCount;
        private static long _birthOverflowCount;

        /// <summary>Enables or disables bounded prefix/postfix callback timing collection.</summary>
        public static void Enable(bool enabled) => Interlocked.Exchange(ref _enabled, enabled ? 1 : 0);
        /// <summary>Clears timing samples and queue-byte peaks without changing enable state.</summary>
        public static void Reset()
        {
            lock (Rings)
            {
                foreach (var ring in Rings.Values) { Array.Clear(ring.ElapsedTicks, 0, ring.ElapsedTicks.Length); ring.Count = 0; ring.Next = 0; ring.OverflowCount = 0; ring.CompletedTransitions = 0; ring.QueueBytesPeak = 0; ring.PendingBirthBytesPeak = 0; ring.RetainedSerializedBytesPeak = 0; }
                foreach (var operation in Operations) { operation.Value.Clear(); OperationDrops[operation.Key] = 0; }
                _queueBytesPeak = 0; _pendingBirthBytesPeak = 0; _retainedSerializedBytesPeak = 0;
                _lateBirthObservationCount = 0; _lateBirthConflictCount = 0; _birthOverflowCount = 0;
            }
        }
        /// <summary>Returns a stable JSON snapshot for local acceptance tooling.</summary>
        public static string SnapshotJson()
        {
            var result = new JObject { ["enabled"] = Volatile.Read(ref _enabled) != 0, ["sample_capacity_per_family"] = SamplesPerFamily, ["stopwatch_frequency"] = Stopwatch.Frequency };
            lock (Rings)
            {
                var families = new JObject();
                foreach (var pair in Rings)
                {
                    var ring = pair.Value;
                    var values = ring.ElapsedTicks.Take(ring.Count).OrderBy(x => x).ToArray();
                    var p95Index = values.Length == 0 ? -1 : (int)Math.Ceiling(0.95 * values.Length) - 1;
                    families[pair.Key] = new JObject
                    {
                        ["sample_count"] = ring.Count,
                        ["sample_overflow_count"] = ring.OverflowCount,
                        ["completed_transitions"] = ring.CompletedTransitions,
                        ["raw_elapsed_stopwatch_ticks"] = new JArray(ring.ElapsedTicks.Take(ring.Count)),
                        ["p95_stopwatch_ticks"] = p95Index < 0 ? JValue.CreateNull() : new JValue(values[p95Index]),
                        ["max_stopwatch_ticks"] = values.Length == 0 ? JValue.CreateNull() : new JValue(values[values.Length - 1]),
                        ["mean_stopwatch_ticks"] = values.Length == 0 ? JValue.CreateNull() : new JValue(values.Average()),
                        ["queue_bytes_peak"] = ring.QueueBytesPeak,
                        ["pending_birth_bytes_peak"] = ring.PendingBirthBytesPeak,
                        ["retained_serialized_bytes_peak"] = ring.RetainedSerializedBytesPeak
                    };
                }
                result["families"] = families;
                var operations = new JObject();
                foreach (var operation in Operations)
                    operations[operation.Key] = new JObject { ["raw_elapsed_stopwatch_ticks"] = new JArray(operation.Value), ["sample_overflow_count"] = OperationDrops[operation.Key] };
                result["separate_operations"] = operations;
            }
            var current = RecorderSessionComponent.Current;
            result["queue_bytes_now"] = current?.QueueSerializedBytes ?? 0;
            result["pending_birth_bytes_now"] = current?.PendingBirthBytes ?? 0;
            result["retained_serialized_bytes_now"] = current?.QueuedBytes ?? 0;
            result["queue_bytes_peak"] = _queueBytesPeak;
            result["pending_birth_bytes_peak"] = _pendingBirthBytesPeak;
            result["retained_serialized_bytes_peak"] = _retainedSerializedBytesPeak;
            result["late_birth_observation_count"] = _lateBirthObservationCount;
            result["late_birth_conflict_count"] = _lateBirthConflictCount;
            result["birth_observation_overflow_count"] = _birthOverflowCount;
            return result.ToString(Newtonsoft.Json.Formatting.None);
        }

        internal static bool Enabled => Volatile.Read(ref _enabled) != 0;
        internal static void Add(string family, long elapsedTicks)
        {
            if (!Enabled || !Rings.TryGetValue(family, out var ring)) return;
            var session = RecorderSessionComponent.Current;
            var queueBytes = session?.QueueSerializedBytes ?? 0;
            var pendingBytes = session?.PendingBirthBytes ?? 0;
            var retainedBytes = session?.QueuedBytes ?? 0;
            lock (Rings)
            {
                UpdatePeak(ref _queueBytesPeak, queueBytes); UpdatePeak(ref _pendingBirthBytesPeak, pendingBytes); UpdatePeak(ref _retainedSerializedBytesPeak, retainedBytes);
                if (ring.Count == ring.ElapsedTicks.Length) ring.OverflowCount++;
                ring.ElapsedTicks[ring.Next] = elapsedTicks;
                ring.Next = (ring.Next + 1) % ring.ElapsedTicks.Length;
                if (ring.Count < ring.ElapsedTicks.Length) ring.Count++;
                if (queueBytes > ring.QueueBytesPeak) ring.QueueBytesPeak = queueBytes;
                if (pendingBytes > ring.PendingBirthBytesPeak) ring.PendingBirthBytesPeak = pendingBytes;
                if (retainedBytes > ring.RetainedSerializedBytesPeak) ring.RetainedSerializedBytesPeak = retainedBytes;
            }
        }
        internal static void ObserveRetention(long queueBytes, long pendingBytes)
        {
            if (!Enabled) return;
            lock (Rings)
            {
                UpdatePeak(ref _queueBytesPeak, queueBytes); UpdatePeak(ref _pendingBirthBytesPeak, pendingBytes);
                UpdatePeak(ref _retainedSerializedBytesPeak, queueBytes + pendingBytes);
            }
        }
        internal static void NoteBirthObservation(BirthObserveResult result)
        {
            if (!Enabled) return;
            lock (Rings)
            {
                if (result == BirthObserveResult.LateObservation || result == BirthObserveResult.LateConflict) _lateBirthObservationCount++;
                if (result == BirthObserveResult.LateConflict) _lateBirthConflictCount++;
                if (result == BirthObserveResult.Overflow || result == BirthObserveResult.CapacityExhausted) _birthOverflowCount++;
            }
        }
        private static void UpdatePeak(ref long peak, long value) { if (value > peak) peak = value; }
        internal static void NoteTransition(string family)
        {
            if (!Enabled || !Rings.TryGetValue(family, out var ring)) return;
            lock (Rings) ring.CompletedTransitions++;
        }
        internal static void RecordOperation(string name, long elapsedTicks)
        {
            if (!Enabled || !Operations.TryGetValue(name, out var samples)) return;
            lock (Rings)
            {
                if (samples.Count >= samples.Capacity) { OperationDrops[name]++; return; }
                samples.Add(elapsedTicks);
            }
        }
    }

    internal static class CollectorMetricInstrumentation
    {
        private sealed class Frame { internal MethodBase Method = null!; internal string Family = ""; internal long PrefixStarted; internal long PrefixElapsed; internal long PostfixStarted; }
        private static readonly Dictionary<MethodBase, string> Targets = new Dictionary<MethodBase, string>();
        [ThreadStatic] private static List<Frame>? _frames;

        internal static void Install(Harmony harmony, MethodBase target, string family)
        {
            lock (Targets) Targets[target] = family;
            var prefix = typeof(CollectorMetricInstrumentation).GetMethod(nameof(Prefix), BindingFlags.Static | BindingFlags.Public)!;
            var prefixStop = typeof(CollectorMetricInstrumentation).GetMethod(nameof(PrefixStop), BindingFlags.Static | BindingFlags.Public)!;
            var postfixStart = typeof(CollectorMetricInstrumentation).GetMethod(nameof(PostfixStart), BindingFlags.Static | BindingFlags.Public)!;
            var postfixStop = typeof(CollectorMetricInstrumentation).GetMethod(nameof(PostfixStop), BindingFlags.Static | BindingFlags.Public)!;
            var finalizer = typeof(CollectorMetricInstrumentation).GetMethod(nameof(Finalizer), BindingFlags.Static | BindingFlags.Public)!;
            harmony.Patch(target, new HarmonyMethod(prefix) { priority = Priority.First });
            harmony.Patch(target, new HarmonyMethod(prefixStop) { priority = Priority.Last });
            harmony.Patch(target, null, new HarmonyMethod(postfixStart) { priority = Priority.First });
            harmony.Patch(target, null, new HarmonyMethod(postfixStop) { priority = Priority.Last }, null, new HarmonyMethod(finalizer) { priority = Priority.Last });
        }

        public static void Prefix(MethodBase __originalMethod)
        {
            if (!RecorderDeveloperMetrics.Enabled) return;
            string? family;
            lock (Targets) Targets.TryGetValue(__originalMethod, out family);
            if (family == null) return;
            (_frames ?? (_frames = new List<Frame>())).Add(new Frame { Method = __originalMethod, Family = family, PrefixStarted = Stopwatch.GetTimestamp() });
        }
        public static void PrefixStop(MethodBase __originalMethod)
        {
            var frame = Find(__originalMethod);
            if (frame != null) frame.PrefixElapsed = Stopwatch.GetTimestamp() - frame.PrefixStarted;
        }
        public static void PostfixStart(MethodBase __originalMethod)
        {
            var frame = Find(__originalMethod);
            if (frame != null) frame.PostfixStarted = Stopwatch.GetTimestamp();
        }
        public static void PostfixStop(MethodBase __originalMethod) => Finish(__originalMethod, false);
        public static Exception? Finalizer(MethodBase __originalMethod, Exception? __exception) { if (__exception != null) Finish(__originalMethod, true); return __exception; }

        private static Frame? Find(MethodBase method)
        {
            var frames = _frames;
            if (frames == null) return null;
            var index = frames.FindLastIndex(frame => frame.Method == method);
            return index < 0 ? null : frames[index];
        }

        private static void Finish(MethodBase method, bool failed)
        {
            var frames = _frames;
            if (frames == null) return;
            var index = frames.FindLastIndex(frame => frame.Method == method);
            if (index < 0) return;
            var frame = frames[index];
            frames.RemoveAt(index);
            if (!failed) RecorderDeveloperMetrics.Add(frame.Family, frame.PrefixElapsed + Math.Max(0, Stopwatch.GetTimestamp() - frame.PostfixStarted));
            if (frames.Count == 0) _frames = null;
        }
    }
}
