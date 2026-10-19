using Rwsi.NativeAcceptance;
using Xunit;

public class DoSingleTickWindowTests
{
    [Fact]
    public void IgnoresWarmupAndRetainsExactlyTheBoundedMeasuredWindow()
    {
        var window = new DoSingleTickWindow(startGameTick: 100, warmupTicks: 2, measuredTicks: 3);

        Assert.False(window.TryRecord(gameTick: 100, durationStopwatchTicks: 11));
        Assert.False(window.TryRecord(gameTick: 101, durationStopwatchTicks: 12));
        Assert.False(window.TryRecord(gameTick: 102, durationStopwatchTicks: 13));
        Assert.False(window.TryRecord(gameTick: 103, durationStopwatchTicks: 14));
        Assert.True(window.TryRecord(gameTick: 104, durationStopwatchTicks: 15));

        Assert.True(window.IsComplete);
        Assert.Equal(3, window.SampleCount);
        Assert.Equal(new long[] { 13, 14, 15 }, window.Samples);
        Assert.False(window.TryRecord(gameTick: 105, durationStopwatchTicks: 16));
        Assert.Equal(3, window.SampleCount);
    }

    [Fact]
    public void RejectsUnexpectedTickGapsInsteadOfInventingMissingSamples()
    {
        var window = new DoSingleTickWindow(startGameTick: 100, warmupTicks: 0, measuredTicks: 3);
        Assert.False(window.TryRecord(100, 10));

        Assert.Throws<System.InvalidOperationException>(() => window.TryRecord(102, 12));
        Assert.Equal(1, window.SampleCount);
        Assert.False(window.IsComplete);
    }
}
