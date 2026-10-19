using Rwsi.NativeAcceptance;
using Xunit;

public class AcceptanceBoundaryTests
{
    private const string Root = "/tmp/rwsi-owned-native";

    private static AcceptanceMarker Marker() => new AcceptanceMarker
    {
        ExpectedRoot = Root,
        AuthorThread = "01a14622-b914-7c81-b9ff-0e08d6436c93",
        SyntheticOnly = true,
        ExpectedSeed = "synthetic-boundary-seed",
        ExpectedWorldId = 12345678,
        ExpectedBuild = "1.7.4980 rev60"
    };

    [Fact]
    public void AllowsOnlyThePinnedDisposableProfileAndWorld()
    {
        Assert.True(AcceptanceBoundary.Allows(Root, "synthetic-boundary-seed", 12345678,
            "1.7.4980 rev60", Marker()));
    }

    [Theory]
    [InlineData("/tmp/rwsi-owned-native-neighbor", "synthetic-boundary-seed", 12345678, "1.7.4980 rev60")]
    [InlineData("/tmp/rwsi-owned-native/child", "synthetic-boundary-seed", 12345678, "1.7.4980 rev60")]
    [InlineData(Root, "another-world", 12345678, "1.7.4980 rev60")]
    [InlineData(Root, "synthetic-boundary-seed", 12345679, "1.7.4980 rev60")]
    [InlineData(Root, "synthetic-boundary-seed", 12345678, "1.6.4535 rev1")]
    public void RejectsAProfileWorldOrBuildMismatch(string root, string seed, long world, string build)
    {
        Assert.False(AcceptanceBoundary.Allows(root, seed, world, build, Marker()));
    }

    [Theory]
    [InlineData("missing-marker")]
    [InlineData("wrong-author")]
    [InlineData("not-synthetic")]
    [InlineData("relative-root")]
    [InlineData("blank-seed")]
    public void RejectsAnUntrustedOrIncompleteMarker(string problem)
    {
        AcceptanceMarker? marker = Marker();
        if (problem == "missing-marker") marker = null;
        if (problem == "wrong-author") marker!.AuthorThread = "unrelated-thread";
        if (problem == "not-synthetic") marker!.SyntheticOnly = false;
        if (problem == "relative-root") marker!.ExpectedRoot = "relative-profile";
        if (problem == "blank-seed") marker!.ExpectedSeed = "";
        Assert.False(AcceptanceBoundary.Allows(Root, "synthetic-boundary-seed", 12345678,
            "1.7.4980 rev60", marker));
    }
}
