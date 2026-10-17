"""Independent, stdlib-only XML oracle for Appendix A and private acceptance.

This module intentionally does not import any rimworld_save_inspector code.
It uses a full ElementTree only for the trusted private acceptance fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def text(node: ET.Element | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.strip()


def one(node: ET.Element, path: str) -> ET.Element | None:
    return node.find(path)


def value(node: ET.Element, path: str) -> str | None:
    return text(one(node, path))


def nodes(node: ET.Element, path: str) -> list[ET.Element]:
    return node.findall(path)


def canonical_paths(root: ET.Element) -> dict[int, str]:
    result: dict[int, str] = {}

    def visit(node: ET.Element, path: str) -> None:
        result[id(node)] = path
        counts: dict[str, int] = {}
        for child in list(node):
            counts[child.tag] = counts.get(child.tag, 0) + 1
            visit(child, f"{path}/{child.tag}[{counts[child.tag]}]")

    visit(root, f"/{root.tag}[1]")
    return result


def record_id(prefix: str, path: str) -> str:
    return prefix + ":" + hashlib.sha256(path.encode("utf-8")).hexdigest()


def name_parts(pawn: ET.Element) -> dict[str, str | None]:
    name = pawn.find("name")
    if name is None:
        return {"first": None, "nick": None, "last": None}
    return {key: text(name.find(key)) for key in ("first", "nick", "last")}


def extract_synthetic(path: Path) -> dict[str, object]:
    """Derive the compact hand-written Appendix A expectations from the seed."""
    root = ET.parse(path).getroot()
    game = root.find("game")
    assert game is not None
    maps = nodes(game, "maps/li")
    map_node = maps[0]
    things = nodes(map_node, "things/thing")
    pawn = next(node for node in things if node.get("Class") == "Pawn")
    corpse = next(node for node in things if node.get("Class") == "Corpse")
    world = game.find("world")
    assert world is not None
    dead = nodes(world, "worldPawns/pawnsDead/li")
    paths = canonical_paths(root)
    genes = nodes(pawn, "genes/endogenes/li")
    gene_by_id = {value(gene, "loadID"): gene for gene in genes}

    faction_raw = value(pawn, "faction")
    faction_status = "resolved" if faction_raw == "Faction_7" else "unresolved"
    host = one(pawn, "guest/hostFaction")
    slave = one(pawn, "guest/slaveFaction")
    h = nodes(pawn, "healthTracker/hediffSet/hediffs/li")[0]
    skills = nodes(pawn, "skills/skills/li")
    traits = nodes(pawn, "story/traits/allTraits/li")
    progress = nodes(game, "researchManager/progress/keys/li")
    progress_values = nodes(game, "researchManager/progress/values/li")
    tech_keys = nodes(game, "researchManager/techprints/keys/li")
    tech_values = nodes(game, "researchManager/techprints/values/li")
    corpse_refs = [text(entry) for entry in nodes(corpse, "innerContainer/innerList/li")]
    dead_ids = {value(record, "id") for record in dead}
    dead_matches = sum(ref.removeprefix("Thing_") in dead_ids for ref in corpse_refs if ref)

    gene_entries = []
    for gene in genes:
        override = one(gene, "overriddenByGene")
        raw_override = text(override)
        if override is None:
            status, target = "absent", None
        elif raw_override == "null" or override.get("IsNull") == "True":
            status, target = "explicit_null", None
        else:
            status, target = "resolved", raw_override.removeprefix("Gene_")
        gene_entries.append({
            "def": value(gene, "def"), "load_id": value(gene, "loadID"),
            "pawn_reference": value(gene, "pawn"),
            "override": {"status": status, "target_load_id": target},
        })

    item_lists = {
        "equipment": nodes(pawn, "equipment/equipment/innerList/li"),
        "apparel": nodes(pawn, "apparel/wornApparel/innerList/li"),
        "inventory": nodes(pawn, "inventory/innerContainer/innerList/li"),
    }
    item_entries = {}
    for holder, entries in item_lists.items():
        item_entries[holder] = [
            {"id": value(item, "id"), "def": value(item, "def"),
             **({"stuff": value(item, "stuff")} if holder == "apparel" else {}),
             **({"quality": value(item, "quality")} if value(item, "quality") is not None else {}),
             **({"health": int(value(item, "health"))} if value(item, "health") is not None else {}),
             "stack_count": int(value(item, "stackCount")) if value(item, "stackCount") is not None else None}
            for item in entries
        ]

    expected = {
        "summary": {
            "map_count": 1, "map_ids": [value(map_node, "uniqueID")],
            "direct_thing_count": len(things), "direct_pawn_count": 1,
            "player_faction": faction_raw, "ticks_game": int(value(game, "tickManager/ticksGame")),
            "world_name": value(game, "world/info/name"), "world_seed": value(game, "world/info/seedString"),
            "parent_label": value(game, "world/worldObjects/worldObjects/li/name"),
        },
        "pawn": {
            "saved_id": value(pawn, "id"), "name": name_parts(pawn), "gender": value(pawn, "gender"),
            "age_biological_ticks": int(value(pawn, "ageTracker/ageBiologicalTicks")),
            "birth_abs_ticks": None, "guest_status": None,
            "host_faction": {"status": "explicit_null" if text(host) == "null" else "absent", "raw": None if text(host) == "null" else text(host)},
            "slave_faction": {"status": "explicit_null" if text(slave) == "null" else "absent", "raw": None if text(slave) == "null" else text(slave)},
            "skills": [{"def": value(s, "def"), "level": int(value(s, "level")), "passion": value(s, "passion"),
                        "xp_raw": value(s, "xpSinceLastLevel"), "xp": float(value(s, "xpSinceLastLevel"))} for s in skills],
            "traits": [{"def": value(t, "def"), "degree": int(value(t, "degree")),
                        "source_gene": "explicit_null" if value(t, "sourceGene") == "null" else value(t, "sourceGene"),
                        "suppressed_by": "explicit_null" if value(t, "suppressedBy") == "null" else value(t, "suppressedBy")} for t in traits],
        },
        "health": {
            "health_state": value(pawn, "healthTracker/healthState"), "health_state_status": "absent",
            "entries": [{"saved_id": value(h, "loadID"), "def": value(h, "def"), "severity_raw": value(h, "severity"),
                         "severity": float(value(h, "severity")), "visible": value(h, "visible") == "True",
                         "part": {"body": value(h, "part/body"), "index": int(value(h, "part/index"))}}],
        },
        "genes": {"endogenes": len(genes), "xenogenes": len(nodes(pawn, "genes/xenogenes/li")),
                  "entries": gene_entries, "active_flags": None},
        "items": {**item_entries, "inventory_explicit_units": sum(int(value(x, "stackCount")) for x in item_lists["inventory"] if value(x, "stackCount") is not None),
                  "inventory_missing_stack_count_records": sum(value(x, "stackCount") is None for x in item_lists["inventory"]),
                  "inventory_estimated_units_when_opted_in": sum(int(value(x, "stackCount")) if value(x, "stackCount") is not None else 1 for x in item_lists["inventory"])},
        "research": {
            "current_project": value(game, "researchManager/currentProj"),
            "progress": [{"project_def": text(key), "progress_raw": text(val), "progress": float(text(val))}
                         for key, val in zip(progress, progress_values, strict=True)],
            "techprints": [{"project_def": text(key), "count_raw": text(val), "count": int(text(val))}
                           for key, val in zip(tech_keys, tech_values, strict=True)],
            "completed": None,
        },
        "mods": [{"package_id": text(mid), "display_name": text(name)}
                 for mid, name in zip(nodes(root, "meta/modIds/li"), nodes(root, "meta/modNames/li"), strict=True)],
        "scope": {"map": 1, "world_alive": len(nodes(world, "worldPawns/pawnsAlive/li")), "world_dead": len(dead),
                  "world_mothballed": len(nodes(world, "worldPawns/pawnsMothballed/li")),
                  "held": 1 if dead_matches else 0, "all_supported": 1 + len(dead)},
        "corpse_reference": {"corpse_count": 1, "dead_pool_matches": dead_matches,
                             "forcefully_kept_is_membership_only": True},
    }
    # These identifiers are independently calculated from the fixture's XML paths.
    expected["pawn"]["record_id"] = record_id("pawn", paths[id(pawn)])  # type: ignore[index]
    expected["health"]["record_id"] = record_id("hediff", paths[id(h)])  # type: ignore[index]
    return expected


def _private_oracle(path: Path) -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    anchor_path = Path(os.environ.get("RWSI_PRIVATE_ANCHOR") or root / ".private/acceptance-inputs.json")
    oracle_path = Path(os.environ.get("RWSI_PRIVATE_ORACLE") or root / ".private/acceptance-oracle.json")
    trusted = os.environ.get("RWSI_PRIVATE_ANCHOR_SHA256")
    if not trusted or len(trusted) != 64 or any(character not in "0123456789abcdef" for character in trusted):
        raise ValueError("external private anchor fingerprint unavailable or malformed")
    anchor_raw = anchor_path.read_bytes()
    if hashlib.sha256(anchor_raw).hexdigest() != trusted:
        raise ValueError("private anchor differs from external trusted fingerprint")
    anchor = json.loads(anchor_raw)
    if hashlib.sha256(oracle_path.read_bytes()).hexdigest() != anchor["oracle_sha256"]:
        raise ValueError("private oracle differs from frozen anchor")
    if path.stat().st_size != anchor["fixture_bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != anchor["fixture_sha256"]:
        raise ValueError("private fixture differs from frozen anchor")
    manifest = json.loads(oracle_path.read_text())
    if manifest["fixture"]["sha256"] != anchor["fixture_sha256"]:
        raise ValueError("private oracle fixture identity differs from anchor")
    return manifest


def extract_private(path: Path) -> dict[str, object]:
    """Return independent scalar observations for F01-F28 and D01-D03."""
    manifest = _private_oracle(path)
    private_rows = {row["check_id"]: row for row in manifest["assertions"]}
    specimen_id = private_rows["F24"]["scope"].removeprefix("pawn_")
    equipped_id = private_rows["F27"]["predicate"].rsplit("with id ", 1)[-1]
    worn_id = private_rows["F28"]["predicate"].rsplit("with id ", 1)[-1]
    research_key = manifest["required_dynamic_comparisons"][2]["selection"].split("key ", 1)[1].split(" after", 1)[0]
    raw = path.read_bytes()
    root = ET.fromstring(raw)
    if root.tag != "savegame":
        raise ValueError("fixture root is not savegame")
    game = root.find("game")
    assert game is not None
    paths = canonical_paths(root)
    maps = nodes(game, "maps/li")
    map_things = [nodes(m, "things/thing") for m in maps]
    map_pawns = [[n for n in group if n.get("Class") == "Pawn"] for group in map_things]
    all_map_pawns = [pawn for group in map_pawns for pawn in group]
    scenario_def = value(game, "scenario/playerFaction/factionDef")
    factions = [f for f in nodes(game, "world/factionManager/allFactions/li") if value(f, "def") == scenario_def]
    faction_load = value(factions[0], "loadID") if len(factions) == 1 else None
    player_pawns = [p for p in all_map_pawns if value(p, "def") == "Human" and value(p, "faction") == f"Faction_{faction_load}"]
    guest_omissions = [p for p in player_pawns if p.find("guest/guestStatus") is None and
                       p.find("guest/hostFaction") is not None and text(p.find("guest/hostFaction")) == "null" and
                       p.find("guest/slaveFaction") is not None and text(p.find("guest/slaveFaction")) == "null"]
    prisoner = [p for p in player_pawns if value(p, "guest/guestStatus") == "Prisoner"]
    hdiffs = [h for p in all_map_pawns for h in nodes(p, "healthTracker/hediffSet/hediffs/li")]
    endogenes = [g for p in all_map_pawns for g in nodes(p, "genes/endogenes/li")]
    xenogenes = [g for p in all_map_pawns for g in nodes(p, "genes/xenogenes/li")]
    gene_nodes = endogenes + xenogenes
    equipment = [i for p in all_map_pawns for i in nodes(p, "equipment/equipment/innerList/li")]
    apparel = [i for p in all_map_pawns for i in nodes(p, "apparel/wornApparel/innerList/li")]
    inventory = [i for p in all_map_pawns for i in nodes(p, "inventory/innerContainer/innerList/li")]
    progress_keys = nodes(game, "researchManager/progress/keys/li")
    progress_values = nodes(game, "researchManager/progress/values/li")
    tech_keys = nodes(game, "researchManager/techprints/keys/li")
    tech_values = nodes(game, "researchManager/techprints/values/li")
    mod_ids = nodes(root, "meta/modIds/li")
    mod_names = nodes(root, "meta/modNames/li")
    world = game.find("world")
    assert world is not None
    dead = nodes(world, "worldPawns/pawnsDead/li")
    dead_ids = {value(p, "id") for p in dead}
    alive = nodes(world, "worldPawns/pawnsAlive/li")
    mothballed = nodes(world, "worldPawns/pawnsMothballed/li")
    all_map_ids = {value(p, "id") for p in all_map_pawns}
    alive_ids = {value(p, "id") for p in alive}
    mothballed_ids = {value(p, "id") for p in mothballed}
    corpse_things = [t for group in map_things for t in group if t.get("Class") == "Corpse"]
    human_corpses = [c for c in corpse_things if value(c, "def") == "Corpse_Human"]
    corpse_refs = [text(li) for corpse in corpse_things for li in nodes(corpse, "innerContainer/innerList/li") if text(li)]
    corpse_targets = {ref.removeprefix("Thing_") for ref in corpse_refs}

    specimen = next(p for p in all_map_pawns if value(p, "id") == specimen_id)
    shooting = next(s for s in nodes(specimen, "skills/skills/li") if value(s, "def") == "Shooting")
    first_endogene = nodes(specimen, "genes/endogenes/li")[0]
    equipped = next(i for i in nodes(specimen, "equipment/equipment/innerList/li") if value(i, "id") == equipped_id)
    worn = next(i for i in nodes(specimen, "apparel/wornApparel/innerList/li") if value(i, "id") == worn_id)

    # F11/F12 distinguish explicit values from missing nodes; only Down is
    # represented in this compact baseline because all other stored states are absent.
    def state_counts(pawns: list[ET.Element]) -> dict[str, int]:
        counts: dict[str, int] = {"absent": 0}
        for pawn in pawns:
            state = value(pawn, "healthTracker/healthState")
            if state is None:
                counts["absent"] += 1
            else:
                counts[state] = counts.get(state, 0) + 1
        return counts

    f: dict[str, object] = {
        "F01": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "game_version": value(root, "meta/gameVersion")},
        "F02": {"count": len(maps), "ids": [value(m, "uniqueID") for m in maps]},
        "F03": {"per_map": [len(x) for x in map_things], "total": sum(map(len, map_things))},
        "F04": {"per_map": [len(x) for x in map_pawns], "total": len(all_map_pawns)},
        "F05": {"world_name": value(game, "world/info/name"), "seed": value(game, "world/info/seedString"), "ticks": int(value(game, "tickManager/ticksGame"))},
        "F06": {"def": scenario_def, "load_id": faction_load, "reference": f"Faction_{faction_load}" if faction_load is not None else None},
        "F07": sum(value(p, "def") == "Human" for p in all_map_pawns),
        "F08": len(player_pawns),
        "F09": len(guest_omissions),
        "F10": {"count": len(prisoner), "id": value(prisoner[0], "id") if prisoner else None,
                "name": name_parts(prisoner[0]) if prisoner else None},
        "F11": state_counts(all_map_pawns),
        "F12": state_counts(player_pawns),
        "F13": {"total": len(hdiffs), "def_counts": {key: sum(value(h, "def") == key for h in hdiffs) for key in private_rows["F13"]["expected"]["def_counts"]}},
        "F14": {"endogenes": len(endogenes), "xenogenes": len(xenogenes)},
        "F15": {"present": sum(g.find("overriddenByGene") is not None for g in gene_nodes),
                "absent": sum(g.find("overriddenByGene") is None for g in gene_nodes)},
        "F16": {"equipment": len(equipment), "apparel": len(apparel), "inventory": len(inventory)},
        "F17": {"explicit_records": sum(i.find("stackCount") is not None for i in inventory),
                "explicit_units": sum(int(value(i, "stackCount")) for i in inventory if value(i, "stackCount") is not None),
                "absent_records": sum(i.find("stackCount") is None for i in inventory),
                "absent_as_one_estimate": sum(int(value(i, "stackCount")) if value(i, "stackCount") is not None else 1 for i in inventory)},
        "F18": {"keys": len(progress_keys), "values": len(progress_values), "current_project": value(game, "researchManager/currentProj")},
        "F19": {"keys": len(tech_keys), "values": len(tech_values)},
        "F20": {"ids": len(mod_ids), "names": len(mod_names), "first": {"package_id": text(mod_ids[0]) if mod_ids else None, "display_name": text(mod_names[0]) if mod_names else None}},
        "F21": {"pawnsAlive": len(alive), "pawnsDead": len(dead), "pawnsMothballed": len(mothballed), "pawnsForcefullyKeptAsWorldPawns": len(nodes(world, "worldPawns/pawnsForcefullyKeptAsWorldPawns/li"))},
        "F22": {"total": len(corpse_things), "human": len(human_corpses)},
        "F23": {"distinct_refs": len(corpse_targets), "dead_pool_matches": len(corpse_targets & dead_ids),
                "map_matches": len(corpse_targets & all_map_ids), "alive_pool_matches": len(corpse_targets & alive_ids),
                "mothballed_pool_matches": len(corpse_targets & mothballed_ids), "unresolved": len(corpse_targets - dead_ids - all_map_ids - alive_ids - mothballed_ids)},
        "F24": {"name": name_parts(specimen), "age_biological_ticks": int(value(specimen, "ageTracker/ageBiologicalTicks"))},
        "F25": {"level": int(value(shooting, "level")), "passion": value(shooting, "passion"), "xp_since_last_level_raw": value(shooting, "xpSinceLastLevel")},
        "F26": {"def": value(first_endogene, "def"), "load_id": value(first_endogene, "loadID"), "pawn_reference": value(first_endogene, "pawn"),
                "overridden_by": "explicit_null" if value(first_endogene, "overriddenByGene") == "null" else value(first_endogene, "overriddenByGene")},
        "F27": {"def": value(equipped, "def"), "stack_count": int(value(equipped, "stackCount")), "health": int(value(equipped, "health"))},
        "F28": {"def": value(worn, "def"), "stuff": value(worn, "stuff"), "quality": value(worn, "quality"),
                "health": int(value(worn, "health")), "stack_count": int(value(worn, "stackCount"))},
    }

    # Dynamic selections are selected from raw XML in document order, before any
    # product output exists. Gene target IDs use the independent canonical paths.
    d01 = None
    for pawn in all_map_pawns:
        for hediff in nodes(pawn, "healthTracker/hediffSet/hediffs/li"):
            if value(hediff, "part/body") is not None and value(hediff, "part/index") is not None:
                d01 = {"owning_pawn_id": value(pawn, "id"), "hediff_load_id": value(hediff, "loadID"),
                       "def": value(hediff, "def"), "severity_raw": value(hediff, "severity"),
                       "part_body": value(hediff, "part/body"), "part_index": int(value(hediff, "part/index"))}
                break
        if d01 is not None:
            break

    # Full pawn definitions are admitted from the direct map/world-pool sources
    # and the named holder adapters. This typed candidate index intentionally
    # does not treat a text reference or arbitrary pawn-like node as a record.
    supported_pawns = list(all_map_pawns) + [x for pool in (alive, dead, mothballed) for x in pool if x.get("Class") == "Pawn"]
    supported_ids = {id(x) for x in supported_pawns}
    known_holder_paths = (
        "/maps/li/things/thing/equipment/equipment/innerList/li",
        "/maps/li/things/thing/apparel/wornApparel/innerList/li",
        "/maps/li/things/thing/inventory/innerContainer/innerList/li",
        "/maps/li/things/thing/innerContainer/innerList/li",
        "/maps/li/things/thing/innerContainer/innerList/li/innerContainer/innerList/li",
        "/world/worldObjects/worldObjects/li/pawns/innerList/li",
        "/transportShipManager/ships/li/shipThing/innerContainer/innerList/li",
    )
    for candidate in root.iter():
        if candidate.get("Class") != "Pawn" or id(candidate) in supported_ids:
            continue
        normalized_path = re.sub(r"\[\d+\]", "", paths[id(candidate)])
        in_map_thing_holder = "/maps/li/things/thing" in normalized_path and "/innerContainer/innerList/li" in normalized_path
        if in_map_thing_holder or any(marker in normalized_path for marker in known_holder_paths):
            supported_pawns.append(candidate)
            supported_ids.add(id(candidate))
    all_gene_records: list[tuple[ET.Element, ET.Element]] = []
    for candidate_pawn in supported_pawns:
        for gene in nodes(candidate_pawn, "genes/endogenes/li") + nodes(candidate_pawn, "genes/xenogenes/li"):
            all_gene_records.append((candidate_pawn, gene))
    gene_index: dict[str, list[str]] = {}
    for _, gene in all_gene_records:
        load_id = value(gene, "loadID")
        if load_id is not None:
            gene_index.setdefault("Gene_" + load_id, []).append(record_id("gene", paths[id(gene)]))
    d02 = None
    for owning_pawn, gene in all_gene_records:
        override_node = one(gene, "overriddenByGene")
        raw_override = text(override_node)
        if raw_override and raw_override != "null":
            key = raw_override if raw_override.startswith("Gene_") else "Gene_" + raw_override
            d02 = {"owning_pawn_id": value(owning_pawn, "id"), "gene_load_id": value(gene, "loadID"),
                   "def": value(gene, "def"), "override_raw": raw_override,
                   "independently_resolved_target_ids": gene_index.get(key, [])}
            break
    d03 = []
    if len(progress_keys) == len(progress_values):
        for ordinal, (key, val) in enumerate(zip(progress_keys, progress_values, strict=True)):
            if text(key) == research_key:
                raw_value = text(val)
                d03.append({"ordinal": ordinal, "progress_raw": raw_value, "progress_number": float(raw_value)})

    expected = {row["check_id"]: row["expected"] for row in manifest["assertions"]}
    matches = {key: f.get(key) == expected.get(key) for key in f}
    return {"assertions": f, "expected": expected, "matches": matches,
            "all_match_spec": set(f) == set(expected) and all(matches.values()),
            "dynamic": {"D01": d01, "D02": d02, "D03": d03}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--synthetic", action="store_true")
    args = parser.parse_args()
    result = extract_synthetic(args.fixture) if args.synthetic else extract_private(args.fixture)
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    sys.stdout.write("\n")
    if args.synthetic:
        expected_path = args.fixture.with_name("expected.json")
        return 0 if result == json.loads(expected_path.read_text()) else 1
    return 0 if result["all_match_spec"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
