from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
import uuid

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from rimworld_save_inspector import mcp_server
from rimworld_save_inspector.errors import InspectorError
from rimworld_save_inspector.models import QueryRequest
from rimworld_save_inspector.service import QueryService
from rimworld_save_inspector.source import load_snapshot
from rimworld_save_inspector.service import record_id_for_holder


ROOT = Path(__file__).parents[2]
FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic/seed.rws"


def _copy_fixture(tmp_path: Path, name: str = "seed.rws") -> Path:
    destination = tmp_path / name
    shutil.copyfile(FIXTURE, destination)
    return destination


def _success(result):
    assert not result.is_error, result.content
    return result.structured_content


def _error_code(result):
    assert result.is_error
    envelope = json.loads(result.content[0].text)
    assert "error" in envelope
    return envelope["error"]["code"]


def test_mcp_errors_cover_wrong_snapshot_cursor_invalid_values_and_failed_load_recovery(tmp_path):
    save = _copy_fixture(tmp_path)
    server = mcp_server.create_server(save)
    lifecycle = server._rim_save_lifecycle
    try:
        assert _error_code(anyio.run(server.call_tool, "get_mods", {"snapshot_id": "before-load"})) == "SNAPSHOT_NOT_LOADED"
        loaded = _success(anyio.run(server.call_tool, "inspect_save", {}))
        sid = loaded["snapshot_id"]
        assert _error_code(anyio.run(server.call_tool, "get_mods", {"snapshot_id": "sha256:wrong"})) == "SNAPSHOT_MISMATCH"
        pawns = _success(anyio.run(server.call_tool, "list_pawns", {"snapshot_id": sid, "limit": 1}))
        assert _error_code(anyio.run(server.call_tool, "list_pawns", {
            "snapshot_id": sid, "limit": 1, "cursor": "not-a-valid-cursor"
        })) == "INVALID_CURSOR"
        with pytest.raises(Exception, match="limit|greater than or equal"):
            anyio.run(server.call_tool, "list_pawns", {"snapshot_id": sid, "limit": 0})
        with pytest.raises(Exception, match="Extra inputs|extra fields"):
            anyio.run(server.call_tool, "get_mods", {"snapshot_id": sid, "path": str(save)})
        assert pawns["snapshot_id"] == sid
    finally:
        lifecycle.close()

    save.write_bytes(b"<savegame><game>")
    recovering = mcp_server.create_server(save)
    try:
        failed = anyio.run(recovering.call_tool, "inspect_save", {})
        assert _error_code(failed) == "MALFORMED_XML"
        worker = recovering._rim_save_lifecycle.worker
        assert worker is not None
        worker.join(timeout=2)
        assert not worker.is_alive()
        shutil.copyfile(FIXTURE, save)
        recovered = _success(anyio.run(recovering.call_tool, "inspect_save", {}))
        assert recovered["snapshot_id"].startswith("sha256:")
    finally:
        recovering._rim_save_lifecycle.close()


def _cli_args(query: str, filters: dict, limit=None, cursor=None) -> list[str]:
    alias = {"inspect": "metadata"}
    args = ["inspect", "{save}", alias.get(query, query)]
    if query in {"pawn", "health", "genes", "equipment", "inventory"}:
        args.append(filters["record_id"])
    flags = {
        "scope": "--scope", "map_id": "--map-id", "faction": "--faction", "pawn_def": "--def",
        "name": "--name", "guest_status": "--guest-status", "section": "--section",
        "project_def": "--project-def",
        "group": "--group", "holder": "--holder", "holder_id": "--holder-id",
    }
    for key, flag in flags.items():
        if key in filters:
            args.extend([flag, str(filters[key])])
    if filters.get("estimate_missing_as_one"):
        args.append("--estimate-missing-as-one")
    if limit is not None:
        args.extend(["--limit", str(limit)])
    if cursor is not None:
        args.extend(["--cursor", cursor])
    return args


def _canonical(value: dict) -> dict:
    # The contract allows just this inspect-only transport lifetime difference.
    result = json.loads(json.dumps(value))
    result.get("data", {}).pop("snapshot_lifetime", None)
    return result


def test_service_cli_and_mcp_match_for_all_domains_selectors_and_paging(tmp_path):
    save = _copy_fixture(tmp_path)
    snapshot = load_snapshot(save)
    service = QueryService(snapshot, snapshot_lifetime="process")
    server = mcp_server.create_server(save)
    try:
        inspect = _success(anyio.run(server.call_tool, "inspect_save", {}))
        sid = inspect["snapshot_id"]
        summary = _success(anyio.run(server.call_tool, "get_colony_summary", {"snapshot_id": sid}))
        map_id = summary["data"]["maps"][0]["map_id"]
        roster = _success(anyio.run(server.call_tool, "list_pawns", {"snapshot_id": sid, "scope": "all_supported", "limit": 1}))
        pawn_id = roster["data"]["entries"][0]["record_id"]
        research_rows = service.execute(QueryRequest("research", {"section": "progress"})).data["entries"]
        project_def = research_rows[0]["project_def"] if research_rows else ""
        pawn_record = next(record for record in snapshot.records if record.record_id == pawn_id)
        holder_id = record_id_for_holder(pawn_record.source_path + "/inventory[1]/innerContainer[1]/innerList[1]")
        requests: list[tuple[str, str, dict, int | None, str | None]] = [
            ("inspect_save", "inspect", {}, None, None),
            ("get_colony_summary", "summary", {"map_id": map_id}, None, None),
        ]
        for scope in ("map", "world_alive", "world_dead", "world_mothballed", "held", "all_supported"):
            requests.append(("list_pawns", "pawns", {"scope": scope}, 1, None))
        requests.extend([
            ("list_pawns", "pawns", {"scope": "map", "map_id": map_id}, 1, None),
            ("list_pawns", "pawns", {"scope": "map", "name": "unlikely-name"}, 1, None),
            ("get_pawn", "pawn", {"record_id": pawn_id, "section": "core"}, None, None),
            ("get_pawn", "pawn", {"record_id": pawn_id, "section": "skills"}, 1, None),
            ("get_pawn", "pawn", {"record_id": pawn_id, "section": "traits"}, 1, None),
            ("get_health", "health", {"record_id": pawn_id}, 1, None),
            ("get_genes", "genes", {"record_id": pawn_id, "group": "endogenes"}, 1, None),
            ("get_genes", "genes", {"record_id": pawn_id, "group": "xenogenes"}, 1, None),
            ("get_equipment", "equipment", {"record_id": pawn_id, "holder": "equipment"}, 1, None),
            ("get_equipment", "equipment", {"record_id": pawn_id, "holder": "apparel"}, 1, None),
            ("get_inventory", "inventory", {"record_id": pawn_id, "estimate_missing_as_one": True}, 1, None),
            ("get_research", "research", {"section": "techprints"}, 1, None),
            ("get_mods", "mods", {}, 1, None),
        ])
        requests.append(("get_research", "research", {"section": "progress", "project_def": project_def}, 1, None))
        if holder_id is not None:
            requests.append(("get_inventory", "inventory", {"record_id": pawn_id, "holder_id": holder_id}, 1, None))

        cli_mismatches = []
        for tool_name, query_name, filters, limit, cursor in requests:
            tool_args = dict(filters)
            if tool_name != "inspect_save":
                tool_args["snapshot_id"] = sid
            if limit is not None:
                tool_args["limit"] = limit
            if cursor is not None:
                tool_args["cursor"] = cursor
            mcp_response = _success(anyio.run(server.call_tool, tool_name, tool_args))
            cli_args = _cli_args(query_name, filters, limit, cursor)
            cli_args[1] = str(save)
            cli_run = subprocess.run([sys.executable, "-m", "rimworld_save_inspector.cli", *cli_args],
                                     cwd=ROOT, capture_output=True, text=True, timeout=8)
            assert cli_run.returncode == 0, (cli_args, cli_run.stdout, cli_run.stderr)
            cli_response = json.loads(cli_run.stdout)
            service_response = service.execute(QueryRequest(query_name, filters, limit=limit, cursor=cursor)).as_dict()
            assert _canonical(mcp_response) == _canonical(service_response), ("MCP/service", tool_name, filters)
            if _canonical(cli_response) != _canonical(service_response):
                cli_mismatches.append({"tool": tool_name, "filters": filters,
                                       "cli_query": cli_response.get("query"), "service_query": service_response.get("query"),
                                       "cli_total": cli_response.get("page", {}).get("total"),
                                       "service_total": service_response.get("page", {}).get("total")})

        # Follow a real cursor and compare the second page through all three surfaces.
        first = _success(anyio.run(server.call_tool, "list_pawns", {"snapshot_id": sid, "scope": "all_supported", "limit": 1}))
        next_cursor = first["page"]["next_cursor"]
        if next_cursor:
            follow = _success(anyio.run(server.call_tool, "list_pawns", {
                "snapshot_id": sid, "scope": "all_supported", "limit": 1, "cursor": next_cursor
            }))
            cli_args = _cli_args("pawns", {"scope": "all_supported"}, 1, next_cursor)
            cli_args[1] = str(save)
            cli_page = subprocess.run([sys.executable, "-m", "rimworld_save_inspector.cli", *cli_args],
                                      cwd=ROOT, capture_output=True, text=True, timeout=8, check=True)
            service_page = service.execute(QueryRequest("pawns", {"scope": "all_supported"}, limit=1, cursor=next_cursor)).as_dict()
            assert _canonical(follow) == _canonical(service_page)
            if _canonical(json.loads(cli_page.stdout)) != _canonical(service_page):
                cli_mismatches.append({"tool": "list_pawns", "filters": {"scope": "all_supported", "cursor": next_cursor},
                                       "cli_query": json.loads(cli_page.stdout).get("query"),
                                       "service_query": service_page.get("query")})
        assert cli_mismatches == [], f"CLI/service parity differences: {cli_mismatches}"
    finally:
        server._rim_save_lifecycle.close()


def test_cancellation_before_parser_entry_reaps_worker(tmp_path, monkeypatch):
    entered = threading.Event()
    observed_cancel = threading.Event()

    def blocked_before_parse(path, *, cancel=None, **kwargs):
        entered.set()
        assert cancel is not None and cancel.wait(3)
        observed_cancel.set()
        raise InspectorError("CANCELLED", "cancelled before parse")

    monkeypatch.setattr(mcp_server, "load_snapshot", blocked_before_parse)
    server = mcp_server.create_server(_copy_fixture(tmp_path))
    lifecycle = server._rim_save_lifecycle

    async def cancel_call():
        async def inspect():
            await server.call_tool("inspect_save", {})
        async with anyio.create_task_group() as group:
            group.start_soon(inspect)
            assert await anyio.to_thread.run_sync(entered.wait, 2)
            group.cancel_scope.cancel()

    try:
        anyio.run(cancel_call)
        worker = lifecycle.worker
        assert worker is not None
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert observed_cancel.is_set()
        assert lifecycle.state == lifecycle.EMPTY
    finally:
        lifecycle.close()


def test_cancellation_during_parser_normalization_reaps_worker(tmp_path, monkeypatch):
    from rimworld_save_inspector import source as source_module

    parser_entered = threading.Event()
    release_parser = threading.Event()
    original_parse = source_module.parse_stream

    def paused_parser(stream, *, limits, cancel=None):
        parser_entered.set()
        assert release_parser.wait(3)
        return original_parse(stream, limits=limits, cancel=cancel)

    monkeypatch.setattr(source_module, "parse_stream", paused_parser)
    server = mcp_server.create_server(_copy_fixture(tmp_path))
    lifecycle = server._rim_save_lifecycle

    def release_when_cancelled():
        assert parser_entered.wait(3)
        assert lifecycle.cancel.wait(3)
        release_parser.set()

    releaser = threading.Thread(target=release_when_cancelled, name="mcp-test-parser-releaser")
    releaser.start()

    async def cancel_call():
        task = asyncio.create_task(lifecycle.inspect())
        deadline = asyncio.get_running_loop().time() + 2
        while not parser_entered.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        assert parser_entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        started = time.monotonic()
        asyncio.run(cancel_call())
        elapsed = time.monotonic() - started
        worker = lifecycle.worker
        assert worker is not None
        worker.join(timeout=2)
        releaser.join(timeout=2)
        assert elapsed < 2
        assert not worker.is_alive()
        assert not releaser.is_alive()
        assert lifecycle.state == lifecycle.EMPTY
    finally:
        release_parser.set()
        releaser.join(timeout=2)
        lifecycle.close()


def test_runtime_io_audit_cli_and_mcp(tmp_path):
    save = _copy_fixture(tmp_path)
    audit_script = ROOT / "tools/audit/mcp_runtime_io.py"
    before = hashlib.sha256(save.read_bytes()).hexdigest()
    mode_before = save.stat().st_mode
    reports = []
    evidence = ROOT / "evidence" / f"mcp-runtime-audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex[:8]}"
    evidence.mkdir(parents=True, exist_ok=False)
    report_paths = []
    for mode in ("cli", "mcp"):
        report_path = evidence / f"{mode}.json"
        report_paths.append(str(report_path))
        proc = subprocess.run([sys.executable, str(audit_script), "--mode", mode, "--save", str(save),
                               "--report", str(report_path)],
                              cwd=ROOT, capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0, (mode, proc.stdout, proc.stderr)
        marker = next((line.removeprefix("MCP_RUNTIME_IO_AUDIT=") for line in proc.stderr.splitlines()
                       if line.startswith("MCP_RUNTIME_IO_AUDIT=")), None)
        assert marker is not None, proc.stderr
        report = json.loads(marker)
        reports.append(report)
        assert report["report_path"] == str(report_path)
        assert json.loads(report_path.read_text(encoding="utf-8")) == report
        assert report["queries_completed"] >= 1
        assert report["writes_under_save_root"] == []
        assert report["network_attempts"] == []
        assert report["source_hash_before"] == report["source_hash_after"] == before
        assert report["mode_before"] == report["mode_after"] == mode_before
        assert "Traceback" not in proc.stderr
    assert hashlib.sha256(save.read_bytes()).hexdigest() == before
    assert save.stat().st_mode == mode_before
    assert all(r["source_read_events"] >= 1 for r in reports)
    probe_path = evidence / "probe.json"
    probe = subprocess.run([sys.executable, str(audit_script), "--mode", "probe", "--save", str(save),
                            "--report", str(probe_path)], cwd=ROOT, capture_output=True, text=True, timeout=8)
    assert probe.returncode == 0, (probe.stdout, probe.stderr)
    probe_marker = next(line.removeprefix("MCP_RUNTIME_IO_AUDIT=") for line in probe.stderr.splitlines()
                        if line.startswith("MCP_RUNTIME_IO_AUDIT="))
    probe_report = json.loads(probe_marker)
    assert json.loads(probe_path.read_text(encoding="utf-8")) == probe_report
    assert [item["kind"] for item in probe_report["writes_under_save_root"]] == ["write-open"]
    assert [item["event"] for item in probe_report["network_attempts"]] == ["socket.getaddrinfo"]
    assert probe_report["source_hash_before"] == probe_report["source_hash_after"] == before
    assert hashlib.sha256(save.read_bytes()).hexdigest() == before
    assert save.stat().st_mode == mode_before
    report_manifest = {
        "schema_version": 1,
        "case": "T20",
        "expected": {"writes_under_save_root": [], "network_attempts": [], "source_hash_preserved": True,
                     "source_mode_preserved": True, "probe_observer_detects_write_and_network": True},
        "observed": {"cli": reports[0], "mcp": reports[1], "probe": probe_report},
        "report_paths": [*report_paths, str(probe_path)],
        "result": "pass",
    }
    manifest_path = evidence / "report.json"
    manifest_path.write_text(json.dumps(report_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("MCP_AUDIT_REPORT=" + str(manifest_path.resolve()))


def test_runtime_io_audit_observer_detects_blocked_write_and_network_attempt(tmp_path):
    save = _copy_fixture(tmp_path)
    before = hashlib.sha256(save.read_bytes()).hexdigest()
    proc = subprocess.run([sys.executable, str(ROOT / "tools/audit/mcp_runtime_io.py"),
                           "--mode", "probe", "--save", str(save)], cwd=ROOT,
                          capture_output=True, text=True, timeout=8)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    marker = next(line.removeprefix("MCP_RUNTIME_IO_AUDIT=") for line in proc.stderr.splitlines()
                  if line.startswith("MCP_RUNTIME_IO_AUDIT="))
    report = json.loads(marker)
    assert [item["kind"] for item in report["writes_under_save_root"]] == ["write-open"]
    assert [item["event"] for item in report["network_attempts"]] == ["socket.getaddrinfo"]
    assert hashlib.sha256(save.read_bytes()).hexdigest() == before

