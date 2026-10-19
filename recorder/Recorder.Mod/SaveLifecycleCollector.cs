using System;
using HarmonyLib;
using Verse;

namespace RimWorld.LifeRecorder
{
    internal static class SaveLifecycleCollector
    {
        public static void Prefix(string __0, string __1, out RecorderSaveAttempt? __state)
        {
            __state = null;
            if (!String.Equals(__1, "savegame", StringComparison.Ordinal)) return;
            try { __state = RecorderSessionComponent.Current?.BeginSave(__0); }
            catch (Exception ex) { Log.Warning("[Life Recorder] Save hook prefix was isolated after " + ex.GetType().Name + "."); }
        }

        public static void Postfix(RecorderSaveAttempt? __state)
        {
            if (__state == null) return;
            try { RecorderSessionComponent.Current?.SaveCompleted(__state); }
            catch (Exception ex) { Log.Warning("[Life Recorder] Save completion hook was isolated after " + ex.GetType().Name + "."); }
        }

        public static Exception? Finalizer(Exception? __exception, RecorderSaveAttempt? __state)
        {
            if (__exception == null || __state == null) return __exception;
            try { RecorderSessionComponent.Current?.SaveFailed(__state, __exception); }
            catch (Exception ex) { Log.Warning("[Life Recorder] Save failure hook was isolated after " + ex.GetType().Name + "."); }
            return __exception;
        }
    }
}
