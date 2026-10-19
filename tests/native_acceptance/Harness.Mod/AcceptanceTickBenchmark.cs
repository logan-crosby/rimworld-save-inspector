using System;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Diagnostics;
using HarmonyLib;
using LudeonTK;
using Newtonsoft.Json;
using RimWorld;
using Verse;

namespace Rwsi.NativeAcceptance
{
    /// <summary>
    /// Optional, independent timing of the real TickManager.DoSingleTick method. It is armed only
    /// by a guarded debug action and remains identical in recorder-off and recorder-on runs.
    /// </summary>
    public static class AcceptanceTickBenchmark
    {
        private const int WarmupTicks = 2500;
        private const int MeasuredTicks = 10000;
        private const string HarmonyId = "rwsi.nativeacceptance.tickprobe";
        private static bool _active;
        private static string _runId = "";
        private static int _startTick;
        private static int _startAbsoluteTick;
        private static int _startMeasuredAbsoluteTick;
        private static string? _probeError;
        private static Harmony? _harmony;
        private static DoSingleTickWindow? _window;

        [DebugAction("RWSI Native Acceptance", "Benchmark: start 2500+10000 actual DoSingleTick samples", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void Start()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error)) { Log.Error("[RWSI Native Acceptance] Tick benchmark rejected: " + error); return; }
            if (_active) { Log.Error("[RWSI Native Acceptance] Tick benchmark already active; no second interval started."); return; }
            if (Find.TickManager == null) { Log.Error("[RWSI Native Acceptance] Tick manager unavailable."); return; }
            var method = typeof(TickManager).GetMethod("DoSingleTick", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                binder: null, types: Type.EmptyTypes, modifiers: null);
            if (method == null) { Log.Error("[RWSI Native Acceptance] Installed native TickManager.DoSingleTick() was not found."); return; }

            _runId = Guid.NewGuid().ToString("D");
            _startTick = Find.TickManager.TicksGame;
            _startAbsoluteTick = Find.TickManager.TicksAbs;
            _startMeasuredAbsoluteTick = 0;
            _probeError = null;
            _window = new DoSingleTickWindow(_startTick, WarmupTicks, MeasuredTicks);
            try
            {
                _harmony = new Harmony(HarmonyId);
                var prefix = typeof(AcceptanceTickBenchmark).GetMethod(nameof(Prefix), BindingFlags.Static | BindingFlags.NonPublic);
                var postfix = typeof(AcceptanceTickBenchmark).GetMethod(nameof(Postfix), BindingFlags.Static | BindingFlags.NonPublic);
                if (prefix == null || postfix == null) throw new MissingMethodException("Tick timing probe callbacks were not found.");
                _harmony.Patch(method, prefix: new HarmonyMethod(prefix), postfix: new HarmonyMethod(postfix));
                _active = true;
                Log.Message("[RWSI Native Acceptance] Tick benchmark armed id=" + _runId + " start_game_tick=" + _startTick +
                    " warmup_ticks=" + WarmupTicks + " measured_ticks=" + MeasuredTicks +
                    " method=Verse.TickManager.DoSingleTick probe=Stopwatch.GetTimestamp prefix/postfix; " +
                    "unpause only through root-owned live controls.");
            }
            catch (Exception exception)
            {
                _active = false;
                Unpatch();
                _window = null;
                _runId = "";
                Log.Error("[RWSI Native Acceptance] Could not arm DoSingleTick probe: " + exception);
            }
        }

        private static void Prefix(TickManager __instance, out TickProbeState __state)
        {
            var tick = __instance.TicksGame;
            var window = _window;
            var measuring = _active && window != null && tick >= window.FirstMeasuredTick && !window.IsComplete;
            __state = new TickProbeState(measuring ? Stopwatch.GetTimestamp() : 0, tick, __instance.TicksAbs, measuring);
        }

        private static void Postfix(TickManager __instance, TickProbeState __state)
        {
            if (!_active || !__state.Measuring || _window == null) return;
            if (_window.SampleCount == 0) _startMeasuredAbsoluteTick = __state.AbsoluteTick;
            var elapsed = Stopwatch.GetTimestamp() - __state.Timestamp;
            try
            {
                if (_window.TryRecord(__state.GameTick, elapsed))
                {
                    _active = false;
                    Unpatch();
                    WriteResult("completed", __instance.TicksGame, __instance.TicksAbs);
                }
            }
            catch (Exception exception)
            {
                _probeError = exception.ToString();
                _active = false;
                Unpatch();
                try { WriteResult("probe_error", __instance.TicksGame, __instance.TicksAbs); }
                catch (Exception receiptException) { Log.Error("[RWSI Native Acceptance] Could not write failed probe receipt: " + receiptException); }
            }
        }

        [DebugAction("RWSI Native Acceptance", "Benchmark: cancel DoSingleTick interval", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void Cancel()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error)) { Log.Error("[RWSI Native Acceptance] Tick benchmark cancel rejected: " + error); return; }
            if (!_active) { Log.Error("[RWSI Native Acceptance] No active tick interval exists."); return; }
            _active = false;
            Unpatch();
            WriteResult("cancelled", Find.TickManager?.TicksGame ?? 0, Find.TickManager?.TicksAbs ?? 0);
        }

        private static void Unpatch()
        {
            try { _harmony?.UnpatchAll(HarmonyId); }
            catch (Exception exception) { Log.Error("[RWSI Native Acceptance] Probe unpatch failed: " + exception); }
            _harmony = null;
        }

        private static void WriteResult(string status, int endTick, int endAbsoluteTick)
        {
            var window = _window;
            if (!AcceptanceRuntime.TryAuthorize(out var error)) { Log.Error("[RWSI Native Acceptance] Tick benchmark receipt rejected: " + error); return; }
            if (window == null || string.IsNullOrWhiteSpace(_runId)) { Log.Error("[RWSI Native Acceptance] Tick benchmark state is incomplete; no receipt written."); return; }
            var samples = window.Samples;
            var payload = new BenchmarkReceipt
            {
                Schema = "rwsi-native-tick-benchmark/2", RunId = _runId, Status = status,
                CreatedUtc = DateTime.UtcNow.ToString("O"), SavedataRoot = GenFilePaths.SaveDataFolderPath,
                WorldSeed = Find.World?.info?.seedString, PersistentRandomValue = Find.World?.info?.persistentRandomValue,
                NativeBuild = VersionControl.CurrentVersionStringWithRev, ProbeMethod = "Verse.TickManager.DoSingleTick()",
                TimingMethod = "Stopwatch.GetTimestamp() before and after the native method; elapsed raw Stopwatch ticks",
                StopwatchFrequency = Stopwatch.Frequency, ProbeError = _probeError,
                WarmupTicks = WarmupTicks, MeasuredTicks = MeasuredTicks,
                StartGameTick = _startTick, FirstMeasuredGameTick = window.FirstMeasuredTick,
                LastMeasuredGameTick = window.LastMeasuredTick, EndGameTick = endTick,
                StartAbsoluteTick = _startAbsoluteTick, FirstMeasuredAbsoluteTick = _startMeasuredAbsoluteTick,
                EndAbsoluteTick = endAbsoluteTick, ActualWarmupTicks = window.FirstMeasuredTick - _startTick,
                ActualMeasuredTicks = samples.Length, SampleStopwatchTicks = samples
            };
            var payloadBytes = Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(payload, Formatting.Indented));
            string hash;
            using (var sha = SHA256.Create()) hash = BitConverter.ToString(sha.ComputeHash(payloadBytes)).Replace("-", "").ToLowerInvariant();
            var envelope = JsonConvert.SerializeObject(new { payload_sha256 = hash, payload }, Formatting.Indented);
            var bytes = Encoding.UTF8.GetBytes(envelope);
            var directory = Path.Combine(GenFilePaths.SaveDataFolderPath, "NativeAcceptance", "benchmarks");
            Directory.CreateDirectory(directory);
            var path = Path.Combine(directory, _runId + ".json");
            using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
            { stream.Write(bytes, 0, bytes.Length); stream.Flush(true); }
            Log.Message("[RWSI Native Acceptance] Tick benchmark " + status + " path=" + path + " payload_sha256=" + hash +
                " sampled=" + samples.Length + "/" + MeasuredTicks + " stopwatch_frequency=" + Stopwatch.Frequency +
                " measured_tick_range=" + window.FirstMeasuredTick + ".." + window.LastMeasuredTick);
            _runId = "";
            _window = null;
            _probeError = null;
        }

        private struct TickProbeState
        {
            internal readonly long Timestamp;
            internal readonly int GameTick;
            internal readonly int AbsoluteTick;
            internal readonly bool Measuring;
            internal TickProbeState(long timestamp, int gameTick, int absoluteTick, bool measuring)
            { Timestamp = timestamp; GameTick = gameTick; AbsoluteTick = absoluteTick; Measuring = measuring; }
        }

        private sealed class BenchmarkReceipt
        {
            [JsonProperty("schema")] public string Schema = "";
            [JsonProperty("run_id")] public string RunId = "";
            [JsonProperty("status")] public string Status = "";
            [JsonProperty("created_utc")] public string CreatedUtc = "";
            [JsonProperty("savedata_root")] public string SavedataRoot = "";
            [JsonProperty("world_seed")] public string? WorldSeed;
            [JsonProperty("persistent_random_value")] public int? PersistentRandomValue;
            [JsonProperty("native_build")] public string? NativeBuild;
            [JsonProperty("probe_method")] public string ProbeMethod = "";
            [JsonProperty("timing_method")] public string TimingMethod = "";
            [JsonProperty("stopwatch_frequency")] public long StopwatchFrequency;
            [JsonProperty("probe_error")] public string? ProbeError;
            [JsonProperty("warmup_target_ticks")] public int WarmupTicks;
            [JsonProperty("measured_target_ticks")] public int MeasuredTicks;
            [JsonProperty("start_game_tick")] public int StartGameTick;
            [JsonProperty("first_measured_game_tick")] public int FirstMeasuredGameTick;
            [JsonProperty("last_measured_game_tick")] public int LastMeasuredGameTick;
            [JsonProperty("end_game_tick")] public int EndGameTick;
            [JsonProperty("start_absolute_tick")] public int StartAbsoluteTick;
            [JsonProperty("first_measured_absolute_tick")] public int FirstMeasuredAbsoluteTick;
            [JsonProperty("end_absolute_tick")] public int EndAbsoluteTick;
            [JsonProperty("actual_warmup_ticks")] public int ActualWarmupTicks;
            [JsonProperty("actual_measured_ticks")] public int ActualMeasuredTicks;
            [JsonProperty("sample_stopwatch_ticks")] public long[] SampleStopwatchTicks = Array.Empty<long>();
        }
    }
}
