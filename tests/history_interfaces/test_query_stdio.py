"""Measure the actual development stdio frames for both discovery follow-up routes."""

import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest

from rimworld_save_inspector import __version__
from rimworld_save_inspector.history.catalog import build_catalog


@pytest.mark.parametrize("tool,version_key,version", [
    ("get_history_events", "history_schema_version", "1.0"),
    ("get_history_discovery", "discovery_schema_version", "history-discovery/1.0"),
])
def test_actual_stdio_query_has_one_payload_and_bounded_frame(tmp_path, tool, version_key, version):
    root = Path(__file__).parents[2]
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "alpha.rws").write_bytes(
        (root / "tests/history_oracle/fixtures/alpha-early.xml").read_bytes())
    db = tmp_path / "catalog.sqlite"
    build_catalog(saves, db)
    process = subprocess.Popen(
        [sys.executable, "-m", "rimworld_save_inspector.cli", "history-mcp", "--catalog", str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(root / "src")})
    try:
        def exchange(request):
            process.stdin.write(json.dumps(request).encode() + b"\n")
            process.stdin.flush()
            assert select.select([process.stdout], [], [], 15)[0], "MCP response timeout"
            return process.stdout.readline()

        initialized = exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "discovery-wire-test", "version": "1"}}})
        assert json.loads(initialized)["id"] == 1
        assert json.loads(initialized)["result"]["serverInfo"]["version"] == __version__
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.flush()
        frame = exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": tool, "arguments": {"freshness": "frozen"}}})
    finally:
        process.stdin.close()
        process.wait(timeout=15)
    assert process.returncode == 0, process.stderr.read().decode()
    result = json.loads(frame)["result"]
    assert not result.get("isError", False)
    assert result["structuredContent"][version_key] == version
    assert len(frame) < 16 * 1024 * 1024
    assert len(result["content"]) == 1
    assert len(result["content"][0]["text"]) < 256
