# RimWorld Save Inspector

A local, read-only Python inspector for RimWorld XML save files. The normative behavior is defined in `docs/design.md` (RWSI-1.0 revision 2026-10-16.1).

Implementation is in progress. Product gate status is recorded in reproducible evidence directories; documentation validation is not a product gate result.

## Command line

`rim-save inspect SAVE QUERY` loads the save once for that invocation and emits one deterministic JSON object on standard output. Supported query names are `metadata`, `summary`, `pawns`, `pawn RECORD_ID`, `health RECORD_ID`, `genes RECORD_ID`, `equipment RECORD_ID`, `inventory RECORD_ID`, `research`, and `mods`. Paged queries accept `--limit 1..200` and an opaque `--cursor`. Query filters use their documented hyphenated options, including `--scope`, `--map-id`, `--faction`, `--def`, `--name`, `--guest-status`, `--section`, `--group`, `--holder`, `--holder-id`, and `--estimate-missing-as-one`. `--include-source-path` opts into the local absolute source path in provenance; `--verbose` writes progress or unexpected tracebacks to standard error.

Metadata includes `record_totals` for each normalized record type and the aggregate pawn metrics `record_count`, `membership_count`, and `resolved_saved_identity_count`. The aggregate counts cover all supported pawn records and memberships, with unique saved identities counted only when their identity resolves unambiguously.

Successful and domain-error responses are JSON Lines with exit code 0 or 2 respectively. Unexpected failures use exit code 1, and an interrupt uses exit code 130 with a `CANCELLED` error when standard output remains available. `--help` and `--version` print plain text. The optional `rim-save mcp --save SAVE` command forwards to the separately installed MCP server component.

## MCP stdio server

Install the optional SDK extra (`uv sync --extra mcp --group dev` in this checkout), then launch either `rim-save mcp --save SAVE` or `rim-save-mcp --save SAVE`. Both commands run the official MCP 2.3.0 SDK over stdio; stdout is reserved for MCP protocol messages and diagnostics go to stderr. The server declares ten read-only tools: `inspect_save`, `get_colony_summary`, `list_pawns`, `get_pawn`, `get_health`, `get_genes`, `get_equipment`, `get_inventory`, `get_research`, and `get_mods`.

The source path is fixed at launch and is never accepted by a tool. `inspect_save` lazily loads one immutable snapshot. Later calls reuse it for the lifetime of that server process, even if the source file changes; restart the process to inspect changed bytes. The SDK handshake completes before parsing starts. Concurrent calls during the initial load receive `SNAPSHOT_LOADING`, and every other query requires the `snapshot_id` returned by `inspect_save`.

## JSON Schema API

The Draft 2020-12 schema dictionaries are available from `rimworld_save_inspector.schemas`: `SUCCESS_SCHEMAS` maps each query name to its strict response schema, and `ERROR_SCHEMA` describes the shared error envelope. For example, validate a parsed success response with `jsonschema.Draft202012Validator(SUCCESS_SCHEMAS[response["query"]["name"]]).validate(response)`. These Python API exports are the schema distribution contract for v1.

