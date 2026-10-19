using System;
using System.Linq;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;
using HarmonyLib;
using RimWorld;
using Verse;

namespace Rwsi.NativeAcceptance
{
    /// <summary>One-shot, profile-gated failure inside the real nested health-add call path.</summary>
    internal static class AcceptanceHealthFaultProbe
    {
        private const string RecorderHarmonyId = "logan.crosby.rimworld.life-recorder";
        private const string HarnessHarmonyId = "rwsi.nativeacceptance.health-fault";
        private static Pawn_HealthTracker? _targetTracker;
        private static HediffDef? _targetDef;
        private static bool _armed;
        private static bool _outerEntered;
        private static bool _innerEntered;
        private static bool _faultConsumed;
        private static int _outerPrefixEntries;
        private static int _innerPrefixEntries;
        private static int _outerFinalizerEntries;
        private static int _innerFinalizerEntries;
        private static int _recorderOuterPrefixEntries;
        private static int _recorderInnerPrefixEntries;
        private static int _recorderOuterFinalizerEntries;
        private static int _recorderInnerFinalizerEntries;
        private static int _faultConsumptionCount;
        private static int? _faultHediffLoadId;
        private static int? _faultHediffReferenceHash;
        private static Harmony? _harmony;

        internal static bool TryArm(Pawn pawn, HediffDef def, out string error)
        {
            error = "";
            var flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
            var outer = typeof(Pawn_HealthTracker).GetMethod("AddHediff", flags, null,
                new[] { typeof(HediffDef), typeof(BodyPartRecord), typeof(DamageInfo?), typeof(DamageWorker.DamageResult) }, null);
            var inner = typeof(Pawn_HealthTracker).GetMethod("AddHediff", flags, null,
                new[] { typeof(Hediff), typeof(BodyPartRecord), typeof(DamageInfo?), typeof(DamageWorker.DamageResult) }, null);
            if (outer == null || inner == null) { error = "Exact nested Pawn_HealthTracker.AddHediff overloads are unavailable."; return false; }
            if (!CallsMethod(outer, inner)) { error = "Installed HediffDef overload does not contain a direct call to the instrumented Hediff overload."; return false; }
            if (!RecorderHasBeforeFaultPrefix(outer, requireFinalizer: true) || !RecorderHasBeforeFaultPrefix(inner, requireFinalizer: true))
            {
                error = "Recorder prefix/finalizer order is not verifiable; refusing to inject a native failure.";
                return false;
            }
            if (_armed || _harmony != null) { error = "A prior health fault probe is still armed."; return false; }

            _targetTracker = pawn.health;
            _targetDef = def;
            _outerEntered = false;
            _innerEntered = false;
            _faultConsumed = false;
            _outerPrefixEntries = 0;
            _innerPrefixEntries = 0;
            _outerFinalizerEntries = 0;
            _innerFinalizerEntries = 0;
            _recorderOuterPrefixEntries = 0;
            _recorderInnerPrefixEntries = 0;
            _recorderOuterFinalizerEntries = 0;
            _recorderInnerFinalizerEntries = 0;
            _faultConsumptionCount = 0;
            _faultHediffLoadId = null;
            _faultHediffReferenceHash = null;
            try
            {
                _harmony = new Harmony(HarnessHarmonyId);
                var outerPrefix = typeof(AcceptanceHealthFaultProbe).GetMethod(nameof(OuterPrefix), BindingFlags.Static | BindingFlags.NonPublic);
                var innerPrefix = typeof(AcceptanceHealthFaultProbe).GetMethod(nameof(InnerPrefix), BindingFlags.Static | BindingFlags.NonPublic);
                var outerFinalizer = typeof(AcceptanceHealthFaultProbe).GetMethod(nameof(OuterFinalizer), BindingFlags.Static | BindingFlags.NonPublic);
                var innerFinalizer = typeof(AcceptanceHealthFaultProbe).GetMethod(nameof(InnerFinalizer), BindingFlags.Static | BindingFlags.NonPublic);
                var recorderFinalizers = ResolveRecorderFinalizers();
                if (outerPrefix == null || innerPrefix == null || outerFinalizer == null || innerFinalizer == null || recorderFinalizers == null)
                    throw new MissingMethodException("Health fault callbacks or the recorder health callbacks are missing.");
                _harmony.Patch(outer, prefix: new HarmonyMethod(outerPrefix) { priority = Priority.Last },
                    finalizer: new HarmonyMethod(outerFinalizer) { priority = Priority.Last });
                _harmony.Patch(inner, prefix: new HarmonyMethod(innerPrefix) { priority = Priority.Last },
                    finalizer: new HarmonyMethod(innerFinalizer) { priority = Priority.Last });
                var recorderOuterFinalizerPostfix = typeof(AcceptanceHealthFaultProbe).GetMethod(nameof(RecorderOuterFinalizerPostfix), BindingFlags.Static | BindingFlags.NonPublic);
                var recorderInnerFinalizerPostfix = typeof(AcceptanceHealthFaultProbe).GetMethod(nameof(RecorderInnerFinalizerPostfix), BindingFlags.Static | BindingFlags.NonPublic);
                var recorderOuterPrefixObserver = typeof(AcceptanceHealthFaultProbe).GetMethod(nameof(RecorderOuterPrefixPostfix), BindingFlags.Static | BindingFlags.NonPublic);
                var recorderInnerPrefixObserver = typeof(AcceptanceHealthFaultProbe).GetMethod(nameof(RecorderInnerPrefixPostfix), BindingFlags.Static | BindingFlags.NonPublic);
                if (recorderOuterFinalizerPostfix == null || recorderInnerFinalizerPostfix == null || recorderOuterPrefixObserver == null || recorderInnerPrefixObserver == null)
                    throw new MissingMethodException("Recorder callback observation methods are missing.");
                _harmony.Patch(recorderFinalizers.Value.OuterPrefix, postfix: new HarmonyMethod(recorderOuterPrefixObserver) { priority = Priority.Last });
                _harmony.Patch(recorderFinalizers.Value.InnerPrefix, postfix: new HarmonyMethod(recorderInnerPrefixObserver) { priority = Priority.Last });
                _harmony.Patch(recorderFinalizers.Value.OuterFinalizer, postfix: new HarmonyMethod(recorderOuterFinalizerPostfix) { priority = Priority.Last });
                _harmony.Patch(recorderFinalizers.Value.InnerFinalizer, postfix: new HarmonyMethod(recorderInnerFinalizerPostfix) { priority = Priority.Last });
                _armed = true;
                return true;
            }
            catch (Exception exception)
            {
                Disarm();
                error = "Could not arm exact nested health fault: " + exception;
                return false;
            }
        }

        private static (MethodInfo OuterPrefix, MethodInfo InnerPrefix, MethodInfo OuterFinalizer, MethodInfo InnerFinalizer)? ResolveRecorderFinalizers()
        {
            var assembly = AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(candidate => candidate.GetName().Name == "RimWorld.LifeRecorder");
            if (assembly == null) return null;
            var outerType = assembly.GetType("RimWorld.LifeRecorder.HealthAddDefCollector", false);
            var innerType = assembly.GetType("RimWorld.LifeRecorder.HealthAddCollector", false);
            var outerPrefix = outerType?.GetMethod("Prefix", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            var innerPrefix = innerType?.GetMethod("Prefix", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            var outerFinalizer = outerType?.GetMethod("Finalizer", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            var innerFinalizer = innerType?.GetMethod("Finalizer", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            return outerPrefix == null || innerPrefix == null || outerFinalizer == null || innerFinalizer == null
                ? null : (outerPrefix, innerPrefix, outerFinalizer, innerFinalizer);
        }

        private static bool RecorderHasBeforeFaultPrefix(MethodBase target, bool requireFinalizer)
        {
            var patches = Harmony.GetPatchInfo(target);
            var prefix = patches?.Prefixes?.Any(patch => patch.owner == RecorderHarmonyId && patch.priority > Priority.Last) == true;
            var finalizer = !requireFinalizer || patches?.Finalizers?.Any(patch => patch.owner == RecorderHarmonyId) == true;
            return prefix && finalizer;
        }

        private static bool CallsMethod(MethodInfo caller, MethodInfo expected)
        {
            var body = caller.GetMethodBody()?.GetILAsByteArray();
            if (body == null) return false;
            var opcodes = new System.Collections.Generic.Dictionary<short, OpCode>();
            foreach (var field in typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static).Where(field => field.FieldType == typeof(OpCode)))
            {
                var opcode = (OpCode)field.GetValue(null)!;
                if (!opcodes.ContainsKey(opcode.Value)) opcodes.Add(opcode.Value, opcode);
            }
            for (var offset = 0; offset < body.Length;)
            {
                var twoByte = body[offset] == 0xfe;
                short value = twoByte
                    ? unchecked((short)(0xfe00 | body[offset + 1]))
                    : body[offset];
                offset += twoByte ? 2 : 1;
                if (!opcodes.TryGetValue(value, out var opcode)) return false;
                if ((opcode == OpCodes.Call || opcode == OpCodes.Callvirt) && offset + 4 <= body.Length &&
                    BitConverter.ToInt32(body, offset) == expected.MetadataToken) return true;
                switch (opcode.OperandType)
                {
                    case OperandType.InlineNone: break;
                    case OperandType.ShortInlineBrTarget:
                    case OperandType.ShortInlineI:
                    case OperandType.ShortInlineVar: offset += 1; break;
                    case OperandType.InlineVar: offset += 2; break;
                    case OperandType.InlineI8:
                    case OperandType.InlineR: offset += 8; break;
                    case OperandType.InlineSwitch:
                    {
                        var count = BitConverter.ToInt32(body, offset);
                        offset += 4 + count * 4;
                        break;
                    }
                    case OperandType.InlineBrTarget:
                    case OperandType.InlineField:
                    case OperandType.InlineI:
                    case OperandType.InlineMethod:
                    case OperandType.InlineSig:
                    case OperandType.InlineString:
                    case OperandType.InlineTok:
                    case OperandType.InlineType:
                    case OperandType.ShortInlineR: offset += 4; break;
                    default: return false;
                }
                if (offset < 0 || offset > body.Length) return false;
            }
            return false;
        }

        private static void OuterPrefix(Pawn_HealthTracker __instance, HediffDef __0)
        {
            if (_armed && ReferenceEquals(__instance, _targetTracker) && ReferenceEquals(__0, _targetDef))
            { _outerEntered = true; _outerPrefixEntries++; }
        }

        private static void InnerPrefix(Pawn_HealthTracker __instance, Hediff __0)
        {
            if (!_armed || __0 == null || !ReferenceEquals(__instance, _targetTracker) || !ReferenceEquals(__0.def, _targetDef)) return;
            _innerEntered = true;
            _innerPrefixEntries++;
            _faultConsumed = true;
            _faultConsumptionCount++;
            _faultHediffLoadId = __0.loadID;
            _faultHediffReferenceHash = RuntimeHelpers.GetHashCode(__0);
            throw new AcceptanceInjectedNativeException("Bounded synthetic-only failure injected after recorder scope prefix and before native Hediff mutation.");
        }

        private static Exception? OuterFinalizer(Exception? __exception)
        {
            if (_armed) _outerFinalizerEntries++;
            return __exception;
        }

        private static Exception? InnerFinalizer(Exception? __exception)
        {
            if (_armed) _innerFinalizerEntries++;
            return __exception;
        }

        private static void RecorderOuterFinalizerPostfix() { if (_armed) _recorderOuterFinalizerEntries++; }
        private static void RecorderInnerFinalizerPostfix() { if (_armed) _recorderInnerFinalizerEntries++; }

        private static void RecorderOuterPrefixPostfix() { if (_armed) _recorderOuterPrefixEntries++; }
        private static void RecorderInnerPrefixPostfix() { if (_armed) _recorderInnerPrefixEntries++; }

        internal static Type ExpectedInjectedExceptionType => typeof(AcceptanceInjectedNativeException);

        internal static string Trace => "outer_addhediff_entered=" + _outerEntered.ToString().ToLowerInvariant() +
            ";inner_addhediff_entered=" + _innerEntered.ToString().ToLowerInvariant() +
            ";outer_prefix_entries=" + _outerPrefixEntries + ";inner_prefix_entries=" + _innerPrefixEntries +
            ";outer_finalizer_entries=" + _outerFinalizerEntries + ";inner_finalizer_entries=" + _innerFinalizerEntries +
            ";recorder_outer_prefix_entries=" + _recorderOuterPrefixEntries +
            ";recorder_inner_prefix_entries=" + _recorderInnerPrefixEntries +
            ";recorder_outer_finalizer_entries=" + _recorderOuterFinalizerEntries +
            ";recorder_inner_finalizer_entries=" + _recorderInnerFinalizerEntries +
            ";fault_consumed=" + _faultConsumed.ToString().ToLowerInvariant() + ";fault_consumption_count=" + _faultConsumptionCount +
            ";fault_hediff_load_id=" + (_faultHediffLoadId?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "unavailable") +
            ";fault_hediff_runtime_reference_hash=" + (_faultHediffReferenceHash?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "unavailable");

        internal static bool Verified => _outerEntered && _innerEntered && _outerPrefixEntries == 1 && _innerPrefixEntries == 1 &&
            _outerFinalizerEntries == 1 && _innerFinalizerEntries == 1 && _recorderOuterPrefixEntries == 1 &&
            _recorderInnerPrefixEntries == 1 && _recorderOuterFinalizerEntries == 1 &&
            _recorderInnerFinalizerEntries == 1 && _faultConsumed && _faultConsumptionCount == 1 &&
            _faultHediffLoadId.HasValue && _faultHediffReferenceHash.HasValue;

        internal static void Disarm()
        {
            _armed = false;
            try { _harmony?.UnpatchAll(HarnessHarmonyId); }
            catch (Exception exception) { Log.Error("[RWSI Native Acceptance] Health fault unpatch failed: " + exception); }
            _harmony = null;
            _targetTracker = null;
            _targetDef = null;
        }

        internal sealed class AcceptanceInjectedNativeException : Exception
        {
            internal AcceptanceInjectedNativeException(string message) : base(message) { }
        }
    }
}
