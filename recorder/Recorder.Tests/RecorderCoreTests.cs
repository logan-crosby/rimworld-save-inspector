using System;
using System.IO;
using System.Linq;
using System.Threading;
using Newtonsoft.Json.Linq;
using RimWorld.LifeRecorder.Core;
using Xunit;

public sealed class RecorderCoreTests
{
    [Fact]
    public void NestedCallbacksPublishOneLogicalTransitionWithBothSources()
    {
        var published = new System.Collections.Generic.List<string[]>();
        using (TransitionCorrelation.Begin())
        {
            using (TransitionCorrelation.Begin())
                TransitionCorrelation.Submit("genes|pawn-1|A->B", "AddGene", sources => published.Add(sources.ToArray()));
            TransitionCorrelation.Submit("genes|pawn-1|A->B", "SetXenotype", sources => published.Add(sources.ToArray()));
            TransitionCorrelation.Submit("genes|pawn-1|A->C", "SetXenotype", sources => published.Add(sources.ToArray()));
            Assert.Empty(published); // outer causal scope owns publication
        }
        Assert.Equal(2, published.Count);
        Assert.Equal(new[] { "AddGene", "SetXenotype" }, published[0]);
        Assert.Equal(new[] { "SetXenotype" }, published[1]);
    }

    [Fact]
    public void SeparateSameTickScopesKeepLegitimateRepeatedTransitionsAndFailureDoesNotPoisonNextScope()
    {
        var published = 0;
        try
        {
            using (TransitionCorrelation.Begin())
            {
                TransitionCorrelation.Submit("health|pawn-1|add:scar", "AddHediff", _ => published++);
                throw new InvalidOperationException("native action failed after completed inner callback");
            }
        }
        catch (InvalidOperationException) { }
        using (TransitionCorrelation.Begin())
            TransitionCorrelation.Submit("health|pawn-1|add:scar", "AddHediff", _ => published++);
        Assert.Equal(2, published);
    }

    [Fact]
    public void HolderTransactionKeepsFirstBeforeAndFinalAfterAcrossSiblingCallbacks()
    {
        var published = new System.Collections.Generic.List<(string Before, string After, string[] Sources)>();
        var finalHolder = "map";
        using (TransitionCorrelation.Begin("CaravanMaker.MakeCaravan"))
        {
            foreach (var callback in new[]
                {
                    (Source: "Thing.DeSpawn", Before: "map", After: "none"),
                    (Source: "ThingOwner.NotifyAdded", Before: "none", After: "caravan"),
                    (Source: "Caravan.AddOrRemovePawn", Before: "map", After: "caravan"),
                    (Source: "WorldPawns.PoolMutation", Before: "caravan", After: "caravan+world_alive")
                })
            {
                using (TransitionCorrelation.Begin(callback.Source))
                {
                    finalHolder = callback.After;
                    var firstBefore = callback.Before;
                    TransitionCorrelation.SubmitFirst("holder|pawn-1", callback.Source,
                        sources => published.Add((firstBefore, finalHolder, sources.ToArray())));
                }
                Assert.Empty(published);
            }
        }
        Assert.Single(published);
        Assert.Equal("map", published[0].Before);
        Assert.Equal("caravan+world_alive", published[0].After);
        Assert.Equal(new[] { "Thing.DeSpawn", "ThingOwner.NotifyAdded", "Caravan.AddOrRemovePawn", "WorldPawns.PoolMutation" }, published[0].Sources);
    }

    [Fact]
    public void HolderTransactionsKeepSeparateSameTickCallsAndReleaseScopeAfterException()
    {
        var published = new System.Collections.Generic.List<string>();
        using (TransitionCorrelation.Begin("CaravanMaker.MakeCaravan"))
            TransitionCorrelation.SubmitFirst("holder|pawn-1", "Thing.DeSpawn", _ => published.Add("first"));
        try
        {
            using (TransitionCorrelation.Begin("CaravanMaker.MakeCaravan"))
            {
                TransitionCorrelation.SubmitFirst("holder|pawn-1", "Thing.DeSpawn", _ => published.Add("failed-call-mutation"));
                throw new InvalidOperationException("native call failed after a real mutation");
            }
        }
        catch (InvalidOperationException) { }
        using (TransitionCorrelation.Begin("CaravanMaker.MakeCaravan"))
            TransitionCorrelation.SubmitFirst("holder|pawn-1", "Thing.DeSpawn", _ => published.Add("third"));
        Assert.Equal(new[] { "first", "failed-call-mutation", "third" }, published);
    }

    [Fact]
    public void HolderTransactionCanDiscardNoNetChangeAtNativeBoundary()
    {
        var published = 0;
        var finalHolder = "map";
        using (TransitionCorrelation.Begin("CaravanMaker.MakeCaravan"))
        {
            Assert.True(TransitionCorrelation.IsWithinSource("CaravanMaker.MakeCaravan"));
            using (TransitionCorrelation.Begin("Thing.DeSpawn"))
            {
                finalHolder = "none";
                TransitionCorrelation.SubmitFirst("holder|pawn-1", "Thing.DeSpawn", _ =>
                {
                    if (finalHolder != "map") published++;
                });
            }
            finalHolder = "map"; // native operation restored the exact original state
            Assert.Equal(0, published);
        }
        Assert.Equal(0, published);
        Assert.False(TransitionCorrelation.IsWithinSource("CaravanMaker.MakeCaravan"));
    }

    [Fact]
    public void DistinctPawnsWithUnreadableSavedIdsKeepSeparateCaravanTransitions()
    {
        var firstPawn = new object();
        var secondPawn = new object();
        var published = new System.Collections.Generic.List<(object Pawn, string Before, string[] Sources)>();
        using (TransitionCorrelation.Begin("CaravanMaker.MakeCaravan"))
        {
            using (TransitionCorrelation.Begin("Thing.DeSpawn"))
            {
                TransitionCorrelation.SubmitFirstForObject(firstPawn, "Thing.DeSpawn",
                    sources => published.Add((firstPawn, "map", sources.ToArray())));
                TransitionCorrelation.SubmitFirstForObject(secondPawn, "Thing.DeSpawn",
                    sources => published.Add((secondPawn, "map", sources.ToArray())));
            }
            using (TransitionCorrelation.Begin("WorldPawns.PoolMutation"))
            {
                TransitionCorrelation.SubmitFirstForObject(firstPawn, "WorldPawns.PoolMutation",
                    sources => published.Add((firstPawn, "intermediate", sources.ToArray())));
                TransitionCorrelation.SubmitFirstForObject(secondPawn, "WorldPawns.PoolMutation",
                    sources => published.Add((secondPawn, "intermediate", sources.ToArray())));
            }
            Assert.Empty(published);
        }
        Assert.Equal(2, published.Count);
        Assert.Same(firstPawn, published[0].Pawn);
        Assert.Same(secondPawn, published[1].Pawn);
        Assert.All(published, observation => Assert.Equal("map", observation.Before));
        Assert.All(published, observation => Assert.Equal(new[] { "Thing.DeSpawn", "WorldPawns.PoolMutation" }, observation.Sources));
    }

    [Fact]
    public void SavedIdentityComparisonReportsUnknownWhenEitherIdCannotBeRead()
    {
        Assert.Null(SavedIdentity.IsPreserved(null, null));
        Assert.Null(SavedIdentity.IsPreserved("", "Thing_Human1"));
        Assert.Null(SavedIdentity.IsPreserved("Thing_Human1", null));
        Assert.True(SavedIdentity.IsPreserved("Thing_Human1", "Thing_Human1"));
        Assert.False(SavedIdentity.IsPreserved("Thing_Human1", "Thing_Human2"));
    }

    [Fact]
    public void BirthObservationsCoalesceWithinSessionButNotAcrossReplayAndFlushOnPausedOrSaveBoundary()
    {
        var firstQueue = new RecorderQueue("r", "g", "campaign-a", "session-1", maxBytes: 4096);
        var firstRun = new BirthObservationBuffer("session-1", firstQueue, maxPendingSubjects: 8);
        var clock = RecorderTimeBasis.Create(7, 107, 100, 5500);
        Assert.Equal(BirthObserveResult.Staged, firstRun.Observe(new BirthObservation("pawn-1", "QuestManager", "unknown", "mother-1", null, "mother-1", clock)));
        Assert.Equal(BirthObserveResult.Merged, firstRun.Observe(new BirthObservation("pawn-1", "TaleRecorder", "biological_birth", "mother-1", "father-1", null, RecorderTimeBasis.Create(8, 108, 100, 5500))));
        Assert.Equal(1, firstRun.Flush()); // caller invokes this at GameComponentUpdate even while paused and before saving
        Assert.Equal(0, firstRun.Flush()); // late duplicate notification in same run cannot emit a second birth
        Assert.True(firstQueue.TryEnqueue(seq => Event(seq, "save_checkpoint", sessionId: "session-1"), out _)); // save marker is queued only after pre-save birth flush
        Assert.True(firstQueue.TryDequeue(out var birth));
        Assert.Equal("pawn_born", birth!.EventType);
        Assert.Equal(7, birth.TicksGame); // first exact callback clock is retained
        Assert.Equal("biological_birth", (string)birth.Payload["birth_kind"]!);
        Assert.Equal("mother-1", (string)birth.Payload["mother_id"]!);
        Assert.Equal("father-1", (string)birth.Payload["father_id"]!);
        Assert.Equal(2, ((JArray)birth.Payload["observations"]!).Count);
        Assert.True(firstQueue.TryDequeue(out var saveMarker));
        Assert.Equal("save_checkpoint", saveMarker!.EventType);
        Assert.Equal(BirthObserveResult.LateObservation, firstRun.Observe(new BirthObservation("pawn-1", "QuestManager", "unknown", "mother-1", null, "mother-1", clock)));
        Assert.Equal(BirthObserveResult.LateConflict, firstRun.Observe(new BirthObservation("pawn-1", "late-source", "unknown", null, null, null, clock)));

        var replayQueue = new RecorderQueue("r", "g", "campaign-a", "session-2", maxBytes: 4096);
        var replayRun = new BirthObservationBuffer("session-2", replayQueue, maxPendingSubjects: 8);
        Assert.Equal(BirthObserveResult.Staged, replayRun.Observe(new BirthObservation("pawn-1", "QuestManager", "unknown", null, null, null, clock)));
        Assert.Equal(1, replayRun.Flush());
        Assert.Equal(2, firstQueue.HighestAllocatedSequence); // one birth followed by the save marker; late duplicate allocated nothing
        Assert.Equal(1, replayQueue.HighestAllocatedSequence);
    }

    [Fact]
    public void BirthCoalescerRetainsConflictsAndCannotExceedSharedQueueByteBudget()
    {
        var queue = new RecorderQueue("r", "g", "campaign-a", "session-a", maxBytes: 1800);
        var buffer = new BirthObservationBuffer("session-a", queue, maxPendingSubjects: 2);
        var clock = RecorderTimeBasis.Create(1, 1, 0, null);
        Assert.Equal(BirthObserveResult.Staged, buffer.Observe(new BirthObservation("pawn-1", "hatch", "hatching", "mother-1", null, null, clock)));
        Assert.Equal(BirthObserveResult.Merged, buffer.Observe(new BirthObservation("pawn-1", "birth", "biological_birth", "mother-2", null, null, clock)));
        Assert.Equal("unknown", buffer.GetPending("pawn-1")!.Payload["birth_kind"]!.Value<string>());
        Assert.True((bool)buffer.GetPending("pawn-1")!.Payload["classification_conflict"]!);
        Assert.Contains("mother-1", buffer.GetPending("pawn-1")!.RelatedIds);
        Assert.Contains("mother-2", buffer.GetPending("pawn-1")!.RelatedIds);
        var mergedEventReservation = buffer.StagedBytes;
        Assert.Equal(mergedEventReservation, queue.RetainedBytes);
        Assert.InRange(mergedEventReservation, 1, 1800); // actual max-sequence envelope bytes fit the declared 1800-byte test cap

        var competingEvent = Event(1, payload: new JObject { ["blob"] = new string('x', 1050) });
        Assert.True(mergedEventReservation + competingEvent.SerializedBytes > 1800); // establishes why the regular queue insertion must be rejected
        Assert.False(queue.TryEnqueue(seq => Event(seq, payload: new JObject { ["blob"] = new string('x', 1050) }), out _));
        Assert.Equal(mergedEventReservation, queue.RetainedBytes); // staged birth reservation blocks the ordinary event; no double-budget path
        Assert.Equal(BirthObserveResult.Staged, buffer.Observe(new BirthObservation("pawn-2", "birth", "unknown", null, null, null, clock)));
        Assert.Equal(BirthObserveResult.Overflow, buffer.Observe(new BirthObservation("pawn-3", "birth", "unknown", null, null, null, clock)));
        Assert.Equal(buffer.StagedBytes, queue.RetainedBytes);
        Assert.True(queue.RetainedBytes <= 1800);
    }

    [Fact]
    public void BirthBufferPreservesDistinctSubjectsAtSameTickAndBoundsSeenIdentityMemory()
    {
        var queue = new RecorderQueue("r", "g", "campaign-a", "session-a", maxBytes: 8192);
        var buffer = new BirthObservationBuffer("session-a", queue, maxPendingSubjects: 2, maxSeenSubjects: 2);
        var clock = RecorderTimeBasis.Create(10, 20, 10, 5500);
        Assert.Equal(BirthObserveResult.Staged, buffer.Observe(new BirthObservation("pawn-1", "birth", "unknown", null, null, null, clock)));
        Assert.Equal(BirthObserveResult.Staged, buffer.Observe(new BirthObservation("pawn-2", "birth", "unknown", null, null, null, clock)));
        Assert.Equal(2, buffer.Flush());
        Assert.Equal(BirthObserveResult.LateConflict, buffer.Observe(new BirthObservation("pawn-1", "late", "unknown", null, null, null, clock)));
        Assert.Equal(BirthObserveResult.CapacityExhausted, buffer.Observe(new BirthObservation("pawn-3", "birth", "unknown", null, null, null, clock)));
        Assert.Equal(2, queue.HighestAllocatedSequence);
    }

    [Fact]
    public void TimeBasisKeepsExactClockRelationAndUnknownStartingYear()
    {
        var basis = RecorderTimeBasis.Create(1234, 53734, 52500, null);
        Assert.Equal(1234, basis.TicksGame);
        Assert.Equal(53734, basis.TicksAbs);
        Assert.Equal(52500, basis.GameStartAbsTick);
        Assert.Null(basis.StartingYear);
        Assert.Throws<ArgumentException>(() => RecorderTimeBasis.Create(1234, 53735, 52500, 5500));
    }

    [Fact]
    public void SaveOutputAssociationRequiresAllExactMarkersAndIsBounded()
    {
        var path = Path.Combine(Path.GetTempPath(), "life-recorder-save-" + Guid.NewGuid().ToString("N") + ".rws");
        try
        {
            File.WriteAllText(path, "<savegame><lifeRecorderCampaignId>campaign-a</lifeRecorderCampaignId><lifeRecorderCheckpointId>checkpoint-a</lifeRecorderCheckpointId><lifeRecorderCheckpointSession>session-a</lifeRecorderCheckpointSession></savegame>");
            var valid = SaveOutputVerifier.Verify(path, "campaign-a", "checkpoint-a", "session-a", maxBytes: 4096);
            Assert.True(valid.Associated, valid.FailureReason);
            Assert.True(valid.ByteCount > 0);
            Assert.Equal(64, valid.Sha256!.Length);
            Assert.False(SaveOutputVerifier.Verify(path, "campaign-a", "wrong-checkpoint", "session-a", maxBytes: 4096).Associated);
            Assert.False(SaveOutputVerifier.Verify(path, "campaign-a", "checkpoint-a", "session-a", maxBytes: 16).Associated);
        }
        finally { if (File.Exists(path)) File.Delete(path); }
    }

    [Theory]
    [InlineData("../outside")]
    [InlineData("../../../../tmp/escape")]
    [InlineData("/absolute/outside")]
    [InlineData("C:\\outside\\campaign")]
    [InlineData("\\\\server\\share\\campaign")]
    public void CampaignStoragePathUsesSafeKeyBeneathConfiguredSaveRoot(string campaignId)
    {
        var configuredSaveRoot = Path.Combine(Path.GetTempPath(), "rwsi-save-root-" + Guid.NewGuid().ToString("N"));
        var actual = RecorderStoragePaths.ResolveCampaignDirectory(configuredSaveRoot, campaignId);
        var parent = Path.GetFullPath(Path.Combine(configuredSaveRoot, "LifeRecorder"));
        var expected = Path.GetFullPath(Path.Combine(parent, RecorderStoragePaths.GetCampaignStorageKey(campaignId)));

        Assert.Equal(expected, actual);
        Assert.Equal(parent, Path.GetDirectoryName(actual));
        Assert.Matches("^[a-f0-9]{64}$", Path.GetFileName(actual));
    }

    private static JournalEvent Event(long sequence, string type = "pawn_baseline", JObject? payload = null, string sessionId = "session-a") =>
        new JournalEvent("0.1.0-candidate", "1.6.4535", "campaign-a", sessionId, sequence, type,
            null, null, null, null, Array.Empty<string>(), null, null, "test", "state_observation", payload ?? new JObject { ["value"] = sequence });

    private sealed class FaultStreamFactory : IJournalSegmentStreamFactory
    {
        private readonly Func<string, FaultStream> _create;
        public readonly System.Collections.Concurrent.ConcurrentBag<FaultStream> Streams = new();
        public FaultStreamFactory(Func<string, FaultStream> create) => _create = create;
        public IJournalSegmentStream Create(string path)
        {
            var stream = _create(path);
            Streams.Add(stream);
            return stream;
        }
    }

    private sealed class FaultStream : IJournalSegmentStream
    {
        private readonly FileStream _inner;
        private readonly Action? _beforeWrite;
        private readonly Action? _beforeFlush;
        private int _disposed;
        public readonly ManualResetEventSlim WriteStarted = new(false);
        public readonly ManualResetEventSlim ReleaseWrite = new(false);
        public readonly ManualResetEventSlim DisposedEvent = new(false);
        public bool IsDisposed => Volatile.Read(ref _disposed) != 0;
        public FaultStream(string path, Action? beforeWrite = null, Action? beforeFlush = null)
        {
            _inner = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
            _beforeWrite = beforeWrite;
            _beforeFlush = beforeFlush;
        }
        public void Write(byte[] buffer, int offset, int count) { WriteStarted.Set(); _beforeWrite?.Invoke(); ThrowIfDisposed(); _inner.Write(buffer, offset, count); }
        public void WriteByte(byte value) { ThrowIfDisposed(); _inner.WriteByte(value); }
        public void FlushDurable() { ThrowIfDisposed(); _beforeFlush?.Invoke(); _inner.Flush(true); }
        public void Dispose() { Interlocked.Exchange(ref _disposed, 1); DisposedEvent.Set(); _inner.Dispose(); }
        private void ThrowIfDisposed() { if (IsDisposed) throw new ObjectDisposedException(nameof(FaultStream)); }
    }

    [Fact]
    public void EventOwnsPayloadAndEmitsExactNullableEnvelope()
    {
        var source = new JObject { ["nested"] = new JObject { ["v"] = 1 } };
        var e = new JournalEvent("r", "g", "c", "s", 1, "future_signal", null, null, null, null,
            Array.Empty<string>(), null, null, "collector", "coverage", source);
        source["nested"]!["v"] = 9;
        var returned = e.Payload;
        returned["nested"]!["v"] = 8;
        var json = JObject.Parse(System.Text.Encoding.UTF8.GetString(e.ToUtf8JsonLine()));
        Assert.Equal(JTokenType.Null, json["ticks_game"]!.Type);
        Assert.Equal(1, (int)json["payload"]!["nested"]!["v"]!);
        Assert.Equal("s:1", (string)json["event_id"]!);
        var encoded = e.ToUtf8JsonLine();
        Assert.False(encoded.Length >= 3 && encoded[0] == 0xEF && encoded[1] == 0xBB && encoded[2] == 0xBF);
    }

    [Fact]
    public void QueueAllocatesDroppedSequencesAndRecoversExactNoncontiguousGap()
    {
        var queue = new RecorderQueue("r", "g", "campaign-a", "session-a", maxRecords: 2, maxBytes: 4096);
        Assert.True(queue.TryEnqueue(seq => Event(seq), out var first));
        Assert.True(queue.TryEnqueue(seq => Event(seq), out var second));
        Assert.False(queue.TryEnqueue(seq => Event(seq), out var third));
        Assert.Equal(new long[] { 1, 2, 3 }, new[] { first, second, third });
        Assert.True(queue.TryDequeue(out var one));
        Assert.Equal(1, one!.Sequence);
        Assert.Equal(4, queue.HighestAllocatedSequence); // gap has consumed sequence 4 before the queue can accept more
        Assert.True(queue.TryDequeue(out var two));
        Assert.Equal(2, two!.Sequence);
        Assert.True(queue.TryEnqueue(seq => Event(seq), out var fifth));
        Assert.Equal(5, fifth);
        Assert.True(queue.TryDequeue(out var gap));
        Assert.Equal("gap", gap!.EventType);
        var payload = gap.Payload;
        Assert.Equal(1, (long)payload["dropped_count"]!);
        Assert.True((bool)payload["ranges_exact"]!);
        Assert.Equal(3, (long)payload["missing_ranges"]![0]!["first_sequence"]!);
        Assert.Equal(3, (long)payload["missing_ranges"]![0]!["last_sequence"]!);
    }

    [Fact]
    public void SerializerFailureConsumesSequenceAndIsReportedAsGap()
    {
        var queue = new RecorderQueue("r", "g", "campaign-a", "session-a", maxRecords: 4, maxBytes: 4096);
        Assert.False(queue.TryEnqueue(_ => throw new InvalidOperationException("fault"), out var lost));
        Assert.Equal(1, lost);
        Assert.True(queue.TryEnqueue(seq => Event(seq), out var accepted));
        Assert.Equal(2, accepted);
        Assert.True(queue.TryDequeue(out var acceptedEvent));
        Assert.Equal(2, acceptedEvent!.Sequence);
        Assert.True(queue.TryDequeue(out var gap));
        Assert.Equal("gap", gap!.EventType);
        Assert.Equal(3, gap.Sequence);
        Assert.Equal("serialization_error", (string)gap.Payload["missing_ranges"]![0]!["cause"]!);
    }

    [Fact]
    public void WriterRotatesWithoutOverwriteAndStopsWithinBound()
    {
        var path = Path.Combine(Path.GetTempPath(), "life-recorder-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        try
        {
            var queue = new RecorderQueue("r", "g", "campaign-a", "session-a", maxRecords: 32, maxBytes: 1024 * 1024);
            using var writer = new JournalWriter(queue, path, "session-a", segmentLimit: 64 * 1024, campaignLimit: 256 * 1024);
            for (var i = 0; i < 3; i++)
            {
                Assert.True(queue.TryEnqueue(seq => Event(seq, payload: new JObject { ["text"] = new string('x', 30000) }), out _));
                writer.Signal();
            }
            Assert.True(writer.Stop(TimeSpan.FromSeconds(2)));
            var files = Directory.GetFiles(path, "*.jsonl");
            Assert.True(files.Length >= 2);
            Assert.All(files, file => Assert.True(new FileInfo(file).Length <= 64 * 1024));
            Assert.True(queue.HighestDurableSequence >= 3);
        }
        finally { Directory.Delete(path, true); }
    }

    [Fact]
    public void InvalidClockAndOversizeLineCannotEnterQueue()
    {
        Assert.Throws<ArgumentException>(() => new JournalEvent("r", "g", "c", "s", 1, "x", 1, 4, 2, null,
            Array.Empty<string>(), null, null, "test", "coverage", new JObject()));
        Assert.Throws<InvalidOperationException>(() => Event(1, payload: new JObject { ["text"] = new string('z', RecorderLimits.MaxLineBytes) }));
    }

    [Fact]
    public void ParserRejectsIdentityMismatchAndPreservesNamespacedExtension()
    {
        var source = Event(7, "future_pawn_signal");
        var json = JObject.Parse(source.ToJson());
        json["extensions"] = new JObject { ["adapter.source_tag"] = "future-adapter" };
        var parsed = JournalEvent.FromJson(json.ToString());
        Assert.Equal("future_pawn_signal", parsed.EventType);
        Assert.Equal("future-adapter", (string)parsed.Extensions!["adapter.source_tag"]!);
        json["event_id"] = "wrong:7";
        Assert.Throws<Newtonsoft.Json.JsonSerializationException>(() => JournalEvent.FromJson(json.ToString()));
        Assert.Throws<Newtonsoft.Json.JsonReaderException>(() => JournalEvent.FromJson("{\"x\":1,\"x\":2}"));
    }

    [Fact]
    public void DefaultQueueEnforcesRecordAndSerializedByteBounds()
    {
        var queue = new RecorderQueue("r", "g", "campaign-a", "session-a");
        for (var i = 0; i < RecorderLimits.DefaultQueueRecords; i++)
            Assert.True(queue.TryEnqueue(seq => Event(seq), out _));
        Assert.False(queue.TryEnqueue(seq => Event(seq), out _));
        Assert.Equal(RecorderLimits.DefaultQueueRecords, queue.Count);
        Assert.True(queue.QueuedBytes <= RecorderLimits.DefaultQueueBytes);
    }

    [Fact]
    public void DefaultQueueReachesProductionSixteenMiBByteBound()
    {
        var queue = new RecorderQueue("r", "g", "campaign-a", "session-a");
        var payload = new JObject { ["text"] = new string('q', 60000) };
        var accepted = 0;
        while (queue.TryEnqueue(seq => Event(seq, payload: payload), out _)) accepted++;

        Assert.InRange(accepted, 250, RecorderLimits.DefaultQueueRecords - 1);
        Assert.Equal(accepted, queue.Count);
        Assert.Equal(1, queue.DroppedCount);
        Assert.True(queue.QueuedBytes <= RecorderLimits.DefaultQueueBytes);
        Assert.True(queue.QueuedBytes > RecorderLimits.DefaultQueueBytes - 100000);
    }

    [Fact]
    public void DefaultWriterRotatesOnlyAfterProductionSixteenMiBSegmentLimit()
    {
        var path = Path.Combine(Path.GetTempPath(), "life-recorder-default-rotation-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        try
        {
            var queue = new RecorderQueue("r", "g", "campaign-a", "session-a");
            using var writer = new JournalWriter(queue, path, "session-a");
            var payload = new JObject { ["text"] = new string('r', 60000) };
            const int total = 300;
            for (var first = 0; first < total; first += 100)
            {
                var end = Math.Min(total, first + 100);
                for (var i = first; i < end; i++)
                    Assert.True(queue.TryEnqueue(seq => Event(seq, payload: payload), out _));
                writer.Signal();
                var lastSequence = queue.HighestAllocatedSequence;
                Assert.True(SpinWait.SpinUntil(() => queue.HighestWrittenSequence >= lastSequence, TimeSpan.FromSeconds(10)));
            }
            Assert.True(writer.Stop(TimeSpan.FromSeconds(2)));

            var files = Directory.GetFiles(path, "*.jsonl");
            Assert.True(files.Length >= 2);
            Assert.All(files, file => Assert.True(new FileInfo(file).Length <= RecorderLimits.DefaultSegmentBytes));
            Assert.True(files.Sum(file => new FileInfo(file).Length) > RecorderLimits.DefaultSegmentBytes);
            Assert.True(queue.HighestDurableSequence >= total);
        }
        finally { if (Directory.Exists(path)) Directory.Delete(path, true); }
    }

    [Fact]
    public void DiskFailureIsContainedAndRecoveredGapIsWritten()
    {
        var path = Path.Combine(Path.GetTempPath(), "life-recorder-io-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        var queue = new RecorderQueue("r", "g", "campaign-a", "session-io", maxRecords: 16, maxBytes: 1024 * 1024);
        using var writer = new JournalWriter(queue, path, "session-io");
        Directory.Delete(path);
        try
        {
            Assert.True(queue.TryEnqueue(seq => Event(seq, sessionId: "session-io"), out var failedSequence));
            writer.Signal();
            Assert.True(SpinWait.SpinUntil(() => writer.Status == RecorderStatus.IoFailure, TimeSpan.FromSeconds(3)));
            Directory.CreateDirectory(path);
            Assert.True(queue.TryEnqueue(seq => Event(seq, sessionId: "session-io"), out var recoveredSequence));
            writer.Signal();
            Assert.True(writer.Stop(TimeSpan.FromSeconds(2)));
            Assert.Equal(1, failedSequence);
            Assert.Equal(2, recoveredSequence);
            var rows = Directory.GetFiles(path, "*.jsonl").SelectMany(file => File.ReadAllLines(file)).Select(JObject.Parse).ToArray();
            var gap = Assert.Single(rows.Where(row => (string?)row["event_type"] == "gap"));
            Assert.Equal("io_error", (string)gap["payload"]!["missing_ranges"]![0]!["cause"]!);
            Assert.Equal(1, (long)gap["payload"]!["dropped_count"]!);
        }
        finally { if (Directory.Exists(path)) Directory.Delete(path, true); }
    }

    [Fact]
    public void FailedFinalDurableFlushReturnsUnflushedAndDoesNotAdvanceWatermark()
    {
        var path = Path.Combine(Path.GetTempPath(), "life-recorder-fsync-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        var factory = new FaultStreamFactory(file => new FaultStream(file, beforeFlush: () => throw new IOException("injected fsync failure")));
        try
        {
            var queue = new RecorderQueue("r", "g", "campaign-a", "session-final", maxRecords: 8, maxBytes: 65536);
            using var writer = new JournalWriter(queue, path, "session-final", streamFactory: factory);
            Assert.True(queue.TryEnqueue(seq => Event(seq, sessionId: "session-final"), out _));
            writer.Signal();
            Assert.True(SpinWait.SpinUntil(() => queue.HighestWrittenSequence == 1, TimeSpan.FromSeconds(2)));
            Assert.False(writer.Stop(TimeSpan.FromSeconds(2)));
            Assert.Equal(RecorderStatus.UnflushedTail, writer.Status);
            Assert.True(writer.UnflushedTail);
            Assert.Equal(0, queue.HighestDurableSequence);
            Assert.Contains("injected fsync failure", writer.LastError);
        }
        finally { if (Directory.Exists(path)) Directory.Delete(path, true); }
    }

    [Fact]
    public void FailedPriorSegmentFlushCannotBeCertifiedByLaterSegmentFlush()
    {
        var path = Path.Combine(Path.GetTempPath(), "life-recorder-rotation-fsync-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        var factory = new FaultStreamFactory(file => new FaultStream(file,
            beforeFlush: () => { if (file.EndsWith("-000001.jsonl", StringComparison.Ordinal)) throw new IOException("injected first-segment fsync failure"); }));
        try
        {
            var queue = new RecorderQueue("r", "g", "campaign-a", "session-rotate", maxRecords: 8, maxBytes: 256 * 1024);
            using var writer = new JournalWriter(queue, path, "session-rotate", segmentLimit: 64 * 1024,
                campaignLimit: 256 * 1024, streamFactory: factory);
            for (var i = 0; i < 2; i++)
                Assert.True(queue.TryEnqueue(seq => Event(seq, sessionId: "session-rotate", payload: new JObject { ["text"] = new string('x', 30000) }), out _));
            writer.Signal();
            Assert.False(writer.Stop(TimeSpan.FromSeconds(2)));
            Assert.True(writer.UnflushedTail);
            Assert.Equal(0, queue.HighestDurableSequence);
            Assert.Single(factory.Streams); // rotation must stop when segment one cannot be certified
        }
        finally { if (Directory.Exists(path)) Directory.Delete(path, true); }
    }

    [Fact]
    public void DisposeTimeoutLeavesBlockedWriterResourcesOwnedUntilWriterExits()
    {
        var path = Path.Combine(Path.GetTempPath(), "life-recorder-blocked-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        FaultStream? blocked = null;
        var factory = new FaultStreamFactory(file => blocked = new FaultStream(file, beforeWrite: () =>
        {
            blocked!.WriteStarted.Set();
            if (!blocked.ReleaseWrite.Wait(TimeSpan.FromSeconds(8))) throw new TimeoutException("test release was not signalled");
        }));
        try
        {
            var queue = new RecorderQueue("r", "g", "campaign-a", "session-block", maxRecords: 8, maxBytes: 65536);
            var writer = new JournalWriter(queue, path, "session-block", streamFactory: factory);
            Assert.True(queue.TryEnqueue(seq => Event(seq, sessionId: "session-block"), out _));
            writer.Signal();
            Assert.True(SpinWait.SpinUntil(() => blocked?.WriteStarted.IsSet == true, TimeSpan.FromSeconds(2)));
            var started = DateTime.UtcNow;
            writer.Dispose();
            var elapsed = DateTime.UtcNow - started;
            Assert.InRange(elapsed.TotalSeconds, 1.8, 2.8);
            Assert.False(blocked!.IsDisposed);
            Assert.Equal(RecorderStatus.UnflushedTail, writer.Status);
            blocked.ReleaseWrite.Set();
            Assert.True(blocked.DisposedEvent.Wait(TimeSpan.FromSeconds(3)));
            Assert.True(blocked.IsDisposed);
            Assert.Equal(RecorderStatus.UnflushedTail, writer.Status);
            Assert.Null(writer.LastError); // the blocked writer exited through the normal caught shutdown path
        }
        finally { blocked?.ReleaseWrite.Set(); if (Directory.Exists(path)) Directory.Delete(path, true); }
    }

}
