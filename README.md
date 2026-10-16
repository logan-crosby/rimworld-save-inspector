# RimWorld Save Inspector

A local, read-only Python inspector for RimWorld XML save files. The normative behavior is defined in `docs/design.md` (RWSI-1.0 revision 2026-10-16.1).

Implementation is in progress. Product gate status is recorded in reproducible evidence directories; documentation validation is not a product gate result.

## Command line

`rim-save inspect SAVE QUERY` loads the save once for that invocation and emits one deterministic JSON object on standard output. Supported query names are `metadata`, `summary`, `pawns`, `pawn RECORD_ID`, `health RECORD_ID`, `genes RECORD_ID`, `equipment RECORD_ID`, `inventory RECORD_ID`, `research`, and `mods`. Paged queries accept `--limit 1..200` and an opaque `--cursor`. Query filters use their documented hyphenated options, including `--scope`, `--map-id`, `--faction`, `--def`, `--name`, `--guest-status`, `--section`, `--group`, `--holder`, `--holder-id`, and `--estimate-missing-as-one`. `--include-source-path` opts into the local absolute source path in provenance; `--verbose` writes progress or unexpected tracebacks to standard error.

Metadata includes `record_totals` for each normalized record type and the aggregate pawn metrics `record_count`, `membership_count`, and `resolved_saved_identity_count`. The aggregate counts cover all supported pawn records and memberships, with unique saved identities counted only when their identity resolves unambiguously.

Successful and domain-error responses are JSON Lines with exit code 0 or 2 respectively. Unexpected failures use exit code 1, and an interrupt uses exit code 130 with a `CANCELLED` error when standard output remains available. `--help` and `--version` print plain text. The optional `rim-save mcp --save SAVE` command forwards to the separately installed MCP server component.

## JSON Schema API

The Draft 2020-12 schema dictionaries are available from `rimworld_save_inspector.schemas`: `SUCCESS_SCHEMAS` maps each query name to its strict response schema, and `ERROR_SCHEMA` describes the shared error envelope. For example, validate a parsed success response with `jsonschema.Draft202012Validator(SUCCESS_SCHEMAS[response["query"]["name"]]).validate(response)`. These Python API exports are the schema distribution contract for v1.

