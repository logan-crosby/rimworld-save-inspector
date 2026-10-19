using Verse;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Invalidates in-memory batch progress whenever a game is loaded or created.</summary>
    public sealed class AcceptanceBatchLifecycle : GameComponent
    {
        public AcceptanceBatchLifecycle(Game game) { }
        public override void LoadedGame() => AcceptanceBatchActions.ResetForLifecycle("loaded_game");
        public override void StartedNewGame() => AcceptanceBatchActions.ResetForLifecycle("started_new_game");
    }
}
