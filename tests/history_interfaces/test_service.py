"""End-to-end synthetic history expectations independent of source parsing."""

from __future__ import annotations

import hashlib
import json

import pytest
from jsonschema import Draft202012Validator

from rimworld_save_inspector.history import catalog
from rimworld_save_inspector.history.service import (
    expand_context_memberships, get_dossier, get_evidence, get_timeline,
)
from rimworld_save_inspector.history.schemas import HISTORY_SCHEMAS
from rimworld_save_inspector.history.mcp import create_server
from rimworld_save_inspector.cli import main as cli_main
from rimworld_save_inspector.errors import InspectorError


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _snapshot(path):
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    suffix = raw.decode().split(":")[1]
    tick = 100 if suffix == "early" else 200
    name = "Founder" if suffix == "early" else "Nova"
    role = "Colonist" if suffix == "early" else "Leader"
    node = {"tag": "role", "attributes": [], "text": role, "children": []}
    evidence_id = _hash(node)
    source_path = "/savegame[1]/pawn[1]/role[1]"
    source_ref = _hash([digest, "test-v1", source_path])
    claim = {"field": "formal_role", "value": role, "raw_value": role,
             "status": "observed", "unit": None, "code": role, "display_label": None,
             "normalization_rule": None, "time_observation": None,
             "evidence_id": evidence_id, "source_ref": source_ref}
    pawn = {"pawn_id": "Human42", "record_id": "pawn:one",
            "source_path": "/savegame[1]/pawn[1]", "scope": "map",
            "holder_chain": [], "aliases": [{"value": name, "evidence_id": evidence_id,
                                            "source_ref": source_ref}],
            "status": "materialized", "domains": {
                "roles": {"status": "complete", "claims": [claim], "links": []}},
            "evidence_refs": [{"evidence_id": evidence_id, "source_ref": source_ref}]}
    return {
        "snapshot_id": digest, "interpretation_id": "test-v1",
        "metadata": {"profile": {"schema_version": "1.0", "parser_version": "test",
                                 "extraction_version": "test", "game_version": "1.6",
                                 "support_status": "verified"},
                     "campaign_key": {name: {"value": value, "status": "observed"}
                                      for name, value in {
                                          "seedString": "coral", "persistentRandomValue": "81234567",
                                          "initialMapSize": "(250,1,250)", "startingYear": "5500",
                                          "gameStartAbsTick": "52500", "startingTile": "42,0"}.items()},
                     "clocks": {"ticksGame": {"value": tick}, "ticksAbs": {"value": tick + 52500}}},
        "pawns": [pawn], "events": [],
        "context": {"maps": [], "factions": [], "mods": [], "research": [],
                    "scenario": [], "art": [], "related_entities": []},
        "evidence": [{"evidence_id": evidence_id, "source_ref": source_ref,
                      "snapshot_id": digest, "interpretation_id": "test-v1",
                      "path": source_path, "node": node}],
        "coverage": {"source_status": "parsed", "domains": {}, "unknown_fields": [], "limits": {}},
        "diagnostics": [],
    }


def test_one_call_factors_all_snapshots_and_preserves_observed_difference(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "early.rws").write_bytes(b"save:early")
    (saves / "late.rws").write_bytes(b"save:late")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)

    result = get_timeline(db, pawn_id="Human42")
    Draft202012Validator(HISTORY_SCHEMAS["timeline"]).validate(result)
    assert result["subject"]["pawn_id"] == "Human42"
    assert {alias["value"] for alias in result["subject"]["aliases"]} == {"Founder", "Nova"}
    assert result["coverage"]["selected_snapshot_count"] == 2
    assert result["coverage"]["compatible_snapshot_count"] == 2
    assert result["coverage"]["incompatible_snapshot_count"] == 0
    assert result["coverage"]["review_needed_snapshot_count"] == 0
    assert result["coverage"]["domain_status_counts"]["roles"]["complete"] == 2
    assert result["coverage"]["earliest_observed_tick"] == 100
    assert result["coverage"]["latest_observed_tick"] == 200
    assert result["timeline"]["lineage_status"] == "unproven"
    assert len(result["timeline"]["state_observations"]) == 2
    assert any(change["field"] == "roles.formal_role" for change in result["timeline"]["observed_differences"])
    assert {fact["value"] for fact in result["context"]["facts"].values()} == {"Colonist", "Leader"}
    assert len(result["context"]["memberships"]) == 2
    assert result["size"]["semantic_complete"] is True
    expanded = get_timeline(db, pawn_id="Human42", detail="full")
    reconstructed = expanded["context"]["expanded_observations"]
    assert len(reconstructed) == 2
    for observation in reconstructed:
        claim = observation["claim"]
        assert claim["raw_value"] == claim["value"]
        assert claim["evidence_id"] and claim["source_ref"]
    assert len(expanded["timeline"]["expanded_differences"]) == len(
        expanded["timeline"]["observed_differences"])
    assert expanded["timeline"]["expanded_differences"][0]["before_claims"][0]["evidence_id"]

    named = get_timeline(db, name="Founder")
    assert named["subject"]["pawn_id"] == "Human42"
    dossier = get_dossier(db, pawn_id="Human42", snapshot_id=result["timeline"]["state_observations"][0]["snapshot_id"])
    Draft202012Validator(HISTORY_SCHEMAS["dossier"]).validate(dossier)
    assert dossier["subject"]["pawn_id"] == "Human42"


def test_timeline_loads_context_without_unrelated_snapshot_inventory(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "early.rws").write_bytes(b"save:early")
    (saves / "late.rws").write_bytes(b"save:late")

    def extraction(path):
        snapshot = _snapshot(path)
        snapshot["coverage"]["unknown_fields"] = [
            {"path": f"/unrelated[{index}]", "raw": "x" * 128}
            for index in range(4000)
        ]
        snapshot["context"]["scenario"] = [{"id": "selected-shared-context"}]
        return snapshot

    monkeypatch.setattr(catalog, "_extract_snapshot", extraction)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    real_unpack = catalog.unpack

    def context_only(blob):
        result = real_unpack(blob)
        assert not (isinstance(result, dict) and "coverage" in result)
        return result

    monkeypatch.setattr(catalog, "unpack", context_only)
    result = get_timeline(db, pawn_id="Human42", freshness="frozen")
    assert result["coverage"]["selected_snapshot_count"] == 2
    assert any(value["scenario"] == [{"id": "selected-shared-context"}]
               for value in result["context"]["shared"].values())


def test_containing_pawn_evidence_keeps_field_locator_through_fact_inverse(
        tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    source = saves / "early.rws"
    source.write_bytes(b"save:early")
    expected = {}

    def extraction(path):
        snapshot = _snapshot(path)
        pawn = snapshot["pawns"][0]
        node = {"tag": "thing", "attributes": [["Class", "Pawn"]], "text": None,
                "children": [{"node": {"tag": "role", "attributes": [], "text": "Colonist",
                                       "children": []}, "tail": None}]}
        evidence_id = _hash(node)
        source_path = pawn["source_path"]
        source_ref = _hash([snapshot["snapshot_id"], snapshot["interpretation_id"],
                            source_path])
        claim = pawn["domains"]["roles"]["claims"][0]
        claim.update({"raw_field": "/role[1]", "evidence_id": evidence_id,
                      "source_ref": source_ref,
                      "time_observation": {"raw_field": "/role[1]/@changedAt",
                                           "raw_value": "100", "status": "known"}})
        pawn["evidence_refs"] = [{"evidence_id": evidence_id, "source_ref": source_ref}]
        snapshot["evidence"] = [{"evidence_id": evidence_id, "source_ref": source_ref,
                                 "snapshot_id": snapshot["snapshot_id"],
                                 "interpretation_id": snapshot["interpretation_id"],
                                 "path": source_path, "node": node}]
        expected.update({"claim": dict(claim), "node": node})
        return snapshot

    monkeypatch.setattr(catalog, "_extract_snapshot", extraction)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    compact = get_timeline(db, pawn_id="Human42", freshness="frozen")
    membership = compact["context"]["memberships"][0]["claims"][0]
    assert membership[2]["x"]["raw_field"] == "/role[1]"
    expanded = get_timeline(db, pawn_id="Human42", detail="full", freshness="frozen")
    assert expanded["context"]["expanded_observations"][0]["claim"] == expected["claim"]
    evidence = get_evidence(db, compact["evidence"][0]["evidence_id"])
    assert evidence["node"] == expected["node"]
    assert evidence["path"] == "/savegame[1]/pawn[1]"


def test_dense_membership_inverse_preserves_repeated_raw_and_mixed_time(
        tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "early.rws").write_bytes(b"save:early")
    expected = []

    def extraction(path):
        snapshot = _snapshot(path)
        pawn = snapshot["pawns"][0]
        base = pawn["domains"]["roles"]["claims"][0]
        pair = {"evidence_id": base["evidence_id"], "source_ref": base["source_ref"]}

        def claim(field, value, raw_value, status, raw_field, time=None, **extensions):
            return {"field": field, "value": value, "raw_value": raw_value,
                    "status": status, "unit": None, "code": None,
                    "display_label": None, "normalization_rule": None,
                    "time_observation": time, "raw_field": raw_field,
                    **extensions, **pair}

        repeated = [
            claim("roles.rank", 1, "01", "observed", "/roles[1]/li[1]"),
            claim("roles.rank", 1, "1", "observed", "/roles[1]/li[2]"),
            claim("roles.rank", 1, "1", "observed", "/roles[1]/li[3]"),
        ]
        empty = claim("inventory.items", [], None, "observed", "/inventory[1]")
        absent = claim("guest.host", None, None, "absent", "/guest")
        timed = claim("health.lastChange", 730, "000730", "observed",
                      "/health[1]/lastChange[1]",
                      {"raw_field": "/health[1]/lastChange[1]",
                       "raw_value": "000730", "clock": "game_relative",
                       "normalized_game_tick": 730, "status": "known"})
        unknown = claim("unknown_fields.opaque[1]", "?", "opaque", "unsupported",
                        "/opaque[1]", structural_kind="unknown_pawn_field",
                        serialized_bytes=42)
        pawn["domains"] = {
            "roles": {"status": "complete", "claims": repeated, "links": []},
            "inventory": {"status": "complete", "claims": [empty], "links": []},
            "guest": {"status": "absent", "claims": [absent], "links": []},
            "health": {"status": "complete", "claims": [timed], "links": []},
            "unknown_fields": {"status": "partial", "claims": [unknown], "links": []},
        }
        snapshot["events"] = [{
            "kind": "tale", "saved_event_id": "tale:Tale_1",
            "participants": [{"pawn_id": "Human42", "role": "subject"}],
            "occurrence": {"status": "known", "normalized_game_tick": 80,
                           "raw_field": "/tales[1]/li[1]/occurred[1]", "raw_value": "080"},
            "claims": [claim("event.text", "Arrival", "Arrival", "observed",
                             "/tales[1]/li[1]/text[1]")],
            "evidence_refs": [pair],
        }]
        expected.extend((domain, item) for domain, items in (
            ("roles", repeated), ("inventory", [empty]), ("guest", [absent]),
            ("health", [timed]), ("unknown_fields", [unknown])) for item in items)
        return snapshot

    monkeypatch.setattr(catalog, "_extract_snapshot", extraction)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    compact = get_timeline(db, pawn_id="Human42", freshness="frozen")
    context = compact["context"]
    assert any(alias["value"] == "Founder" for alias in compact["subject"]["aliases"])
    assert compact["timeline"]["recorded_events"][0]["saved_event_id"] == "tale:Tale_1"
    assert compact["timeline"]["recorded_events"][0]["claims"][0]["raw_field"] == (
        "/tales[1]/li[1]/text[1]")
    assert context["membership_encoding"] == "context/1"
    assert len(context["memberships"]) == 1
    assert len(context["memberships"][0]["claims"]) == len(expected)
    assert len(context["fact_index"]) < len(expected)
    assert len(context["unknown_inventory_index"]) == 1
    group_index, claim_index = context["unknown_inventory_index"][0]
    assert group_index == 0
    assert context["memberships"][0]["claims"][claim_index][2]["x"]["serialized_bytes"] == 42
    inverse = expand_context_memberships(context)
    assert [(item["domain"], item["claim"]) for item in inverse] == sorted(
        expected, key=lambda item: item[0])
    assert [item["claim"]["raw_value"] for item in inverse
            if item["domain"] == "roles"] == ["01", "1", "1"]
    expanded = get_timeline(db, pawn_id="Human42", freshness="frozen", detail="full")
    assert expanded["context"]["expanded_observations"] == inverse


def test_generation_bound_evidence_survives_refresh(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    source = saves / "one.rws"
    source.write_bytes(b"save:early")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    timeline = get_timeline(db, pawn_id="Human42")
    token = timeline["evidence"][0]["evidence_id"]
    old_generation_id = timeline["catalog"]["generation_id"]
    first_page = get_evidence(db, token, limit=10)
    assert first_page["next_cursor"]
    source.write_bytes(b"save:late")
    catalog.refresh_catalog(db)
    old = get_evidence(db, token)
    Draft202012Validator(HISTORY_SCHEMAS["evidence"]).validate(old)
    assert old["node"]["text"] == "Colonist"
    assert old["catalog"]["generation_id"] != catalog.inspect_catalog(db)["catalog"]["generation_id"]
    old_page = get_evidence(db, token, cursor=first_page["next_cursor"], limit=10)
    assert old_page["offset"] == 10
    old_timeline = get_timeline(db, pawn_id="Human42", generation_id=old_generation_id)
    assert old_timeline["context"]["latest_dossiers"][0]["domains"]["roles"]["claims"][0]["value"] == "Colonist"
    old_dossier = get_dossier(db, pawn_id="Human42", generation_id=old_generation_id)
    assert old_dossier["dossier"]["pawns"][0]["domains"]["roles"]["claims"][0]["value"] == "Colonist"
    import anyio
    old_mcp = anyio.run(create_server(db).call_tool, "get_pawn_timeline",
                        {"pawn_id": "Human42", "generation_id": old_generation_id})
    assert not old_mcp.is_error
    assert old_mcp.structured_content["catalog"]["generation_id"] == old_generation_id
    current_token = get_timeline(db, pawn_id="Human42")["evidence"][0]["evidence_id"]
    with pytest.raises(Exception, match="cursor"):
        get_evidence(db, current_token, cursor=first_page["next_cursor"], limit=10)


def test_name_collision_requires_disambiguation(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "one.rws").write_bytes(b"save:early")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    with pytest.raises(Exception, match="Exactly one|selector|pawn_id"):
        get_timeline(db, pawn_id="Human42", name="Founder")


def test_response_cap_and_evidence_cursor_fail_explicitly(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "one.rws").write_bytes(b"save:early")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    with pytest.raises(Exception, match="exceeds"):
        get_timeline(db, pawn_id="Human42", response_limit=256)
    token = get_timeline(db, pawn_id="Human42")["evidence"][0]["evidence_id"]
    with pytest.raises(InspectorError) as invalid_cursor:
        get_evidence(db, token, cursor="h1.invalid", limit=10)
    assert invalid_cursor.value.code == "INVALID_CURSOR"
    with pytest.raises(Exception, match="page limit"):
        get_evidence(db, token, limit=512 * 1024 + 1)


def test_idless_saved_events_keep_snapshot_origins_separate(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "early.rws").write_bytes(b"save:early")
    (saves / "late.rws").write_bytes(b"save:late")

    def with_idless_event(path):
        snapshot = _snapshot(path)
        snapshot["events"] = [{
            "kind": "play_log", "saved_event_id": None,
            "participants": [{"pawn_id": "Human42", "role": "unknown"}],
            "occurrence": {"status": "unknown", "normalized_game_tick": None},
            "claims": [], "evidence_refs": [],
        }]
        return snapshot

    monkeypatch.setattr(catalog, "_extract_snapshot", with_idless_event)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    result = get_timeline(db, pawn_id="Human42")
    events = result["timeline"]["unknown_time_events"]
    assert len(events) == 2
    assert all(item["possible_duplicate"] for item in events)
    assert len({item["supporting_snapshots"][0] for item in events}) == 2


def test_mcp_is_configured_and_carries_one_structured_payload(tmp_path, monkeypatch):
    import anyio
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "one.rws").write_bytes(b"save:early")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    server = create_server(db)
    tools = anyio.run(server.list_tools)
    assert {tool.name for tool in tools} == {
        "get_save_catalog", "get_pawn_dossier", "get_pawn_timeline",
        "get_history_evidence", "get_history_schema",
        "get_history_events",
        "get_history_discovery",
    }
    result = anyio.run(server.call_tool, "get_pawn_timeline", {"pawn_id": "Human42"})
    assert not result.is_error
    assert result.structured_content["subject"]["pawn_id"] == "Human42"
    assert len(result.content) == 1
    assert len(result.content[0].text) < 256
    assert "latest_dossiers" not in result.content[0].text
    assert len(result.model_dump_json(by_alias=True).encode()) < 16 * 1024 * 1024


def test_cli_history_matches_shared_service_without_changing_legacy_parser(tmp_path, monkeypatch, capsys):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "one.rws").write_bytes(b"save:early")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    assert cli_main(["catalog", "build", str(saves), "--output", str(db)]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["history_schema_version"] == "1.0"
    assert cli_main(["timeline", str(db), "--pawn-id", "Human42"]) == 0
    cli_value = json.loads(capsys.readouterr().out)
    service_value = get_timeline(db, pawn_id="Human42")
    assert cli_value["subject"] == service_value["subject"]
    assert cli_value["context"] == service_value["context"]
    import anyio
    mcp_value = anyio.run(create_server(db).call_tool, "get_pawn_timeline",
                          {"pawn_id": "Human42"}).structured_content
    assert mcp_value["subject"] == cli_value["subject"]
    assert mcp_value["context"] == cli_value["context"]
    assert mcp_value["timeline"] == cli_value["timeline"]
    assert cli_main(["dossier", str(db), "--snapshot-id",
                     cli_value["timeline"]["state_observations"][0]["snapshot_id"],
                     "--pawn-id", "Human42"]) == 0
    assert json.loads(capsys.readouterr().out)["subject"]["pawn_id"] == "Human42"


def test_journal_checkpoint_join_requires_verified_matching_output_hash(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    source = saves / "one.rws"
    source.write_bytes(b"save:late")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    def anchored(path):
        snapshot = _snapshot(path)
        snapshot["metadata"]["clocks"]["gameStartAbsTick"] = {"value": 52500}
        snapshot["metadata"]["clocks"]["startingYear"] = {"value": 5500}
        snapshot["context"]["related_entities"].append({
            "id": "recorder-1", "kind": "recorder_checkpoint", "links": [],
            "evidence_refs": [], "claims": [
                {"field": "campaign_token", "value": "recorder-campaign", "status": "observed"},
                {"field": "checkpoint_id", "value": "cp-1", "status": "observed"},
                {"field": "producer_session_id", "value": "session-1", "status": "observed"},
            ],
        })
        return snapshot

    monkeypatch.setattr(catalog, "_extract_snapshot", anchored)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)

    common = {
        "schema_version": "1.0", "recorder_version": "0.1.0", "game_version": "1.6",
        "campaign_id": "recorder-campaign", "session_id": "session-1",
        "game_start_abs_tick": 52500, "subject_id": None, "related_ids": [],
        "map_id": None, "source": {"collector": "synthetic", "semantics": "coverage"},
    }
    started = {**common, "sequence": 1, "event_id": "session-1:1",
               "event_type": "session_started", "ticks_game": 0, "ticks_abs": 52500,
               "checkpoint_id": None,
               "payload": {"loaded_checkpoint_id": None, "parent_session_id": None,
                           "parent_association": "absent", "collectors": [],
                           "time_basis": {"starting_year": 5500}}}
    checkpoint = {**common, "sequence": 2, "event_id": "session-1:2",
                  "event_type": "save_checkpoint", "ticks_game": 200, "ticks_abs": 52700,
                  "checkpoint_id": "cp-1",
                  "payload": {"checkpoint_id": "cp-1", "save_attempt_id": "attempt-1",
                              "status": "succeeded", "parent_checkpoint_id": None,
                              "observed_sequence": 1, "written_sequence": 1,
                              "durable_sequence": 1, "output_association": "verified",
                              "output_sha256": digest, "output_bytes": len(source.read_bytes())}}
    journal = tmp_path / "journal.jsonl"

    def write_journal(event):
        journal.write_text("\n".join(json.dumps(row, sort_keys=True)
                                     for row in (started, event)) + "\n")

    write_journal(checkpoint)
    joined = get_timeline(db, pawn_id="Human42", journal_path=journal)
    assert joined["context"]["recorder_joins"][0]["status"] == "verified"
    assert joined["timeline"]["lineage_status"] == "unproven"
    assert joined["timeline"]["journal_events"][0]["event"]["event_type"] == "session_started"

    no_hash = json.loads(json.dumps(checkpoint))
    del no_hash["payload"]["output_sha256"]
    write_journal(no_hash)
    unverified = get_timeline(db, pawn_id="Human42", journal_path=journal)
    assert unverified["context"]["recorder_joins"][0]["status"] == "unverified"

    bad_hash = json.loads(json.dumps(checkpoint))
    bad_hash["payload"]["output_sha256"] = "0" * 64
    write_journal(bad_hash)
    mismatched = get_timeline(db, pawn_id="Human42", journal_path=journal)
    assert mismatched["context"]["recorder_joins"][0]["status"] == "unverified"
    unavailable = get_timeline(db, pawn_id="Human42", journal_path=tmp_path / "missing.jsonl")
    assert unavailable["coverage"]["journal"]["status"] == "failed"
    assert unavailable["context"]["recorder_joins"][0]["status"] == "journal_unavailable"


def test_real_source_dossier_retains_saved_event_for_selected_pawn(tmp_path):
    from pathlib import Path
    fixture = Path(__file__).parents[1] / "history_oracle" / "fixtures" / "alpha-early.xml"
    saves = tmp_path / "saves"
    saves.mkdir()
    save = saves / "alpha.rws"
    save.write_bytes(fixture.read_bytes())
    direct = get_dossier(save, pawn_id="pawn-alpha-01")
    assert direct["dossier"]["events"][0]["saved_event_id"] == "tale:Tale:Tale_1"
    direct_evidence = get_evidence(save, direct["evidence"][0]["evidence_id"])
    assert direct_evidence["node"]["tag"]
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    indexed = get_dossier(db, pawn_id="pawn-alpha-01")
    assert indexed["dossier"]["events"][0]["saved_event_id"] == "tale:Tale:Tale_1"


def test_history_mcp_stdio_frame_has_one_structured_payload(tmp_path):
    import os
    import select
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).parents[2]
    fixture = root / "tests" / "history_oracle" / "fixtures" / "alpha-early.xml"
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "alpha.rws").write_bytes(fixture.read_bytes())
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "wire-test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_pawn_timeline", "arguments": {"pawn_id": "pawn-alpha-01"}}},
    ]
    process = subprocess.Popen(
        [sys.executable, "-m", "rimworld_save_inspector.cli", "history-mcp",
         "--catalog", str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
    )
    try:
        def send(request):
            process.stdin.write(json.dumps(request).encode() + b"\n")
            process.stdin.flush()

        def receive():
            assert select.select([process.stdout], [], [], 10)[0]
            return process.stdout.readline()

        send(requests[0])
        assert json.loads(receive())["id"] == 1
        send(requests[1])
        send(requests[2])
        raw_frame = receive()
    finally:
        process.stdin.close()
        process.wait(timeout=10)
    frame = json.loads(raw_frame)
    assert frame["id"] == 2
    assert len(raw_frame) < 16 * 1024 * 1024
    assert frame["result"]["structuredContent"]["subject"]["pawn_id"] == "pawn-alpha-01"
    assert len(frame["result"]["content"][0]["text"]) < 256
