# Pawn dossiers and multi-save life histories

Status: implementation authorized by the user's instruction to continue until the ultimate goal is fulfilled; acceptance pending.
Author: Codex thread 01a14622-b914-7c81-b9ff-0e08d6436c93.
Coordination: Beads raf-ubq; delegation policy v2026.09.26.1.

## 1. Intent and success

A caller must be able to ask about one pawn once and receive a coherent, evidence-backed account across every compatible snapshot in a configured save collection. It must not need to open each save, discover hidden holders manually, remember name changes, reconcile clocks itself, or mistake missing observations for events. Existing single-save JSON CLI and read-only MCP behavior remains available.

The product provides evidence and chronology, not an invented narrative. A successful answer exposes the full examined population of saves, unresolved identities, conflicting observations, omissions and evidence access. An archive of save files is not assumed to be one uninterrupted playthrough.

Python >=3.11 remains the implementation platform. No game launch, game assembly execution, network service, external model or mod installation is needed. Original saves are never modified. Public fixtures are synthetic. Private acceptance evidence stays out of source distributions and public history.

## 2. Deliverables and boundaries

1. A **pawn dossier** for an individual snapshot: all supported pawn scopes and domains, relationships, dated records, contextual entities, diagnostics and field metadata in one result.
2. A **save catalog**: discover configured local `.rws` files, inspect and fingerprint them, preserve duplicate origins, identify compatible history groups and record every outcome.
3. A **pawn timeline** across a catalog: historical identity/aliases, events, observed states and differences, reference context and source links, with explicit chronology and branch uncertainty.
4. JSON CLI, Python and MCP access to those results and to all retained evidence. Sources remain read-only. A normal history call refreshes the application-owned derived cache automatically, then queries an immutable generation. An explicitly requested frozen mode supports reproducibility.
5. Machine-readable schemas, examples, migration/version documentation, independent oracles, packaged acceptance and reproducible receipts.

This first extension does not simulate the game, claim runtime compatibility, infer motives, identify death causes from disappearance, generate political biographies, reconstruct unseen events, or silently resolve branches. It does not decode unkeyed record arrays without a validated definition map. A general relationship graph UI and a visual timeline can follow; they are not prerequisites for correct JSON.

“All available information” means all supported semantic domains plus losslessly retained source evidence for selected pawn records and directly linked records within the declared catalog. Unknown mod fields remain inspectable evidence with unknown interpretation. A compact response is never advertised as the entire raw XML payload.

## 3. User-visible interfaces

Proposed CLI (new commands; existing commands unchanged):

```
rim-save catalog build SAVE_DIRECTORY --output CATALOG.sqlite
rim-save catalog refresh CATALOG.sqlite
rim-save catalog inspect CATALOG.sqlite
rim-save dossier SAVE.rws --pawn-id Human42
rim-save timeline CATALOG.sqlite --anchor SNAPSHOT_SHA --pawn-id Human42
rim-save timeline CATALOG.sqlite --anchor SNAPSHOT_SHA --name 'Alex'
rim-save evidence CATALOG.sqlite EVIDENCE_ID
rim-save history-mcp --catalog CATALOG.sqlite
```

The catalog build configures the collection; callers never enumerate saves individually. Normal timeline calls rediscover that collection and refresh changed sources automatically. Discovery is recursive only with `--recursive`; hidden directories and symlink traversal are excluded by default and reported in the collection policy. URL inputs and non-regular files are rejected. The directory is an explicit authority boundary, not a search of the user's home directory. An explicit manifest of paths is also supported for a curated collection.

MCP additions on the catalog-configured server:

- `get_save_catalog()` — refreshed generation, compatible groups, coverage and freshness policy.
- `get_pawn_dossier(snapshot_id, pawn_id | name, detail='context')`.
- `get_pawn_timeline(anchor_snapshot_id, pawn_id | name, detail='context', domains=null)`.
- `get_history_evidence(evidence_id, cursor=null, limit=null)`.
- `get_history_schema()` — supported domains, field meanings, clock rules, limits and enum descriptions.

The main dossier/timeline calls perform selection and return context without a prerequisite inspect/list call. The anchor defaults only when the catalog contains exactly one compatible group with a unique highest-tick snapshot; otherwise candidates are returned. Exactly one of pawn_id/name is accepted. Name lookup uses historical aliases and returns candidate IDs with disambiguating context when ambiguous; it never merges people because names match. IDs are preferred for programmatic use.

All caller arguments are selectors within the configured catalog, never arbitrary filesystem paths, SQL, shell commands or XML expressions. CLI and MCP use the same query service and schemas.

## 4. Catalog and generation model

Use a local SQLite index and standard-library SQLite support. The original XML remains authoritative; the index is derived data. It lives outside the save directory unless the operator explicitly chooses otherwise, with private file permissions. No credentials or cloud uploads are involved.

Each generation records discovery policy, directory/manifest identity, indexed-at time, parser/profile/schema versions, all candidate file outcomes, snapshot hashes, immutable compatibility metadata and extraction coverage. `catalog_id` is a deterministic digest of the logical manifest and interpretation versions, independent of incidental database row order or build time.

Byte-identical files produce one snapshot with multiple origin records. Different bytes at the same tick remain different snapshots. File modification time and filenames are descriptive provenance, never in-game chronology.

The catalog path is a small SQLite registry containing configuration and the current-generation pointer. Immutable generation databases live in the application-owned sibling directory `<catalog-path>.generations/<catalog_id>.sqlite`. Build/refresh writes a temporary generation, validates and fsyncs it, installs it under its content-derived ID, then transactionally advances the registry pointer. A failure before pointer advancement leaves the previous generation current; orphan temporary files can be removed only inside this owned directory. Existing generations are never overwritten. Per-source failures remain in the new generation's coverage manifest; they cannot silently become successful empty snapshots. Source stat/fd identity and before/after integrity checks detect mutation or replacement during parsing. Source roots themselves cannot be an index output target; the chosen output must not overwrite any input file.

Every evidence ID and cursor binds its catalog namespace, immutable generation ID, record/page key and schema version. Evidence retrieval opens that specific generation, not the current pointer. Retained generations and their evidence remain readable across refresh and server restart. This release performs no automatic generation pruning; all generations count toward the storage budget and exhaustion fails refresh explicitly while preserving existing evidence. If an operator externally removes/corrupts a generation, return GENERATION_UNAVAILABLE or INVALID_CATALOG, never reinterpret an old cursor against a newer generation. Future pruning requires its own explicit retention contract.

Before a normal catalog/timeline call, rediscover the configured collection and compare device/inode, size, nanosecond mtime and ctime with the indexed manifest. Hash and reparse new or changed files; unchanged files reuse their indexed content hash. This is metadata-based freshness, not a claim of freshly hashing unchanged bytes. `freshness=strict` hashes every candidate before reuse; `freshness=frozen` explicitly queries a prior generation without filesystem freshness checks. Normal dossier calls validate the selected snapshot's source; if it changed, preserve the requested historical snapshot and report the new available revision rather than substituting it silently.

After refresh, pin one validated generation read-only for each query. Concurrent refresh never changes that query's evidence. The MCP process may write only its configured application-owned cache and temporary files; it cannot modify saves or caller-selected paths. These cache writes are disclosed in documentation; read-only tool annotations describe the absence of source/user-data mutation, not zero internal writes. Refresh is serialized and atomic, cancellation leaves the previous generation usable, and old generations remain addressable while evidence references/cursors require them. Retention exhaustion fails explicitly; it must not silently invalidate evidence.

Record discovery start/end, freshness mode, files rehashed/reused, additions/removals/changes, generation and indexed-at time in every response. Recheck directory membership and candidate metadata before committing refresh; a changing collection is retried once, then returns COLLECTION_CHANGED with diagnostics rather than claimed complete history. This gives a bounded observation window, not an atomic filesystem snapshot. Removed files remain historical evidence labeled unavailable and are excluded from the currently available-source count. An unreadable source has an explicit failed outcome and makes coverage partial; a failed refresh must not silently fall back to stale success. Newly compatible saves must appear on the next normal call without manual refresh or server restart.

Default safety limits: at most 10,000 candidates, 512 MiB per source, depth256, 16 MiB per retained logical record, 128 MiB retained working values per parsed source, 120s parse deadline per source, and a configurable bounded catalog storage budget with a documented default of20GiB. Limits fail explicitly with source-specific coverage. They do not produce truncated success. Catalog-level cancellation is checked between files and during parsing. One file is parsed at a time by default.

## 5. Compatibility and pawn identity

Compatibility has separate dimensions: readable format, supported extraction profile, likely shared world/campaign identity, clock consistency, and pawn identity. Different mod lists or game versions are not by themselves proof of a different colony, and equal names/seeds are not proof of the same colony.

The initial strong campaign key requires validated persisted fields: world seedString, persistentRandomValue, initialMapSize, game startingYear, gameStartAbsTick and startingTile. All must be present and agree under explicit profile normalization. World/faction display names are descriptive and may change. Mutable starting-pawn references, current maps, current mod list and filenames are excluded from identity. Unknown/missing required fields produce a review-needed group, not an automatic merge. A caller may supply an explicit grouping manifest; this is recorded as operator assertion with provenance, never relabeled proven ancestry. The initial profile must be tested against real archive diversity before H2 acceptance; if a supposedly immutable field changes, revise the rule and rerun identity acceptance rather than silently loosening it.

RimWorld1.6 is the initial verified extraction profile. Other versions may be indexed as best-effort with explicit field/profile coverage. Cross-version automatic grouping requires tests for the relevant metadata/clock representations; native save compatibility is outside this tool's claim. Migrated copies can belong to the same campaign group while remaining branch-unresolved.

Within a compatible group, normalized saved pawn IDs are the primary identity key. XML paths and inspector per-snapshot record hashes are not cross-save identity. Prefix normalization is explicit and conservative. Names, titles, sex/gender fields, faction and xenotype can change without creating a new pawn. Conflicting full records with the same ID in one snapshot remain candidates with an ambiguity diagnostic. Conflicting birth/definition evidence is reported as a possible ID collision; it is not automatically reconciled through a name match.

Reference-only mentions and materialized records are distinct. A grave tale, spouse reference or corpse reference does not mean a living pawn was present on that map. Historical aliases retain their observation/event provenance and are not retroactively overwritten with the latest name.

## 6. Complete single-save scope and context

Find materialized pawns in direct maps, world alive/dead/mothballed pools, caravans and transport holders, cryptosleep, growth vats, corpses, inventories and pawn carry containers, including recursively nested holders. Resolve reference-only holders separately. Identify unknown holder/pawn candidates and expose their source evidence and coverage diagnostics rather than dropping them.

The dossier carries:

- Identity, aliases, race/def/kind, title, faction, guest/prisoner/slave status and relevant interaction settings.
- Location with map identity/name/type, complete holder chain, holder state, world pool and explicit living/dead evidence. Absence of healthState is not manufactured into an explicitly stored “healthy” value.
- Biological age, birth absolute tick, chronological age where clocks permit, growth/vat/cryosleep fields and their raw values. No claim that vat time equals total elapsed age unless the fields support it.
- Backstory, traits and suppression, skills/passions, health/implants/injuries/body references, genes and override information, equipment/apparel/inventory quantities and ambiguity.
- Direct relationships with related pawn IDs, aliases/names at the relevant time and materialization status. Parent direction is explicit. Parentage does not imply romance or conception method.
- Abilities and saved use timestamps, mechanitor assignments with resolved/unresolved targets, bed/casket/grave ownership, work settings, needs/memories and serialized records where interpretable.
- Relevant retained tales, archive letters/messages, play/battle log records, and art objects whose tale references link to the pawn. Unknown event semantics remain raw linked evidence rather than an asserted actor/victim direction. Text-only name mentions are labeled ambiguous leads and cannot establish an event.
- Snapshot-level mods, research, scenario and map/faction context via a shared context table; do not repeat huge context blobs on every event.
- Unknown/custom serialized fields as retained evidence, with type/units/meaning explicitly unknown. Do not pretend data stored outside these supported areas has been exhaustively interpreted.

References resolve within a snapshot first. Names from another save are fallback context labeled with their source and time. Traversal defaults to direct linked entities, not unbounded recursive biographies. Linked entities get concise identity/status context and stable IDs for further queries.

## 7. Chronology contract

Every time-bearing value includes `raw_value`, `raw_field`, `clock`, `normalized_game_tick`, `normalization_rule`, and status. Snapshot time includes ticksGame, gameStartAbsTick and startingYear independently. Conversion is integer arithmetic; no float rounding for ordering.

For a validated profile: snapshot absolute tick = ticksGame + gameStartAbsTick. A field explicitly defined as absolute converts by subtracting that snapshot's gameStartAbsTick; a game-relative field does not. Biological-age ticks are a duration, not an event timestamp. File mtime is wall-clock provenance only. Dates that cannot be normalized remain unknown and cannot be sorted as zero. Negative normalized times may represent pre-arrival events and remain valid; events after the supporting snapshot are flagged as temporal contradictions. Calendar renderings are secondary and state the validated calendar rule; raw ticks remain available.

Each timeline item distinguishes:

1. `recorded_event`: an event with an explicit recorded occurrence field and supported semantics. It may predate the earliest available save.
2. `state_observation`: a state seen at a specific snapshot time; “first observed dead” is not an exact death timestamp.
3. `observed_difference`: facts differ between two observations. Both endpoints and the intervening unobserved interval are included. It is not asserted to have happened at the later save tick.
4. `inference`: a separately labeled rule-based interpretation with premises and limitations. Optional; never substituted for the observations.

Elapsed time and biological age are presented separately. Missing fields, missing records, changed extraction profiles and unreadable saves are coverage gaps, not zero values or deletions. Explicit empty collections can support removal observations; absent collections generally cannot.

## 8. Branches, contradictions and event deduplication

RimWorld saves generally do not contain a reliable parent-save graph. The default timeline is a chronology of evidence, not a certified causal lineage. Every result states `lineage_status=unproven` unless the operator supplies an explicit branch selection/parent manifest. A branch manifest is labeled asserted, not independently proven.

Same-tick/different-hash observations are displayed together with alternatives. There is no single “current” state when the maximum tick has conflicting candidates. Later ticks and filename patterns never establish descent. A transition across snapshots carries branch uncertainty even when values seem consistent. Disappearance never establishes death; dead→alive can be consistent with resurrection or a different branch unless stronger evidence resolves it.

Deduplicate retained events using campaign-scoped saved event identity plus a canonical semantic payload. All supporting snapshot/path references remain attached. The same event ID with conflicting payload/time creates a conflict set, not an arbitrary winner. Events with no stable ID use a documented content key including participants, raw time and type; ambiguous duplicates are retained with a possible-duplicate label. References match exact normalized IDs, not substrings or names alone.

## 9. Metadata and response shape

The new namespace is `history_schema_version=1.0`; it does not redefine the existing v1 query envelopes. A result includes:

- `catalog`: generation, manifest, freshness policy, versions, compatible-group selection.
- `subject`: stable identity, historical aliases, ambiguity and first/last observation.
- `overview`: observed life/status/location/relationship/role phases with evidence IDs, never unsourced prose.
- `timeline`: deduplicated dated events, state observations, differences and contradiction groups in stable game-time order; unknown-time items have a separate section.
- `context`: related entities and snapshot-level world/map/faction/mod/research metadata, keyed once.
- `coverage`: candidates discovered, duplicate origins, compatible/incompatible/review-needed sources, parsed/failed/excluded counts, per-domain status, earliest/latest observation, unknown clocks and omitted-detail counts.
- `evidence`: content-addressed references with snapshotSHA, exact indexed XML path, record kind, extraction profile, semantic rule and raw-value status.

Field metadata distinguishes observed, derived, ambiguous, absent, explicit-null, unsupported and invalid values; includes units and code/display-label pairs when a label is actually known. Raw def names remain available and invented human-readable translations are forbidden. Numeric record arrays without definition maps are explicitly uninterpreted.

`detail=context` is the one-call default: every distinct supported semantic fact/value across the selected history, all semantic events and phase changes, a full latest dossier (or all latest alternatives), relevant related entities, anomalies, coverage, and evidence IDs. Stable repeated facts are factored into shared records and runs with all snapshot references, including gaps. The factored representation must reconstruct the supported semantic observations for every snapshot; retaining only latest values or selected highlights is forbidden. It must not discard a domain merely because an agent did not ask about it. Unknown raw fields are inventoried by path, structural kind, size and evidence ID; their uninterpreted raw payloads remain available through evidence access and are not described as semantically summarized.

`detail=full` expands every retained semantic observation/difference for the selected pawn and includes raw-evidence references. `get_history_evidence` returns lossless retained nodes with attributes, repeated-child order and text, paginated by stable generation-bound cursor. No source file reopening by the caller is needed. Before implementation, H0 pins a private corpus manifest and pawn cohort covering all H5 cases and all materialized player-faction pawns in the canonical acceptance snapshot. Every cohort pawn must fit the complete default one-call context contract; paging, domain filters or a different fixture cannot substitute for this acceptance.

Use bounded configurable response limits, initially2MiB context,8MiB full,512KiB evidence pages, with an absolute16MiB ceiling for any response. H0 independent volume estimates and H5 measured output determine whether the default context limit must be increased within that ceiling before release. Such calibration cannot remove facts or weaken the one-call assertion; record the final default and rerun affected schema/interface/package checks. If any required cohort result exceeds16MiB after lossless factoring, acceptance is blocked pending an explicit scope decision. Other oversized requests return RESULT_TOO_LARGE with measured required bytes and selectors, never truncated success. Responses include serialized bytes, configured limit, semantic completeness and raw-detail coverage. Legacy limits remain unchanged.

## 10. Implementation architecture

- `history/source.py`: secure bounded streaming extractor, canonical XML locations, immutable source observation, broad pawn/linked-record retention. Reuse hardened source-access primitives, not an unchecked ElementTree whole-file shortcut. Existing parser/query behavior remains regression-tested.
- `history/catalog.py`: discovery manifest, schema migrations, atomic generation build/refresh, SQLite storage and read-only generation opening. Index pawns/events/references by campaign and saved IDs; hash-deduplicate evidence.
- `history/identity.py`: compatibility classifications, normalized IDs, aliases, collision diagnostics and operator assertions.
- `history/chronology.py`: clock conversion, event identity, branch/tie handling, observation comparisons.
- `history/service.py`: dossier, timeline, context factoring, schemas, size limits and evidence access.
- `cli.py` and `mcp_server.py` or a dedicated `history/mcp.py`: thin adapters. MCP lifecycle refreshes the configured cache, pins a generation per query and contains cancellation/errors; no caller-selected source paths or arbitrary writes.

Module details are builder choices within these contracts. New public schemas live under docs/rimworld-save-inspector/history/. Query execution uses parameterized SQL. A malformed/tampered index is rejected with structured errors; database extensions and arbitrary SQL are never enabled. Save content is untrusted data throughout. Result strings cannot grant instructions or permissions.

## 11. Verification gates H0–H7

- **H0 independent baseline:** before product implementation, commit synthetic expectations and separately preserve private raw-XML oracle fixtures/hashes, corpus manifest and acceptance pawn cohort. Pin schema and chronology examples and independently estimate complete factored context volume. Retain baseline and negative-test failures.
- **H1 extraction:** all scopes and known domains, nested holders, references, unknown extensions and limits; demonstrate no lost carried/casket/world-dead pawns. DTD/entities, depth/record/storage bounds, source replacement and cancellation fail safely. No save bytes change.
- **H2 catalog/identity:** deterministic generation, duplicate files, same tick conflicts, unrelated identical names/IDs, mutable names/mods, missing metadata, mixed versions, failed files, atomic refresh and output/input collision. Every discovered candidate has an outcome.
- **H3 chronology/context:** absolute/game/biological/file clocks, unknown/future/pre-arrival times, retained old events, event conflicts/dedup, historical names, ambiguous references, missing≠removed, branch-unproven differences, and exact evidence links.
- **H4 interfaces:** Python/CLI/MCP semantic equivalence; one timeline call without prerequisite per-save calls; strict JSON schemas; automatic new/changed/removed-file refresh, frozen/strict modes, concurrent collection changes, cancellation, stale-generation errors, invalid selectors, cursor binding, result caps and read-only-source boundaries. Legacy interfaces remain compatible.
- **H5 private archive:** discover all compatible originals in the pinned source collection; use independent birth/arrival, death/return, wild-pawn transition, holder migration, role change and accelerated-growth cases. Required one-call results must include previously missed context. Exact population identities/coverage must reconcile; originals rehash unchanged.
- **H6 packaged candidate:** clean wheel/sdist install outside source checkout, full public regression suite, new public tests with zero skipped mandatory cases, source-distribution privacy scan, supported-Python checks, cold-index and warm-query measurements with peak memory. Warm context timeline p95<=2s over30 serial runs per acceptance pawn, including the default unchanged-collection metadata freshness check. Initial index, changed-source refresh and strict full-hash timings are measured separately and reported; their costs cannot be hidden in warm results. No performance threshold can be revised after observing failure without explicit acceptance review.
- **H7 independent review:** fresh reviewer reproduces consequential packaged results, branch and clock negatives, preservation, one-call coverage and privacy. Match every required test/receipt to the final candidate commit and artifacts. Missing evidence or skipped checks do not pass.

Acceptance requirements are enumerated in the companion `2026-10-17-pawn-history-requirements.json`. New code invalidates affected gates; evidence from a prior candidate cannot silently certify a later one. Public publication and host installation/registration are separate actions requiring their own authorization. A locally tested candidate is not a deployed capability.

## 12. Decisions made by the designer

The local SQLite catalog, automatic derived-cache refresh with per-query generation pinning, conservative campaign key, unproven lineage default, losslessly factored context/full split and bounded evidence API are proposed design choices. Automatic freshness and complete semantic context implement the user's requested all-compatible-save, one-call behavior; frozen mode is an explicit reproducibility option. Approval of this design authorizes planning/implementation of these choices; routine implementation decisions within the contracts remain autonomous.
