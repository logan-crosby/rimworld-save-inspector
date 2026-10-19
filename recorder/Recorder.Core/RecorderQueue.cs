using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;

namespace RimWorld.LifeRecorder.Core
{
    public sealed class GapRange
    {
        public long FirstSequence { get; }
        public long LastSequence { get; }
        public string Cause { get; }
        internal GapRange(long first, long last, string cause) { FirstSequence = first; LastSequence = last; Cause = cause; }
    }

    /// <summary>Allocates event IDs before enqueue and owns the immutable serialized snapshots.</summary>
    public sealed class RecorderQueue
    {
        private readonly object _sync = new object();
        private readonly Queue<JournalEvent> _items = new Queue<JournalEvent>();
        private readonly List<GapRange> _lost = new List<GapRange>();
        private readonly string _recorderVersion, _gameVersion, _campaignId, _sessionId;
        private readonly int _maxRecords;
        private readonly long _maxBytes;
        private long _nextSequence;
        private long _queuedBytes;
        private long _reservedBytes;
        private long _droppedCount;
        private bool _rangesExact = true;

        public RecorderQueue(string recorderVersion, string gameVersion, string campaignId, string sessionId,
            long firstSequence = 1, int maxRecords = RecorderLimits.DefaultQueueRecords, long maxBytes = RecorderLimits.DefaultQueueBytes)
        {
            if (firstSequence <= 0 || maxRecords < 1 || maxBytes < 1) throw new ArgumentOutOfRangeException("Queue bounds and starting sequence must be positive.");
            _recorderVersion = recorderVersion; _gameVersion = gameVersion; _campaignId = campaignId; _sessionId = sessionId;
            _nextSequence = firstSequence; _maxRecords = maxRecords; _maxBytes = maxBytes;
        }

        public long HighestAllocatedSequence { get { lock (_sync) return _nextSequence - 1; } }
        public string RecorderVersion => _recorderVersion;
        public string GameVersion => _gameVersion;
        public string CampaignId => _campaignId;
        public string SessionId => _sessionId;
        public int Count { get { lock (_sync) return _items.Count; } }
        public long QueuedBytes { get { lock (_sync) return _queuedBytes; } }
        /// <summary>Serialized queue entries plus serialized snapshots staged by bounded collectors.</summary>
        public long RetainedBytes { get { lock (_sync) return _queuedBytes + _reservedBytes; } }
        public long DroppedCount { get { lock (_sync) return _droppedCount; } }
        public long HighestWrittenSequence { get; private set; }
        public long HighestDurableSequence { get; private set; }

        public bool TryEnqueue(Func<long, JournalEvent> create, out long sequence)
        {
            if (create == null) throw new ArgumentNullException(nameof(create));
            lock (_sync)
            {
                sequence = _nextSequence++;
                if (_items.Count >= _maxRecords)
                {
                    RecordDropLocked(sequence, "queue_full");
                    return false;
                }
                JournalEvent item;
                try { item = create(sequence); }
                catch (Exception) { RecordDropLocked(sequence, "serialization_error"); return false; }
                if (item.Sequence != sequence || item.SessionId != _sessionId)
                { RecordDropLocked(sequence, "serialization_error"); return false; }
                if (item.SerializedBytes > _maxBytes || _queuedBytes + _reservedBytes + item.SerializedBytes > _maxBytes)
                { RecordDropLocked(sequence, "queue_full"); return false; }
                _items.Enqueue(item);
                _queuedBytes += item.SerializedBytes;
                return true;
            }
        }

        /// <summary>Reserves or releases serialized bytes for a bounded staged collector snapshot.</summary>
        public bool TryChangeReservedBytes(long delta)
        {
            lock (_sync)
            {
                if (delta < 0)
                {
                    if (-delta > _reservedBytes) return false;
                    _reservedBytes += delta;
                    return true;
                }
                if (_queuedBytes + _reservedBytes + delta > _maxBytes) return false;
                _reservedBytes += delta;
                return true;
            }
        }

        /// <summary>Atomically converts one retained staged snapshot into its queued immutable event.</summary>
        public bool TryEnqueueReserved(long reservedBytes, Func<long, JournalEvent> create, out long sequence)
        {
            if (reservedBytes < 0) throw new ArgumentOutOfRangeException(nameof(reservedBytes));
            if (create == null) throw new ArgumentNullException(nameof(create));
            lock (_sync)
            {
                sequence = _nextSequence++;
                if (reservedBytes > _reservedBytes || _items.Count >= _maxRecords)
                { RecordDropLocked(sequence, "queue_full"); return false; }
                JournalEvent item;
                try { item = create(sequence); }
                catch (Exception) { RecordDropLocked(sequence, "serialization_error"); return false; }
                if (item.Sequence != sequence || item.SessionId != _sessionId)
                { RecordDropLocked(sequence, "serialization_error"); return false; }
                var retainedAfterTransfer = _queuedBytes + _reservedBytes - reservedBytes + item.SerializedBytes;
                if (item.SerializedBytes > _maxBytes || retainedAfterTransfer > _maxBytes)
                { RecordDropLocked(sequence, "queue_full"); return false; }
                _reservedBytes -= reservedBytes;
                _items.Enqueue(item);
                _queuedBytes += item.SerializedBytes;
                return true;
            }
        }

        /// <summary>Dequeues one snapshot and appends a gap after older queued snapshots when capacity recovers.</summary>
        public bool TryDequeue(out JournalEvent? item)
        {
            lock (_sync)
            {
                if (_items.Count == 0) { item = null; return false; }
                item = _items.Dequeue();
                _queuedBytes -= item.SerializedBytes;
                if (_droppedCount > 0 && _items.Count < _maxRecords)
                    TryMaterializeGapLocked();
                return true;
            }
        }

        public void MarkWritten(long sequence)
        {
            lock (_sync) if (sequence > HighestWrittenSequence) HighestWrittenSequence = sequence;
        }

        public void MarkDurable(long sequence)
        {
            lock (_sync)
            {
                if (sequence > HighestDurableSequence) HighestDurableSequence = sequence;
                if (HighestDurableSequence > HighestWrittenSequence) HighestWrittenSequence = HighestDurableSequence;
            }
        }

        public void RecordWriteFailure(JournalEvent item)
        {
            lock (_sync) RecordDropLocked(item.Sequence, "io_error");
        }

        private void RecordDropLocked(long sequence, string cause)
        {
            _droppedCount++;
            if (_lost.Count > 0)
            {
                var last = _lost[_lost.Count - 1];
                if (last.Cause == cause && last.LastSequence + 1 == sequence)
                { _lost[_lost.Count - 1] = new GapRange(last.FirstSequence, sequence, cause); return; }
            }
            if (_lost.Count < RecorderLimits.MaxGapRanges) _lost.Add(new GapRange(sequence, sequence, cause));
            else
            {
                _rangesExact = false;
                // Keep a bounded conservative envelope. Its positions are explicitly approximate.
                var first = _lost[0].FirstSequence;
                _lost.Clear();
                _lost.Add(new GapRange(first, sequence, "queue_full"));
            }
        }

        private void TryMaterializeGapLocked()
        {
            var gapSequence = _nextSequence++;
            var ranges = new JArray();
            foreach (var range in _lost)
                ranges.Add(new JObject { ["first_sequence"] = range.FirstSequence, ["last_sequence"] = range.LastSequence, ["cause"] = range.Cause });
            var payload = new JObject { ["missing_ranges"] = ranges, ["dropped_count"] = _droppedCount, ["ranges_exact"] = _rangesExact };
            try
            {
                var gap = new JournalEvent(_recorderVersion, _gameVersion, _campaignId, _sessionId, gapSequence, "gap", null, null, null,
                    null, Array.Empty<string>(), null, null, "queue", "coverage", payload);
                if (_items.Count < _maxRecords && _queuedBytes + _reservedBytes + gap.SerializedBytes <= _maxBytes)
                {
                    _items.Enqueue(gap);
                    _queuedBytes += gap.SerializedBytes;
                    _lost.Clear(); _droppedCount = 0; _rangesExact = true;
                }
                else _nextSequence--;
            }
            catch (Exception) { _nextSequence--; }
        }
    }
}
