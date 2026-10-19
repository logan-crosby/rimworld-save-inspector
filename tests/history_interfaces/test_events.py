"""Real synthetic XML proves participantless events have a lossless query route."""

import hashlib
import json
from pathlib import Path

import anyio
import jsonschema
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from rimworld_save_inspector.cli import main
from rimworld_save_inspector.errors import InspectorError
from rimworld_save_inspector.history import catalog, service
from rimworld_save_inspector.history.mcp import create_server
from rimworld_save_inspector.history.schemas import HISTORY_SCHEMAS


@pytest.fixture
def event_catalog(tmp_path):
    original = (Path(__file__).parents[1] / "history_oracle" / "fixtures" /
                "alpha-early.xml").read_text()
    text = original.replace("</tales>", '<li id="Tale_99"><def>Visitor</def>'
                            '<occurred>52525</occurred><text>no saved participant</text>'
                            '</li></tales>')
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "misleading-late-name.rws").write_text(text)
    (saves / "misleading-early-name.rws").write_text(
        text.replace("<ticksGame>730</ticksGame>", "<ticksGame>900</ticksGame>"))
    db = tmp_path / "events.sqlite"
    catalog.build_catalog(saves, db)
    return db, saves


def test_all_event_occurrences_page_without_participant_or_time_loss(event_catalog):
    db, _ = event_catalog
    before = db.read_bytes()
    pages, entries, cursor = [], [], None
    while True:
        page = service.get_events(db, limit=1, cursor=cursor, freshness="frozen")
        jsonschema.validate(page, HISTORY_SCHEMAS["events"])
        pages.append(page)
        entries.extend(page["events"])
        assert page["page"]["matching_occurrences"] == 4
        assert page["page"]["ordering"] == "snapshot_id,event_index; not chronology"
        cursor = page["page"]["next_cursor"]
        if cursor is None:
            break
    assert len(pages) == 4
    assert [(e["snapshot_id"], e["event_index"]) for e in entries] == sorted(
        (e["snapshot_id"], e["event_index"]) for e in entries)
    missing = [e for e in entries if e["event"]["saved_event_id"] == "tale:Tale:Tale_99"]
    assert len(missing) == 2
    assert all(e["event"]["participants"] == [] for e in missing)
    assert all(e["event"]["occurrence"]["normalized_game_tick"] == 25 for e in missing)
    assert {e["game_tick"] for e in missing} == {730, 900}
    for entry in entries:
        assert entry["evidence"]
        bodies = []
        for ref in entry["evidence"]:
            raw = service.get_evidence(db, ref["evidence_id"])
            assert raw["snapshot_id"] == entry["snapshot_id"]
            assert raw["content_id"] == ref["content_id"]
            encoded = json.dumps(raw["node"], sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False).encode()
            assert hashlib.sha256(encoded).hexdigest() == ref["content_id"]
            if raw["node"]["tag"] == "li":
                bodies.append(raw)
        assert len(bodies) == 1
        assert ["id", entry["event"]["raw_saved_id"]] in bodies[0]["node"]["attributes"]
        assert "/tales[1]/li[" in bodies[0]["path"]
    assert db.read_bytes() == before


def test_exact_filters_and_cursor_binding(event_catalog, tmp_path):
    db, saves = event_catalog
    first = service.get_events(db, saved_event_id="tale:Tale:Tale_99", limit=1,
                               freshness="frozen")
    cursor = first["page"]["next_cursor"]
    assert cursor
    second = service.get_events(db, saved_event_id="tale:Tale:Tale_99", limit=1,
                                cursor=cursor, freshness="frozen")
    assert second["events"][0]["snapshot_id"] != first["events"][0]["snapshot_id"]
    filtered = service.get_events(db, snapshot_id=first["events"][0]["snapshot_id"],
                                  saved_event_id="tale:Tale:Tale_99", freshness="frozen")
    assert filtered["page"]["matching_occurrences"] == 1
    assert len(filtered["events"]) == 1
    for kwargs in ({"cursor": cursor}, {"cursor": cursor, "saved_event_id": "wrong"}):
        with pytest.raises(InspectorError) as caught:
            service.get_events(db, freshness="frozen", **kwargs)
        assert caught.value.code == "INVALID_CURSOR"
    foreign = tmp_path / "foreign.sqlite"
    catalog.build_catalog(saves, foreign)
    with pytest.raises(InspectorError) as caught:
        service.get_events(foreign, saved_event_id="tale:Tale:Tale_99", cursor=cursor,
                           freshness="frozen")
    assert caught.value.code == "INVALID_CURSOR"


@pytest.mark.parametrize("limit", [True, 0, -1, 501, 1.5, "1"])
def test_bad_page_limit_rejected_before_catalog_io(tmp_path, limit):
    with pytest.raises(InspectorError) as caught:
        service.get_events(tmp_path / "absent.sqlite", limit=limit)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_explicit_oversize_and_no_match_are_distinct(event_catalog):
    db, _ = event_catalog
    with pytest.raises(InspectorError) as caught:
        service.get_events(db, response_limit=256, freshness="frozen")
    assert caught.value.code == "RESULT_TOO_LARGE"
    assert caught.value.details["required_bytes"] > 256
    assert caught.value.details["selectors"]["limit"] == 50
    result = service.get_events(db, saved_event_id="not-present", freshness="frozen")
    assert result["events"] == []
    assert result["page"]["matching_occurrences"] == 0
    assert result["coverage"]["compatible_snapshot_count"] == 2


def test_cli_and_mcp_share_participantless_query(event_catalog, capsys):
    db, _ = event_catalog
    assert main(["events", str(db), "--saved-event-id", "tale:Tale:Tale_99",
                 "--freshness", "frozen"]) == 0
    cli = json.loads(capsys.readouterr().out)
    server = create_server(db)
    result = anyio.run(server.call_tool, "get_history_events", {
        "saved_event_id": "tale:Tale:Tale_99", "freshness": "frozen"})
    assert not result.is_error
    assert result.structured_content["events"] == cli["events"]
    assert len(result.content) == 1
    assert len(result.model_dump_json(by_alias=True).encode()) < 16 * 1024 * 1024
    tool = next(t for t in anyio.run(server.list_tools) if t.name == "get_history_events")
    assert tool.annotations.read_only_hint and not tool.annotations.destructive_hint
    with pytest.raises(ToolError, match="Extra inputs are not permitted"):
        anyio.run(server.call_tool, "get_history_events", {"catalog_path": str(db)})


@pytest.mark.parametrize("case", ["unknown_clock", "multiple_campaigns"])
def test_exact_snapshot_selects_its_campaign_without_guessing_latest(event_catalog, case):
    db, saves = event_catalog
    for index, path in enumerate(sorted(saves.glob("*.rws"))):
        text = path.read_text()
        if case == "unknown_clock":
            import re
            text = re.sub(r"<ticksGame>\d+</ticksGame>", "", text)
        elif index == 1:
            text = text.replace("synthetic-seed-coral", "independent-campaign")
        path.write_text(text)
    report = catalog.refresh_catalog(db, freshness="strict")
    sid = report["sources"][0]["snapshot_id"]
    result = service.get_events(db, snapshot_id=sid, freshness="frozen")
    assert result["catalog"]["anchor_snapshot_id"] == sid
    assert result["page"]["matching_occurrences"] == 2
    assert {e["snapshot_id"] for e in result["events"]} == {sid}
    if case == "unknown_clock":
        assert all(e["game_tick"] is None for e in result["events"])
    else:
        other = report["sources"][1]["snapshot_id"]
        with pytest.raises(InspectorError) as caught:
            service.get_events(db, snapshot_id=sid, anchor_snapshot_id=other,
                               freshness="frozen")
        assert caught.value.code == "INCOMPATIBLE_SNAPSHOT"


def test_unvalidated_campaign_exact_snapshot_is_retrievable_without_cross_save_join(event_catalog):
    db, saves = event_catalog
    for path in saves.glob("*.rws"):
        path.write_text(path.read_text().replace("<seedString>synthetic-seed-coral</seedString>", ""))
    report = catalog.refresh_catalog(db, freshness="strict")
    sid, other = [s["snapshot_id"] for s in report["sources"]]
    result = service.get_events(db, snapshot_id=sid, freshness="frozen")
    assert result["selection"]["identity_basis"] == "snapshot_only"
    assert result["selection"]["campaign_id"] is None
    assert result["page"]["matching_occurrences"] == 2
    assert {e["snapshot_id"] for e in result["events"]} == {sid}
    assert result["coverage"]["selected_snapshot_count"] == 1
    assert result["coverage"]["compatible_snapshot_count"] == 0
    jsonschema.validate(result, HISTORY_SCHEMAS["events"])
    with pytest.raises(InspectorError) as caught:
        service.get_events(db, snapshot_id=sid, anchor_snapshot_id=other, freshness="frozen")
    assert caught.value.code == "INCOMPATIBLE_SNAPSHOT"
