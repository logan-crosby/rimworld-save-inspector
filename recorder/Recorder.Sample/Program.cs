using System.Text;
using Newtonsoft.Json.Linq;
using RimWorld.LifeRecorder.Core;

if (args.Length != 1) throw new ArgumentException("Usage: Recorder.Sample <output.jsonl>");
var output = Path.GetFullPath(args[0]);
Directory.CreateDirectory(Path.GetDirectoryName(output)!);
var events = new[]
{
    new JournalEvent("0.1.0-candidate", "1.6.4535", "sample-campaign", "sample-session", 1, "session_started", null, null, null,
        null, Array.Empty<string>(), null, null, "session_lifecycle", "coverage", new JObject
        {
            ["loaded_checkpoint_id"] = JValue.CreateNull(), ["parent_session_id"] = JValue.CreateNull(), ["parent_association"] = "absent",
            ["time_basis"] = new JObject { ["starting_year"] = JValue.CreateNull() }, ["collectors"] = new JArray()
        }),
    new JournalEvent("0.1.0-candidate", "1.6.4535", "sample-campaign", "sample-session", 2, "pawn_baseline", 100, null, null,
        "Pawn_123", Array.Empty<string>(), "Map_1", null, "baseline", "state_observation", new JObject
        {
            ["label_at_observation"] = "Sample pawn", ["faction_id"] = "Faction_0", ["relations"] = new JArray(),
            ["health_conditions"] = new JArray("Flu"), ["domain_coverage"] = new JObject { ["health"] = "current_condition_defs_only" }
        }),
    new JournalEvent("0.1.0-candidate", "1.6.4535", "sample-campaign", "sample-session", 3, "future_pawn_signal", 101, null, null,
        "Pawn_123", Array.Empty<string>(), "Map_1", null, "future_adapter", "recorded_event", new JObject { ["opaque_value"] = "retained as event data" },
        new JObject { ["example.source_tag"] = "csharp-production-serializer" })
};
using var stream = new FileStream(output, FileMode.Create, FileAccess.Write, FileShare.None);
foreach (var item in events)
{
    var line = item.ToUtf8JsonLine();
    stream.Write(line, 0, line.Length);
    stream.WriteByte((byte)'\n');
}
stream.Flush(true);
Console.WriteLine(output);
