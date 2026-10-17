# RimWorld Save Inspector

RimWorld Save Inspector reads a local RimWorld XML save and returns bounded, structured JSON. The same query contract is available through Python, the `rim-save` command, and an optional read-only MCP stdio server. It does not modify saves or contact a remote service. See the [normative contract](docs/design.md) for field definitions and serialized-data rules.

## Install

Python 3.11 or newer is required. When the public GitHub v1.0.0 release is available, install its wheel or source archive directly:

```sh
python -m pip install "https://github.com/logan-crosby/rimworld-save-inspector/releases/download/v1.0.0/rimworld_save_inspector-1.0.0-py3-none-any.whl"
# or
python -m pip install "https://github.com/logan-crosby/rimworld-save-inspector/releases/download/v1.0.0/rimworld_save_inspector-1.0.0.tar.gz"
```

The project can also be installed from a source checkout:

```sh
git clone https://github.com/logan-crosby/rimworld-save-inspector.git
cd rimworld-save-inspector
python -m pip install .
# Optional MCP stdio server:
python -m pip install '.[mcp]'
```

The repository's locked development environment uses `uv sync --frozen --extra dev --extra mcp`. The MCP SDK is optional for CLI and Python users; install the `[mcp]` extra to run the server. The v1 release is distributed through GitHub; this project does not claim a PyPI package. The repository, release assets, and CI results should be checked at their public GitHub pages after publication; they are not established by this documentation alone.

## Quick start: command line

The CLI takes a save path and a query name. It writes one JSON response to stdout and diagnostics to stderr.

```sh
rim-save inspect /path/to/colony.rws summary
rim-save inspect /path/to/colony.rws pawns --scope map --limit 20
rim-save inspect /path/to/colony.rws pawn PAWN_RECORD_ID --section core
rim-save inspect /path/to/colony.rws health PAWN_RECORD_ID --limit 20
rim-save inspect /path/to/colony.rws genes PAWN_RECORD_ID --group xenogenes
rim-save inspect /path/to/colony.rws equipment PAWN_RECORD_ID --holder apparel
rim-save inspect /path/to/colony.rws inventory PAWN_RECORD_ID --limit 20
rim-save inspect /path/to/colony.rws research --section progress
rim-save inspect /path/to/colony.rws mods --limit 20
```

Paged queries default to 50 results and allow limits from 1 to 200. Use the returned `page.next_cursor` with the same query, filters, and limit to request the next page. The `rim-save inspect SAVE QUERY --help` form documents query-specific options. `--include-source-path` opts into adding the local absolute path to provenance; it is omitted by default.

## Python API

`load_snapshot` reads the explicitly supplied save into one immutable snapshot. `QueryService` runs the same queries as the CLI and MCP tools.

```python
from rimworld_save_inspector import QueryRequest, QueryService, load_snapshot

snapshot = load_snapshot("/path/to/colony.rws")
service = QueryService(snapshot)

summary = service.execute(QueryRequest("summary")).as_dict()
pawns_page = service.execute(
    QueryRequest("pawns", {"scope": "map"}, limit=20)
).as_dict()

first_pawn = pawns_page["data"]["entries"][0]
apparel = service.execute(
    QueryRequest("equipment", {"record_id": first_pawn["record_id"], "holder": "apparel"})
).as_dict()

print(summary["data"]["map_count"])
print(apparel["data"]["entries"])
```

To continue a page, pass `cursor=pawns_page["page"]["next_cursor"]` in a new `QueryRequest` with the same query, effective filters, and limit. JSON response schemas are available from `rimworld_save_inspector.schemas`; use a Draft 2020-12 validator if you need runtime validation. `jsonschema` is a development dependency, not required at runtime.

## Read-only MCP stdio server

Install the optional extra, then start a local stdio server bound to one save:

```sh
python -m pip install 'rimworld-save-inspector[mcp]'
rim-save mcp --save /path/to/colony.rws
# Equivalent entry point:
rim-save-mcp --save /path/to/colony.rws
```

The server exposes ten read-only tools: `inspect_save`, `get_colony_summary`, `list_pawns`, `get_pawn`, `get_health`, `get_genes`, `get_equipment`, `get_inventory`, `get_research`, and `get_mods`. First call `inspect_save`; pass its `snapshot_id` to later calls. For apparel, call `get_equipment` with `holder: "apparel"`. Paged tools accept `limit` and `cursor`.

An illustrative MCP client configuration is:

```json
{
  "mcpServers": {
    "rimworld-save-inspector": {
      "command": "rim-save",
      "args": ["mcp", "--save", "/path/to/colony.rws"]
    }
  }
}
```

Use the MCP client's configuration format and environment-specific executable path if they differ. The save path is fixed when the server starts and is never accepted as a tool argument. The process loads one immutable snapshot on the first `inspect_save` call and uses it until exit. Restart the server to read changed save bytes. Stdout carries MCP protocol messages; diagnostics go to stderr.

## Serialized data and unknown values

The inspector reports what the save serializes. It cannot establish runtime health, whether a saved mod is installed or compatible, whether a pawn is alive in the running game, or whether the save loads in a particular game installation. It does not load game or mod definitions. Claims that depend on those sources remain unknown.

Missing, explicit-null, invalid, empty, and derived values are distinct. An unavailable collection has an unknown total; it is not an empty collection or zero. Query totals describe their documented population, such as direct map records, pawn memberships, or unique saved identities; those measures are not interchangeable. Inventory record counts and stack-unit estimates are also separate. A response can succeed while reporting partial coverage or diagnostics.

## Compatibility and release verification

The initial parser profile targets RimWorld 1.6. Other game versions are best-effort and may return partial coverage. The program reads plain XML `.rws` files only. It does not open compressed archives, install or inspect game files, fetch definitions, make runtime network requests, launch RimWorld, change settings, or write to saves. Windows support is not claimed.

The public CI workflow is configured for Linux x86_64 and macOS arm64 with Python 3.11 and 3.14. It runs selected synthetic tests and exercises both wheel and source-archive installs. CI demonstrates those public synthetic checks only. Private-fixture acceptance, all release gates, full platform evidence, independent review, and final release publication are separate checks; a local example or green public workflow does not establish them. Until the workflow runs on the public repository, its result is unverified.

The repository includes a small synthetic save and a runnable end-to-end example. Install the project with its MCP extra first:

```sh
python -m pip install '.[mcp]'
python examples/verify_examples.py
```

The script checks the Python API, CLI, paging, apparel selection, and an actual MCP stdio exchange. It does not use or require a real colony save.

## Troubleshooting

- `MCP_EXTRA_REQUIRED`: install `rimworld-save-inspector[mcp]` in the same environment that runs `rim-save`.
- `SOURCE_CHANGED`: the file changed during a read. Wait for a stable copy, load a new snapshot, and restart the query.
- `INVALID_CURSOR`: cursors belong to one snapshot, query, filter set, and page size. Start again at the first page with the same request options.
- `SNAPSHOT_NOT_LOADED`: call `inspect_save` before another MCP tool.
- `SNAPSHOT_MISMATCH`: use the `snapshot_id` returned by that server process, or restart and inspect again.
- `SNAPSHOT_LOADING`: the initial MCP load is still in progress; retry after it finishes.
- Exit code 2 indicates an input, argument, or domain error; inspect the JSON `error.code` and `error.details`. Exit code 1 indicates an unexpected internal failure; retain stderr for diagnosis. Exit code 130 indicates interruption.

By default, provenance identifies the source by basename. CLI `--include-source-path` deliberately reveals its absolute path; consider that before sharing output. MCP tool calls never disclose the configured path. Do not publish save contents or query results unless you have reviewed them for private colony data.

## Changelog and license

See [CHANGELOG.md](CHANGELOG.md) for release notes. This project is licensed under the [MIT License](LICENSE).
