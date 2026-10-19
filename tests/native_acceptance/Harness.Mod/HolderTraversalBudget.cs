using System;
using System.Collections.Generic;
using System.Runtime.CompilerServices;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Bounds holder graph traversal and reports cycles using reference identity.</summary>
    internal sealed class HolderTraversalBudget<T> where T : class
    {
        private readonly HashSet<T> _ancestors = new HashSet<T>(ReferenceComparer<T>.Instance);
        private readonly int _maxDepth;
        private readonly int _maxNodes;

        internal HolderTraversalBudget(int maxDepth, int maxNodes)
        {
            if (maxDepth < 0) throw new ArgumentOutOfRangeException(nameof(maxDepth));
            if (maxNodes < 1) throw new ArgumentOutOfRangeException(nameof(maxNodes));
            _maxDepth = maxDepth;
            _maxNodes = maxNodes;
        }

        internal int Visited { get; private set; }

        /// <returns>null after entering a node, or a stable failure code when traversal must stop.</returns>
        internal string? TryEnter(T node, int depth)
        {
            if (node == null) return "null_holder";
            if (depth > _maxDepth) return "depth_limit";
            if (_ancestors.Contains(node)) return "cycle";
            if (Visited >= _maxNodes) return "node_limit";
            _ancestors.Add(node);
            Visited++;
            return null;
        }

        internal void Exit(T node)
        {
            if (node != null) _ancestors.Remove(node);
        }

        private sealed class ReferenceComparer<TReference> : IEqualityComparer<TReference> where TReference : class
        {
            internal static readonly ReferenceComparer<TReference> Instance = new ReferenceComparer<TReference>();
            public bool Equals(TReference? left, TReference? right) => ReferenceEquals(left, right);
            public int GetHashCode(TReference value) => RuntimeHelpers.GetHashCode(value);
        }
    }
}
