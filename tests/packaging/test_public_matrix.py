"""T34 public synthetic/CLI/MCP subprocess smoke test for each interpreter cell."""

from __future__ import annotations

import anyio
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/synthetic/seed.rws"


def _orchestration_timeout(seconds: int) -> int:
    """Allow bounded startup/request headroom for explicit QEMU TCG cells."""
    try:
        scale = float(os.environ.get("RWSI_TCG_TIMEOUT_SCALE", "1"))
    except ValueError as exc:
        raise AssertionError("RWSI_TCG_TIMEOUT_SCALE must be a number") from exc
    assert math.isfinite(scale) and 1 <= scale <= 10
    return math.ceil(seconds * scale)


def test_public_synthetic_json_cli_and_real_mcp_subprocess(tmp_path):
    save = tmp_path / "public-seed.rws"
    shutil.copyfile(FIXTURE, save)
    base = [sys.executable, "-m", "rimworld_save_inspector.cli", "inspect", str(save)]

    metadata = subprocess.run([*base, "metadata"], cwd=ROOT, capture_output=True, text=True,
                              timeout=_orchestration_timeout(15))
    assert metadata.returncode == 0, metadata.stderr
    identity = json.loads(metadata.stdout)
    assert identity["provenance"]["parser_version"]
    print("MATRIX_PROFILE=" + json.dumps({
        "parser_version": identity["provenance"]["parser_version"],
        "profile_version": identity["provenance"]["profile_version"],
        "interpretation_id": identity["provenance"]["interpretation_id"],
    }, sort_keys=True))

    pawns = subprocess.run([*base, "pawns", "--scope", "map"], cwd=ROOT,
                           capture_output=True, text=True, timeout=_orchestration_timeout(15))
    assert pawns.returncode == 0, pawns.stderr
    pawn_id = json.loads(pawns.stdout)["data"]["entries"][0]["record_id"]
    queries = [
        ("summary",), ("pawns", "--scope", "map"), ("pawn", pawn_id),
        ("pawn", pawn_id, "--section", "skills"), ("pawn", pawn_id, "--section", "traits"),
        ("health", pawn_id), ("genes", pawn_id), ("equipment", pawn_id),
        ("inventory", pawn_id), ("research",), ("mods",),
    ]
    for query in queries:
        result = subprocess.run([*base, *query], cwd=ROOT, capture_output=True, text=True,
                                timeout=_orchestration_timeout(15))
        assert result.returncode == 0, (query, result.stderr, result.stdout)
        assert isinstance(json.loads(result.stdout), dict)

    async def run_mcp():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "rimworld_save_inspector.cli", "mcp", "--save", str(save)],
            cwd=str(ROOT),
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=_orchestration_timeout(15)) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert len(listed.tools) == 10
                loaded = await session.call_tool("inspect_save", {})
                assert not loaded.is_error
                assert loaded.structured_content["data"]["snapshot_lifetime"] == "process"
                snapshot_id = loaded.structured_content["snapshot_id"]
                mcp_pawns = await session.call_tool("list_pawns", {"snapshot_id": snapshot_id, "scope": "map"})
                pawn_record_id = mcp_pawns.structured_content["data"]["entries"][0]["record_id"]
                calls = [
                    ("get_colony_summary", {"snapshot_id": snapshot_id}),
                    ("list_pawns", {"snapshot_id": snapshot_id, "scope": "map"}),
                    ("get_pawn", {"snapshot_id": snapshot_id, "record_id": pawn_record_id}),
                    ("get_health", {"snapshot_id": snapshot_id, "record_id": pawn_record_id}),
                    ("get_genes", {"snapshot_id": snapshot_id, "record_id": pawn_record_id}),
                    ("get_equipment", {"snapshot_id": snapshot_id, "record_id": pawn_record_id}),
                    ("get_inventory", {"snapshot_id": snapshot_id, "record_id": pawn_record_id}),
                    ("get_research", {"snapshot_id": snapshot_id}),
                    ("get_mods", {"snapshot_id": snapshot_id}),
                ]
                for tool_name, arguments in calls:
                    result = await session.call_tool(tool_name, arguments)
                    assert not result.is_error, (tool_name, result.content)
                    assert result.structured_content is not None, tool_name

                cli_page = subprocess.run(
                    [*base, "pawns", "--scope", "map", "--limit", "1"],
                    cwd=ROOT, capture_output=True, text=True,
                    timeout=_orchestration_timeout(15),
                )
                assert cli_page.returncode == 0, cli_page.stderr
                mcp_page = await session.call_tool(
                    "list_pawns", {"snapshot_id": snapshot_id, "scope": "map", "limit": 1},
                )
                assert json.loads(cli_page.stdout) == mcp_page.structured_content

    anyio.run(run_mcp)
