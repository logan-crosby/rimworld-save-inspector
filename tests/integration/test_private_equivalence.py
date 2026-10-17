"""Private product checks against the independent frozen oracle.

The oracle executes in a separate process and has no product imports. Only
scalar acceptance facts and selected identifiers are written to the report;
the save body is never copied or logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.private_acceptance import load_private_inputs  # noqa: E402

from rimworld_save_inspector import QueryRequest, QueryService, load_snapshot  # noqa: E402


def _all_entries(service: QueryService, name: str, filters: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    filters = filters or {}
    cursor = None
    first: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    while True:
        response = service.execute(QueryRequest(name, filters, limit=200, cursor=cursor)).as_dict()
        first = first or response
        entries.extend(response["data"].get("entries", []))
        page = response.get("page")
        if not page or not page.get("next_cursor"):
            break
        cursor = page["next_cursor"]
        if cursor in seen:
            raise AssertionError("product returned a repeated paging cursor")
        seen.add(cursor)
    assert first is not None
    return first, entries


def _status_count(records: list[Any], path: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        raw = record.value(path)
        status = record.field_status.get(path, "present" if path in record.values else "absent")
        label = str(raw) if raw is not None else status
        counts[label] = counts.get(label, 0) + 1
    return counts


def _integer(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class PrivateEquivalenceTests(unittest.TestCase):
    def test_product_matches_frozen_f01_f28_and_dynamic_d01_d03(self) -> None:
        private = load_private_inputs()
        if not private.ready or private.fixture is None:
            self.skipTest("frozen private acceptance inputs unavailable; gate records required cases as BLOCKED")
        fixture = private.fixture
        rows = {row["check_id"]: row for row in private.oracle["assertions"]}
        specimen_id = rows["F24"]["scope"].removeprefix("pawn_")
        equipped_id = rows["F27"]["predicate"].rsplit("with id ", 1)[-1]
        worn_id = rows["F28"]["predicate"].rsplit("with id ", 1)[-1]
        research_key = private.oracle["required_dynamic_comparisons"][2]["selection"].split("key ", 1)[1].split(" after", 1)[0]

        before = hashlib.sha256(fixture.read_bytes()).hexdigest()
        oracle_started = time.monotonic()
        oracle_argv = [sys.executable, str(ROOT / "tests/oracle/extract.py"), str(fixture)]
        oracle = subprocess.run(oracle_argv, cwd=ROOT, capture_output=True, text=True, timeout=240)
        oracle_duration = time.monotonic() - oracle_started
        if oracle.returncode != 0:
            raise AssertionError(f"independent oracle failed with exit {oracle.returncode}: {oracle.stderr[-1000:]}")
        oracle_report = json.loads(oracle.stdout)
        if oracle_report.get("all_match_spec") is not True:
            raise AssertionError("independent oracle did not match the frozen F01-F28 baseline")

        snapshot = load_snapshot(fixture)
        service = QueryService(snapshot)
        summary_response = service.execute(QueryRequest("summary")).as_dict()
        summary = summary_response["data"]
        map_pawns = [record for record in snapshot.records if record.type_name == "pawn" and record.scope == "map"]
        human_pawns = [record for record in map_pawns if record.value("/def") == "Human"]
        player_ref = summary["player_faction"]
        player_humans = [record for record in human_pawns if record.value("/faction") == player_ref["raw"]]
        prisoner_records = [record for record in player_humans if record.value("/guest/guestStatus") == "Prisoner"]
        map_ids = [entry["map_id"] for entry in summary["maps"]]

        observed: dict[str, Any] = {}
        observed["F01"] = {"bytes": snapshot.source_bytes, "sha256": before, "game_version": snapshot.game_version}
        observed["F02"] = {"count": summary["map_count"], "ids": map_ids}
        observed["F03"] = {"per_map": [entry["direct_thing_count"] for entry in summary["maps"]],
                            "total": sum(entry["direct_thing_count"] for entry in summary["maps"])}
        observed["F04"] = {"per_map": [entry["direct_pawn_count"] for entry in summary["maps"]],
                            "total": sum(entry["direct_pawn_count"] for entry in summary["maps"])}
        observed["F05"] = {"world_name": summary["world_name"], "seed": summary["world_seed"], "ticks": summary["ticks_game"]}
        target_factions = [record for record in snapshot.records if record.type_name == "faction" and
                           record.record_id in player_ref["target_record_ids"]]
        observed["F06"] = {"def": target_factions[0].value("/def") if len(target_factions) == 1 else None,
                            "load_id": target_factions[0].saved_id if len(target_factions) == 1 else None,
                            "reference": player_ref["raw"]}
        observed["F07"] = len(human_pawns)
        observed["F08"] = len(player_humans)
        observed["F09"] = sum(
            record.field_status.get("/guest/guestStatus") == "absent" and
            record.field_status.get("/guest/hostFaction") == "explicit_null" and
            record.field_status.get("/guest/slaveFaction") == "explicit_null"
            for record in player_humans
        )
        prisoner = prisoner_records[0] if len(prisoner_records) == 1 else None
        observed["F10"] = {"count": len(prisoner_records), "id": prisoner.saved_id if prisoner else None,
                            "name": {key: prisoner.value(f"/name/{key}") for key in ("first", "nick", "last")} if prisoner else None}
        observed["F11"] = _status_count(map_pawns, "/healthTracker/healthState")
        observed["F12"] = _status_count(player_humans, "/healthTracker/healthState")

        map_pawn_ids = {record.record_id for record in map_pawns}
        owned = [record for record in snapshot.records if record.owner_record_id in map_pawn_ids]
        hediffs = [record for record in owned if record.type_name == "hediff"]
        genes = [record for record in owned if record.type_name == "gene"]
        items = [record for record in owned if record.type_name == "item"]
        observed["F13"] = {"total": len(hediffs), "def_counts": {
            key: sum(record.value("/def") == key for record in hediffs) for key in rows["F13"]["expected"]["def_counts"]}}
        observed["F14"] = {"endogenes": sum("/endogenes[" in record.source_path for record in genes),
                            "xenogenes": sum("/xenogenes[" in record.source_path for record in genes)}
        observed["F15"] = {"present": sum(record.field_status.get("/overriddenByGene") != "absent" for record in genes),
                            "absent": sum(record.field_status.get("/overriddenByGene") == "absent" for record in genes)}
        observed["F16"] = {"equipment": sum("/equipment[" in record.source_path for record in items),
                            "apparel": sum("/wornApparel[" in record.source_path for record in items),
                            "inventory": sum("/inventory[" in record.source_path for record in items)}
        inventory = [record for record in items if "/inventory[" in record.source_path]
        explicit_stack = [record.value("/stackCount") for record in inventory
                          if "/stackCount" in record.values and record.value("/stackCount") is not None and
                          record.field_status.get("/stackCount") not in {"invalid", "explicit_null"}]
        absent_stack = sum(record.field_status.get("/stackCount") == "absent" for record in inventory)
        explicit_units = sum(int(value) for value in explicit_stack)
        observed["F17"] = {"explicit_records": len(explicit_stack), "explicit_units": explicit_units,
                            "absent_records": absent_stack, "absent_as_one_estimate": explicit_units + absent_stack}

        progress = service.execute(QueryRequest("research", {"section": "progress"})).as_dict()["data"]
        techprints = service.execute(QueryRequest("research", {"section": "techprints"})).as_dict()["data"]
        observed["F18"] = {"keys": progress["key_count"], "values": progress["value_count"],
                            "current_project": progress["current_project"]}
        observed["F19"] = {"keys": techprints["key_count"], "values": techprints["value_count"]}
        mods = _all_entries(service, "mods")[1]
        observed["F20"] = {"ids": len(mods), "names": len(mods),
                            "first": {"package_id": mods[0]["package_id"], "display_name": mods[0]["display_name"]} if mods else None}

        all_supported = _all_entries(service, "pawns", {"scope": "all_supported"})[1]
        pool_paths: dict[str, set[str]] = {name: set() for name in
            ("alive", "dead", "mothballed", "forcefully_kept")}
        for pawn in all_supported:
            for membership in pawn.get("memberships", []):
                pool = membership.get("pool")
                source_path = membership.get("source_path")
                if pool in pool_paths and source_path:
                    pool_paths[pool].add(source_path)
        observed["F21"] = {"pawnsAlive": len(pool_paths["alive"]), "pawnsDead": len(pool_paths["dead"]),
                            "pawnsMothballed": len(pool_paths["mothballed"]),
                            "pawnsForcefullyKeptAsWorldPawns": len(pool_paths["forcefully_kept"])}
        corpses = [record for record in snapshot.auxiliary_records if record.type_name == "corpse" and record.scope == "map"]
        human_corpses = [record for record in corpses if record.value("/def") == "Corpse_Human"]
        observed["F22"] = {"total": len(corpses), "human": len(human_corpses)}

        direct_map_ids = {record.saved_id for record in map_pawns}
        world_pool_ids = {name: {record.saved_id for record in snapshot.records if record.type_name == "pawn" and
                                 record.scope == f"world_{name}"}
                          for name in ("alive", "dead", "mothballed")}
        target_ids: set[str] = set()
        resolution_statuses: list[str] = []
        for corpse in corpses:
            for path, raw in snapshot.globals.items():
                if path.startswith(corpse.source_path + "/innerContainer[") and "/innerList[" in path and isinstance(raw, str) and raw.startswith("Thing_"):
                    reference = service.indexes.resolve(raw, "thing")
                    resolution_statuses.append(reference.status)
                    for target_id in reference.target_record_ids:
                        target = next((record for record in snapshot.records if record.record_id == target_id), None)
                        if target is not None and target.saved_id is not None:
                            target_ids.add(target.saved_id)
        observed["F23"] = {
            "distinct_refs": len(target_ids),
            "dead_pool_matches": len(target_ids & world_pool_ids["dead"]),
            "map_matches": len(target_ids & direct_map_ids),
            "alive_pool_matches": len(target_ids & world_pool_ids["alive"]),
            "mothballed_pool_matches": len(target_ids & world_pool_ids["mothballed"]),
            "unresolved": sum(status != "resolved" for status in resolution_statuses),
        }

        specimen = next(record for record in map_pawns if record.saved_id == specimen_id)
        specimen_core = service.execute(QueryRequest("pawn", {"record_id": specimen.record_id})).as_dict()["data"]["pawn"]
        observed["F24"] = {"name": {key: specimen_core["name"][key] for key in ("first", "nick", "last")},
                            "age_biological_ticks": specimen_core["age_biological_ticks"]}
        skills = _all_entries(service, "pawn", {"record_id": specimen.record_id, "section": "skills"})[1]
        shooting = next(row for row in skills if row["skill_def"] == "Shooting")
        observed["F25"] = {"level": shooting["level"], "passion": shooting["passion"],
                            "xp_since_last_level_raw": shooting["raw_values"].get("/xp_since_last_level")}
        endogenes = _all_entries(service, "genes", {"record_id": specimen.record_id, "group": "endogenes"})[1]
        first_gene = endogenes[0]
        observed["F26"] = {"def": first_gene["gene_def"], "load_id": first_gene["saved_id"],
                            "pawn_reference": first_gene["pawn"]["raw"],
                            "overridden_by": first_gene["overridden_by"]["status"]}
        equipment = _all_entries(service, "equipment", {"record_id": specimen.record_id})[1]
        weapon = next(entry["item"] for entry in equipment if entry.get("item") and
                      entry["item"]["saved_id"] == equipped_id)
        observed["F27"] = {"def": weapon["item_def"], "stack_count": weapon["stack_count"], "health": weapon["hit_points"]}
        apparel = _all_entries(service, "equipment", {"record_id": specimen.record_id, "holder": "apparel"})[1]
        worn = next(entry["item"] for entry in apparel if entry.get("item") and
                    entry["item"]["saved_id"] == worn_id)
        observed["F28"] = {"def": worn["item_def"], "stuff": worn["stuff"], "quality": worn["quality"],
                            "health": worn["hit_points"], "stack_count": worn["stack_count"]}

        dynamic = oracle_report["dynamic"]
        d01 = dynamic["D01"]
        d01_pawn = next(record for record in map_pawns if record.saved_id == d01["owning_pawn_id"])
        d01_rows = _all_entries(service, "health", {"record_id": d01_pawn.record_id})[1]
        d01_row = next(row for row in d01_rows if row["saved_id"] == d01["hediff_load_id"])
        observed_dynamic = {
            "D01": {"owning_pawn_id": d01_pawn.saved_id, "hediff_load_id": d01_row["saved_id"],
                    "def": d01_row["hediff_def"], "severity_raw": d01_row["raw_values"].get("/severity"),
                    "part_body": d01_row["part"]["body"], "part_index": d01_row["part"]["index"]},
            "D02": None,
            "D03": [],
        }
        d02 = dynamic["D02"]
        if d02:
            d02_pawn = next(record for record in map_pawns if record.saved_id == d02["owning_pawn_id"])
            gene_rows = []
            for group in ("endogenes", "xenogenes"):
                gene_rows.extend(_all_entries(service, "genes", {"record_id": d02_pawn.record_id, "group": group})[1])
            d02_row = next(row for row in gene_rows if row["saved_id"] == d02["gene_load_id"])
            observed_dynamic["D02"] = {"owning_pawn_id": d02_pawn.saved_id, "gene_load_id": d02_row["saved_id"],
                                        "def": d02_row["gene_def"], "override_raw": d02_row["overridden_by"]["raw"],
                                        "independently_resolved_target_ids": d02_row["overridden_by"]["target_record_ids"]}
        for selected in dynamic["D03"]:
            progress_rows = _all_entries(service, "research", {"section": "progress", "project_def": research_key})[1]
            row = next(item for item in progress_rows if item["ordinal"] == selected["ordinal"])
            observed_dynamic["D03"].append({"ordinal": row["ordinal"], "progress_raw": row["raw_values"].get("/progress_points"),
                                             "progress_number": row["progress_points"]})

        after = hashlib.sha256(fixture.read_bytes()).hexdigest()
        self.assertEqual(before, private.expected_sha256)
        self.assertEqual(after, before, "product/oracle execution changed the private source")
        expected = oracle_report["expected"]
        failures = {case_id: {"expected": expected[case_id], "observed": observed[case_id]}
                    for case_id in expected if observed.get(case_id) != expected[case_id]}
        dynamic_failures = {case_id: {"expected": dynamic[case_id], "observed": observed_dynamic[case_id]}
                            for case_id in dynamic if observed_dynamic.get(case_id) != dynamic[case_id]}

        report = {"private_input_hashes": private.input_hashes(),
                  "fixture": {"bytes": snapshot.source_bytes, "sha256_before": before, "sha256_after": after,
                               "game_version": snapshot.game_version},
                  "oracle": {"all_match_spec": oracle_report["all_match_spec"],
                             "expected": expected, "observed": oracle_report["assertions"],
                             "dynamic": dynamic,
                             "argv": [sys.executable, "tests/oracle/extract.py", "<private-fixture>"],
                             "exit_code": oracle.returncode, "duration_seconds": round(oracle_duration, 6)},
                  "product": {"assertions": observed, "dynamic": observed_dynamic},
                  "failures": {"assertions": failures, "dynamic": dynamic_failures}}
        report["checks"] = [
            {"check_id": case_id, "expected": expected[case_id], "observed": observed.get(case_id),
             "status": "PASS" if observed.get(case_id) == expected[case_id] else "FAIL"}
            for case_id in expected
        ] + [
            {"check_id": case_id, "expected": dynamic[case_id], "observed": observed_dynamic.get(case_id),
             "status": "PASS" if observed_dynamic.get(case_id) == dynamic[case_id] else "FAIL"}
            for case_id in dynamic
        ]
        report["oracle_all_match_spec"] = oracle_report["all_match_spec"]
        report["oracle_exit_code"] = oracle.returncode
        report["source_preserved"] = before == after
        # This line is captured by the gate runner and copied into its immutable
        # run directory. It contains only typed facts already named by the
        # public F/D acceptance contract, never serialized XML or raw excerpts.
        print("PRIVATE_PRODUCT_REPORT=" + json.dumps(report, sort_keys=True, separators=(",", ":")))
        target = os.environ.get("RWSI_PRIVATE_REPORT")
        if target:
            Path(target).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        self.assertEqual(failures, {}, "private product values differ from frozen independent facts")
        self.assertEqual(dynamic_failures, {}, "private product values differ from independent dynamic selections")


if __name__ == "__main__":
    unittest.main()
