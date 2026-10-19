# Bounded discovery implementation plan

## Scope and source of truth

Implement deterministic first-pass discovery over the frozen nine-key `HistorySnapshot.as_dict()` mapping. Preserve source IDs, snapshot-scoped records, claim status, evidence references, and clock observations. Use only stable saved IDs and validated campaign identity for cross-snapshot joins. The frozen public cases and `docs/requirements/discovery-acceptance.json` define semantic guardrails; this work does not evaluate private holdouts or claim product acceptance.

## Tasks

1. Add synthetic tests under `tests/discovery/` for exact envelope validation, ID-only joins, clock separation, contradictions, coverage/outcome accounting, and no-pruning ranking. Run each test first and retain its red output under the unique `.superpowers/evidence/discovery-implementation-20261017-01/` directory.
2. Implement `discover_snapshot(mapping, semantic_adapter=None)` and `discover_collection(iterable, source_outcomes, semantic_adapter=None)` in `src/rimworld_save_inspector/discovery/`. Return deterministic JSON-compatible records for entities, events/observations, evidence-backed claims, contradictions, coverage, uncertainty, and evidence use. Preserve every snapshot and source outcome, including low-ranked records and failures.
3. Implement an opt-in offline adapter contract with a versioned rubric, deterministic evidence packet and cache-key builder over source/profile/rubric/model/provider/options. Validate every adapter annotation against packet evidence IDs and source references; keep annotations separate from authoritative source facts.
4. Run `pytest tests/discovery` and the public discovery oracle integrity tests. Record exact commands, outcomes, and limits in the evidence directory. Do not edit the oracle, thresholds, history source, product integration, or private files.

## Review focus

- A repeated pawn name never joins records; stable pawn IDs join only under validated campaign identity, otherwise they remain separate.
- A repeated/ambiguous event stays a separate observation unless its saved event identity is stable and scoped to the same campaign.
- Game ticks, absolute ticks, biological durations, calendar labels, and wall-clock observations stay distinct; unknown time never sorts as zero.
- Different values remain visible as supporting and counterevidence with source references; missing records do not prove death, departure, motive, or causality.
- Ranking never removes a record, and scores are explicitly uncalibrated retrieval hints.
- Offline annotations cannot alter or overwrite claims, coverage, identities, or evidence.

## Boundaries

No network or paid inference, private-original reads, deployment, live save mutation, CLI/MCP changes, history-source changes, or Git/index operations. Report exact files, commands, and limitations to `/root` for CLI/MCP integration.

## Bounded catalog discovery extension

Add `discover_catalog(catalog_path, anchor_snapshot_id=None, freshness="metadata", response_limit=16 MiB, generation_id=None, semantic_adapter=None)` in a separate discovery module. It refreshes only the app-owned index for metadata/strict freshness, otherwise opens one frozen immutable generation through `history.catalog`. Pin the selected generation for every evidence token and follow-up selector.

Read source outcomes and scalar snapshot/index columns with SQL. Stream one pawn or event payload at a time with an explicit decompression cap, plus one separately stored snapshot-context row at a time with its own cap; never decode snapshot payloads or global evidence nodes, or materialize all history snapshots/claims. Keep bounded per-entity candidates and counters, then make a second streaming pass for every membership of a selected fact so selected highlights retain all exact source paths, raw fields/values, indices, and generation-bound evidence tokens. Bound aliases, same-tick branch examples, conflict fields, event occurrence samples, event claim memberships, and art examples, while reporting exact selected/omitted counts. Bundle active/overridden/suppressed/xenotype gene facts as a vector, separate from role labels. Report per-domain selected and omitted occurrence counts, unknown statuses, same-tick alternatives, unproven continuity, every source outcome (including duplicate and failed origins), and total stored record counts.

Select episodes by a deterministic, uncalibrated policy, preserving counterevidence and all selected provenance. Every omitted pawn record remains fetchable with `get_timeline`/`get_dossier`; selected raw evidence opens with `get_evidence`. Omitted or unsampled event rows expose one exact `get_events`/`get_history_events` selector per affected snapshot, including weak-identity snapshots, pinned to the immutable generation. Unknown-clock pawn follow-ups anchor to an observed snapshot; weak-identity pawns use an exact snapshot dossier selector. The first-pass channel is labeled `save_xml_catalog`; verified recorder and defs/annotation channels are explicitly absent, with pre-install recorder questions marked abstain. Stream each snapshot's bounded context row to count and display a few source-linked art examples; every snapshot has a concrete `get_dossier(snapshot_id=...)` follow-up for full tales/messages/logs/art context. The offline adapter sees a deterministic packet with event claim-membership evidence; its `included` ledger is separate from annotation-specific citation use, and citation does not establish entailment. The adapter cannot alter facts; its cache key includes source hashes/IDs, catalog generation, source channel, resolver, rubric, provider/model, prompt version, and options. A five-item skeptic-followup limit is the default and is hard-capped at twenty. Highlight caps describe display selection only and do not delete or prune catalog records. Measure the complete JSON result against `min(response_limit, 16 MiB)` and raise `RESULT_TOO_LARGE` with measured bytes and narrowing selectors if the result cannot fit.

Review cases: 151-snapshot synthetic streaming and bounded decompression; validated six-field campaign joins from real public XML fixtures; same-name different pawn/campaign separation; stable episode variants with reordered claim sets and preserved duplicates; exact evidence-token round-trip; unknown-clock timeline and weak-identity dossier selector round-trips; per-source omitted-event selectors across multiple campaigns and weak snapshots; pinned-generation selector behavior and stale/cross-generation errors; complete origin/record counts including duplicate and failed sources; same-tick alternatives with unknown clocks; bounded event occurrence samples; event-claim evidence packet and annotation-specific use; snapshot-context/art summary with concrete dossier retrieval; cache mutation matrix; and response-limit failure without truncated success.
