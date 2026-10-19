# Changelog

## 1.1.0b1 — 2026-10-19 (beta)

### Added

- Local save catalogs with immutable generations, source freshness checks and
  explicit campaign compatibility.
- Pawn timelines across compatible saves, snapshot dossiers, historical aliases,
  shared colony context and exact evidence retrieval.
- Saved-event queries and offline discovery with bounded highlights, source
  outcomes, conflicts and follow-up selectors.
- JSON CLI and configured-catalog read-only MCP interfaces for these queries.
- Experimental read-only journal inspection for explicitly supplied journal files.

### Fixed

- Retain pawns nested in passing ships and supported native holders.
- Require exact native holder classes and XML names before assigning native
  semantics; preserve unsupported shapes as raw evidence.
- Read native save-version metadata and reject malformed version strings from
  verified profile selection. History extraction version 7 invalidates earlier
  interpretation fingerprints.
- Report the package version consistently in the CLI, parser provenance and all
  MCP server handshakes.

### Release boundaries

- Existing single-save commands and JSON schema versions are retained.
- History and discovery are beta features. Full private-corpus and original
  performance/aggregate acceptance remain deferred; cold MCP startup can take
  approximately 10–20 seconds on a busy host.
- The Life Recorder mod is excluded from Python distributions and is not ready
  for dependable logging. Its source remains experimental in the repository.
- This is a GitHub prerelease; it does not register MCP servers in a host. See the [version policy](docs/rimworld-save-inspector/versioning.md)
  and [delivery scope](docs/rimworld-save-inspector/delivery-scope.md).

## 1.0.0

- Define the first release line for the local, read-only RimWorld save inspector.
- Provide Python, JSON CLI, and optional MCP stdio interfaces over a shared query API.
- Add public synthetic examples and CI checks for Linux and macOS, including wheel and source-archive installs.
- Preserve explicit distinctions between missing, null, invalid, empty, and derived serialized values.

This changelog describes the release scope. It does not assert that private acceptance gates, external platform runs, or publication have completed.
