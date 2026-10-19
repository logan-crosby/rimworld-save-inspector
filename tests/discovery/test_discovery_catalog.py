from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from rimworld_save_inspector.errors import InspectorError
from rimworld_save_inspector.history import catalog
from rimworld_save_inspector.history.models import evidence_id_for_node, record_id_for_occurrence, source_ref_for
from rimworld_save_inspector.history.service import get_dossier, get_evidence, get_events, get_timeline
from rimworld_save_inspector.discovery.catalog import _episode_key, _selected_packet
from rimworld_save_inspector.discovery import (OfflineSemanticAdapter, discover_catalog,
                                               semantic_cache_key)


_FIELDS = {
    "seedString": "coral", "persistentRandomValue": "81234567", "initialMapSize": "(250,1,250)",
    "startingYear": "5500", "gameStartAbsTick": "52500", "startingTile": "42,0",
}


def _snapshot(path: Path) -> dict:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        tick = int(raw.decode().split(":")[1])
    except (IndexError, ValueError, UnicodeDecodeError):
        raise ValueError("synthetic failure")
    interpretation = "synthetic-discovery/1"
    node = {"tag": "work", "attributes": [], "text": "cook", "children": []}
    evidence_id = evidence_id_for_node(node)
    source_path = f"/pawn[{tick}]/work[1]"
    source_ref = source_ref_for(digest, interpretation, source_path)
    claim = {"field": "work.assignment", "raw_field": "/work[1]", "value": "cook",
             "raw_value": "cook", "status": "observed", "unit": None, "code": "Cook",
             "display_label": "Cook", "normalization_rule": None, "time_observation": None,
             "evidence_id": evidence_id, "source_ref": source_ref}
    return {
        "snapshot_id": digest, "interpretation_id": interpretation,
        "metadata": {
            "profile": {"profile_id": interpretation, "support_status": "verified"},
            "campaign_key": {"fields": {key: {"value": value, "status": "observed"}
                                           for key, value in _FIELDS.items()},
                             "identity": {"status": "compatible", "value": "ignored-display-value"}},
            "clocks": {"ticksGame": {"value": tick}, "ticksAbs": {"value": tick + 52500},
                       "startingYear": {"value": 5500}},
        },
        "pawns": [{"pawn_id": "Human42", "record_id": record_id_for_occurrence(
                    digest, interpretation, "pawn", f"/pawn[{tick}]"),
                   "source_path": f"/pawn[{tick}]", "scope": "map", "holder_chain": [],
                   "aliases": [{"field": "name.nick", "value": "Nova", "evidence_id": evidence_id,
                                "source_ref": source_ref}], "status": "materialized",
                   "domains": {"work": {"status": "complete", "claims": [claim], "links": []},
                               "genes": {"status": "absent", "claims": [], "links": []}},
                   "evidence_refs": [{"evidence_id": evidence_id, "source_ref": source_ref}]}],
        "events": [],
        "context": {"maps": [], "factions": [], "mods": [], "research": [], "scenario": [],
                    "art": [], "related_entities": []},
        "evidence": [{"evidence_id": evidence_id, "source_ref": source_ref,
                      "snapshot_id": digest, "interpretation_id": interpretation,
                      "path": source_path, "node": node}],
        "coverage": {"source_status": "parsed", "domains": {}, "unknown_fields": [], "limits": {}},
        "diagnostics": [],
    }


def _make_catalog(tmp_path: Path, monkeypatch, rows: int = 2):
    saves = tmp_path / "saves"
    saves.mkdir(parents=True)
    for tick in range(rows):
        (saves / f"{tick:04}.rws").write_text(f"save:{tick}", encoding="utf-8")

    def extract(path):
        if path.name == "failed.rws":
            raise ValueError("synthetic parse failure")
        return _snapshot(path)

    monkeypatch.setattr(catalog, "_extract_snapshot", extract)
    db = tmp_path / "history.sqlite"
    built = catalog.build_catalog(saves, db)
    return saves, db, built


def test_catalog_discovery_streams_151_snapshots_and_reports_every_outcome(tmp_path, monkeypatch):
    saves, db, built = _make_catalog(tmp_path, monkeypatch, rows=151)
    shutil.copyfile(saves / "0000.rws", saves / "duplicate-source.rws")
    (saves / "failed.rws").write_text("bad", encoding="utf-8")
    refreshed = catalog.refresh_catalog(db)
    original_unpack = catalog.unpack
    calls = []

    def counted_unpack(blob, *, limit=128 * 1024 * 1024):
        calls.append((len(blob), limit))
        return original_unpack(blob, limit=limit)

    monkeypatch.setattr(catalog, "unpack", counted_unpack)
    result = discover_catalog(db, freshness="frozen", generation_id=refreshed["catalog"]["generation_id"])

    assert result["coverage"]["snapshot_count"] == 151
    assert result["coverage"]["pawn_record_count"] == 151
    assert result["coverage"]["event_record_count"] == 0
    assert result["coverage"]["source_outcome_count"] == 153
    assert result["coverage"]["source_outcome_status_counts"]["failed"] == 1
    assert len(result["entities"]) == 1
    assert result["entities"][0]["observation_count"] == 151
    assert result["entities"][0]["latest_observation_by_game_tick"]["game_tick"] == 150
    assert result["entities"][0]["continuity_status"] == "unproven"
    assert len(calls) == 453  # two pawn passes plus one bounded context row per snapshot
    assert sum(1 for _, cap in calls if cap == 2 * 1024 * 1024) == 151
    assert {limit for _, limit in calls} == {8 * 1024 * 1024, 2 * 1024 * 1024}
    assert result["evidence_use"]["queried"]["snapshot_payloads_decompressed"] == 0
    assert result["source_channel"]["recorder_channel"] == "not_included"
    assert result["coverage"]["domains"]["work"]["selected_occurrence_count"] == 151
    exact_bytes = len(json.dumps(result, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8"))
    assert result["size"]["serialized_bytes"] == exact_bytes


def test_actual_public_xml_campaign_ids_join_stable_pawn_ids_and_separate_same_name(tmp_path):
    fixtures = Path(__file__).parents[1] / "history_oracle" / "fixtures"
    saves = tmp_path / "saves"
    saves.mkdir()
    names = ("alpha-early.xml", "alpha-later.xml", "campaign-b-same-name.xml")
    for name in names:
        shutil.copyfile(fixtures / name, saves / (Path(name).stem + ".rws"))
    db = tmp_path / "actual-source.sqlite"
    catalog.build_catalog(saves, db)

    result = discover_catalog(db, freshness="frozen")
    founders = [entity for entity in result["entities"] if entity["saved_id"] == "pawn-alpha-01"]

    assert len(founders) >= 2
    assert len({entity["campaign_id"] for entity in founders}) >= 2
    assert all(entity["join_basis"] == "validated_campaign_and_saved_pawn_id" for entity in founders)


def test_selected_highlight_evidence_token_round_trips_and_binds_generation(tmp_path, monkeypatch):
    _, db, built = _make_catalog(tmp_path, monkeypatch, rows=1)
    result = discover_catalog(db, freshness="frozen")
    claim = result["entities"][0]["domain_highlights"]["work"]["fact_bundles"][0]["memberships"][0]
    evidence_token = claim["evidence"][0]["retrieval_id"]

    retained = get_evidence(db, evidence_token)

    assert retained["catalog"]["generation_id"] == built["catalog"]["generation_id"]
    assert retained["source_ref"] == claim["evidence"][0]["source_ref"]
    assert retained["node"]["text"] == "cook"


def test_cross_catalog_generation_token_is_rejected_as_invalid_cursor(tmp_path, monkeypatch):
    _, first_db, _ = _make_catalog(tmp_path / "first", monkeypatch, rows=1)
    first = discover_catalog(first_db, freshness="frozen")
    token = first["entities"][0]["domain_highlights"]["work"]["fact_bundles"][0]["memberships"][0]["evidence"][0]["retrieval_id"]
    saves = tmp_path / "second" / "saves"
    saves.mkdir(parents=True)
    (saves / "0.rws").write_text("save:0", encoding="utf-8")
    second_db = tmp_path / "second" / "history.sqlite"
    catalog.build_catalog(saves, second_db)

    with pytest.raises(InspectorError) as error:
        get_evidence(second_db, token)

    assert error.value.code in {"INVALID_CURSOR", "GENERATION_UNAVAILABLE"}


def test_response_limit_returns_measured_result_too_large_without_truncation(tmp_path, monkeypatch):
    _, db, _ = _make_catalog(tmp_path, monkeypatch, rows=2)

    with pytest.raises(InspectorError) as error:
        discover_catalog(db, freshness="frozen", response_limit=256)

    assert error.value.code == "RESULT_TOO_LARGE"
    assert error.value.details["required_bytes"] > error.value.details["configured_limit"]
    assert error.value.details["selectors"]["generation_id"]


def test_same_tick_branch_alternatives_are_not_ordered_and_unknown_tick_is_not_latest(tmp_path, monkeypatch):
    saves = tmp_path / "branch-saves"
    saves.mkdir()
    (saves / "known-a.rws").write_text("save:7:a", encoding="utf-8")
    (saves / "known-b.rws").write_text("save:7:b", encoding="utf-8")
    (saves / "unknown.rws").write_text("save:9:unknown", encoding="utf-8")

    def extract(path):
        snapshot = _snapshot(path)
        if path.name == "unknown.rws":
            snapshot["metadata"]["clocks"]["ticksGame"] = {"value": None}
        return snapshot

    monkeypatch.setattr(catalog, "_extract_snapshot", extract)
    db = tmp_path / "branch.sqlite"
    catalog.build_catalog(saves, db)
    result = discover_catalog(db, freshness="frozen")
    entity = result["entities"][0]

    assert entity["latest_observation_by_game_tick"]["status"] == "same_tick_alternatives"
    assert entity["latest_observation_by_game_tick"]["game_tick"] == 7
    assert entity["latest_observation_by_game_tick"]["alternative_count"] == 2
    assert entity["latest_observation_by_game_tick"]["unknown_clock_observation_count"] == 1


def test_semantic_cache_key_changes_for_generation_channel_resolver_and_prompt():
    class Annotator:
        def annotate(self, packet):
            return {"claims": []}

    adapter = OfflineSemanticAdapter(Annotator(), model_id="local/test",
                                     options={"prompt_version": "prompt/1"})
    base = {"sources": [{"snapshot_id": "a"}],
            "catalog": {"generation_id": "generation-a"},
            "source_channel": {"id": "save_xml", "resolver_version": "resolver/1"}}
    keys = {semantic_cache_key(base, adapter),
            semantic_cache_key({**base, "catalog": {"generation_id": "generation-b"}}, adapter),
            semantic_cache_key({**base, "source_channel": {"id": "recorder", "resolver_version": "resolver/1"}}, adapter),
            semantic_cache_key({**base, "source_channel": {"id": "save_xml", "resolver_version": "resolver/2"}}, adapter),
            semantic_cache_key(base, OfflineSemanticAdapter(Annotator(), model_id="local/test",
                                                             options={"prompt_version": "prompt/2"}))}

    assert len(keys) == 5


def test_catalog_offline_adapter_sees_bounded_channel_packet_and_keeps_annotations_separate(tmp_path, monkeypatch):
    _, db, _ = _make_catalog(tmp_path, monkeypatch, rows=1)

    class Annotator:
        packet = None

        def annotate(self, packet):
            self.packet = packet
            cited = packet["evidence_use"]["included"][0]
            return {"claims": [{"text": "The pawn has a cooking assignment.",
                                 "support": [cited["evidence_id"]],
                                 "source_refs": [cited["source_ref"]]}]}

    annotator = Annotator()
    result = discover_catalog(db, freshness="frozen",
                              semantic_adapter=OfflineSemanticAdapter(annotator, model_id="offline/test"))

    assert annotator.packet["source_channel"]["id"] == "save_xml_catalog"
    assert annotator.packet["catalog"]["generation_id"] == result["catalog"]["generation_id"]
    assert annotator.packet["evidence_use"]["included"]
    assert annotator.packet["evidence_use"]["used"] == []
    assert result["annotations"][0]["status"] == "needs_semantic_review"
    assert result["annotations"][0]["semantic_entailment"] == "unchecked"
    assert result["entities"][0]["domain_highlights"]["work"]["fact_bundles"][0]["value"] == "cook"


def test_episode_occurrence_sampling_is_bounded_and_names_event_query_gap(tmp_path, monkeypatch):
    saves = tmp_path / "event-saves"
    saves.mkdir()
    for index in range(12):
        (saves / f"{index:02}.rws").write_text(f"save:{index}", encoding="utf-8")

    def extract(path):
        snapshot = _snapshot(path)
        claim = snapshot["pawns"][0]["domains"]["work"]["claims"][0]
        snapshot["events"] = [{
            "record_id": record_id_for_occurrence(snapshot["snapshot_id"], snapshot["interpretation_id"],
                                                  "event", "/event[1]"),
            "saved_event_id": "Event77", "kind": "injury",
            "occurrence": {"raw_value": "7", "raw_field": "/occurred", "clock": "game",
                           "normalized_game_tick": 7, "status": "known"},
            "claims": [{**claim, "field": "event.kind", "value": "injury"}],
            "participants": [], "evidence_refs": [{"evidence_id": claim["evidence_id"],
                                                     "source_ref": claim["source_ref"]}]}]
        return snapshot

    monkeypatch.setattr(catalog, "_extract_snapshot", extract)
    db = tmp_path / "events.sqlite"
    catalog.build_catalog(saves, db)
    result = discover_catalog(db, freshness="frozen")

    assert len(result["episodes"]) == 1
    episode = result["episodes"][0]
    assert episode["total_occurrence_count"] == 12
    assert episode["selected_occurrence_count"] == 8
    assert episode["omitted_occurrence_count"] == 4
    followup = next(item for item in result["evidence_use"]["unresolved"]
                    if item["uncertainty"] == "omitted_or_unsampled_event_records")
    assert followup["selector"]["mcp"] == "get_history_events"
    assert followup["selector"]["availability"] == "available_for_each_omitted_source"
    assert result["coverage"]["no_pruning"]["event_records_retrievable"] is True
    assert result["evidence_use"]["excluded"]["events"]["record_count"] == 4

    anchor = result["sources"][0]["snapshot_id"]
    page = get_events(db, anchor_snapshot_id=anchor,
                      generation_id=result["catalog"]["generation_id"],
                      saved_event_id="Event77", limit=1, freshness="frozen")
    assert page["page"]["matching_occurrences"] == 12
    assert page["events"][0]["event"]["participants"] == []
    assert page["events"][0]["event"]["saved_event_id"] == "Event77"
    evidence_token = page["events"][0]["evidence"][0]["evidence_id"]
    assert get_evidence(db, evidence_token)["node"]["text"] == "cook"
    next_page = get_events(db, anchor_snapshot_id=anchor,
                           generation_id=result["catalog"]["generation_id"],
                           saved_event_id="Event77", cursor=page["page"]["next_cursor"],
                           limit=1, freshness="frozen")
    assert next_page["events"][0]["event"]["saved_event_id"] == "Event77"


def test_unknown_clock_entity_followup_uses_an_actual_observed_snapshot(tmp_path, monkeypatch):
    saves = tmp_path / "unknown-clock-saves"
    saves.mkdir()
    (saves / "source.rws").write_text("save:0", encoding="utf-8")
    def extract(path):
        snapshot = _snapshot(path)
        snapshot["metadata"]["clocks"]["ticksGame"]["value"] = None
        return snapshot
    monkeypatch.setattr(catalog, "_extract_snapshot", extract)
    db = tmp_path / "unknown-clock.sqlite"
    catalog.build_catalog(saves, db)
    result = discover_catalog(db, freshness="frozen")
    entity = result["entities"][0]
    assert entity["latest_observation_by_game_tick"]["status"] == "unknown_clock"
    assert entity["followup"]["availability"] == "available"
    assert entity["followup"]["params"]["anchor_snapshot_id"] in {row["snapshot_id"] for row in result["sources"]}
    timeline = get_timeline(db, pawn_id="Human42", anchor_snapshot_id=entity["followup"]["params"]["anchor_snapshot_id"],
                            generation_id=result["catalog"]["generation_id"], freshness="frozen")
    assert timeline["subject"]["pawn_id"] == "Human42"


def test_episode_key_canonicalizes_reordered_claim_sets_but_preserves_duplicates():
    a = {"kind": "injury", "saved_event_id": "E", "claims": [
        {"field": "event.kind", "value": "injury", "status": "observed"},
        {"field": "event.site", "value": "arm", "status": "observed"}], "participants": []}
    b = {**a, "claims": list(reversed(a["claims"]))}
    assert _episode_key(a, "snapshot", "campaign", 0) == _episode_key(b, "snapshot", "campaign", 0)
    duplicate = {**a, "claims": a["claims"] + [a["claims"][0]]}
    assert _episode_key(a, "snapshot", "campaign", 0) != _episode_key(duplicate, "snapshot", "campaign", 0)


def test_event_claim_evidence_is_included_but_only_marked_used_by_its_annotation(tmp_path, monkeypatch):
    saves, db, _ = _make_catalog(tmp_path, monkeypatch, rows=1)
    def extract(path):
        snapshot = _snapshot(path)
        claim = dict(snapshot["pawns"][0]["domains"]["work"]["claims"][0])
        claim.update(field="event.kind", value="injury")
        snapshot["events"] = [{"record_id": record_id_for_occurrence(snapshot["snapshot_id"],
                                snapshot["interpretation_id"], "event", "/event[1]"),
                               "saved_event_id": "E", "kind": "injury", "claims": [claim],
                               "participants": [], "evidence_refs": []}]
        return snapshot
    monkeypatch.setattr(catalog, "_extract_snapshot", extract)
    (saves / "event.rws").write_text("save:9", encoding="utf-8")
    catalog.refresh_catalog(db)

    class Adapter:
        def annotate(self, packet):
            self.packet = packet
            claim = packet["episodes"][0]["claims"][0]
            member = claim["memberships"][0]["evidence"][0]
            return {"claims": [{"text": "Recorded claim.", "support": [member["content_id"]],
                                 "source_refs": [member["source_ref"]]}]}
    adapter = Adapter()
    result = discover_catalog(db, freshness="frozen", semantic_adapter=OfflineSemanticAdapter(adapter, model_id="offline/test"))
    packet = adapter.packet
    event_claim = packet["episodes"][0]["claims"][0]
    member = event_claim["memberships"][0]["evidence"][0]
    assert {"evidence_id": member["content_id"], "source_ref": member["source_ref"]} in packet["evidence_use"]["included"]
    assert packet["evidence_use"]["used"] == []
    assert result["annotations"][0]["semantic_entailment"] == "unchecked"
    assert result["evidence_use"]["used_by_annotations"][0]["annotation_index"] == 0


def test_linked_artifact_summary_has_snapshot_specific_dossier_selectors(tmp_path, monkeypatch):
    saves = tmp_path / "art-saves"
    saves.mkdir()
    for tick in range(2):
        (saves / f"{tick}.rws").write_text(f"save:{tick}", encoding="utf-8")
    def extract(path):
        snapshot = _snapshot(path)
        claim = snapshot["pawns"][0]["domains"]["work"]["claims"][0]
        snapshot["context"]["art"] = [{"id": "Portrait0", "kind": "art", "record_id": "portrait-record",
                                        "claims": [{**claim, "field": "art.title", "value": "Portrait evidence"}],
                                        "links": [],
                                        "evidence_refs": [{"evidence_id": claim["evidence_id"],
                                                           "source_ref": claim["source_ref"]}]}]
        return snapshot
    monkeypatch.setattr(catalog, "_extract_snapshot", extract)
    db = tmp_path / "art.sqlite"
    catalog.build_catalog(saves, db)
    result = discover_catalog(db, freshness="frozen")
    artifacts = result["linked_artifacts"]
    assert artifacts["coverage"]["art_record_count"] == 2
    assert len(artifacts["sources"]) == 2
    for source in artifacts["sources"]:
        assert source["art_record_count"] == 1
        assert source["followup"]["mcp"] == "get_pawn_dossier"
        assert source["followup"]["params"]["snapshot_id"] == source["snapshot_id"]
        assert source["art_examples"][0]["claims"][0]["value"] == "Portrait evidence"
        token = source["art_examples"][0]["evidence_refs"][0]["retrieval_id"]
        assert get_evidence(db, token)["node"]["text"] == "cook"


def _build_normative_art_catalog(tmp_path, monkeypatch, *, with_pawn=True, art_count=1):
    saves = tmp_path / "normative-art-saves"
    saves.mkdir()
    (saves / "art.rws").write_text("save:1", encoding="utf-8")
    def extract(path):
        snapshot = _snapshot(path)
        claim = snapshot["pawns"][0]["domains"]["work"]["claims"][0]
        snapshot["context"]["art"] = [{
            "id": f"Art{index}", "kind": "art", "record_id": f"art-record-{index}",
            "claims": [{**claim, "field": "art.title", "value": "A Sunlit Colony"}],
            "links": [{"kind": "subject", "target_id": "Human42",
                       "evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"]}],
            "evidence_refs": [{"evidence_id": claim["evidence_id"],
                               "source_ref": claim["source_ref"]}],
        } for index in range(art_count)]
        if not with_pawn:
            snapshot["pawns"] = []
        return snapshot
    monkeypatch.setattr(catalog, "_extract_snapshot", extract)
    db = tmp_path / "normative-art.sqlite"
    catalog.build_catalog(saves, db)
    return db


def test_normative_art_summary_preserves_generic_entity_shape_and_count_bounds(tmp_path, monkeypatch):
    db = _build_normative_art_catalog(tmp_path, monkeypatch, art_count=4)
    result = discover_catalog(db, freshness="frozen")
    source = result["linked_artifacts"]["sources"][0]
    art = source["art_examples"][0]
    assert art["id"] == "Art0"
    assert art["record_id"] == "art-record-0"
    assert art["claims"][0]["value"] == "A Sunlit Colony"
    assert art["links"][0]["target_id"] == "Human42"
    assert art["evidence_refs"][0]["retrieval_id"]
    assert source["selected_art_entity_count"] == 3
    assert source["omitted_art_entity_count"] == 1
    assert result["linked_artifacts"]["coverage"]["art_record_count"] == 4


def test_normative_art_advertised_dossier_and_evidence_retrieval_round_trip(tmp_path, monkeypatch):
    db = _build_normative_art_catalog(tmp_path, monkeypatch)
    result = discover_catalog(db, freshness="frozen")
    source = result["linked_artifacts"]["sources"][0]
    dossier = get_dossier(db, **source["followup"]["params"])
    assert dossier["dossier"]["context"]["art"][0]["id"] == "Art0"
    ref = source["art_examples"][0]["evidence_refs"][0]
    assert get_evidence(db, ref["retrieval_id"])["node"]["text"] == "cook"


def test_art_without_pawn_marks_dossier_unavailable_and_exposes_exact_evidence_fallback(tmp_path, monkeypatch):
    db = _build_normative_art_catalog(tmp_path, monkeypatch, with_pawn=False)
    result = discover_catalog(db, freshness="frozen")
    source = result["linked_artifacts"]["sources"][0]
    assert source["followup"]["availability"] == "available_for_exact_evidence_refs_only"
    assert source["followup"]["evidence"][0]["mcp"] == "get_history_evidence"
    token = source["art_examples"][0]["evidence_refs"][0]["retrieval_id"]
    assert get_evidence(db, token)["node"]["text"] == "cook"


def test_omitted_event_fallback_covers_each_campaign_and_weak_snapshot(tmp_path, monkeypatch):
    saves = tmp_path / "mixed-event-saves"
    saves.mkdir()
    campaign_groups = ("campaign-a", "campaign-b")
    records = []
    for group in campaign_groups:
        for index in range(9):
            records.append((group, f"{group}-{index:02}.rws", f"save:{index}"))
    records.append(("weak", "weak.rws", "save:50"))
    for _, name, text in records:
        (saves / name).write_text(text, encoding="utf-8")

    def extract(path):
        snapshot = _snapshot(path)
        group = next(group for group, name, _ in records if name == path.name)
        if group == "weak":
            snapshot["metadata"]["campaign_key"]["identity"]["status"] = "unsupported"
            snapshot["events"] = [{"saved_event_id": f"weak-{index}", "kind": "injury",
                                   "claims": [], "participants": []} for index in range(241)]
        else:
            snapshot["metadata"]["campaign_key"]["fields"]["seedString"]["value"] = group
            claim = dict(snapshot["pawns"][0]["domains"]["work"]["claims"][0])
            snapshot["events"] = [{"saved_event_id": "shared-event", "kind": "injury",
                                   "claims": [claim], "participants": []}]
        return snapshot
    monkeypatch.setattr(catalog, "_extract_snapshot", extract)
    db = tmp_path / "mixed-events.sqlite"
    catalog.build_catalog(saves, db)
    result = discover_catalog(db, freshness="frozen")
    descriptors = result["coverage"]["event_followup_selectors"]
    omitted_sources = {item["snapshot_id"] for item in descriptors}
    assert result["coverage"]["omitted_episode_record_count"] > 0
    assert omitted_sources
    weak_snapshot = next(row["snapshot_id"] for row in result["sources"] if row["campaign_id"] is None)
    assert weak_snapshot in omitted_sources
    campaign_ids = {row["campaign_id"] for row in result["sources"] if row["campaign_id"] is not None}
    assert {item["campaign_id"] for item in descriptors if item["campaign_id"] is not None} == campaign_ids
    assert all(item["selector"]["params"]["snapshot_id"] == item["snapshot_id"] for item in descriptors)
    weak_page = get_events(db, snapshot_id=weak_snapshot,
                           generation_id=result["catalog"]["generation_id"],
                           limit=1, freshness="frozen")
    assert weak_page["page"]["matching_occurrences"] == 241
    weak_entity = next(entity for entity in result["entities"] if entity["campaign_id"] is None)
    weak_dossier = get_dossier(db, pawn_id="Human42", snapshot_id=weak_entity["followup"]["params"]["snapshot_id"],
                               generation_id=result["catalog"]["generation_id"])
    assert weak_dossier["subject"]["pawn_id"] == "Human42"
    followup = next(item for item in result["evidence_use"]["unresolved"]
                    if item["uncertainty"] == "omitted_or_unsampled_event_records")
    assert {item["snapshot_id"] for item in followup["selector"]["selectors"]} == omitted_sources
