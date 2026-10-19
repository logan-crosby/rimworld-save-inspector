import hashlib
import json
import pytest

from rimworld_save_inspector.history.models import (
    HistorySnapshot,
    evidence_id_for_node,
    record_id_for_occurrence,
    source_ref_for,
)


def test_canonical_content_and_occurrence_ids_are_separate_and_deterministic():
    node_a = {
        "children": [{"node": {"tag": "name", "attributes": [], "text": "Zoë", "children": []}, "tail": None}],
        "text": None,
        "attributes": [["Class", "Pawn"]],
        "tag": "Pawn",
    }
    node_b = dict(reversed(list(node_a.items())))
    canonical_node = json.dumps(node_a, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    content_id = hashlib.sha256(canonical_node).hexdigest()
    assert evidence_id_for_node(node_a) == content_id
    assert evidence_id_for_node(node_b) == content_id

    path_a = "/savegame[1]/maps[1]/li[1]/things[1]/thing[1]"
    path_b = "/savegame[1]/world[1]/worldPawns[1]/pawnsAlive[1]/li[1]"
    ref_a = hashlib.sha256(json.dumps(["a" * 64, "history/1", path_a], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert source_ref_for("a" * 64, "history/1", path_a) == ref_a
    assert source_ref_for("a" * 64, "history/1", path_a) != source_ref_for("a" * 64, "history/1", path_b)
    assert record_id_for_occurrence("a" * 64, "history/1", "pawn", path_a) != record_id_for_occurrence("b" * 64, "history/1", "pawn", path_a)


def test_history_snapshot_serializes_exact_nine_top_level_keys():
    snapshot = HistorySnapshot(
        snapshot_id="a" * 64,
        interpretation_id="history/1",
        metadata={"profile": {}, "campaign_key": {}, "clocks": {}},
        pawns=[],
        events=[],
        context={"maps": [], "factions": [], "mods": [], "research": [], "scenario": [], "art": [], "related_entities": []},
        evidence=[],
        coverage={"source_status": "parsed", "domains": {}, "unknown_fields": [], "limits": {}},
        diagnostics=[],
    )
    assert list(snapshot.as_dict()) == [
        "snapshot_id", "interpretation_id", "metadata", "pawns", "events",
        "context", "evidence", "coverage", "diagnostics",
    ]
    assert snapshot.as_dict()["snapshot_id"] == "a" * 64


def test_snapshot_nested_values_are_immutable_and_as_dict_is_detached():
    snapshot = HistorySnapshot(
        snapshot_id="a" * 64,
        interpretation_id="history/1",
        metadata={"profile": {"version": "1.6"}},
        pawns=[{"pawn_id": "p1", "domains": {"skills": {"claims": []}}}],
        events=[], context={}, evidence=[], coverage={}, diagnostics=[],
    )
    with pytest.raises(TypeError):
        snapshot.metadata["profile"]["version"] = "changed"
    serialized = snapshot.as_dict()
    serialized["metadata"]["profile"]["version"] = "changed"
    assert snapshot.as_dict()["metadata"]["profile"]["version"] == "1.6"


def test_trusted_snapshot_expansion_preserves_raw_tree_without_populating_lazy_caches():
    raw_node = {
        "tag": "Pawn", "attributes": [["name", "Zoë"]], "text": None,
        "children": [
            {"node": {"tag": "skill", "attributes": [], "text": "5", "children": []}, "tail": " "},
            {"node": {"tag": "skill", "attributes": [], "text": "6", "children": []}, "tail": None},
        ],
    }
    raw_evidence = [{"node": raw_node, "path": "/savegame[1]/pawn[1]"}]
    snapshot = HistorySnapshot(
        snapshot_id="a" * 64, interpretation_id="history/1",
        metadata={}, pawns=[], events=[], context={}, evidence=raw_evidence,
        coverage={}, diagnostics=[], trusted_immutable_inputs=True,
    )

    expanded = snapshot.as_dict()

    assert list(expanded) == [
        "snapshot_id", "interpretation_id", "metadata", "pawns", "events",
        "context", "evidence", "coverage", "diagnostics",
    ]
    assert expanded["evidence"] == raw_evidence
    assert object.__getattribute__(snapshot.evidence, "_cache") == {}
    expanded["evidence"][0]["node"]["children"][0]["node"]["text"] = "changed"
    assert raw_node["children"][0]["node"]["text"] == "5"
    with pytest.raises(TypeError):
        snapshot.evidence[0]["node"]["children"][0]["node"]["text"] = "changed"
