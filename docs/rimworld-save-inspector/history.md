# Pawn histories and first-pass discovery

The history catalog reads selected saves and keeps an application-owned SQLite
cache. It never modifies saves or journals. The MCP server accepts selectors
within its configured catalog; callers cannot supply another source path.

```sh
rim-save catalog build /path/to/saves --output /path/to/cache.sqlite
rim-save discover /path/to/cache.sqlite
rim-save timeline /path/to/cache.sqlite --pawn-id Human123
rim-save events /path/to/cache.sqlite --saved-event-id tale:Tale:Tale_1
rim-save history-mcp --catalog /path/to/cache.sqlite
```

`discover` / `get_history_discovery` provides an offline first pass without a pawn
or field selector. It returns domain highlights, entities, episodes, conflicts,
source outcomes, an evidence-use ledger and bounded follow-up descriptors. The
response explicitly counts omitted highlights. Highlights do not replace the
complete default pawn timeline. Source channels remain separate, and save-only
discovery abstains from claims about recorder coverage before installation.
Optional semantic annotations are separate from source facts. The MCP interface
does not accept remote adapters or credentials.

`timeline` / `get_pawn_timeline` returns the selected pawn's supported observations
across compatible snapshots. Names are historical aliases, not identity keys.
Unknown clocks remain unknown; same-tick alternatives and unproven ancestry are
explicit. `dossier` / `get_pawn_dossier` selects one snapshot. Retained evidence
is available through `evidence` / `get_history_evidence` without reopening saves.

`events` / `get_history_events` retrieves full normalized event occurrences,
including records with no linked participant. Exact `snapshot_id` filters select
their own campaign even when observation clocks are unknown. An explicit anchor
from a different campaign is rejected. `saved_event_id` filters preserve every
source occurrence and conflicting payload. Pages sort by snapshot digest and
event index for stable retrieval; this order does not assert chronology.

Event pages default to 50 records and permit 1–500. Cursors bind the catalog
namespace, immutable generation and selectors. A continuation reads its original
generation after a refresh. Change the selectors by starting a new query.
An oversized response returns `RESULT_TOO_LARGE` with measured bytes and selectors;
it never returns truncated success. CLI and actual MCP transport have a 16 MiB
absolute response ceiling. The default skeptic follow-up limit is five, with a
hard maximum of twenty. `get_history_schema` describes the JSON contracts.

Metadata freshness checks run by default. `--freshness strict` rehashes sources;
`--freshness frozen` reads an existing generation. `--generation-id` selects a
retained generation explicitly. Indexing, refresh and warm-query timing are
measured separately during acceptance.

The current delivery includes save inspection, history catalogs and offline
discovery. The experimental recorder is deferred. Complete private-corpus
acceptance and the original aggregate G/H/L/A release gates are not certified by
the bounded delivery checks; see [delivery scope](delivery-scope.md).

Author: Codex thread `01a14622-b914-7c81-b9ff-0e08d6436c93`.
