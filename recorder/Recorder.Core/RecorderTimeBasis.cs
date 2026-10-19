using System;

namespace RimWorld.LifeRecorder.Core
{
    /// <summary>Immutable game-clock values captured together on the game thread.</summary>
    public sealed class RecorderTimeBasis
    {
        public long? TicksGame { get; }
        public long? TicksAbs { get; }
        public long? GameStartAbsTick { get; }
        public int? StartingYear { get; }

        private RecorderTimeBasis(long? ticksGame, long? ticksAbs, long? gameStartAbsTick, int? startingYear)
        {
            TicksGame = ticksGame;
            TicksAbs = ticksAbs;
            GameStartAbsTick = gameStartAbsTick;
            StartingYear = startingYear;
        }

        public static RecorderTimeBasis Create(long? ticksGame, long? ticksAbs, long? gameStartAbsTick, int? startingYear)
        {
            if (ticksGame.HasValue && ticksAbs.HasValue && gameStartAbsTick.HasValue &&
                ticksAbs.Value != checked(ticksGame.Value + gameStartAbsTick.Value))
                throw new ArgumentException("ticks_abs must equal ticks_game plus game_start_abs_tick.");
            return new RecorderTimeBasis(ticksGame, ticksAbs, gameStartAbsTick, startingYear);
        }
    }
}
