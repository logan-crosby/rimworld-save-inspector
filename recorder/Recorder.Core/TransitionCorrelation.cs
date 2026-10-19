using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Threading;

namespace RimWorld.LifeRecorder.Core
{
    /// <summary>Correlates synchronous nested collector callbacks without merging separate operations.</summary>
    public static class TransitionCorrelation
    {
        private sealed class Pending
        {
            internal readonly List<string> Sources = new List<string>();
            internal Action<IReadOnlyList<string>>? Publish;
        }

        private sealed class Root
        {
            internal readonly Dictionary<string, Pending> Pending = new Dictionary<string, Pending>(StringComparer.Ordinal);
            internal readonly Dictionary<object, Pending> PendingObjects = new Dictionary<object, Pending>(ObjectIdentityComparer.Instance);
            internal readonly List<Pending> Order = new List<Pending>();
        }

        private sealed class ObjectIdentityComparer : IEqualityComparer<object>
        {
            internal static readonly ObjectIdentityComparer Instance = new ObjectIdentityComparer();
            public new bool Equals(object? left, object? right) => ReferenceEquals(left, right);
            public int GetHashCode(object value) => RuntimeHelpers.GetHashCode(value);
        }

        private sealed class Frame
        {
            internal Root Root = null!;
            internal int Id;
            internal string Source = "unknown_hook";
        }

        [ThreadStatic] private static List<Frame>? _frames;
        [ThreadStatic] private static int _nextId;

        public static string? CurrentSource => _frames != null && _frames.Count > 0 ? _frames[_frames.Count - 1].Source : null;

        /// <summary>Whether the current synchronous call stack contains a named native scope.</summary>
        public static bool IsWithinSource(string source) => _frames != null && _frames.Any(frame => String.Equals(frame.Source, source, StringComparison.Ordinal));

        /// <summary>Begins a synchronous native callback scope. Dispose from postfix or finalizer.</summary>
        public static IDisposable Begin(string source = "unknown_hook")
        {
            var frames = _frames ?? (_frames = new List<Frame>());
            var frame = new Frame { Root = frames.Count == 0 ? new Root() : frames[frames.Count - 1].Root, Id = ++_nextId, Source = source };
            frames.Add(frame);
            return new Scope(frame);
        }

        /// <summary>Publishes immediately outside a scope, or once per canonical transition at outermost scope exit.</summary>
        public static void Submit(string canonicalTransitionKey, string source, Action<IReadOnlyList<string>> publish)
            => SubmitCore(canonicalTransitionKey, source, publish, preserveFirst: false);

        /// <summary>Publishes the first callback's snapshot after all sibling callbacks in the root scope complete.</summary>
        public static void SubmitFirst(string canonicalTransitionKey, string source, Action<IReadOnlyList<string>> publish)
            => SubmitCore(canonicalTransitionKey, source, publish, preserveFirst: true);

        /// <summary>Correlates one in-memory subject by reference, even when its saved ID cannot be read.</summary>
        public static void SubmitFirstForObject(object subject, string source, Action<IReadOnlyList<string>> publish)
        {
            if (subject == null) throw new ArgumentNullException(nameof(subject));
            if (String.IsNullOrEmpty(source)) throw new ArgumentException("Source is required.", nameof(source));
            if (publish == null) throw new ArgumentNullException(nameof(publish));
            var frames = _frames;
            if (frames == null || frames.Count == 0) { publish(new[] { source }); return; }
            var root = frames[frames.Count - 1].Root;
            if (!root.PendingObjects.TryGetValue(subject, out var pending))
            {
                pending = new Pending { Publish = publish };
                root.PendingObjects.Add(subject, pending);
                root.Order.Add(pending);
            }
            if (!pending.Sources.Contains(source, StringComparer.Ordinal)) pending.Sources.Add(source);
        }

        private static void SubmitCore(string canonicalTransitionKey, string source, Action<IReadOnlyList<string>> publish, bool preserveFirst)
        {
            if (String.IsNullOrEmpty(canonicalTransitionKey)) throw new ArgumentException("Transition key is required.", nameof(canonicalTransitionKey));
            if (String.IsNullOrEmpty(source)) throw new ArgumentException("Source is required.", nameof(source));
            if (publish == null) throw new ArgumentNullException(nameof(publish));
            var frames = _frames;
            if (frames == null || frames.Count == 0) { publish(new[] { source }); return; }
            var root = frames[frames.Count - 1].Root;
            if (!root.Pending.TryGetValue(canonicalTransitionKey, out var pending))
            {
                pending = new Pending { Publish = publish };
                root.Pending.Add(canonicalTransitionKey, pending);
                root.Order.Add(pending);
            }
            else if (!preserveFirst) pending.Publish = publish; // ordinary outer callbacks usually hold the complete before/after state
            if (!pending.Sources.Contains(source, StringComparer.Ordinal)) pending.Sources.Add(source);
        }

        private sealed class Scope : IDisposable
        {
            private Frame? _frame;
            internal Scope(Frame frame) { _frame = frame; }
            public void Dispose()
            {
                var frame = Interlocked.Exchange(ref _frame, null);
                if (frame == null) return;
                var frames = _frames;
                if (frames == null) return;
                var index = frames.LastIndexOf(frame);
                if (index < 0) return;
                frames.RemoveAt(index);
                if (frames.Any(existing => ReferenceEquals(existing.Root, frame.Root))) return;
                foreach (var pending in frame.Root.Order)
                {
                    try { pending.Publish?.Invoke(pending.Sources.ToArray()); }
                    catch { /* one instrumentation callback cannot prevent scope cleanup or later publications */ }
                }
                frame.Root.Pending.Clear();
                frame.Root.PendingObjects.Clear();
                frame.Root.Order.Clear();
                if (frames.Count == 0) _frames = null;
            }
        }
    }

    /// <summary>Compares saved pawn IDs without treating unavailable IDs as evidence of sameness or difference.</summary>
    public static class SavedIdentity
    {
        public static bool? IsPreserved(string? beforeId, string? afterId)
        {
            if (String.IsNullOrWhiteSpace(beforeId) || String.IsNullOrWhiteSpace(afterId)) return null;
            return String.Equals(beforeId, afterId, StringComparison.Ordinal);
        }
    }
}
