# RimWorld Life Recorder candidate

**Experimental and deferred.** Native holder-transition failures remain unresolved.
Do not install this candidate for dependable logging. Recorder source is retained
for development; it is excluded from the Python wheel and source distribution.
Reference builds and synthetic tests do not establish native gameplay acceptance.

This directory contains an optional RimWorld mod, a game-independent JSONL core, core fault/bounds tests, and a serializer sample. The current build is a development candidate. A successful reference build does not certify Harmony bindings or native game behavior.

## Local build

The mod targets .NET Framework 4.7.2 and pins the cached RimWorld reference package 1.6.4535, Harmony 2.3.3, and Newtonsoft.Json 13.0.3. Core tests target .NET 8. The runtime compiles the same core source files into the mod assembly, so no .NET 8 assembly is loaded by the game.

```sh
dotnet restore recorder/Recorder.Mod/Recorder.Mod.csproj
dotnet build recorder/Recorder.Mod/Recorder.Mod.csproj -c Release
dotnet test recorder/Recorder.Tests/Recorder.Tests.csproj -c Release
```

The mod build copies `About/About.xml`, `LoadFolders.xml`, the project license, and Harmony/Newtonsoft license notices. It does not deploy the mod or start RimWorld. The candidate About metadata intentionally omits `supportedVersions`; native compatibility has not been demonstrated. The RimWorld reference package is used only at compile time and its assemblies are not bundled.

## Candidate behavior

The runtime makes cryptographic campaign/session IDs, creates one session per loaded game, persists only campaign/checkpoint metadata through the normal save serializer, and writes immutable event snapshots to local append-only JSONL segments. The default queue is bounded to 4,096 events and 16 MiB serialized data; event lines are limited to 64 KiB, segments to 16 MiB, and a campaign folder to 512 MiB. Flush occurs at 128 records or one second. Shutdown asks the writer to stop within two seconds. These limits are implemented in the core; they are not a performance or native acceptance result.

The initial observation is limited to at most 1,024 player-linked pawns on loaded local maps. It records saved pawn/map IDs, faction, available biological age, direct relations, current health condition defs, and available gene snapshots. Per-domain coverage labels identify unavailable fields. It does not census world pawns. Queue loss appears as sequence gaps when writing recovers. Disk failures are isolated from the game callback; storage exhaustion disables the writer and leaves the remaining tail uncertified.

Candidate collectors bind death, resurrection, faction, guest status, direct relations, genes/xenotype, health, ideology roles, royal titles, birth/hatching, and pawn holder transitions. Payloads use post-call success predicates or observed before/after differences; the exact audited selectors and limits are in `native-hook-audit.md`. Every session has a coverage manifest; binding failures are surfaced as `missing` or `failed` and logged. Build success does not mean those collectors will install or behave correctly in a running game.

Birth-specific native callbacks stage immutable observations in a per-loaded-session coalescer. Quest and tale notifications coalesce with successful egg hatching; a biological kind is claimed only from a positive birth-specific signal, while conflicting positive kinds remain `unknown` and preserve every source/parent observation. Late notifications in the same session become coverage records rather than a second completed birth. Staged envelopes share the queue's 16 MiB serialized-byte budget and flush on paused-safe `GameComponentUpdate`, before save serialization, and before close/reinitialization. Capacity overflow suppresses uncertain completed births and attempts a coverage diagnostic. These semantics have game-free unit coverage but have not yet been exercised in native gameplay.

Save callbacks record an attempt and perform bounded read-only output association after the game save callback returns; only matching metadata in the completed output can produce `succeeded`. The verifier and completion binding still require isolated native lifecycle validation. `session_started.parent_association` remains unproven until the reader confirms a nonconflicting succeeded checkpoint. Game clocks and starting year are captured together when available; unavailable values remain null. A requested clean session end is written on application shutdown; crashes can leave an uncertified tail.

## Verification recorded for this candidate

- `dotnet build recorder/Recorder.Mod/Recorder.Mod.csproj --no-restore --verbosity minimal` — passed against cached reference packages; this is reference compilation only.
- `dotnet test recorder/Recorder.Tests/Recorder.Tests.csproj --no-restore --logger 'console;verbosity=minimal'` — 8 passed, 0 failed. It covers immutable payload ownership, strict event identity, unknown event/extension retention, queue sequence/gap behavior, default queue bounds, line cap, segment rotation, bounded shutdown, and simulated I/O failure/recovery.
- `dotnet run --project recorder/Recorder.Sample/Recorder.Sample.csproj --no-restore -- .superpowers/sdd/2026-10-17-life-recorder/recorder-sample.jsonl` — emitted the sample through the production C# serializer. Python standard-library readback verified the envelope, event IDs, line bounds, unknown event type, and extension retention.
- `python tests/journal_oracle/oracle_check.py` — frozen independent oracle passed 8/8 fixtures. Its assertions apply to frozen fixtures; the sample was checked separately and is ready for the Python reader integration.

Native hook binding, event success semantics, save/load branch association, crash and migration behavior, packaging from an installed artifact, performance overhead, and isolated colony acceptance remain unverified. No game was launched and no mod was installed.
