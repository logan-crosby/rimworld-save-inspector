using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;
using RimWorld;
using Verse;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Opt-in development metrics controls; their output is never an event oracle.</summary>
    public static class AcceptanceMetricsActions
    {
        [LudeonTK.DebugAction("RWSI Native Acceptance", "Metrics: enable", allowedGameStates = LudeonTK.AllowedGameStates.PlayingOnMap)]
        public static void Enable() => SetEnabled(true);

        [LudeonTK.DebugAction("RWSI Native Acceptance", "Metrics: disable", allowedGameStates = LudeonTK.AllowedGameStates.PlayingOnMap)]
        public static void Disable() => SetEnabled(false);

        [LudeonTK.DebugAction("RWSI Native Acceptance", "Metrics: reset", allowedGameStates = LudeonTK.AllowedGameStates.PlayingOnMap)]
        public static void Reset()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error)) { Log.Error("[RWSI Native Acceptance] Metrics rejected: " + error); return; }
            Invoke("Reset", Type.EmptyTypes, Array.Empty<object>());
        }

        [LudeonTK.DebugAction("RWSI Native Acceptance", "Metrics: write snapshot receipt", allowedGameStates = LudeonTK.AllowedGameStates.PlayingOnMap)]
        public static void Snapshot()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error)) { Log.Error("[RWSI Native Acceptance] Metrics rejected: " + error); return; }
            var raw = Invoke("SnapshotJson", Type.EmptyTypes, Array.Empty<object>());
            if (!(raw is string json)) { Log.Error("[RWSI Native Acceptance] RecorderDeveloperMetrics.SnapshotJson unavailable; no snapshot was written."); return; }
            var actionId = Guid.NewGuid().ToString("D");
            var payload = new { schema = "rwsi-native-metrics-snapshot/1", action_id = actionId,
                created_utc = DateTime.UtcNow.ToString("O"), game_ticks = Find.TickManager?.TicksGame,
                absolute_ticks = Find.TickManager?.TicksAbs, savedata_root = GenFilePaths.SaveDataFolderPath,
                recorder_metrics_json = json };
            var payloadJson = JsonConvert.SerializeObject(payload, Formatting.Indented);
            var payloadBytes = Encoding.UTF8.GetBytes(payloadJson);
            string hash;
            using (var sha = SHA256.Create()) hash = BitConverter.ToString(sha.ComputeHash(payloadBytes)).Replace("-", "").ToLowerInvariant();
            var bytes = Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(new { payload_sha256 = hash, payload }, Formatting.Indented));
            var directory = Path.Combine(GenFilePaths.SaveDataFolderPath, "NativeAcceptance", "metrics");
            Directory.CreateDirectory(directory);
            var path = Path.Combine(directory, actionId + ".json");
            using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
            { stream.Write(bytes, 0, bytes.Length); stream.Flush(true); }
            Log.Message("[RWSI Native Acceptance] Metrics snapshot path=" + path + " payload_sha256=" + hash +
                " (diagnostic only; independent transition receipts remain the event oracle)");
        }

        private static void SetEnabled(bool enabled)
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error)) { Log.Error("[RWSI Native Acceptance] Metrics rejected: " + error); return; }
            Invoke("Enable", new[] { typeof(bool) }, new object[] { enabled });
        }

        private static object? Invoke(string methodName, Type[] parameterTypes, object[] arguments)
        {
            var type = AppDomain.CurrentDomain.GetAssemblies().Select(assembly => assembly.GetType(
                "RimWorld.LifeRecorder.RecorderDeveloperMetrics", false)).FirstOrDefault(candidate => candidate != null);
            var method = type?.GetMethod(methodName, BindingFlags.Public | BindingFlags.Static, null, parameterTypes, null);
            if (method == null) { Log.Error("[RWSI Native Acceptance] RecorderDeveloperMetrics." + methodName + " unavailable; recorder assembly/API was not found."); return null; }
            try { var result = method.Invoke(null, arguments); Log.Message("[RWSI Native Acceptance] RecorderDeveloperMetrics." + methodName + " invoked."); return result; }
            catch (TargetInvocationException exception)
            { Log.Error("[RWSI Native Acceptance] RecorderDeveloperMetrics." + methodName + " failed: " + (exception.InnerException ?? exception)); return null; }
        }
    }
}
