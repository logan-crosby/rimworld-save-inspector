using Rwsi.NativeAcceptance;
using Xunit;

namespace Harness.Tests;

public sealed class TransitionOracleTests
{
    [Fact]
    public void SingletonReplacementRequiresExactlyTheOldAndNewHolder()
    {
        Assert.True(TransitionOracle.ExactMembershipTransition(new[] { "old" }, new[] { "new" }, new[] { "old" }, new[] { "new" }));
        Assert.False(TransitionOracle.ExactMembershipTransition(new[] { "wrong" }, new[] { "new" }, new[] { "old" }, new[] { "new" }));
        Assert.False(TransitionOracle.ExactMembershipTransition(new[] { "old" }, new[] { "wrong" }, new[] { "old" }, new[] { "new" }));
        Assert.False(TransitionOracle.ExactMembershipTransition(new[] { "old" }, new[] { "old", "new" }, new[] { "old" }, new[] { "new" }));
        Assert.False(TransitionOracle.ExactMembershipTransition(new[] { "old" }, Array.Empty<string>(), new[] { "old" }, new[] { "new" }));
        Assert.False(TransitionOracle.ExactMembershipTransition(new[] { "old" }, new[] { "new", "new" }, new[] { "old" }, new[] { "new" }));
    }

    [Fact]
    public void SingletonClearRequiresPreviousHolderAndEmptyResult()
    {
        Assert.True(TransitionOracle.ExactMembershipTransition(new[] { "new" }, Array.Empty<string>(), new[] { "new" }, Array.Empty<string>()));
        Assert.False(TransitionOracle.ExactMembershipTransition(Array.Empty<string>(), Array.Empty<string>(), new[] { "new" }, Array.Empty<string>()));
        Assert.False(TransitionOracle.ExactMembershipTransition(new[] { "new" }, new[] { "other" }, new[] { "new" }, Array.Empty<string>()));
    }

    [Fact]
    public void SingletonPreparationStillRequiresExactTargetAndAnObservedChange()
    {
        Assert.True(TransitionOracle.ExactMembershipTransition(Array.Empty<string>(), new[] { "first" }, null, new[] { "first" }));
        Assert.True(TransitionOracle.ExactMembershipTransition(new[] { "previous" }, new[] { "first" }, null, new[] { "first" }));
        Assert.False(TransitionOracle.ExactMembershipTransition(new[] { "first" }, new[] { "first" }, null, new[] { "first" }));
        Assert.False(TransitionOracle.ExactMembershipTransition(Array.Empty<string>(), new[] { "wrong" }, null, new[] { "first" }));
    }

    [Theory]
    [InlineData(false, true, true, true)]
    [InlineData(false, false, true, false)]
    [InlineData(true, true, true, false)]
    [InlineData(true, false, false, true)]
    public void BooleanEdgeRequiresObservedChange(bool before, bool after, bool target, bool expected) =>
        Assert.Equal(expected, TransitionOracle.BooleanEdge(before, after, target));

    [Fact]
    public void ResurrectionRequiresBothNativeReturnAndDeadStateEdge()
    {
        Assert.True(TransitionOracle.BooleanEdgeAndReturn(true, false, false, true, true));
        Assert.False(TransitionOracle.BooleanEdgeAndReturn(true, false, false, false, true));
        Assert.False(TransitionOracle.BooleanEdgeAndReturn(true, true, false, true, true));
    }

    [Fact]
    public void CasketEntryRequiresSpawnedPawnToBecomeUnspawnedInsideExactHolder()
    {
        Assert.True(TransitionOracle.HolderEntry(true, false, "casket:42", "casket:42"));
        Assert.False(TransitionOracle.HolderEntry(false, false, "map:1", "casket:42"));
        Assert.False(TransitionOracle.HolderEntry(true, true, "map:1", "casket:42"));
        Assert.False(TransitionOracle.HolderEntry(true, false, "casket:41", "casket:42"));
    }

    [Fact]
    public void RepeatedTransitionsAtOneTickRemainDistinctByActionId()
    {
        Assert.True(TransitionOracle.DistinctActionIds("first", "second"));
        Assert.False(TransitionOracle.DistinctActionIds("same", "same"));
        Assert.False(TransitionOracle.DistinctActionIds("", "second"));
    }

    [Fact]
    public void ExpectedFaultControlAcceptsOnlyTheExactInjectedExceptionType()
    {
        Assert.True(TransitionOracle.ExpectedExceptionMatches("Rwsi.NativeAcceptance.InjectedFailure", "Rwsi.NativeAcceptance.InjectedFailure"));
        Assert.False(TransitionOracle.ExpectedExceptionMatches("Rwsi.NativeAcceptance.InjectedFailure", "System.InvalidOperationException"));
        Assert.False(TransitionOracle.ExpectedExceptionMatches("Rwsi.NativeAcceptance.InjectedFailure", null));
        Assert.False(TransitionOracle.ExpectedExceptionMatches(null, "Rwsi.NativeAcceptance.InjectedFailure"));
    }

    [Theory]
    [InlineData(true, false, 1)]
    [InlineData(false, false, 0)]
    [InlineData(true, true, 0)]
    [InlineData(false, true, 0)]
    public void ControlsNeverExpectTransitionEvents(bool observedTransition, bool controlOnly, int expectedEvents) =>
        Assert.Equal(expectedEvents, TransitionOracle.ExpectedEventCount(observedTransition, controlOnly));

    [Fact]
    public void OneCompletedBirthTransitionCountsEveryDistinctMaterializedSubject()
    {
        Assert.Equal(3, TransitionOracle.ExpectedEventCount(true, false, 3));
        Assert.Equal(0, TransitionOracle.ExpectedEventCount(false, false, 3));
        Assert.Equal(0, TransitionOracle.ExpectedEventCount(true, true, 3));
    }

    [Fact]
    public void StableHolderIdentityUsesNativeIdsAndRejectsUnknownReferenceHashes()
    {
        Assert.Equal("Map:123", HolderIdentityOracle.StableIdentity("Map", null, null, 123));
        Assert.Equal("Building_Casket:Thing_42", HolderIdentityOracle.StableIdentity("Building_Casket", "Thing_42", null, null));
        Assert.Equal("Caravan:7", HolderIdentityOracle.StableIdentity("Caravan", null, "7", null));
        Assert.Null(HolderIdentityOracle.StableIdentity("UnknownHolder", null, null, null));
    }

    [Fact]
    public void OwnerBackedTrackersUseStableParentIdentityWithoutObjectHashes()
    {
        Assert.Equal("Verse.Pawn_InventoryTracker@Pawn:Human42",
            HolderIdentityOracle.StableIdentity("Verse.Pawn_InventoryTracker", null, null, null, "Pawn:Human42"));
        Assert.Equal("RimWorld.Planet.Settlement_TraderTracker@Settlement:7",
            HolderIdentityOracle.StableIdentity("RimWorld.Planet.Settlement_TraderTracker", null, null, null, "Settlement:7"));
        Assert.Null(HolderIdentityOracle.StableIdentity("UnknownHolder", null, null, null, ""));
        Assert.Null(HolderIdentityOracle.StableIdentity("UnknownHolder", null, null, null, "Pawn:Human42"));
        Assert.Equal("Map:123", HolderIdentityOracle.StableIdentity("Map", null, null, 123, "ignored"));
    }

    [Fact]
    public void BatchCountsOnlyUniqueCompletedActionReceiptsAndEveryAffectedSubject()
    {
        var ledger = new NativeBatchActionLedger();
        Assert.False(ledger.TryRecord("failed", false, 0));
        Assert.False(ledger.TryRecord("noop", false, 0));
        Assert.True(ledger.TryRecord("hatch-action", true, 2));
        Assert.False(ledger.TryRecord("hatch-action", true, 2));
        Assert.True(ledger.TryRecord("health-action", true, 1));
        Assert.Equal(3, ledger.CompletedSubjectTransitions);
        Assert.Equal(new[] { "hatch-action", "health-action" }, ledger.ActionIds);
    }
}
