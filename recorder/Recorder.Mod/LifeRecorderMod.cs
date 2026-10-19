using Verse;

namespace RimWorld.LifeRecorder
{
    public sealed class LifeRecorderMod : Mod
    {
        public LifeRecorderMod(ModContentPack content) : base(content)
        {
            RecorderBootstrap.Initialize();
            Log.Warning("[Life Recorder] Development candidate loaded. Native hook and lifecycle compatibility is unverified; see the session coverage manifest.");
        }
    }
}
