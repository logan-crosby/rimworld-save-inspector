using System;
using System.Linq;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Game-free predicates shared by native actions and acceptance tests.</summary>
    internal static class TransitionOracle
    {
        internal static bool ExactMembershipTransition(string[] before, string[] after, string[]? expectedBefore, string[] expectedAfter)
        {
            bool Exact(string[] actual, string[] expected) => actual.Length == actual.Distinct(StringComparer.Ordinal).Count() &&
                actual.OrderBy(x => x, StringComparer.Ordinal).SequenceEqual(expected.OrderBy(x => x, StringComparer.Ordinal));
            return before.All(x => !string.IsNullOrWhiteSpace(x)) && after.All(x => !string.IsNullOrWhiteSpace(x)) &&
                before.Length == before.Distinct(StringComparer.Ordinal).Count() &&
                (expectedBefore == null || Exact(before, expectedBefore)) && Exact(after, expectedAfter) &&
                !before.OrderBy(x => x, StringComparer.Ordinal).SequenceEqual(after.OrderBy(x => x, StringComparer.Ordinal));
        }

        internal static bool BooleanEdge(bool before, bool after, bool expectedAfter) =>
            before != expectedAfter && after == expectedAfter;

        internal static bool BooleanEdgeAndReturn(bool before, bool after, bool expectedAfter, object? nativeReturn, bool expectedReturn) =>
            Equals(nativeReturn, expectedReturn) && BooleanEdge(before, after, expectedAfter);

        internal static bool HolderEntry(bool beforeSpawned, bool afterSpawned, string? actualHolder, string expectedHolder) =>
            beforeSpawned && !afterSpawned && string.Equals(actualHolder, expectedHolder, StringComparison.Ordinal);

        internal static bool MembershipEdge(bool before, bool after, bool expectedAfter) =>
            before != expectedAfter && after == expectedAfter;

        internal static bool DistinctActionIds(string first, string second) =>
            !string.IsNullOrWhiteSpace(first) && !string.IsNullOrWhiteSpace(second) &&
            !string.Equals(first, second, StringComparison.Ordinal);

        internal static bool ExpectedExceptionMatches(string? expectedType, string? actualType) =>
            !string.IsNullOrWhiteSpace(expectedType) && string.Equals(expectedType, actualType, StringComparison.Ordinal);

        internal static int ExpectedEventCount(bool observedTransition, bool controlOnly) =>
            observedTransition && !controlOnly ? 1 : 0;

        internal static int ExpectedEventCount(bool completed, bool controlOnly, int affectedSubjectCount) =>
            completed && !controlOnly ? Math.Max(0, affectedSubjectCount) : 0;
    }

    internal static class HolderIdentityOracle
    {
        internal static string? StableIdentity(string typeName, string? thingId, string? worldObjectId, int? mapUniqueId, string? parentIdentity = null)
        {
            if (mapUniqueId.HasValue) return "Map:" + mapUniqueId.Value.ToString(System.Globalization.CultureInfo.InvariantCulture);
            if (!string.IsNullOrWhiteSpace(thingId)) return typeName + ":" + thingId;
            if (!string.IsNullOrWhiteSpace(worldObjectId)) return typeName + ":" + worldObjectId;
            // These native trackers are unique by type within their owning pawn or settlement.
            // Unknown holders must still fail closed rather than inventing a stable identity.
            if (!string.IsNullOrWhiteSpace(parentIdentity) &&
                (typeName == "Verse.Pawn_InventoryTracker" || typeName == "Verse.Pawn_CarryTracker" ||
                 typeName == "Verse.Pawn_EquipmentTracker" || typeName == "RimWorld.Pawn_ApparelTracker" ||
                 typeName == "RimWorld.Planet.Settlement_TraderTracker"))
                return typeName + "@" + parentIdentity;
            return null;
        }
    }
}
