"""Product-versus-handwritten-oracle comparison for all public domains."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rimworld_save_inspector import QueryRequest, QueryService, load_snapshot  # noqa: E402


def _all(service: QueryService, name: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cursor = None
    rows: list[dict[str, Any]] = []
    while True:
        response = service.execute(QueryRequest(name, filters or {}, limit=200, cursor=cursor)).as_dict()
        rows.extend(response["data"].get("entries", []))
        cursor = (response.get("page") or {}).get("next_cursor")
        if cursor is None:
            return rows


class SyntheticDomainEquivalenceTests(unittest.TestCase):
    def test_all_eight_domains_match_handwritten_appendix_a_facts(self) -> None:
        self.maxDiff = None
        fixture = ROOT / "tests/fixtures/synthetic/seed.rws"
        expected = json.loads((ROOT / "tests/fixtures/synthetic/expected.json").read_text())
        service = QueryService(load_snapshot(fixture))
        summary = service.execute(QueryRequest("summary")).as_dict()["data"]
        pawn_record = next(record for record in service.snapshot.records if record.type_name == "pawn" and record.scope == "map")
        pawn_core = service.execute(QueryRequest("pawn", {"record_id": pawn_record.record_id})).as_dict()["data"]["pawn"]
        skill_rows = _all(service, "pawn", {"record_id": pawn_record.record_id, "section": "skills"})
        trait_rows = _all(service, "pawn", {"record_id": pawn_record.record_id, "section": "traits"})
        health_result = service.execute(QueryRequest("health", {"record_id": pawn_record.record_id})).as_dict()["data"]
        gene_records = []
        for group in ("endogenes", "xenogenes"):
            gene_records.extend(_all(service, "genes", {"record_id": pawn_record.record_id, "group": group}))
        gene_records.sort(key=lambda row: row["source_path"])
        equipment = _all(service, "equipment", {"record_id": pawn_record.record_id})
        apparel = _all(service, "equipment", {"record_id": pawn_record.record_id, "holder": "apparel"})
        inventory_response = service.execute(QueryRequest("inventory", {"record_id": pawn_record.record_id,
                                                                          "estimate_missing_as_one": True})).as_dict()["data"]
        progress = _all(service, "research", {"section": "progress"})
        techprints = _all(service, "research", {"section": "techprints"})
        mods = _all(service, "mods")
        scopes = {scope: _all(service, "pawns", {"scope": scope}) for scope in
                  ("map", "world_alive", "world_dead", "world_mothballed", "held", "all_supported")}
        corpse_refs = [value for path, value in service.snapshot.globals.items()
                       if "/innerList[" in path and isinstance(value, str) and value.startswith("Thing_")]
        dead_ids = {record.saved_id for record in service.snapshot.records
                    if record.type_name == "pawn" and record.scope == "world_dead"}
        resolved_dead_refs = sum(
            service.indexes.resolve(raw, "thing").status == "resolved" and
            any(target.saved_id in dead_ids for target in service.snapshot.records
                if target.record_id in service.indexes.resolve(raw, "thing").target_record_ids)
            for raw in corpse_refs
        )

        expected_pawn = expected["pawn"]
        actual = {
            "summary": {
                "map_count": summary["map_count"], "map_ids": [entry["map_id"] for entry in summary["maps"]],
                "direct_thing_count": summary["maps"][0]["direct_thing_count"],
                "direct_pawn_count": summary["maps"][0]["direct_pawn_count"],
                "player_faction": summary["player_faction"]["raw"], "ticks_game": summary["ticks_game"],
                "world_name": summary["world_name"], "world_seed": summary["world_seed"],
                "parent_label": summary["maps"][0]["parent_label"],
            },
            "pawn": {
                "saved_id": pawn_core["saved_id"], "record_id": pawn_core["record_id"],
                "name": {key: pawn_core["name"][key] for key in ("first", "nick", "last")},
                "gender": pawn_core["gender"], "age_biological_ticks": pawn_core["age_biological_ticks"],
                "birth_abs_ticks": pawn_core["birth_abs_ticks"], "guest_status": pawn_core["guest_status"],
                "host_faction": {"status": pawn_core["host_faction"]["status"], "raw": pawn_core["host_faction"]["raw"]},
                "slave_faction": {"status": pawn_core["slave_faction"]["status"], "raw": pawn_core["slave_faction"]["raw"]},
                "skills": [{"def": row["skill_def"], "level": row["level"], "passion": row["passion"],
                            "xp_raw": row["raw_values"].get("/xp_since_last_level"), "xp": row["xp_since_last_level"]}
                           for row in skill_rows],
                "traits": [{"def": row["trait_def"], "degree": row["degree"],
                            "source_gene": "explicit_null" if row["source_gene"]["status"] == "explicit_null" else row["source_gene"]["raw"],
                            "suppressed_by": "explicit_null" if row["suppressed_by"]["status"] == "explicit_null" else row["suppressed_by"]["raw"]}
                           for row in trait_rows],
            },
            "health": {
                "record_id": health_result["entries"][0]["record_id"], "health_state": health_result["health_state"],
                "health_state_status": health_result["field_status"].get("/health_state", "present"),
                "entries": [{"saved_id": row["saved_id"], "def": row["hediff_def"],
                             "severity_raw": row["raw_values"].get("/severity"), "severity": row["severity"],
                             "visible": row["visible"], "part": {"body": row["part"]["body"], "index": row["part"]["index"]}}
                            for row in health_result["entries"]],
            },
            "genes": {
                "endogenes": sum(row["source_path"].find("/endogenes[") >= 0 for row in gene_records),
                "xenogenes": sum(row["source_path"].find("/xenogenes[") >= 0 for row in gene_records),
                "entries": [{"def": row["gene_def"], "load_id": row["saved_id"], "pawn_reference": row["pawn"]["raw"],
                             "override": {"status": row["overridden_by"]["status"],
                                          "target_load_id": row["overridden_by"]["normalized_id"].removeprefix("Gene_") if row["overridden_by"]["normalized_id"] else None}}
                            for row in gene_records],
                "active_flags": None,
            },
            "items": {
                "equipment": [{"id": row["item"]["saved_id"], "def": row["item"]["item_def"],
                               **({"quality": row["item"]["quality"]} if row["item"]["quality"] is not None else {}),
                               **({"health": row["item"]["hit_points"]} if row["item"]["hit_points"] is not None else {}),
                               "stack_count": row["item"]["stack_count"]} for row in equipment],
                "apparel": [{"id": row["item"]["saved_id"], "def": row["item"]["item_def"],
                             "stuff": row["item"]["stuff"], "quality": row["item"]["quality"],
                             "health": row["item"]["hit_points"], "stack_count": row["item"]["stack_count"]} for row in apparel],
                "inventory": [{"id": row["item"]["saved_id"], "def": row["item"]["item_def"],
                               "stack_count": row["item"]["stack_count"]}
                              for row in inventory_response["entries"] if row["entry_kind"] == "record"],
                "inventory_explicit_units": inventory_response["quantity"]["explicit_stack_units"],
                "inventory_missing_stack_count_records": inventory_response["quantity"]["missing_stack_count_records"],
                "inventory_estimated_units_when_opted_in": inventory_response["quantity"]["estimated_stack_units"],
            },
            "research": {
                "current_project": service.execute(QueryRequest("research", {"section": "progress"})).as_dict()["data"]["current_project"],
                "progress": [{"project_def": row["project_def"], "progress_raw": row["raw_values"].get("/progress_points"),
                              "progress": row["progress_points"]} for row in progress],
                "techprints": [{"project_def": row["project_def"], "count_raw": row["raw_values"].get("/techprint_count"),
                                "count": row["techprint_count"]} for row in techprints],
                "completed": None,
            },
            "mods": [{"package_id": row["package_id"], "display_name": row["display_name"]} for row in mods],
            "scope": {key: len(rows) for key, rows in scopes.items()},
            "corpse_reference": {"corpse_count": sum(record.type_name == "corpse" for record in service.snapshot.auxiliary_records),
                                 "dead_pool_matches": resolved_dead_refs, "forcefully_kept_is_membership_only": True},
        }
        # Keep this assertion intentionally whole-object: a partial domain can
        # never satisfy T01 through a collection of cherry-picked fields.
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
