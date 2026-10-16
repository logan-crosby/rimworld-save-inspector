from __future__ import annotations

import json
import importlib.util
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from rimworld_save_inspector import InspectorError, QueryRequest, QueryService, load_snapshot
from rimworld_save_inspector import cli
from rimworld_save_inspector.schemas import ERROR_SCHEMA, SUCCESS_SCHEMAS

ROOT = Path(__file__).resolve().parents[2]
SAVE = ROOT / "tests/fixtures/synthetic/seed.rws"
PYTHON = ROOT / ".venv/bin/python"


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PYTHON), "-m", "rimworld_save_inspector.cli", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def test_handwritten_contract_examples_validate_and_reject_unknown_fields():
    # Hand-written contract examples deliberately do not come from QueryService.
    ref = {"raw": "Thing_Human1", "namespace": "thing", "normalized_id": "Human1",
           "status": "resolved", "target_record_ids": ["pawn:" + "a" * 64], "rule": "thing-prefix-v1"}
    meta = {"field_status": {}, "raw_values": {}, "derivations": []}
    record = {**meta, "record_id": "pawn:" + "a" * 64, "saved_id": "Human1", "source_path": "/pawn[1]"}
    name = {**meta, "first": "Ada", "nick": None, "last": "Example", "simple": None, "display": "Ada Example"}
    pawn = {**record, "pawn_def": "Human", "kind_def": None, "name": name, "faction": ref,
            "memberships": [], "guest_status": None}
    coverage = {"status": "complete_for_scope", "scope": "map", "diagnostic_counts": {}, "diagnostics": []}
    provenance = {"source_name": "seed.rws", "game_version": "1.6.4871 rev591", "parser_version": "0.1.0",
                  "profile_version": "rimworld-1.6/v1", "interpretation_id": "interpretation:" + "b" * 64}
    success = {
        "schema_version": "1.0", "snapshot_id": "sha256:" + "c" * 64,
        "query": {"name": "pawns", "filters": {"scope": "map"}, "limit": 50},
        "data": {**meta, "collection_state": "present", "entries": [pawn]},
        "coverage": coverage, "provenance": provenance, "page": {"total": 1, "returned": 1, "next_cursor": None},
    }
    assert "pawns" in SUCCESS_SCHEMAS
    Draft202012Validator(SUCCESS_SCHEMAS["pawns"]).validate(success)
    with pytest.raises(Exception):
        Draft202012Validator(SUCCESS_SCHEMAS["pawns"]).validate({**success, "uncontracted": True})
    nested_unknown = json.loads(json.dumps(success))
    nested_unknown["data"]["entries"][0]["name"]["uncontracted"] = "extra"
    with pytest.raises(Exception):
        Draft202012Validator(SUCCESS_SCHEMAS["pawns"]).validate(nested_unknown)
    error = {"schema_version": "1.0", "snapshot_id": None,
             "error": {"code": "SOURCE_NOT_FOUND", "message": "missing", "details": {}, "retryable": False}}
    Draft202012Validator(ERROR_SCHEMA).validate(error)
    with pytest.raises(Exception):
        Draft202012Validator(ERROR_SCHEMA).validate({**error, "traceback": "private"})
    with pytest.raises(Exception):
        Draft202012Validator(ERROR_SCHEMA).validate({**error, "error": {**error["error"], "retryable": True}})
    retryable = {**error, "error": {**error["error"], "code": "SOURCE_CHANGED", "retryable": True}}
    Draft202012Validator(ERROR_SCHEMA).validate(retryable)


def test_handwritten_examples_cover_each_success_variant_independently():
    meta = {"field_status": {}, "raw_values": {}, "derivations": []}
    record_id = "pawn:" + "a" * 64
    ref = {"raw": None, "namespace": "faction", "normalized_id": None,
           "status": "explicit_null", "target_record_ids": [], "rule": None}
    record = {**meta, "record_id": record_id, "saved_id": None, "source_path": "/pawn[1]"}
    pawn_index = {**record, "pawn_def": None, "kind_def": None,
                  "name": {**meta, "first": None, "nick": None, "last": None, "simple": None, "display": "Unknown"},
                  "faction": ref, "memberships": [], "guest_status": None}
    pawn_core = {**pawn_index, "gender": None, "age_biological_ticks": None, "birth_abs_ticks": None,
                 "host_faction": ref, "slave_faction": ref,
                 "child_counts": {key: None for key in ("skills", "traits", "hediffs", "endogenes", "xenogenes", "equipment", "apparel", "inventory")}}
    scopes = ("map", "world_alive", "world_dead", "world_mothballed", "held", "all_supported")
    domains = ("summary", "pawn", "health", "genes", "equipment", "inventory", "research", "mods")
    response_data = {
        "inspect": {**meta, "snapshot_id": "sha256:" + "c" * 64,
                    "interpretation_id": "interpretation:" + "b" * 64, "source_bytes": 0,
                    "game_version": None, "profile_version": "rimworld-1.6/v1", "snapshot_lifetime": "invocation",
                    "record_totals": {key: 0 for key in ("pawn", "item", "gene", "hediff", "map", "faction", "world_object", "skill", "trait", "research", "mod", "record_count", "membership_count", "resolved_saved_identity_count")},
                    "scope_coverage": {key: {"status": "complete_for_scope", "scope": key, "diagnostic_counts": {}, "diagnostics": []} for key in scopes},
                    "domain_availability": {key: "absent" for key in domains}},
        "summary": {**meta, "game_version": None, "world_name": None, "world_seed": None, "ticks_game": None,
                    "player_faction": ref, "maps_state": "absent", "map_count": None, "maps": [],
                    "population": {"direct_map_pawn_records": None, "human_def_records": None, "player_human_records": None,
                                   "map_guest_status_counts": None, "map_health_state_counts": None,
                                   "player_human_guest_status_counts": None, "player_human_health_state_counts": None}},
        "pawns": {**meta, "collection_state": "absent", "entries": []},
        "pawn_core": {**meta, "pawn": pawn_core},
        "pawn_skills": {**meta, "pawn_record_id": record_id, "collection_state": "absent", "entries": []},
        "pawn_traits": {**meta, "pawn_record_id": record_id, "collection_state": "absent", "entries": []},
        "health": {**meta, "pawn_record_id": record_id, "health_state": None, "collection_state": "absent", "entries": []},
        "genes": {**meta, "pawn_record_id": record_id,
                  "header": {**meta, "xenotype": None, "xenotype_name": None, "hybrid": None, "icon_def": None},
                  "group": "endogenes", "group_counts": {"endogenes": None, "xenogenes": None},
                  "collection_state": "absent", "entries": []},
        "equipment": {**meta, "pawn_record_id": record_id,
                      "holder": {"holder_id": "holder:" + "d" * 64, "collection_state": "absent", "entry_count": None},
                      "collection_state": "absent", "entry_count": None, "item_record_count": None,
                      "resolved_item_count": None,
                      "quantity": {key: None for key in ("explicit_stack_units", "missing_stack_count_records", "null_stack_count_records", "invalid_stack_count_records", "unresolved_item_entries", "estimated_stack_units")},
                      "entries": []},
        "inventory": {**meta, "pawn_record_id": record_id,
                      "holder": {"holder_id": "holder:" + "e" * 64, "collection_state": "absent", "entry_count": None},
                      "collection_state": "absent", "entry_count": None, "item_record_count": None,
                      "resolved_item_count": None,
                      "quantity": {key: None for key in ("explicit_stack_units", "missing_stack_count_records", "null_stack_count_records", "invalid_stack_count_records", "unresolved_item_entries", "estimated_stack_units")},
                      "entries": []},
        "research": {**meta, "current_project": None, "section": "progress", "alignment": "unpaired",
                     "collection_state": "absent", "key_count": None, "value_count": None, "entries": []},
        "mods": {**meta, "alignment": "unpaired", "collection_state": "absent", "id_count": None,
                 "name_count": None, "entries": []},
    }
    variants = {
        "inspect": ("inspect", {}, response_data["inspect"], None),
        "summary": ("summary", {}, response_data["summary"], None),
        "pawns": ("pawns", {"scope": "map"}, response_data["pawns"], {"total": None, "returned": 0, "next_cursor": None}),
        "pawn_core": ("pawn", {"record_id": record_id, "section": "core"}, response_data["pawn_core"], None),
        "pawn_skills": ("pawn", {"record_id": record_id, "section": "skills"}, response_data["pawn_skills"], {"total": None, "returned": 0, "next_cursor": None}),
        "pawn_traits": ("pawn", {"record_id": record_id, "section": "traits"}, response_data["pawn_traits"], {"total": None, "returned": 0, "next_cursor": None}),
        "health": ("health", {"record_id": record_id}, response_data["health"], {"total": None, "returned": 0, "next_cursor": None}),
        "genes": ("genes", {"record_id": record_id, "group": "endogenes"}, response_data["genes"], {"total": None, "returned": 0, "next_cursor": None}),
        "equipment": ("equipment", {"record_id": record_id, "holder": "equipment"}, response_data["equipment"], {"total": None, "returned": 0, "next_cursor": None}),
        "inventory": ("inventory", {"record_id": record_id, "holder_id": None}, response_data["inventory"], {"total": None, "returned": 0, "next_cursor": None}),
        "research": ("research", {"section": "progress"}, response_data["research"], {"total": None, "returned": 0, "next_cursor": None}),
        "mods": ("mods", {}, response_data["mods"], {"total": None, "returned": 0, "next_cursor": None}),
    }
    for name, (query_name, filters, data, page) in variants.items():
        example = {
            "schema_version": "1.0", "snapshot_id": "sha256:" + "c" * 64,
            "query": {"name": query_name, "filters": filters, "limit": 50 if page is not None else None},
            "data": data,
            "coverage": {"status": "complete_for_scope", "scope": "all_supported", "diagnostic_counts": {}, "diagnostics": []},
            "provenance": {"source_name": "seed.rws", "game_version": None, "parser_version": "0.1.0",
                           "profile_version": "rimworld-1.6/v1", "interpretation_id": "interpretation:" + "b" * 64},
            "page": page,
        }
        Draft202012Validator.check_schema(SUCCESS_SCHEMAS[query_name])
        Draft202012Validator(SUCCESS_SCHEMAS[query_name]).validate(example)
    Draft202012Validator.check_schema(ERROR_SCHEMA)


def test_cli_runs_every_query_as_one_json_line():
    base = invoke("inspect", str(SAVE), "pawns")
    assert base.returncode == 0, base.stderr
    pawns = json.loads(base.stdout)
    record_id = pawns["data"]["entries"][0]["record_id"]
    cases = [
        ("metadata",), ("summary",), ("pawns",), ("pawn", record_id),
        ("pawn", record_id, "--section", "skills"), ("pawn", record_id, "--section", "traits"),
        ("health", record_id), ("genes", record_id), ("equipment", record_id),
        ("inventory", record_id), ("research",), ("mods",),
    ]
    for query in cases:
        result = invoke("inspect", str(SAVE), *query)
        assert result.returncode == 0, (query, result.stderr, result.stdout)
        assert result.stderr == ""
        assert result.stdout.endswith("\n") and result.stdout.count("\n") == 1
        response = json.loads(result.stdout)
        assert response["query"]["name"] == ("inspect" if query[0] == "metadata" else query[0])
        assert result.stdout == json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
        Draft202012Validator(SUCCESS_SCHEMAS[response["query"]["name"]]).validate(response)


def test_cli_errors_help_version_and_invalid_arguments_have_contract_exit_codes():
    missing = invoke("inspect", str(ROOT / "missing.rws"), "summary")
    assert missing.returncode == 2
    error = json.loads(missing.stdout)
    Draft202012Validator(ERROR_SCHEMA).validate(error)
    assert error["error"]["code"] == "SOURCE_NOT_FOUND"
    assert "Traceback" not in missing.stderr

    bad_arg = invoke("inspect", str(SAVE), "pawns", "--not-a-filter", "x")
    assert bad_arg.returncode == 2
    Draft202012Validator(ERROR_SCHEMA).validate(json.loads(bad_arg.stdout))

    help_result = invoke("--help")
    version_result = invoke("--version")
    assert help_result.returncode == 0 and "usage:" in help_result.stdout.lower()
    assert version_result.stdout.strip().startswith("rim-save ")


def test_cli_transport_options_and_mcp_component_error_are_structured():
    disclosed = invoke("inspect", str(SAVE), "metadata", "--include-source-path")
    assert disclosed.returncode == 0
    disclosed_response = json.loads(disclosed.stdout)
    assert disclosed_response["provenance"]["source_path"] == str(SAVE.resolve())
    Draft202012Validator(SUCCESS_SCHEMAS["inspect"]).validate(disclosed_response)

    verbose = invoke("inspect", str(SAVE), "metadata", "--verbose")
    assert verbose.returncode == 0
    assert "Loading seed.rws" in verbose.stderr
    Draft202012Validator(SUCCESS_SCHEMAS["inspect"]).validate(json.loads(verbose.stdout))

    # Simulate a CLI-only install where the module exists but its optional SDK
    # dependency is unavailable. This exercises import failure during startup.
    assert importlib.util.find_spec("rimworld_save_inspector.mcp_server") is not None
    import_code = (
        "import importlib.abc, importlib.util, sys; "
        "assert importlib.util.find_spec('rimworld_save_inspector.mcp_server') is not None; "
        "class_blocker = type('BlockMcp', (importlib.abc.MetaPathFinder,), "
        "{'find_spec': lambda self, fullname, path=None, target=None: "
        "(_ for _ in ()).throw(ModuleNotFoundError(\"No module named 'mcp'\", name='mcp')) "
        "if fullname == 'mcp' or fullname.startswith('mcp.') else None}); "
        "sys.meta_path.insert(0, class_blocker()); "
        "from rimworld_save_inspector import cli; "
        f"raise SystemExit(cli.main(['mcp', '--save', {str(SAVE)!r}]))"
    )
    mcp_result = subprocess.run([str(PYTHON), "-c", import_code], cwd=ROOT, text=True, capture_output=True)
    assert mcp_result.returncode == 2, (mcp_result.stdout, mcp_result.stderr)
    mcp_error = json.loads(mcp_result.stdout)
    Draft202012Validator(ERROR_SCHEMA).validate(mcp_error)
    assert mcp_error["error"]["code"] == "MCP_EXTRA_REQUIRED"
    assert "Traceback" not in mcp_result.stderr


def test_service_and_cli_emit_canonical_equal_query_objects():
    snapshot = load_snapshot(SAVE)
    service = QueryService(snapshot)
    cases = [
        QueryRequest("summary"), QueryRequest("pawns", {"scope": "map"}, limit=1),
        QueryRequest("mods", {}, limit=1),
    ]
    for request in cases:
        expected = service.execute(request).as_dict()
        args = ["inspect", str(SAVE), request.name]
        for key, value in request.filters.items():
            args.extend(["--" + key.replace("_", "-"), str(value)])
        if request.limit is not None:
            args.extend(["--limit", str(request.limit)])
        actual = json.loads(invoke(*args).stdout)
        assert actual == expected


def test_cli_research_project_def_filter_matches_service():
    cli_result = invoke("inspect", str(SAVE), "research", "--project-def", "SolarPower")
    assert cli_result.returncode == 0, cli_result.stderr
    cli_response = json.loads(cli_result.stdout)
    service = QueryService(load_snapshot(SAVE), snapshot_lifetime="invocation")
    service_response = service.execute(QueryRequest(
        "research", {"project_def": "SolarPower", "section": "progress"},
    )).as_dict()
    assert cli_response["query"] == service_response["query"]
    assert cli_response["data"]["entries"] == service_response["data"]["entries"]
    assert cli_response["page"]["total"] == 1


def test_service_schemas_reject_non_finite_values_and_unknown_nested_fields():
    schema = SUCCESS_SCHEMAS["health"]
    assert schema["additionalProperties"] is False
    assert set(SUCCESS_SCHEMAS) == {"inspect", "summary", "pawns", "pawn", "health", "genes", "equipment", "inventory", "research", "mods"}
    assert schema["$defs"]["Record"]["properties"]["raw_values"]["additionalProperties"] == {"type": "string"}
    assert json.loads(json.dumps({"raw": "1.00", "value": 1.0}, allow_nan=False)) == {"raw": "1.00", "value": 1.0}
    with pytest.raises(ValueError):
        json.dumps({"value": float("nan")}, allow_nan=False)


def test_cli_paging_empty_results_and_invalid_cursor_contract():
    all_rows = None
    for size in (1, 50, 200):
        result = invoke("inspect", str(SAVE), "pawns", "--scope", "all_supported", "--limit", str(size))
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        Draft202012Validator(SUCCESS_SCHEMAS["pawns"]).validate(payload)
        if size == 200:
            all_rows = payload
    assert all_rows is not None
    first = invoke("inspect", str(SAVE), "pawns", "--scope", "all_supported", "--limit", "1")
    assert first.returncode == 0, first.stderr
    page1 = json.loads(first.stdout)
    assert len(first.stdout.encode("utf-8")) < len(invoke("inspect", str(SAVE), "pawns", "--scope", "all_supported", "--limit", "200").stdout.encode("utf-8"))
    assert page1["page"]["total"] == all_rows["page"]["total"]
    if page1["page"]["next_cursor"]:
        second = invoke("inspect", str(SAVE), "pawns", "--scope", "all_supported", "--limit", "1", "--cursor", page1["page"]["next_cursor"])
        assert second.returncode == 0, second.stderr
        page2 = json.loads(second.stdout)
        ids = [row["record_id"] for response in (page1, page2) for row in response["data"]["entries"]]
        assert ids == [row["record_id"] for row in all_rows["data"]["entries"]]
        assert page2["page"]["next_cursor"] is None

    empty = invoke("inspect", str(SAVE), "pawns", "--scope", "map", "--faction", "no-such-faction")
    assert empty.returncode == 0, empty.stderr
    empty_response = json.loads(empty.stdout)
    assert empty_response["page"] == {"total": 0, "returned": 0, "next_cursor": None}
    assert empty_response["data"]["entries"] == []
    Draft202012Validator(SUCCESS_SCHEMAS["pawns"]).validate(empty_response)

    corrupt = invoke("inspect", str(SAVE), "pawns", "--cursor", "not-a-cursor")
    assert corrupt.returncode == 2
    corrupt_response = json.loads(corrupt.stdout)
    Draft202012Validator(ERROR_SCHEMA).validate(corrupt_response)
    assert corrupt_response["error"]["code"] == "INVALID_CURSOR"

    if page1["page"]["next_cursor"]:
        wrong_query = invoke("inspect", str(SAVE), "mods", "--cursor", page1["page"]["next_cursor"])
        assert wrong_query.returncode == 2
        wrong_response = json.loads(wrong_query.stdout)
        Draft202012Validator(ERROR_SCHEMA).validate(wrong_response)
        assert wrong_response["error"]["code"] == "INVALID_CURSOR"
        wrong_limit = invoke("inspect", str(SAVE), "pawns", "--scope", "all_supported", "--limit", "2", "--cursor", page1["page"]["next_cursor"])
        assert wrong_limit.returncode == 2
        limit_response = json.loads(wrong_limit.stdout)
        Draft202012Validator(ERROR_SCHEMA).validate(limit_response)
        assert limit_response["error"]["code"] == "INVALID_CURSOR"
        wrong_filter = invoke("inspect", str(SAVE), "pawns", "--scope", "map", "--limit", "1", "--cursor", page1["page"]["next_cursor"])
        assert wrong_filter.returncode == 2
        filter_response = json.loads(wrong_filter.stdout)
        Draft202012Validator(ERROR_SCHEMA).validate(filter_response)
        assert filter_response["error"]["code"] == "INVALID_CURSOR"


def test_service_large_metadata_returns_structured_result_too_large():
    snapshot = load_snapshot(SAVE)
    oversized = replace(snapshot, source_name="synthetic-" + "x" * 600_000)
    with pytest.raises(InspectorError) as raised:
        QueryService(oversized).execute(QueryRequest("inspect"))
    assert getattr(raised.value, "code", None) == "RESULT_TOO_LARGE"
    assert "narrower_query" in getattr(raised.value, "details", {})


def test_service_single_oversized_record_is_not_silently_shortened():
    snapshot = load_snapshot(SAVE)
    hediff = next(record for record in snapshot.records if record.type_name == "hediff")
    oversized_hediff = replace(hediff, raw_values={"/severity": "0" * 600_000})
    changed_records = tuple(oversized_hediff if record.record_id == hediff.record_id else record for record in snapshot.records)
    synthetic_snapshot = replace(snapshot, records=changed_records)
    with pytest.raises(InspectorError) as raised:
        QueryService(synthetic_snapshot).execute(QueryRequest("health", {"record_id": hediff.owner_record_id}, limit=1))
    assert raised.value.code == "RESULT_TOO_LARGE"
    assert "narrower_query" in raised.value.details


def test_cli_broken_pipe_and_interrupt_exit_behavior(monkeypatch, capsys):
    process = subprocess.Popen(
        [str(PYTHON), "-m", "rimworld_save_inspector.cli", "inspect", str(SAVE), "metadata"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    process.stdout.close()
    stderr = process.stderr.read().decode("utf-8") if process.stderr is not None else ""
    return_code = process.wait(timeout=10)
    command = [str(PYTHON), "-m", "rimworld_save_inspector.cli", "inspect", str(SAVE), "metadata"]
    assert return_code == 0, f"command={command!r}; stdout=<reader closed>; stderr={stderr!r}"
    assert "BrokenPipe" not in stderr, f"command={command!r}; stdout=<reader closed>; stderr={stderr!r}"

    # Exercise the error-response path with a real closed OS pipe. The child
    # raises a domain error before writing JSON; the parent closes its reader
    # immediately, so an uncaught write/flush failure would show up in stderr
    # and a non-contract exit code.
    error_code = (
        "from rimworld_save_inspector import cli; "
        "from rimworld_save_inspector.errors import InspectorError; "
        "cli.load_snapshot = lambda _source: (_ for _ in ()).throw("
        "InspectorError('SOURCE_NOT_FOUND', 'synthetic missing source')); "
        f"raise SystemExit(cli.main(['inspect', {str(SAVE)!r}, 'metadata']))"
    )
    error_command = [str(PYTHON), "-c", error_code]
    error_child = subprocess.Popen(
        error_command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert error_child.stdout is not None
    error_child.stdout.close()
    error_stderr = error_child.stderr.read().decode("utf-8") if error_child.stderr is not None else ""
    error_return_code = error_child.wait(timeout=10)
    assert error_return_code == 2, (
        f"command={error_command!r}; stdout=<reader closed>; stderr={error_stderr!r}"
    )
    assert "BrokenPipe" not in error_stderr, (
        f"command={error_command!r}; stdout=<reader closed>; stderr={error_stderr!r}"
    )

    monkeypatch.setattr(cli, "load_snapshot", lambda _source: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.main(["inspect", str(SAVE), "metadata"]) == 130
    output = capsys.readouterr().out
    response = json.loads(output)
    Draft202012Validator(ERROR_SCHEMA).validate(response)
    assert response["error"]["code"] == "CANCELLED"
    assert response["error"]["retryable"] is True

    interrupt_code = (
        "import sys, time; from rimworld_save_inspector import cli; "
        "def_slow = lambda _source: (print('READY', file=sys.stderr, flush=True), time.sleep(60)); "
        "cli.load_snapshot = def_slow; "
        f"raise SystemExit(cli.main(['inspect', {str(SAVE)!r}, 'metadata']))"
    )
    child = subprocess.Popen(
        [str(PYTHON), "-c", interrupt_code], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert child.stderr is not None
    assert child.stderr.readline().strip() == "READY"
    child.send_signal(signal.SIGINT)
    stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 130
    interrupted = json.loads(stdout)
    Draft202012Validator(ERROR_SCHEMA).validate(interrupted)
    assert interrupted["error"]["code"] == "CANCELLED"
    assert "Traceback" not in stderr
