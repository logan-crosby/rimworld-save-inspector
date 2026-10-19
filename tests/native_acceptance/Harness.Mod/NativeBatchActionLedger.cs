using System.Collections.Generic;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Counts unique completed native action receipts, including multi-subject transitions.</summary>
    internal sealed class NativeBatchActionLedger
    {
        private readonly HashSet<string> _seenActionIds = new HashSet<string>(System.StringComparer.Ordinal);
        private readonly List<string> _actionIds = new List<string>();
        internal int CompletedSubjectTransitions { get; private set; }
        internal string[] ActionIds => _actionIds.ToArray();

        internal bool TryRecord(string actionId, bool completed, int expectedEventCount)
        {
            if (!completed || expectedEventCount < 1 || string.IsNullOrWhiteSpace(actionId)) return false;
            if (!_seenActionIds.Add(actionId)) return false;
            _actionIds.Add(actionId);
            CompletedSubjectTransitions += expectedEventCount;
            return true;
        }

        internal void Clear()
        {
            _seenActionIds.Clear();
            _actionIds.Clear();
            CompletedSubjectTransitions = 0;
        }
    }
}
