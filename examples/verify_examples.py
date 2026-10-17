"""Run public synthetic CLI, Python, and stdio MCP examples against an install."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

import rimworld_save_inspector
from rimworld_save_inspector import QueryRequest, QueryService, __version__, load_snapshot


def run_cli(*args: str) -> dict:
    executable = Path(sys.executable).absolute().parent / "rim-save"
    if not executable.is_file():
        raise RuntimeError("rim-save console entry point was not installed")
    result = subprocess.run(
        [executable, "inspect", *args], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


async def check_mcp(save: Path, expected_page: dict) -> None:
    executable = Path(sys.executable).absolute().parent / "rim-save"
    if not executable.is_file():
        raise RuntimeError("rim-save console entry point was not installed")
    params = StdioServerParameters(
        command=str(executable), args=["mcp", "--save", str(save)]
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            loaded = await session.call_tool("inspect_save", {})
            if loaded.is_error:
                raise RuntimeError(loaded.content)
            snapshot_id = loaded.structured_content["snapshot_id"]
            page = await session.call_tool(
                "list_pawns", {"snapshot_id": snapshot_id, "scope": "map", "limit": 1}
            )
            if page.is_error or page.structured_content != expected_page:
                raise AssertionError("MCP pawn page differs from Python result")
            pawn_id = page.structured_content["data"]["entries"][0]["record_id"]
            apparel = await session.call_tool(
                "get_equipment",
                {"snapshot_id": snapshot_id, "record_id": pawn_id, "holder": "apparel"},
            )
            if apparel.is_error:
                raise RuntimeError(apparel.content)
            assert [row["item"]["item_def"] for row in apparel.structured_content["data"]["entries"]] == ["Jacket"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", type=Path, default=Path(__file__).with_name("minimal_colony.rws"))
    args = parser.parse_args()
    save = args.save.resolve(strict=True)
    package_path = Path(rimworld_save_inspector.__file__).resolve()
    checkout = Path(__file__).resolve().parents[1]
    if package_path.is_relative_to(checkout / "src" / "rimworld_save_inspector"):
        raise RuntimeError("install the package first; this example must exercise the installed distribution")

    snapshot = load_snapshot(save)
    service = QueryService(snapshot)
    summary = service.execute(QueryRequest("summary")).as_dict()
    assert summary["provenance"]["parser_version"] == __version__
    assert summary["data"]["map_count"] == 1

    first = service.execute(QueryRequest("pawns", {"scope": "map"}, limit=1)).as_dict()
    assert first["page"]["total"] == 2 and first["page"]["next_cursor"]
    second = service.execute(
        QueryRequest("pawns", {"scope": "map"}, limit=1, cursor=first["page"]["next_cursor"])
    ).as_dict()
    assert len(first["data"]["entries"]) == len(second["data"]["entries"]) == 1

    pawn_id = first["data"]["entries"][0]["record_id"]
    apparel = service.execute(QueryRequest("equipment", {"record_id": pawn_id, "holder": "apparel"})).as_dict()
    assert [row["item"]["item_def"] for row in apparel["data"]["entries"]] == ["Jacket"]

    cli_page = run_cli(str(save), "pawns", "--scope", "map", "--limit", "1")
    assert cli_page == first
    cli_apparel = run_cli(str(save), "equipment", pawn_id, "--holder", "apparel")
    assert cli_apparel["data"]["entries"][0]["item"]["item_def"] == "Jacket"
    anyio.run(check_mcp, save, first)
    print("Synthetic Python, CLI, pagination, apparel, and stdio MCP examples passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
