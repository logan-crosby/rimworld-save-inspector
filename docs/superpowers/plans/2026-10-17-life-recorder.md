# Life Recorder Implementation Plan

> For agentic workers: use superpowers:subagent-driven-development. Beads owns task status; this plan defines deliverables, not a parallel task tracker.

Goal: build the optional recorder and independently usable read-only journal ingestion according to ../specs/2026-10-17-life-recorder-design.md.

Architecture: immutable JSONL event records bridge a standalone C# mod and bounded Python services. Runtime collection, writer and reader have explicit failure/coverage semantics; the larger save-history subsystem consumes these records later.

Tech: C# net472/runtime, modern .NET core tests, pinned RimWorld1.6.4535/Harmony2.3.3/Newtonsoft13.0.3; Python>=3.11 with existing dependencies.

## Global constraints

Original saves and active profiles unchanged. No deployment/publication/registration. Exact envelope and bounds in spec §§3/6 are normative. Unknown clocks null; ticks_abs=ticks_game+game_start_abs_tick when known. Forked sessions never merge by pawn name/ID alone. Missing evidence and skipped mandatory tests never pass. Independent L0 expectations precede product code.

## Review focus

Reloading old saves must preserve alternative sessions; duplicate event IDs with different payloads must conflict; source mutations and partial JSONL tails must not masquerade as complete data; writer failures must not affect game exceptions; pawn filters must retain relevant coverage gaps.

## Task 1: independent contract/oracles and feasibility

Own tests/journal_oracle/**, docs/recorder/hook-audit.md and review receipt only. Read spec; independently create synthetic valid/fork/conflict/gap/unknown-clock/unsafe/truncated fixtures and expected assertions. Define expected schema field values without importing product code. Audit native signatures from cached references/current source, record precise target versions and missing families. Report contradictions to root. Run oracle-only checks and retain output. Root resolves design findings and freezes fixture hashes before either implementer starts.

## Task 2: standalone recorder

Own recorder/** only, including C# core/runtime/tests/build docs. Consume frozen event envelope and fixtures. Implement immutable JournalEvent, bounded RecorderQueue, JournalWriter and SessionTracker; game component plus audited collectors and coverage registry. Tests assert drop-range accounting,64KiB lines,4096/16MiB queue,16MiB rotation,512MiB storage,2s shutdown and failure isolation. Compile pinned reference profile without deployment. Emit synthetic integration JSONL using the production serializer. Add lifecycle/native test recipe and clearly list unverified hooks. Commit only owned paths and return commit IDs, exact commands, pass/fail/blocked status, emitted sample and limitations.

## Task 3: Python ingestion/adapters

Own src/rimworld_save_inspector/journal.py, journal_mcp.py, cli.py and tests/journal/** only. Consume frozen envelope/fixtures. Implement read_journal(path, *, pawn_id=None, session_id=None), bounded limits, strict source handling, chronology/branch/conflicts and origin evidence. Add journal/journal-mcp CLI and three configured-path MCP tools. Tests assert independent oracle expectations plus all reader bounds and source lifecycle faults, CLI/MCP equivalence and preserved legacy commands. Produce JSON schema in docs/recorder/journal.schema.json (ownership assigned to this task). Commit owned paths and return commands/results/IDs. Do not touch recorder/**, oracle fixtures, pyproject or shared spec.

## Task 4: lead integration and packaging

Root owns README/pyproject/package inclusion and normative docs. Reconcile C#-emitted data with Python reader, run public baseline/regressions, build clean wheel/sdist and recorder artifact, install outside checkout, preserve failures. Establish unique evidence filenames per attempt and hashes. Create reviewer diff/package from initial2bfa544 to final candidate. Native gate work requires an unowned disposable game profile; otherwise record L2/L5/L6 as unverified/blocked with concrete reason, never accept compilation as native success.

## Task 5: independent review and handoff

Fresh reviewer reads spec, frozen oracle, diff and receipts, reproduces consequential candidate results and reports gate-by-gate. Root fixes consequential findings and reruns affected checks without changing expectations to match results. Commit focused changes, update Beads and durable Obsidian handoff with author thread and exact verified/open state. No final completion claim unless L0–L7 all pass.
