from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from rimworld_save_inspector.errors import InspectorError
from rimworld_save_inspector.journal import JournalLimits, _overlapping_observed_sequences, read_journal

ROOT = Path(__file__).resolve().parents[2]
ORACLE = ROOT / "tests" / "journal_oracle" / "fixtures"


def event_lines(name: str) -> bytes:
    return (ORACLE / name).read_bytes()


def test_frozen_oracle_valid_fork_conflict_and_gap_semantics():
    valid = read_journal(ORACLE / "valid.jsonl")
    assert [x["event"]["event_id"] for x in valid["events"]] == ["s-valid:1", "s-valid:2", "s-valid:3"]
    fork = read_journal(ORACLE / "fork.jsonl")
    assert {x["session_id"] for x in fork["sessions"]} == {"s-root", "s-left", "s-right"}
    left = next(x for x in fork["sessions"] if x["session_id"] == "s-left")
    assert left["parent"] == {"session_id": "s-root", "checkpoint_id": "cp-root",
                               "checkpoint_metadata_id": "cp-root",
                               "claimed_status": "verified", "verified": True, "status": "verified"}
    conflict = read_journal(ORACLE / "conflict.jsonl")
    assert len(conflict["conflicts"]) == 1
    assert len(conflict["conflicts"][0]["alternatives"]) == 2
    assert sum(len(a["origins"]) for a in conflict["conflicts"][0]["alternatives"]) == 4
    assert conflict["coverage"]["duplicate_count"] == 2
    gap = read_journal(ORACLE / "gap.jsonl")
    gap_event = next(x["event"] for x in gap["events"] if x["event"]["event_type"] == "gap")
    assert gap_event["payload"]["dropped_count"] == 2
    assert [r["first_sequence"] for r in gap_event["payload"]["missing_ranges"]] == [2, 4]


def test_unknown_clocks_and_pawn_filtered_control_coverage():
    result = read_journal(ORACLE / "unknown-clock.jsonl", pawn_id="pawn-unknown")
    assert [x["event"]["event_type"] for x in result["events"]] == ["session_started", "future_pawn_signal"]
    unknown = result["events"][1]["event"]
    assert unknown["ticks_abs"] is None and unknown["ticks_game"] is None
    pawn = read_journal(ORACLE / "gap.jsonl", pawn_id="pawn-a")
    assert {x["event"]["event_type"] for x in pawn["events"]} == {"session_started", "pawn_relation_changed", "gap"}
    no_match = read_journal(ORACLE / "gap.jsonl", pawn_id="pawn-not-in-journal")
    assert [x["event"]["event_type"] for x in no_match["events"]] == ["session_started", "gap"]
    assert no_match["coverage"]["gap_count"] == 1


def test_truncated_tail_excluded_and_malformed_interior_is_a_source_failure(tmp_path):
    truncated = read_journal(ORACLE / "truncated-tail.jsonl")
    assert [x["event"]["event_id"] for x in truncated["events"]] == ["s-truncated:1"]
    assert truncated["sources"][0]["outcome"] == "incomplete_tail"
    assert truncated["coverage"]["status"] == "partial"
    folder = tmp_path / "journal"
    folder.mkdir()
    (folder / "bad.jsonl").write_bytes(event_lines("malformed-interior.jsonl"))
    (folder / "good.jsonl").write_bytes(event_lines("valid.jsonl"))
    result = read_journal(folder)
    assert len(result["sources"]) == 2
    assert {x["outcome"] for x in result["sources"]} == {"failed", "complete"}
    assert result["coverage"]["status"] == "partial"
    assert all(x["event"]["session_id"] == "s-valid" for x in result["events"])


def test_directory_source_deduplicates_raw_origins_and_never_mutates_files(tmp_path):
    folder = tmp_path / "journals"
    folder.mkdir()
    raw = event_lines("valid.jsonl")
    (folder / "one.jsonl").write_bytes(raw)
    (folder / "two.jsonl").write_bytes(raw)
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())]
    result = read_journal(folder)
    assert len(result["events"]) == 3
    assert all(len(item["origins"]) == 2 for item in result["events"])
    assert result["coverage"]["duplicate_count"] == 3
    assert before == [hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())]


def test_strict_json_and_limits_fail_without_truncated_success(tmp_path):
    source = tmp_path / "bad.jsonl"
    source.write_text('{"x":1,"x":2}\n', encoding="utf-8")
    result = read_journal(source)
    assert result["sources"][0]["outcome"] == "failed"
    assert result["coverage"]["partial"]
    with pytest.raises(InspectorError) as caught:
        read_journal(ORACLE / "valid.jsonl", limits=JournalLimits(line_bytes=8))
    assert caught.value.code == "JOURNAL_LIMIT_EXCEEDED"


def test_source_replacement_during_read_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "events.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    source.write_bytes(event_lines("valid.jsonl"))
    replacement.write_bytes(event_lines("gap.jsonl"))
    original_open = Path.open

    @contextmanager
    def replace_after_read(self, *args, **kwargs):
        with original_open(self, *args, **kwargs) as stream:
            yield stream
            if self == source:
                os.replace(replacement, source)

    monkeypatch.setattr(Path, "open", replace_after_read)
    with pytest.raises(InspectorError) as caught:
        read_journal(source)
    assert caught.value.code == "SOURCE_CHANGED"


def test_clock_mismatch_is_retained_with_invalid_chronology(tmp_path):
    row = json.loads(event_lines("valid.jsonl").splitlines()[0])
    row["ticks_abs"] += 7
    path = tmp_path / "clock.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = read_journal(path)
    assert result["events"][0]["event"]["ticks_abs"] == 1007
    assert result["events"][0]["chronology"] == "invalid"
    assert result["events"][0]["diagnostics"][0]["code"] == "CLOCK_MISMATCH"


@pytest.mark.parametrize("raw", [
    b'{"x":1,"x":2}\n',
    b'{"x":NaN}\n',
    b'{"x":"\\ud800"}\n',
])
def test_duplicate_keys_and_nonstandard_numbers_fail_source(tmp_path, raw):
    path = tmp_path / "invalid.jsonl"
    path.write_bytes(raw)
    result = read_journal(path)
    assert result["sources"][0]["outcome"] == "failed"
    assert result["events"] == []
    assert result["coverage"]["partial"]


@pytest.mark.parametrize("mutation", ["sequence_bool", "deep_payload", "negative_tick"])
def test_integer_depth_and_negative_clock_boundaries(tmp_path, mutation):
    row = json.loads(event_lines("valid.jsonl").splitlines()[0])
    if mutation == "sequence_bool":
        row["sequence"] = True
        row["event_id"] = "s-valid:True"
    elif mutation == "deep_payload":
        value = "leaf"
        for _ in range(35):
            value = {"nested": value}
        row["payload"] = value
    else:
        row["ticks_game"] = -1
        row["ticks_abs"] = 999
    path = tmp_path / f"{mutation}.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = read_journal(path)
    if mutation in {"sequence_bool", "deep_payload"}:
        assert result["sources"][0]["outcome"] == "failed"
        assert result["events"] == []
    else:
        assert result["events"][0]["chronology"] == "invalid"
        assert result["events"][0]["diagnostics"][0]["code"] == "CLOCK_NEGATIVE"


def test_conflict_uses_entire_envelope_not_only_payload(tmp_path):
    first = json.loads(event_lines("valid.jsonl").splitlines()[1])
    second = dict(first)
    second["game_version"] = "future-game-version"
    one, two = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    one.write_text(json.dumps(first) + "\n", encoding="utf-8")
    two.write_text(json.dumps(second) + "\n", encoding="utf-8")
    result = read_journal(tmp_path)
    assert result["conflicts"][0]["identity"]["sequence"] == first["sequence"]
    assert {x["event"]["game_version"] for x in result["conflicts"][0]["alternatives"]} == {
        first["game_version"], "future-game-version"}


def test_path_symlink_and_missing_or_attempt_only_parent_are_unproven(tmp_path):
    link = tmp_path / "linked.jsonl"
    link.symlink_to(ORACLE / "valid.jsonl")
    with pytest.raises(InspectorError) as caught:
        read_journal(link)
    assert caught.value.code == "UNSAFE_SOURCE"
    link.unlink()

    rows = [json.loads(line) for line in event_lines("fork.jsonl").splitlines()]
    # Remove the successful outcome while retaining the save attempt and the child's claim.
    rows = [row for row in rows if not (row["session_id"] == "s-root" and row["event_type"] == "save_checkpoint"
                                        and row["payload"]["status"] == "succeeded")]
    journal = tmp_path / "attempt-only.jsonl"
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = read_journal(journal)
    left = next(s for s in result["sessions"] if s["session_id"] == "s-left")
    assert left["parent"]["verified"] is False
    assert left["parent"]["status"] == "unproven"

    rows = [json.loads(line) for line in event_lines("fork.jsonl").splitlines()]
    child_start = next(row for row in rows if row["session_id"] == "s-left")
    child_start["payload"]["loaded_checkpoint_id"] = None
    child_start["checkpoint_id"] = None
    missing = tmp_path / "missing-marker.jsonl"
    missing.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    missing_result = read_journal(missing)
    missing_left = next(s for s in missing_result["sessions"] if s["session_id"] == "s-left")
    assert missing_left["parent"]["verified"] is False
    assert missing_left["parent"]["status"] == "unproven"


def test_backward_ticks_and_session_basis_mismatch_are_diagnosed(tmp_path):
    rows = [json.loads(line) for line in event_lines("valid.jsonl").splitlines()]
    rows[2]["ticks_game"] = 0
    rows[2]["ticks_abs"] = 1000
    path = tmp_path / "backwards.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = read_journal(path)
    assert any(d["code"] == "SESSION_CHRONOLOGY_MISMATCH" for d in result["coverage"]["diagnostics"])
    assert all(x["chronology"] == "invalid" for x in result["events"])
    assert [x["event"]["sequence"] for x in result["events"]] == [1, 2, 3]

    root = json.loads(event_lines("valid.jsonl").splitlines()[0])
    other = dict(root)
    other["campaign_id"] = root["campaign_id"]
    other["session_id"] = "s-other"
    other["event_id"] = "s-other:1"
    other["payload"] = dict(root["payload"])
    other["payload"]["time_basis"] = {"starting_year": 5501}
    other["game_start_abs_tick"] = 2000
    other["ticks_abs"] = 2000
    other_path = tmp_path / "basis.jsonl"
    other_path.write_text(json.dumps(root) + "\n" + json.dumps(other) + "\n", encoding="utf-8")
    basis_result = read_journal(other_path)
    assert len({x["time_basis_id"] for x in basis_result["sessions"]}) == 2
    assert any(d["code"] == "TIME_BASIS_SEPARATED" for d in basis_result["coverage"]["diagnostics"])
    with pytest.raises(InspectorError) as caught:
        read_journal(ORACLE / "valid.jsonl", limits={"records": 1})
    assert caught.value.code == "JOURNAL_LIMIT_EXCEEDED"


def test_nested_arrays_cannot_bypass_json_depth_limit(tmp_path):
    rows = [json.loads(line) for line in event_lines("fork.jsonl").splitlines()]
    value: object = "leaf"
    for _ in range(40):
        value = [value]
    rows[0]["payload"]["depth_probe"] = value
    source = tmp_path / "nested-arrays.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = read_journal(source)
    assert result["sources"][0]["outcome"] == "failed"
    assert result["events"] == []


def test_child_start_before_succeeded_checkpoint_cannot_verify_ancestry(tmp_path):
    rows = [json.loads(line) for line in event_lines("fork.jsonl").splitlines()]
    child = next(row for row in rows if row["session_id"] == "s-left" and row["event_type"] == "session_started")
    child["ticks_game"], child["ticks_abs"] = 5, 1005
    source = tmp_path / "child-before-checkpoint.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = read_journal(source)
    left = next(session for session in result["sessions"] if session["session_id"] == "s-left")
    assert left["parent"]["verified"] is False
    assert left["parent"]["status"] == "unproven"
    assert any(item["code"] == "ANCESTRY_CLOCK_INCONSISTENT" for item in result["coverage"]["diagnostics"])


def test_conflicting_child_session_start_cannot_verify_ancestry(tmp_path):
    rows = [json.loads(line) for line in event_lines("fork.jsonl").splitlines()]
    child = next(row for row in rows if row["session_id"] == "s-left" and row["event_type"] == "session_started")
    conflict = json.loads(json.dumps(child))
    conflict["payload"]["parent_association"] = "unproven"
    rows.append(conflict)
    source = tmp_path / "conflicting-child-start.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = read_journal(source)
    left = next(session for session in result["sessions"] if session["session_id"] == "s-left")
    assert len(result["conflicts"]) == 1
    assert left["parent"]["verified"] is False
    assert left["parent"]["status"] == "unproven"
    assert any(item["code"] == "ANCESTRY_START_CONFLICT" for item in result["coverage"]["diagnostics"])


def test_exact_gap_range_cannot_claim_an_observed_sequence(tmp_path):
    rows = [json.loads(line) for line in event_lines("gap.jsonl").splitlines()]
    gap = next(row for row in rows if row["event_type"] == "gap")
    gap["sequence"], gap["event_id"] = 4, "s-gap:4"
    gap["payload"]["missing_ranges"] = [{"first_sequence": 2, "last_sequence": 3, "cause": "queue_full"}]
    source = tmp_path / "overlapping-gap.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = read_journal(source)
    assert result["sources"][0]["outcome"] == "complete"
    overlap = next(item for item in result["coverage"]["diagnostics"]
                   if item["code"] == "GAP_OVERLAPS_OBSERVED_SEQUENCE")
    assert overlap["observed_sequences"] == [3]
    assert result["coverage"]["partial"] is True


def test_gap_overlap_index_finds_all_hits_across_many_ranges():
    observed = list(range(1, 40_000, 2))
    ranges = [{"first_sequence": first, "last_sequence": first + 1}
              for first in range(0, 40_000, 4)]
    assert _overlapping_observed_sequences(observed, ranges) == list(range(1, 40_000, 4))


def test_cli_and_configured_mcp_use_the_same_service(tmp_path):
    journal = ORACLE / "gap.jsonl"
    cli = subprocess.run([sys.executable, "-m", "rimworld_save_inspector.cli", "journal", str(journal),
                          "--pawn-id", "pawn-a"], cwd=ROOT, capture_output=True, text=True, check=True)
    cli_result = json.loads(cli.stdout)
    from rimworld_save_inspector.journal_mcp import create_server
    import anyio
    from jsonschema import Draft202012Validator
    server = create_server(journal)
    overview = anyio.run(server.call_tool, "get_pawn_journal", {"pawn_id": "pawn-a"})
    assert not overview.is_error
    overview_tool = next(x for x in anyio.run(server.list_tools) if x.name == "get_pawn_journal")
    Draft202012Validator(overview_tool.output_schema).validate(overview.structured_content)
    assert overview.structured_content == cli_result
    tools = anyio.run(server.list_tools)
    assert {x.name for x in tools} == {"get_journal_overview", "get_pawn_journal", "get_journal_schema"}
    assert all(x.annotations.read_only_hint is True and x.annotations.destructive_hint is False for x in tools)
    assert "path" not in json.dumps(next(x for x in tools if x.name == "get_pawn_journal").input_schema)
    schema_response = anyio.run(server.call_tool, "get_journal_schema", {})
    assert not schema_response.is_error
    response_schema = schema_response.structured_content
    from rimworld_save_inspector.journal import JOURNAL_RESPONSE_SCHEMA
    assert response_schema == JOURNAL_RESPONSE_SCHEMA
    assert json.loads((ROOT / "docs/recorder/journal.schema.json").read_text()) == JOURNAL_RESPONSE_SCHEMA
    Draft202012Validator.check_schema(response_schema)
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError):
        anyio.run(server.call_tool, "get_journal_overview", {"path": "/tmp/arbitrary.jsonl"})
    missing_server = create_server(tmp_path / "missing.jsonl")
    unavailable = anyio.run(missing_server.call_tool, "get_journal_overview", {})
    assert unavailable.is_error


def test_journal_mcp_stdio_tool_readback_and_schema_validation():
    from rimworld_save_inspector import __version__
    from jsonschema import Draft202012Validator
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    import anyio

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "rimworld_save_inspector.cli", "journal-mcp", "--journal", str(ORACLE / "valid.jsonl")],
            cwd=str(ROOT),
            env={"PYTHONPATH": str(ROOT / "src")},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=20) as client:
                initialized = await client.initialize()
                assert initialized.server_info.version == __version__
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                assert names == {"get_journal_overview", "get_pawn_journal", "get_journal_schema"}
                overview_tool = next(tool for tool in tools.tools if tool.name == "get_journal_overview")
                assert overview_tool.annotations.read_only_hint is True
                assert overview_tool.annotations.destructive_hint is False
                response = await client.call_tool("get_journal_overview", {})
                assert not response.is_error
                data = response.structured_content
                Draft202012Validator(overview_tool.output_schema).validate(data)
                schema_result = await client.call_tool("get_journal_schema", {})
                assert not schema_result.is_error
                Draft202012Validator.check_schema(schema_result.structured_content)
                assert schema_result.structured_content == JOURNAL_SCHEMA

    from rimworld_save_inspector.journal import JOURNAL_RESPONSE_SCHEMA as JOURNAL_SCHEMA
    anyio.run(run)


def test_pawn_filter_keeps_payload_subject_association(tmp_path):
    records = [json.loads(line) for line in event_lines("valid.jsonl").splitlines()]
    records[2]["subject_id"] = None
    records[2]["payload"]["subject_id"] = "pawn-1"
    path = tmp_path / "payload-subject.jsonl"
    path.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n")
    result = read_journal(path, pawn_id="pawn-1")
    assert any(row["event"]["event_type"] == "pawn_died" for row in result["events"])
