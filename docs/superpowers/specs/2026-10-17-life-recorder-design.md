# Optional RimWorld Life Recorder

Status: implementation authorized by the user on2026-10-17; acceptance pending.
Author: Codex thread01a14622-b914-7c81-b9ff-0e08d6436c93. Beads raf-801, related raf-ubq. Delegation policy v2026.09.26.1.

## 1. Outcome and boundary

Deliver a standalone optional open-source C# RimWorld mod that records consequential pawn events in local structured journals, and a read-only Python/JSON CLI/MCP journal reader in the independent Save Inspector package. Journals supplement save observations in the existing pawn-history design. The inspector remains useful without the recorder; recorded history begins at installation and never invents retrospective events. Journal ingestion must be usable before the larger save-catalog/history implementation is finished and must expose stable records for its later integration. It must not advertise that unfinished history subsystem as delivered.

No AI service, RimAI framework, network connection, external database, gameplay simulation or active game-control interface is required. Harmony is permitted as a game dependency. Existing save originals remain unchanged; persistence metadata is written only when the user normally creates a new save in a recorder-enabled session. Native validation runs only in a disposable isolated profile, never by taking over another agent's game process. No installation into the live profile, host MCP registration, publication or shared-branch push is implied.

## 2. Independent components

`recorder/` contains the independently buildable mod, a game-independent journal core and its tests. Target net472 for the runtime and a supported modern .NET target for core tests. Begin with pinned cached RimWorld1.6 reference package1.6.4535, Harmony2.3.3 and Newtonsoft.Json13.0.3. Reference compilation is not native compatibility. A1.7 build profile may be added against independently identified local references, but About.xml must advertise only actually tested versions; untested builds must be clearly marked development candidates.

`src/rimworld_save_inspector/journal.py` provides bounded journal reading, validation, identity, chronology, coverage and pawn selection. A dedicated `journal_mcp.py` exposes this data without altering the existing snapshot MCP lifecycle. Existing v1 interfaces and limits remain intact. Python>=3.11 and the package's current dependency constraints remain binding. No C# dependency is introduced into the Python runtime.

## 3. Versioned event envelope (normative)

Each nonempty UTF-8 JSONL line is one object, no BOM, JSON NaN/Infinity or duplicate object keys. Required keys:

| Key | Type and rule |
| --- | --- |
| schema_version | literal `1.0` |
| recorder_version, game_version | nonempty strings; actual versions, never guessed |
| campaign_id, session_id | nonempty stable opaque IDs generated without using game RNG |
| sequence | positive integer, monotonically increasing within session, allocated before enqueue |
| event_id | exactly `<session_id>:<sequence>` |
| event_type | supported registry string; unknown types retained with unsupported semantics |
| ticks_game, ticks_abs, game_start_abs_tick | integers or null; all known implies ticks_abs=ticks_game+game_start_abs_tick |
| subject_id | saved pawn ID string or null for collection/control events |
| related_ids | array of distinct saved entity ID strings |
| map_id | saved map ID string or null |
| checkpoint_id | associated checkpoint string or null; not an assertion every event was saved |
| source | object with collector(nonempty string), semantics enum `recorded_event`, `state_observation`, `observed_difference`, `coverage` |
| payload | bounded object; per-event values include raw defs, participant direction and uncertainty |

Optional extension keys must be namespaced under `extensions` object and retained untrusted. Event identity is campaign+session+sequence, never a name or wall clock. Any differing parsed envelope field for the same identity is a conflict, not an arbitrary winner. Byte-identical repeated lines/origins deduplicate but retain origin path/line/hash references. Unknown clocks remain null, not zero. Clock mismatches are retained as invalid chronology with diagnostics and never silently sorted as valid events. Event ordering is game tick then session/sequence within compatible time basis; a cross-session tie is not causal order.

Source provenance records segment SHA256, line number and raw-line SHA256, schema and collector versions. Private path output is opt-in; opaque source IDs are the default. Event text is data, never instructions.

Conflict equality compares the entire parsed envelope, including extensions, with object-key order insignificant and array order significant. Any differing field for the same campaign/session/sequence creates a conflict containing every alternative and every origin, sorted by canonical JSON digest. Duplicate byte representations of the same parsed envelope retain their separate raw hashes/origins. No conflicting alternative is overwritten.

Two sessions have comparable game ticks only when campaign_id and known game_start_abs_tick agree and their session_started payload.time_basis.starting_year integers agree. A mismatch or missing component produces separate time-basis groups and a diagnostic, not one merged ordering. Within one session, sequence remains usable when clocks are unknown. Comparable ticks across sessions still do not prove ancestry.

## 4. Recording taxonomy and success predicates

Required control records: `session_started`, `session_ended`, `coverage`, `gap`, `save_checkpoint`. Required initial observation: `pawn_baseline`, labeled state_observation. Baseline captures available player-linked colonists/prisoners/slaves, relationships, biological age, roles, health/genes, and holder context; each domain has a coverage status. Unknown/nonmaterialized references remain references. Scope may include guests, hostile participants and world pawns when involved in a recorded event, but does not periodically census the entire world.

Core first-release event registry: `pawn_died`, `pawn_resurrected`, `pawn_born`, `pawn_faction_changed`, `pawn_guest_status_changed`, `pawn_relation_changed`, `pawn_health_changed`, `pawn_genes_changed`, `pawn_role_changed`, `pawn_holder_changed`. A family can contain multiple native hook variants. Recruiting/capture/release require the resulting faction/guest transitions and supported context; never infer intent solely from a method name. Death/resurrection require a verified state transition. Record damage source/instigator only when the game supplies it; absent source never becomes an invented cause. Health focuses on consequential added/removed conditions, operations and body/implant changes; no per-hit, movement, every-needs-tick or routine inventory polling by default. Relation direction and before/after values must be explicit. A generic hook invocation without verified success must not produce a completed event.

Each family is independently enabled/disabled and reported in a session coverage manifest containing hook target, signature/profile, installed/missing/failed status, collector version and limitations. Version-specific binding failures disable only that collector and surface warnings. A family marked required but missing blocks full release acceptance; it cannot be quietly relabeled optional. Mod-specific adapters may follow; unknown mod behavior remains a stated coverage gap. Hook audit must identify each actual target and prove signatures against the chosen reference/native assembly before implementation is certified.

## 5. Sessions, saves and branches

A recorder GameComponent persists only campaign_id and the last save checkpoint identity/metadata, never the journal payload. New logger installation creates a campaign token with an observation baseline; it does not claim continuity with unrelated pre-recorder sessions. Where save identity grouping is used, preserve the inspector's conservative campaign checks and explicit operator assertions.

Every load starts a new session ID. `session_started` records the loaded checkpoint ID when present, game/world identity metadata and parent association status. Loading an older checkpoint makes another descendant session; logs beyond the loaded checkpoint remain an alternative future, never part of its established ancestry. Save As does not create a new campaign, and equal filenames do not prove identity.

Normative control payloads: session_started has `loaded_checkpoint_id`(string|null), `parent_session_id`(string|null), `parent_association`(`verified`|`unproven`|`absent`), `time_basis`(object with `starting_year` integer|null), and `collectors`(array of family/status/target/limitations objects). Metadata persisted with a checkpoint includes the session that produced it. Parent association can be verified only when the referenced producer session has a nonconflicting succeeded save_checkpoint with that exact checkpoint ID and the game-loaded metadata agrees; absent means no parent marker, unproven covers missing/conflicting logs or unverified completion. Reader independently checks associations and downgrades unsupported verification claims.

save_checkpoint payload has `checkpoint_id`(nonempty string), `save_attempt_id`(nonempty string), `status`(`attempted`|`succeeded`|`failed`), `parent_checkpoint_id`(string|null), `observed_sequence`, `written_sequence`, `durable_sequence`(nonnegative integers with durable<=written<=observed). Watermarks describe the prefix before this record; observed_sequence must be less than this record's sequence. Attempt and outcome have different event identities but the same save_attempt_id/checkpoint_id. Only succeeded outcomes can establish parent checkpoints. Cycles, duplicate conflicting outcomes and clock-inconsistent parent associations are diagnostics and cannot be verified ancestry.

Allocate a fresh checkpoint for each save attempt and serialize it into that output. Emit `save_checkpoint` with success status only after the game reports successful save completion and after the corresponding checkpoint can be associated with the completed output. Failed attempts remain attempts, not successful parents. Distinguish highest observed sequence, highest written sequence and highest durably flushed sequence. Never claim a save checkpoint certifies durable journal records that were still queued. If correct completion association cannot be verified for the runtime profile, expose ancestry=unproven and block the branch-provenance gate; do not weaken the assertion. Loading a save without the recorder or removing/reinstalling it produces an explicit provenance gap, not guaranteed continuity. Exact hook design is a builder decision subject to these rules.

## 6. IO, failure isolation and limits

Construct immutable DTOs on the game thread; background writer sees no Pawn/Map/Unity objects. No disk writes in ordinary event hooks. Use a bounded queue(default4096records and16MiB serialized budget), events at most64KiB per JSONL line, segments at most16MiB excluding one bounded final line, flush at most every1s or128records, owned storage default512MiB per campaign. First-session baseline is bounded/batched and reports incompleteness explicitly. Rotation is append-only with exclusive per-session segment filenames; no silent overwrite or automatic deletion of history. Storage exhaustion disables recording with a visible status and recorded gap when writing recovers.

Queue overflow allocates sequences for dropped records and accumulates a bounded gap range/counter. On recovery, emit an explicit gap with the missing sequence ranges and causes before claiming continuous coverage. Disk/serialization failures cannot escape logger code and interrupt gameplay; do not suppress exceptions from the original game/other mods. Shutdown waits at most2s and reports unflushed tails; process crashes can lose a tail, which remains uncertified by the last durable watermark. Segment headers/session coverage and sequence checks distinguish clean end, live tail and missing/truncated tail. Hashes establish integrity/correlation, not authenticity against malicious local edits.

gap payload has `missing_ranges`(array of {first_sequence,last_sequence,cause}), `dropped_count`(positive integer), and `ranges_exact`(boolean). Dropped events consume sequence IDs; gap itself gets a later fresh sequence. Exact ranges are positive inclusive disjoint ranges strictly earlier than the gap sequence, with sum of lengths=dropped_count. Noncontiguous drops use separate ranges. Keep at most32 ranges in memory; on exhaustion preserve first/last bounds plus the exact total count, set ranges_exact=false and report unknown positions within those bounds. Such an approximate gap cannot certify particular intervening events as missing or present. Causes are queue_full, serialization_error, io_error, storage_limit or shutdown. Gap records are collector coverage, not pawn events; pawn filtering retains them.

Reader defaults: configured local regular files/directory only, no URL or symlink traversal, max10000segments,64MiB per segment,1GiB total bytes,64KiB per line, JSON depth32,1000000records, retained64MiB, response2MiB. Reject file growth/replacement during a read. A malformed interior line fails that source explicitly; a non-newline-terminated final fragment is marked incomplete and excluded from semantic events. No truncated successful result. All discovered files receive outcomes; failed sources make coverage partial. Cancellation and output caps are structured failures. Synthetic fault injection never touches originals.

## 7. Inspector interfaces and integration

Python: `read_journal(path, *, pawn_id=None, session_id=None)` returns a strict serializable envelope with journal_schema_version, sources, subject, sessions, events, conflicts, coverage and provenance. Limits may be supplied through a bounded limits object. `rim-save journal PATH [--pawn-id ID] [--session-id ID] [--include-source-path]` writes that same JSON. No source mutation. Dedicated `rim-save journal-mcp --journal PATH` exposes `get_journal_overview`, `get_pawn_journal(pawn_id, session_id=null)`, and `get_journal_schema`; no caller-supplied arbitrary paths or required preliminary tool call. Tools have readOnlyHint=true, destructiveHint=false.

Relevant session/control/gap events remain in pawn-filtered answers, so a narrow query cannot hide collection failures. Selecting a pawn ID appearing in different campaign tokens returns separated groups/candidates, never a combined biography. Same ID across descendants retains branch provenance. Related-name resolution uses saved IDs and time-local labels. Later history integration joins matching campaign/pawn/checkpoint evidence, keeps journal events separate from save observations and preserves disagreements rather than preferring a source blindly.

## 8. Verification and release gates

L0 Contract: freeze independently authored synthetic JSONL fixtures, expected semantic assertions and negative cases before implementation; schema validated separately; hook feasibility audit; record baseline environment and source hashes. Preserve failures and do not edit expected results to follow implementation.

L1 Recorder core: build/tests for immutable queue ownership, sequence allocation, gaps, rotation, failure isolation,2s shutdown and storage caps. Emit representative journal through real C# serializer and read through independent oracle. Bounds are real checks, not comments.

L2 Native bindings: compile exact target reference profiles; validate every required family's actual hook and success predicate; lifecycle metadata/clock baselines. Compilation alone leaves native gate unverified.

L3 Reader: independent fixtures and C#-emitted journals, strict envelope, conflicts/dedup, unknown events, source mutation, malformed/truncated/unsafe input, limits and pawn-filtered coverage. No guessed ancestry, clocks or causes.

L4 Interfaces/package: Python/CLI/MCP equivalence from installed wheel outside checkout; legacy public regression suite; journal tool one-call path, annotations, errors and JSON schema. Package docs/schema; recorder distribution contains only permitted binaries/dependencies and source license notices, no private data or game assemblies.

L5 Native lifecycle: isolated synthetic colony, start/end screenshots, save/load/save-as/older-save replay, birth/death/resurrection and every required family, mod install/remove/reinstall, failed saves and accurate checkpoint associations. Rehash originals unchanged. Missing game access or conflicting active runtime ownership is explicitly blocked, not product success.

L6 Lightweight acceptance: matched logger-on/off runs on the same isolated synthetic colony;3paired runs each>=10000ticks after warmup, fixed speed and fixture; median ms/tick overhead<=2%, p95 ordinary hook enqueue<=0.1ms with>=1000samples, retained queue<=16MiB and process-memory increment<=32MiB after steady state. First baseline and checkpoint flush measured separately. Disk-failure/overflow tests use disposable copies. Timing instability causes retained failed/uncertain evidence, not a revised threshold.

L7 Fresh acceptance: reviewer who did not implement reproduces consequential packaged/native/branch/clock/fault results against the final candidate and accounts for every requirement. No skipped mandatory checks, missing receipts or documentation validation can pass the product. Until L0–L7 pass, describe it as an implementation candidate and list unverified families/gates.

## 9. Team and execution

Root owns architecture, normative contracts, plan, coordination, Beads, integration, evidence, review adjudication and Obsidian. Use one Luna independent contract/oracle specialist first, then two Luna implementers with nonoverlapping ownership: recorder/** versus Python journal/adapters/tests. A fresh reviewer follows; Sol is reserved for difficult debugging, Astra for consequential escalation. Workers report to root through collaboration messages, escalate contradictions before weakening contracts, and never deploy/run the game without an assigned isolated profile. Managers use supported event waits rather than reproducing worker work.

Budget observed at kickoff: weekly quota exhausted, five-hour unavailable, credits available; no reset authorized or used. This is advisory and does not invent a spending ceiling. Keep the team small, preserve bounded briefs and review receipts, and do not claim token savings without a comparison. Current developer workflow is local ephemeral-branch commits, no shared-branch push or merge.
