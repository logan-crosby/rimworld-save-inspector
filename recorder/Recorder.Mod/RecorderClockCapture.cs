using RimWorld.LifeRecorder.Core;
using Verse;

namespace RimWorld.LifeRecorder
{
    internal static class RecorderClockCapture
    {
        internal static RecorderTimeBasis Capture()
        {
            try
            {
                var manager = Find.TickManager;
                return manager == null
                    ? RecorderTimeBasis.Create(null, null, null, null)
                    : RecorderTimeBasis.Create(manager.TicksGame, manager.TicksAbs, manager.gameStartAbsTick, manager.StartingYear);
            }
            catch
            {
                return RecorderTimeBasis.Create(null, null, null, null);
            }
        }
    }
}
