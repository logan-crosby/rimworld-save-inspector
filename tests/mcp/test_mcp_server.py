from __future__ import annotations

import json
import select
import shutil
import subprocess
import sys
import threading
import time

import anyio
import pytest
from jsonschema import Draft202012Validator
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from rimworld_save_inspector.mcp_server import create_server
from rimworld_save_inspector import mcp_server


TOOL_NAMES = {
    "inspect_save", "get_colony_summary", "list_pawns", "get_pawn", "get_health",
    "get_genes", "get_equipment", "get_inventory", "get_research", "get_mods",
}


def test_discovery_has_exact_read_only_tools_and_strict_schemas(tmp_path):
    server = create_server(tmp_path / "not-read-until-call.rws")
    tools = anyio.run(server.list_tools)
    assert {tool.name for tool in tools} == TOOL_NAMES
    assert len(tools) == 10
    for tool in tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.open_world_hint is False
        assert tool.input_schema.get("additionalProperties") is False
        assert tool.output_schema.get("additionalProperties") is False
        assert "save_path" not in json.dumps(tool.input_schema)
        assert "path" not in tool.input_schema.get("properties", {})
    async def invoke():
        with pytest.raises(Exception, match="extra fields|Extra inputs"):
            await server.call_tool("inspect_save", {"unexpected": True})
    anyio.run(invoke)


def test_unloaded_query_uses_domain_error_envelope(tmp_path):
    server = create_server(tmp_path / "unused.rws")
    result = anyio.run(server.call_tool, "get_mods", {"snapshot_id": "x"})
    assert result.is_error
    error = json.loads(result.content[0].text)
    assert error["error"]["code"] == "SNAPSHOT_NOT_LOADED"


def test_cli_reports_missing_optional_sdk_as_structured_error():
    root = __import__("pathlib").Path(__file__).parents[2]
    code = '''import sys\nclass BlockMcp:\n    def find_spec(self, fullname, path=None, target=None):\n        if fullname == "mcp" or fullname.startswith("mcp."):\n            raise ModuleNotFoundError("blocked optional MCP SDK", name=fullname)\nsys.meta_path.insert(0, BlockMcp())\nfrom rimworld_save_inspector.cli import main\nraise SystemExit(main(["mcp", "--save", "synthetic.rws"]))\n'''
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert json.loads(result.stdout)["error"]["code"] == "MCP_EXTRA_REQUIRED"


def test_sdk_calls_all_domains_and_returns_structured_and_text_data(tmp_path):
    fixture = __import__("pathlib").Path(__file__).parents[1] / "fixtures/synthetic/seed.rws"
    save = tmp_path / "seed.rws"
    shutil.copyfile(fixture, save)
    server = create_server(save)
    tools_by_name = {tool.name: tool for tool in anyio.run(server.list_tools)}

    async def run():
        loaded = await server.call_tool("inspect_save", {})
        assert not loaded.is_error
        inspect_data = loaded.structured_content
        assert inspect_data["data"]["snapshot_lifetime"] == "process"
        Draft202012Validator(tools_by_name["inspect_save"].output_schema).validate(inspect_data)
        assert json.loads(loaded.content[0].text) == inspect_data
        sid = inspect_data["snapshot_id"]
        pawns = await server.call_tool("list_pawns", {"snapshot_id": sid, "scope": "map"})
        assert not pawns.is_error
        pawn_id = pawns.structured_content["data"]["entries"][0]["record_id"]
        calls = [
            ("get_colony_summary", {"snapshot_id": sid}),
            ("get_pawn", {"snapshot_id": sid, "record_id": pawn_id}),
            ("get_pawn", {"snapshot_id": sid, "record_id": pawn_id, "section": "skills"}),
            ("get_health", {"snapshot_id": sid, "record_id": pawn_id}),
            ("get_genes", {"snapshot_id": sid, "record_id": pawn_id}),
            ("get_equipment", {"snapshot_id": sid, "record_id": pawn_id}),
            ("get_inventory", {"snapshot_id": sid, "record_id": pawn_id}),
            ("get_research", {"snapshot_id": sid}),
            ("get_mods", {"snapshot_id": sid}),
        ]
        for name, args in calls:
            result = await server.call_tool(name, args)
            assert not result.is_error, (name, result.content)
            assert result.structured_content["schema_version"] == "1.0"
            Draft202012Validator(tools_by_name[name].output_schema).validate(result.structured_content)
            assert json.loads(result.content[0].text) == result.structured_content
        cli = subprocess.run(
            [sys.executable, "-m", "rimworld_save_inspector.cli", "inspect", str(save), "pawns",
             "--scope", "map", "--limit", "1"],
            cwd=str(__import__("pathlib").Path(__file__).parents[2]),
            capture_output=True, text=True, timeout=8, check=True,
        )
        mcp_page = await server.call_tool("list_pawns", {"snapshot_id": sid, "scope": "map", "limit": 1})
        assert json.loads(cli.stdout) == mcp_page.structured_content
        mismatch = await server.call_tool("get_mods", {"snapshot_id": "sha256:other"})
        assert mismatch.is_error
        assert json.loads(mismatch.content[0].text)["error"]["code"] == "SNAPSHOT_MISMATCH"
    anyio.run(run)
    server._rim_save_lifecycle.close()


def test_real_stdio_initialize_call_shutdown_has_protocol_only_stdout(tmp_path):
    fixture = __import__("pathlib").Path(__file__).parents[1] / "fixtures/synthetic/seed.rws"
    save = tmp_path / "stdio-seed.rws"
    shutil.copyfile(fixture, save)

    async def run():
        commands = [
            (sys.executable, ["-m", "rimworld_save_inspector.cli", "mcp", "--save", str(save)]),
            (str(__import__("pathlib").Path(__file__).parents[2] / ".venv/bin/rim-save-mcp"), ["--save", str(save)]),
        ]
        for command, args in commands:
            params = StdioServerParameters(command=command, args=args,
                cwd=str(__import__("pathlib").Path(__file__).parents[2]))
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream, read_timeout_seconds=8) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == TOOL_NAMES
                    result = await session.call_tool("inspect_save", {})
                    assert not result.is_error
                    assert result.structured_content["data"]["snapshot_lifetime"] == "process"
                    assert json.loads(result.content[0].text) == result.structured_content
            # stdio_client terminates and reaps the child when its context exits.
    anyio.run(run)


def test_concurrent_first_load_is_single_worker_and_ready_snapshot_is_pinned(tmp_path, monkeypatch):
    fixture = __import__("pathlib").Path(__file__).parents[1] / "fixtures/synthetic/seed.rws"
    save = tmp_path / "pinned.rws"
    shutil.copyfile(fixture, save)
    original_load = mcp_server.load_snapshot
    started = threading.Event()
    release = threading.Event()
    calls = []

    def delayed_load(path, **kwargs):
        calls.append(threading.current_thread().ident)
        started.set()
        assert release.wait(5)
        return original_load(path, **kwargs)

    monkeypatch.setattr(mcp_server, "load_snapshot", delayed_load)
    server = create_server(save)
    lifecycle = server._rim_save_lifecycle
    first_result = {}

    async def first_call():
        first_result["result"] = await server.call_tool("inspect_save", {})

    async def run():
        async with anyio.create_task_group() as group:
            group.start_soon(first_call)
            assert await anyio.to_thread.run_sync(started.wait, 2)
            second = await server.call_tool("inspect_save", {})
            assert second.is_error
            assert json.loads(second.content[0].text)["error"]["code"] == "SNAPSHOT_LOADING"
            during = await server.call_tool("get_mods", {"snapshot_id": "pending"})
            assert json.loads(during.content[0].text)["error"]["code"] == "SNAPSHOT_LOADING"
            release.set()
            worker = lifecycle.worker
            assert worker is not None
            await anyio.to_thread.run_sync(worker.join, 5)
        assert not first_result["result"].is_error
    anyio.run(run)
    assert len(calls) == 1
    ready = anyio.run(server.call_tool, "inspect_save", {})
    sid = ready.structured_content["snapshot_id"]
    save.write_bytes(b"changed after ready")
    again = anyio.run(server.call_tool, "inspect_save", {})
    assert again.structured_content["snapshot_id"] == sid
    assert len(calls) == 1
    lifecycle.close()
    changed = fixture.read_bytes().replace(b"<seedString>fixture</seedString>", b"<seedString>fixture-new</seedString>")
    save.write_bytes(changed)
    restarted = create_server(save)
    fresh = anyio.run(restarted.call_tool, "inspect_save", {})
    assert not fresh.is_error
    assert fresh.structured_content["snapshot_id"] != sid
    restarted._rim_save_lifecycle.close()


def test_request_cancellation_cancels_and_joins_owned_loader(tmp_path, monkeypatch):
    started = threading.Event()

    def cancellable_load(path, *, cancel=None, **kwargs):
        started.set()
        while cancel is not None and not cancel.wait(0.01):
            pass
        raise mcp_server.InspectorError("CANCELLED", "cancelled by test")

    monkeypatch.setattr(mcp_server, "load_snapshot", cancellable_load)
    server = create_server(tmp_path / "synthetic-only.rws")
    lifecycle = server._rim_save_lifecycle

    async def run():
        with anyio.move_on_after(0.2) as scope:
            await server.call_tool("inspect_save", {})
            scope.cancel()
        assert scope.cancel_called
    cancel_test_started = time.monotonic()
    anyio.run(run)
    assert time.monotonic() - cancel_test_started < 2
    assert started.is_set()
    worker = lifecycle.worker
    assert worker is not None
    worker.join(2)
    assert not worker.is_alive()
    assert lifecycle.state == lifecycle.EMPTY
    assert not [thread for thread in threading.enumerate() if thread.name == "rim-save-mcp-loader"]
    lifecycle.close()


def test_stdio_disconnect_during_load_reaps_worker_and_process(tmp_path):
    marker = tmp_path / "loader-started"
    fixture = __import__("pathlib").Path(__file__).parents[1] / "fixtures/synthetic/seed.rws"
    save = tmp_path / "disconnect.rws"
    shutil.copyfile(fixture, save)
    # Keep the delay and marker in this synthetic subprocess only. EOF invokes the
    # real server shutdown path, which signals and joins its owned parser worker.
    code = f'''import asyncio, sys, threading\nfrom pathlib import Path\nfrom rimworld_save_inspector import mcp_server\ndef delayed_load(path, *, cancel=None, **kwargs):\n    Path({str(marker)!r}).write_text("started")\n    while cancel is not None and not cancel.wait(0.01):\n        pass\n    raise mcp_server.InspectorError("CANCELLED", "disconnect test")\nmcp_server.load_snapshot = delayed_load\nserver = mcp_server.create_server({str(save)!r})\nasyncio.run(mcp_server._run(server))\n'''
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(__import__("pathlib").Path(__file__).parents[2]),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert proc.stdin is not None and proc.stdout is not None
    try:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "disconnect-test", "version": "1"}}}
        proc.stdin.write(json.dumps(init) + "\n")
        proc.stdin.flush()
        ready, _, _ = select.select([proc.stdout], [], [], 8)
        assert ready, "server did not complete protocol initialization"
        assert json.loads(proc.stdout.readline())["id"] == 1
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "inspect_save", "arguments": {}}}) + "\n")
        proc.stdin.flush()
        deadline = __import__("time").monotonic() + 3
        while not marker.exists() and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.01)
        assert marker.exists(), "load worker did not start before client disconnect"
        proc.stdin.close()
        cleanup_started = __import__("time").monotonic()
        proc.wait(timeout=5)
        cleanup_seconds = __import__("time").monotonic() - cleanup_started
        assert cleanup_seconds < 5
        stdout_tail = proc.stdout.read()
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        for line in stdout_tail.splitlines():
            json.loads(line)
        assert proc.returncode == 0, stderr
    finally:
        if proc.poll() is None:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

