"""Independent expectations for all required shared-context source domains."""

import json
from pathlib import Path

from rimworld_save_inspector.history.models import evidence_id_for_node
from rimworld_save_inspector.history.source import extract_history_snapshot


ROOT = Path(__file__).parent
GOLD = json.loads((ROOT / "shared-context-v1-expectations.json").read_text())


def test_shared_context_preserves_raw_values_ids_and_art_evidence(tmp_path):
    path = tmp_path / "shared.rws"
    path.write_bytes((ROOT / GOLD["fixture"]).read_bytes())
    snapshot = extract_history_snapshot(path).as_dict()
    assert sorted(p["pawn_id"] for p in snapshot["pawns"]) == GOLD["expected_materialized_pawn_ids"]
    evidence = {(r["evidence_id"], r["source_ref"]): r for r in snapshot["evidence"]}
    for domain in GOLD["required_context_domains"]:
        entities = snapshot["context"][domain]
        assert entities, f"{domain} is present in XML and cannot be an empty placeholder"
        raw = []
        for entity in entities:
            assert {"id", "kind", "claims", "links", "evidence_refs"} <= entity.keys()
            assert entity["evidence_refs"]
            for claim in entity["claims"]:
                raw.append(claim["raw_value"])
                ref = evidence[(claim["evidence_id"], claim["source_ref"])]
                assert evidence_id_for_node(ref["node"]) == ref["evidence_id"]
                assert ref["snapshot_id"] == snapshot["snapshot_id"]
        assert set(GOLD["expected_raw_values_by_domain"][domain]) <= set(
            value for value in raw if isinstance(value, str))
    art = snapshot["context"]["art"]
    assert any(link["target_id"] == GOLD["required_art_exact_link_target"]
               for item in art for link in item["links"])
    bodies = [evidence[(ref["evidence_id"], ref["source_ref"])]["node"]
              for item in art for ref in item["evidence_refs"]]
    def walk(node):
        yield node
        for child in node["children"]:
            yield from walk(child["node"])
    opaque = [node for body in bodies for node in walk(body) if node["tag"] == "opaque"]
    assert len(opaque) == 1
    assert opaque[0]["text"] == "before "
    assert opaque[0]["children"][0]["node"]["tag"] == "{urn:shared-context}part"
    assert opaque[0]["children"][0]["node"]["attributes"] == [["importance", "low"]]
    assert opaque[0]["children"][0]["tail"] == " after"


def test_observed_empty_shared_domain_is_not_missing(tmp_path):
    path = tmp_path / "empty.rws"
    path.write_text('<savegame><meta><gameVersion>1.6.4535</gameVersion><modIds/>'
                    '<modNames/></meta><game><maps/><researchManager/><scenario/>'
                    '<world><factionManager><allFactions/></factionManager></world></game></savegame>')
    snapshot = extract_history_snapshot(path).as_dict()
    for domain in ["maps", "mods", "factions", "research", "scenario"]:
        assert snapshot["context"][domain] == []
        assert snapshot["coverage"]["domains"][domain]["status"] == "complete"
        assert snapshot["coverage"]["domains"][domain]["observed_count"] == 0
    assert snapshot["coverage"]["domains"]["art"]["status"] == "absent"
