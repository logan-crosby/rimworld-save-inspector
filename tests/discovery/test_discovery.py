from __future__ import annotations

from copy import deepcopy
from rimworld_save_inspector.history.identity import campaign_identity

import pytest

from rimworld_save_inspector.discovery import (
    OfflineSemanticAdapter,
    build_evidence_packet,
    discover_collection,
    discover_snapshot,
)


def _ref(evidence_id: str, path: str) -> dict[str, str]:
    return {"evidence_id": evidence_id, "source_ref": f"ref:{path}"}


def _claim(field: str, value: object, ref: dict[str, str], **extra: object) -> dict[str, object]:
    return {
        "field": field,
        "value": value,
        "raw_value": value,
        "status": "observed",
        "unit": None,
        "code": None,
        "display_label": None,
        "normalization_rule": None,
        "time_observation": None,
        **ref,
        **extra,
    }


def _snapshot(sid: str, campaign: str, pawn_id: str, name: str, evidence_id: str) -> dict:
    ref = _ref(evidence_id, f"/{pawn_id}")
    return {
        "snapshot_id": sid * 64,
        "interpretation_id": "test-profile/1",
        "metadata": {
            "profile": {"profile_id": "test-profile/1", "support_status": "verified"},
            "campaign_key": {
                "fields": {key: _claim("campaign_key." + key, value, ref)
                           for key, value in {"seedString": campaign, "persistentRandomValue": 81234567,
                               "initialMapSize": "(250,1,250)", "startingYear": 5500,
                               "gameStartAbsTick": 5000, "startingTile": "42,0"}.items()},
                "identity": {"status": "compatible", "value": campaign},
            },
            "clocks": {
                "ticksGame": _claim("clock.ticksGame", 120, _ref("clock-game", f"/clock/{pawn_id}"), unit="ticks"),
                "gameStartAbsTick": _claim("clock.gameStartAbsTick", 5000, _ref("clock-abs", f"/absolute/{pawn_id}"), unit="ticks"),
                "startingYear": _claim("clock.startingYear", 5500, _ref("clock-year", f"/year/{pawn_id}"), unit="year"),
            },
        },
        "pawns": [
            {
                "pawn_id": pawn_id,
                "record_id": f"record:{sid}:{pawn_id}",
                "scope": "map",
                "aliases": [_claim("identity.name", name, ref)],
                "status": {"materialization": "materialized"},
                "domains": {
                    "identity": {"status": "complete", "claims": [_claim("identity.name", name, ref)], "links": []},
                    "roles": {"status": "complete", "claims": [], "links": []},
                    "genes": {"status": "complete", "claims": [], "links": []},
                    "memories": {"status": "complete", "claims": [], "links": []},
                },
                "evidence_refs": [ref],
            }
        ],
        "events": [],
        "context": {},
        "evidence": [
            {"evidence_id": evidence_id, "source_ref": ref["source_ref"], "snapshot_id": sid * 64,
             "interpretation_id": "test-profile/1", "path": f"/{pawn_id}", "node": {"tag": "Pawn"}}
        ],
        "coverage": {"source_status": "parsed", "domains": {}, "unknown_fields": [], "limits": []},
        "diagnostics": [],
    }


def test_discover_snapshot_requires_the_exact_history_envelope():
    snapshot = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")
    snapshot["extra"] = True

    with pytest.raises(ValueError, match="exact nine-key"):
        discover_snapshot(snapshot)


def test_collection_joins_saved_ids_only_within_validated_campaign():
    first = _snapshot("a", "campaign-a", "pawn-1", "Same Name", "e1")
    same_campaign = _snapshot("b", "campaign-a", "pawn-1", "Renamed", "e2")
    other_campaign = _snapshot("c", "campaign-b", "pawn-1", "Same Name", "e3")
    same_name_other_id = _snapshot("d", "campaign-a", "pawn-2", "Same Name", "e4")

    result = discover_collection([first, same_campaign, other_campaign, same_name_other_id])
    entities = [entity for entity in result["entities"] if entity["kind"] == "pawn"]

    assert len(entities) == 3
    joined = next(entity for entity in entities if entity["saved_id"] == "pawn-1" and entity["campaign_id"] == campaign_identity(first["metadata"]))
    assert joined["snapshot_ids"] == ["a" * 64, "b" * 64]
    assert len(result["records"]) == 4


def test_clock_ledger_keeps_calendar_game_absolute_and_biological_values_separate():
    snapshot = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")
    snapshot["pawns"][0]["domains"]["age"] = {
        "status": "complete",
        "claims": [_claim("age.biological_ticks", 900, _ref("bio", "/age"), unit="ticks")],
        "links": [],
    }

    result = discover_snapshot(snapshot)
    clocks = {item["field"]: item for item in result["clock_observations"]}

    assert clocks["clock.ticksGame"]["clock"] == "game_tick"
    assert clocks["clock.gameStartAbsTick"]["clock"] == "absolute_tick"
    assert clocks["clock.startingYear"]["clock"] == "calendar_year"
    assert clocks["age.biological_ticks"]["clock"] == "biological_duration"
    assert clocks["age.biological_ticks"]["normalized_game_tick"] is None


def test_collection_accounts_for_every_source_outcome_and_never_prunes_ranked_records():
    parsed = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")
    outcomes = [
        {"source_id": "a.rws", "status": "parsed", "snapshot_id": "a" * 64},
        {"source_id": "b.rws", "status": "failed", "reason": "parse_failed"},
        {"source_id": "c.rws", "status": "excluded", "reason": "incompatible"},
        {"source_id": "d.rws", "status": "unavailable", "reason": "missing"},
    ]

    result = discover_collection([parsed], outcomes)

    assert len(result["source_outcomes"]) == 4
    assert result["coverage"]["outcome_count"] == 4
    assert result["coverage"]["unaccounted_source_count"] == 0
    assert set(result["ranked_record_ids"]) == {record["record_id"] for record in result["records"]}
    assert result["ranking"]["calibrated"] is False


def test_offline_annotations_are_packet_bound_and_do_not_change_authoritative_claims():
    snapshot = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")
    original = deepcopy(snapshot)

    class Annotator:
        def annotate(self, packet: dict) -> dict:
            return {
                "claims": [{"text": "Rowan is saved by name.", "support": ["e1"], "source_refs": ["ref:/pawn-1"]}],
                "notes": [],
            }

    adapter = OfflineSemanticAdapter(Annotator(), model_id="local-model/1")
    result = discover_snapshot(snapshot, semantic_adapter=adapter)

    assert result["annotations"][0]["status"] == "needs_semantic_review"
    assert result["annotations"][0]["citation_status"] == "verified"
    assert result["annotations"][0]["semantic_entailment"] == "unchecked"
    assert result["annotations"][0]["support"] == ["e1"]
    assert result["semantic_cache_key"]
    assert snapshot == original


def test_offline_annotations_with_unlinked_evidence_are_rejected():
    snapshot = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")

    class Annotator:
        def annotate(self, packet: dict) -> dict:
            return {"claims": [{"text": "Unsupported.", "support": ["missing"], "source_refs": ["ref:/elsewhere"]}]}

    adapter = OfflineSemanticAdapter(Annotator(), model_id="local-model/1")
    result = discover_snapshot(snapshot, semantic_adapter=adapter)

    assert result["annotations"][0]["status"] == "rejected"
    assert result["authoritative_facts_unchanged"] is True


def test_different_saved_claim_values_remain_source_linked_counterevidence():
    first = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")
    second = _snapshot("b", "campaign-a", "pawn-1", "Rowan", "e2")
    first["pawns"][0]["domains"]["roles"]["claims"].append(
        _claim("roles.current", "cook", _ref("e-cook", "/roles/cook")))
    second["pawns"][0]["domains"]["roles"]["claims"].append(
        _claim("roles.current", "doctor", _ref("e-doctor", "/roles/doctor")))

    result = discover_collection([first, second])

    conflict = next(row for row in result["support_and_counterevidence"] if row["field"] == "roles.current")
    assert conflict["status"] == "conflicting_observations"
    assert {row["value"] for row in conflict["supporting_variants"]} == {"cook", "doctor"}
    assert all(variant["occurrences"][0]["source_ref"] for variant in conflict["supporting_variants"])
    assert result["uncertainty"]["campaigns_with_unproven_ancestry"] == [campaign_identity(first["metadata"])]


def test_event_episode_joins_participants_by_saved_id_and_keeps_occurrence_provenance():
    snapshot = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")
    snapshot["events"] = [{
        "kind": "tale",
        "saved_event_id": "tale:ValidatedTale:Tale_7",
        "record_id": "event-record-7",
        "participants": [{"pawn_id": "pawn-1", "role": "subject", "status": "observed",
                          "evidence_id": "e-event-participant", "source_ref": "ref:/event/participant"}],
        "occurrence": {"raw_value": "40", "raw_field": "occurred", "clock": "game_tick",
                        "normalized_game_tick": 40, "normalization_rule": "validated",
                        "status": "known", "evidence_id": "e-event-time", "source_ref": "ref:/event/time"},
        "claims": [],
        "evidence_refs": [_ref("e-event", "/event")],
    }]

    result = discover_collection([snapshot])
    event = next(row for row in result["episodes"] if row["kind"] == "recorded_event")

    assert event["saved_event_id"] == "tale:ValidatedTale:Tale_7"
    assert event["participants"][0]["entity_id"] == result["entities"][0]["entity_id"]
    assert event["occurrences"][0]["source_ref"] == "ref:/event/time"
    assert any(row["kind"] == "state_observation" for row in result["episodes"])


def test_packet_is_deterministic_and_cache_key_covers_provider_model_and_options():
    snapshot = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")
    discovery = discover_snapshot(snapshot)
    packet1 = build_evidence_packet(discovery)
    packet2 = build_evidence_packet(discovery)
    assert packet1 == packet2

    class Annotator:
        def annotate(self, packet: dict) -> dict:
            return {"claims": []}

    default = OfflineSemanticAdapter(Annotator(), model_id="local/1")
    changed = OfflineSemanticAdapter(Annotator(), model_id="local/1", provider_id="ollama-local",
                                     options={"temperature": 0})
    result_default = discover_snapshot(snapshot, semantic_adapter=default)
    result_changed = discover_snapshot(snapshot, semantic_adapter=changed)
    assert result_default["semantic_cache_key"] != result_changed["semantic_cache_key"]


def test_offline_claim_checker_rejects_unsupported_causal_language():
    snapshot = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")

    class Annotator:
        def annotate(self, packet: dict) -> dict:
            return {"claims": [{"text": "The xenotype caused the conflict.", "support": ["e1"],
                                "source_refs": ["ref:/pawn-1"]}]}

    result = discover_snapshot(snapshot, semantic_adapter=OfflineSemanticAdapter(Annotator(), model_id="local/1"))
    assert result["annotations"][0]["status"] == "rejected"


def test_snapshot_scoped_record_ids_do_not_join_as_cross_save_event_identity():
    first = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")
    second = _snapshot("b", "campaign-a", "pawn-1", "Rowan", "e2")
    for snapshot in (first, second):
        snapshot["events"] = [{"kind": "tale", "record_id": "snapshot-local-event",
                               "participants": [], "occurrence": None, "claims": [], "evidence_refs": []}]

    result = discover_collection([first, second])
    events = [episode for episode in result["episodes"] if episode["kind"] == "recorded_event"]

    assert len(events) == 2
    assert all(event["join_basis"] == "snapshot_only" for event in events)


def test_event_claim_conflicts_join_only_through_stable_saved_event_identity():
    first = _snapshot("a", "campaign-a", "pawn-1", "Rowan", "e1")
    second = _snapshot("b", "campaign-a", "pawn-1", "Rowan", "e2")
    for snapshot, record_id, value, evidence_id in (
        (first, "event-record-a", "relocated", "e-relocated"),
        (second, "event-record-b", "rescued", "e-rescued"),
    ):
        snapshot["events"] = [{
            "kind": "tale", "saved_event_id": "tale:ValidatedTale:Tale_7", "record_id": record_id,
            "participants": [], "occurrence": None,
            "claims": [_claim("event.outcome", value, _ref(evidence_id, f"/{record_id}/outcome"))],
            "evidence_refs": [],
        }]

    result = discover_collection([first, second])
    conflict = next(row for row in result["support_and_counterevidence"] if row["field"] == "event.outcome")

    assert conflict["entity_id"].startswith("episode:")
    assert conflict["status"] == "conflicting_observations"
    assert {variant["value"] for variant in conflict["supporting_variants"]} == {"relocated", "rescued"}
