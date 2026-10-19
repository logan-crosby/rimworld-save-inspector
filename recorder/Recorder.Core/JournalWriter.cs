using System;
using System.IO;
using System.Linq;
using System.Diagnostics;
using System.Threading;

namespace RimWorld.LifeRecorder.Core
{
    public enum RecorderStatus { Starting, Recording, StorageLimit, IoFailure, Stopped, UnflushedTail }

    /// <summary>Small file boundary so storage faults can be exercised without relying on host disk behavior.</summary>
    public interface IJournalSegmentStream : IDisposable
    {
        void Write(byte[] buffer, int offset, int count);
        void WriteByte(byte value);
        void FlushDurable();
    }

    public interface IJournalSegmentStreamFactory
    {
        IJournalSegmentStream Create(string path);
    }

    internal sealed class FileJournalSegmentStreamFactory : IJournalSegmentStreamFactory
    {
        public IJournalSegmentStream Create(string path) => new FileJournalSegmentStream(path);
    }

    internal sealed class FileJournalSegmentStream : IJournalSegmentStream
    {
        private readonly FileStream _stream;
        public FileJournalSegmentStream(string path) => _stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read, 64 * 1024, FileOptions.SequentialScan);
        public void Write(byte[] buffer, int offset, int count) => _stream.Write(buffer, offset, count);
        public void WriteByte(byte value) => _stream.WriteByte(value);
        public void FlushDurable() => _stream.Flush(true);
        public void Dispose() => _stream.Dispose();
    }

    /// <summary>Append-only segmented writer. It receives serialized event values only and never sees game objects.</summary>
    public sealed class JournalWriter : IDisposable
    {
        private readonly RecorderQueue _queue;
        private readonly string _directory, _sessionId;
        private readonly long _segmentLimit, _campaignLimit;
        private readonly IJournalSegmentStreamFactory _streamFactory;
        private readonly Func<bool>? _diagnosticsEnabled;
        private readonly Action<long>? _flushDiagnostic;
        private readonly AutoResetEvent _wake = new AutoResetEvent(false);
        private readonly Thread _thread;
        private volatile bool _stopRequested;
        private volatile bool _storageDisabled;
        private volatile bool _unflushedTail;
        private volatile bool _durabilityBroken;
        private volatile RecorderStatus _status = RecorderStatus.Starting;
        private volatile string? _lastError;
        private volatile bool _disposed;
        private IJournalSegmentStream? _stream;
        private long _segmentBytes, _campaignBytes, _segmentLastSequence;
        private int _segmentNumber;
        private int _sinceFlush;
        private DateTime _lastFlushUtc = DateTime.UtcNow;
        public RecorderStatus Status { get => _status; private set => _status = value; }
        public string? LastError { get => _lastError; private set => _lastError = value; }
        public bool UnflushedTail { get => _unflushedTail; private set => _unflushedTail = value; }
        public event Action<RecorderStatus, string?>? StatusChanged;

        public JournalWriter(RecorderQueue queue, string directory, string sessionId,
            long segmentLimit = RecorderLimits.DefaultSegmentBytes, long campaignLimit = RecorderLimits.DefaultCampaignBytes,
            IJournalSegmentStreamFactory? streamFactory = null, Func<bool>? diagnosticsEnabled = null, Action<long>? flushDiagnostic = null)
        {
            _queue = queue ?? throw new ArgumentNullException(nameof(queue));
            _directory = Path.GetFullPath(directory ?? throw new ArgumentNullException(nameof(directory)));
            _sessionId = Sanitize(sessionId);
            _streamFactory = streamFactory ?? new FileJournalSegmentStreamFactory();
            _diagnosticsEnabled = diagnosticsEnabled;
            _flushDiagnostic = flushDiagnostic;
            _segmentLimit = Math.Min(segmentLimit, RecorderLimits.DefaultSegmentBytes);
            _campaignLimit = Math.Min(campaignLimit, RecorderLimits.DefaultCampaignBytes);
            if (_segmentLimit < RecorderLimits.MaxLineBytes || _campaignLimit < RecorderLimits.MaxLineBytes) throw new ArgumentOutOfRangeException("Storage limits must fit one event line.");
            Directory.CreateDirectory(_directory);
            _campaignBytes = Directory.EnumerateFiles(_directory, "*.jsonl", SearchOption.TopDirectoryOnly)
                .Select(path => new FileInfo(path).Length).Sum();
            _thread = new Thread(WriterLoop) { IsBackground = true, Name = "LifeRecorder JSONL writer" };
            _thread.Start();
        }

        public void Signal() { if (!_disposed) _wake.Set(); }

        public bool Stop(TimeSpan? maximumWait = null)
        {
            _stopRequested = true;
            try { _wake.Set(); } catch (ObjectDisposedException) { }
            var wait = maximumWait ?? TimeSpan.FromSeconds(2);
            if (wait > TimeSpan.FromSeconds(2)) wait = TimeSpan.FromSeconds(2);
            if (wait < TimeSpan.Zero) wait = TimeSpan.Zero;
            if (!_thread.Join(wait))
            {
                UnflushedTail = true;
                SetStatus(RecorderStatus.UnflushedTail, "Writer did not drain within the shutdown bound; tail durability is unknown.");
                return false;
            }
            return !UnflushedTail;
        }

        private void WriterLoop()
        {
            try
            {
                SetStatus(RecorderStatus.Recording, null);
                while (true)
                {
                    if (_durabilityBroken)
                    {
                        UnflushedTail = true;
                        SetStatus(RecorderStatus.UnflushedTail, LastError ?? "A segment durability operation failed; later writes cannot certify the journal prefix.");
                        return;
                    }
                    if (_storageDisabled)
                    {
                        UnflushedTail = _queue.Count > 0 || _queue.DroppedCount > 0 || _sinceFlush > 0 || UnflushedTail;
                        SetStatus(UnflushedTail ? RecorderStatus.UnflushedTail : RecorderStatus.StorageLimit,
                            "Recording stopped at the campaign storage limit; remaining tail is not durable.");
                        return;
                    }
                    bool didWork = false;
                    while (_queue.TryDequeue(out var item))
                    {
                        didWork = true;
                        if (!Write(item!))
                        {
                            _queue.RecordWriteFailure(item!);
                            Thread.Sleep(200);
                        }
                        if (_durabilityBroken) break;
                    }
                    if (_sinceFlush > 0 && (DateTime.UtcNow - _lastFlushUtc).TotalSeconds >= 1) FlushDurable();
                    if (_stopRequested && _queue.Count == 0)
                    {
                        if (_sinceFlush > 0) FlushDurable();
                        SetStatus(UnflushedTail ? RecorderStatus.UnflushedTail : RecorderStatus.Stopped, LastError);
                        return;
                    }
                    if (!didWork) _wake.WaitOne(100);
                }
            }
            catch (Exception ex)
            {
                LastError = ex.GetType().Name + ": " + ex.Message;
                UnflushedTail = true;
                SetStatus(RecorderStatus.UnflushedTail, "Writer terminated unexpectedly; durability of its tail is unknown. " + LastError);
            }
            finally
            {
                CloseStream();
                try { _wake.Dispose(); } catch { }
            }
        }

        private bool Write(JournalEvent item)
        {
            try
            {
                if (_durabilityBroken) return false;
                var bytes = item.ToUtf8JsonLine();
                var lineBytes = checked(bytes.Length + 1L);
                if (lineBytes > RecorderLimits.MaxLineBytes || _campaignBytes + lineBytes > _campaignLimit)
                {
                    _storageDisabled = true;
                    SetStatus(RecorderStatus.StorageLimit, "Campaign storage limit reached; recording is paused.");
                    return false;
                }
                if (_stream == null || _segmentBytes + lineBytes > _segmentLimit) OpenNextSegment();
                _stream!.Write(bytes, 0, bytes.Length);
                _stream.WriteByte((byte)'\n');
                _segmentBytes += lineBytes;
                _campaignBytes += lineBytes;
                _sinceFlush++;
                _segmentLastSequence = Math.Max(_segmentLastSequence, item.Sequence);
                _queue.MarkWritten(item.Sequence);
                if (_sinceFlush >= 128) FlushDurable();
                return true;
            }
            catch (Exception ex)
            {
                LastError = ex.GetType().Name + ": " + ex.Message;
                if (_sinceFlush > 0) UnflushedTail = true;
                CloseStream();
                SetStatus(RecorderStatus.IoFailure, LastError);
                return false;
            }
        }

        private void OpenNextSegment()
        {
            if (_stream != null && _sinceFlush > 0 && !FlushDurable())
                throw new IOException("Cannot rotate after the current segment failed its durable flush.");
            CloseStream();
            while (true)
            {
                _segmentNumber++;
                var path = Path.Combine(_directory, _sessionId + "-" + _segmentNumber.ToString("D6", System.Globalization.CultureInfo.InvariantCulture) + ".jsonl");
                try
                {
                    _stream = _streamFactory.Create(path);
                    _segmentBytes = 0;
                    return;
                }
                catch (IOException) when (File.Exists(path)) { }
            }
        }

        private bool FlushDurable()
        {
            try
            {
                if (_sinceFlush == 0) return true;
                if (_stream == null) throw new IOException("Cannot certify a segment after its stream was closed.");
                var segmentSequence = _segmentLastSequence;
                var started = _diagnosticsEnabled?.Invoke() == true ? Stopwatch.GetTimestamp() : 0;
                try { _stream.FlushDurable(); }
                finally { if (started != 0) try { _flushDiagnostic?.Invoke(Stopwatch.GetTimestamp() - started); } catch { } }
                _sinceFlush = 0;
                _segmentLastSequence = 0;
                _lastFlushUtc = DateTime.UtcNow;
                if (!_durabilityBroken && segmentSequence > 0) _queue.MarkDurable(segmentSequence);
                if (!UnflushedTail && !_durabilityBroken) SetStatus(RecorderStatus.Recording, null);
                return true;
            }
            catch (Exception ex)
            {
                LastError = ex.GetType().Name + ": " + ex.Message;
                _durabilityBroken = true;
                UnflushedTail = true;
                CloseStream();
                SetStatus(RecorderStatus.IoFailure, LastError);
                return false;
            }
        }

        private void CloseStream() { try { _stream?.Dispose(); } catch { } _stream = null; _segmentBytes = 0; }
        private void SetStatus(RecorderStatus status, string? detail) { Status = status; try { StatusChanged?.Invoke(status, detail); } catch { } }
        private static string Sanitize(string value)
        {
            if (string.IsNullOrWhiteSpace(value)) throw new ArgumentException("Session ID is required.", nameof(value));
            var result = new string(value.Where(c => char.IsLetterOrDigit(c) || c == '-' || c == '_').ToArray());
            if (result.Length == 0) throw new ArgumentException("Session ID has no safe filename characters.", nameof(value));
            return result;
        }

        public void Dispose()
        {
            if (_disposed) return;
            Stop(TimeSpan.FromSeconds(2));
            _disposed = true;
            // The writer thread owns its stream and wait handle. If the bounded join timed out,
            // it retains both until it exits; WriterLoop's finally block performs cleanup.
        }
    }
}
