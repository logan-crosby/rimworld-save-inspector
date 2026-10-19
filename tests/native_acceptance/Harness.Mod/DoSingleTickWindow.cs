using System;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Stores actual DoSingleTick durations after a fixed game-tick warmup.</summary>
    internal sealed class DoSingleTickWindow
    {
        private readonly int _firstMeasuredTick;
        private readonly long[] _samples;
        private int _count;
        private int _lastTick;

        internal DoSingleTickWindow(int startGameTick, int warmupTicks, int measuredTicks)
        {
            if (warmupTicks < 0) throw new ArgumentOutOfRangeException(nameof(warmupTicks));
            if (measuredTicks < 1) throw new ArgumentOutOfRangeException(nameof(measuredTicks));
            _firstMeasuredTick = checked(startGameTick + warmupTicks);
            _lastTick = _firstMeasuredTick - 1;
            _samples = new long[measuredTicks];
        }

        internal bool IsComplete => _count == _samples.Length;
        internal int SampleCount => _count;
        internal int FirstMeasuredTick => _firstMeasuredTick;
        internal int LastMeasuredTick => _count == 0 ? 0 : _lastTick;
        internal long[] Samples
        {
            get
            {
                var copy = new long[_count];
                Array.Copy(_samples, copy, _count);
                return copy;
            }
        }

        /// <returns>true only when the final expected measured tick is recorded.</returns>
        internal bool TryRecord(int gameTick, long durationStopwatchTicks)
        {
            if (durationStopwatchTicks < 0) throw new ArgumentOutOfRangeException(nameof(durationStopwatchTicks));
            if (gameTick < _firstMeasuredTick || IsComplete) return false;
            if (gameTick != _lastTick + 1)
                throw new InvalidOperationException("DoSingleTick sample tick gap: expected " + (_lastTick + 1) + ", got " + gameTick + ".");
            _samples[_count++] = durationStopwatchTicks;
            _lastTick = gameTick;
            return IsComplete;
        }
    }
}
