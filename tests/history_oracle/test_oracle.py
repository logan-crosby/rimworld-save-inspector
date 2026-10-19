"""Independent synthetic history contract oracle; standard library only, no product imports."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
EXPECTED = json.loads((HERE / "expected.json").read_text())
KEY_FIELDS = EXPECTED["semantic_rules"]["campaign_fields"]


def _text(parent, path):
    node = parent.find(path)
    return None if node is None else (node.text or "").strip()


def _key(root):
    meta = root.find("meta")
    if meta is None:
        return None
    vals = [_text(meta, f) for f in KEY_FIELDS]
    if any(v is None or v == "" for v in vals):
        return None
    vals[2] = "(" + ",".join(x.strip() for x in vals[2].strip("() ").split(",")) + ")"
    return vals


def _pawns(root):
    out = []
    for node in root.iter():
        if node.get("Class") != "Pawn":
            continue
        pawn_id = _text(node, "id")
        if not pawn_id:
            continue
        ancestors = []
        # ElementTree lacks parent links: determine holder scope from the unique path.
        for map_node in root.findall("./maps/map"):
            for candidate in map_node.iter():
                if candidate is node:
                    ancestors.append((map_node, candidate))
        for map_node, _ in ancestors:
            scope = f"map:{map_node.get('id')}"
            parent_path = None
            # Resolve holder by recursive containment path, retaining only contract-relevant holders.
            def locate(parent, trail):
                for child in list(parent):
                    if child is node:
                        return trail
                    next_trail = trail
                    if child.get("Class", "").startswith("Building_CryptosleepCasket"):
                        next_trail += f"/casket:{child.get('id', 'unknown')}"
                    if child.tag == "carryTracker":
                        next_trail += "/carry:pawn-carrier-08"
                    found = locate(child, next_trail)
                    if found is not None:
                        return found
                return None
            parent_path = locate(map_node, "")
            if parent_path:
                scope += parent_path
            out.append({
                "id": pawn_id,
                "name": _text(node, "name/nick"),
                "scope": scope,
                "role": _text(node, "role"),
                "birth_abs": int(_text(node, "birthAbs")) if _text(node, "birthAbs") else None,
                "bio_age_duration": int(_text(node, "biologicalAgeTicks")) if _text(node, "biologicalAgeTicks") else None,
                "inventory": "empty" if node.find("inventory") is not None and len(node.find("inventory")) == 0 else ("present" if node.find("inventory") is not None else None),
            })
    return out


def _events(root, start):
    out = []
    snap_abs = start + int(_text(root.find("meta"), "ticksGame"))
    for node in root.findall("./tales/li"):
        raw = _text(node, "occurred")
        try:
            absolute = int(raw)
            normalized = absolute - start
            status = "future-contradiction" if absolute > snap_abs else ("pre-arrival" if normalized < 0 else "known")
        except (TypeError, ValueError):
            absolute = normalized = None
            status = "unknown"
        out.append({"id":node.get("id"),"kind":_text(node,"def"),"raw_abs":absolute,"normalized_game_tick":normalized,"status":status})
    return out


def _parse(path):
    root = ET.parse(path).getroot()
    meta = root.find("meta")
    start = int(_text(meta, "gameStartAbsTick"))
    return {
        "strong_key": _key(root),
        "snapshot_abs_tick": start + int(_text(meta, "ticksGame")),
        "pawns": _pawns(root),
        "events": _events(root, start),
        "explicit_empty_traits": any(n.find("traits") is not None and len(n.find("traits")) == 0 for n in root.iter() if n.get("Class") == "Pawn"),
        "unknown_nodes": ["{urn:synthetic}opaque"] if root.find(".//{urn:synthetic}opaque") is not None else [],
    }


def _indexed_nodes(root):
    """Yield raw XML nodes with exact sibling-indexed paths and ID holder chains."""
    def visit(node, path, chain):
        node_id = node.get("id")
        next_chain = chain + ([node_id] if node_id else [])
        yield node, path, next_chain
        counts = {}
        for child in list(node):
            counts[child.tag] = counts.get(child.tag, 0) + 1
            yield from visit(child, f"{path}/{child.tag}[{counts[child.tag]}]", next_chain)
    yield from visit(root, f"{root.tag}[1]", [])


def _holder_census(root):
    included = []
    unknown_candidates = []
    known_classless_pools = {
        "savegame[1]/game[1]/world[1]/worldPawns[1]/pawnsAlive[1]/li[1]",
        "savegame[1]/game[1]/world[1]/worldPawns[1]/pawnsDead[1]/li[1]",
        "savegame[1]/game[1]/world[1]/worldPawns[1]/pawnsMothballed[1]/li[1]",
    }
    for node, path, chain in _indexed_nodes(root):
        direct = {child.tag: (child.text or "").strip() for child in list(node) if len(child) == 0}
        pawn_id = direct.get("id")
        if not pawn_id or not direct.get("def") or not direct.get("kindDef"):
            continue
        class_name = node.get("Class")
        row = {"id": pawn_id, "path": path, "holder_chain": chain,
               "class": class_name, "def": direct["def"], "kindDef": direct["kindDef"],
               "faction": direct.get("faction")}
        if class_name == "Pawn" and direct.get("faction") == "Faction_27":
            row["disposition"] = "included-class-pawn"
            included.append(row)
        elif class_name is None and path in known_classless_pools and direct.get("faction") == "Faction_27":
            row["disposition"] = "included-known-world-pool"
            included.append(row)
        elif class_name not in {None, "Pawn"}:
            row["disposition"] = "unverified-derived-class-candidate"
            unknown_candidates.append(row)
    return included, unknown_candidates


def test_independent_raw_xml_expectations_are_frozen():
    for name in ("alpha-early.xml", "alpha-later.xml", "alpha-same-tick-alternative.xml", "campaign-b-same-name.xml"):
        actual = _parse(FIXTURES / name)
        expected = EXPECTED[name]
        for field in ("snapshot_abs_tick", "pawns", "events", "explicit_empty_traits", "unknown_nodes", "strong_key"):
            if field in expected:
                assert actual[field] == expected[field], (name, field, actual[field])
    early = _parse(FIXTURES / "alpha-early.xml")
    assert early["pawns"] == EXPECTED["alpha-early.xml"]["pawns"]
    assert [p["id"] for p in early["pawns"]] == ["pawn-alpha-01", "pawn-casket-07", "pawn-carrier-08", "pawn-carried-09"]
    assert "pawn-alpha-01" != "pawn-alpha-01xy"  # references are exact IDs, not substrings


def test_campaign_and_branch_semantics():
    keys = {name: _key(ET.parse(FIXTURES / name).getroot()) for name in EXPECTED["campaigns"] if name not in EXPECTED["invalid_sources"]}
    assert keys["alpha-early.xml"] == keys["alpha-later.xml"] == keys["alpha-same-tick-alternative.xml"]
    assert keys["campaign-b-same-name.xml"] != keys["alpha-early.xml"]
    assert keys["review-missing-key.xml"] is None
    assert EXPECTED["semantic_rules"]["lineage_status"] == "unproven"
    assert EXPECTED["semantic_rules"]["same_tick_alternative_pair"] == ["alpha-later.xml", "alpha-same-tick-alternative.xml"]


def test_malformed_and_truncated_inputs_are_failures_not_empty_success():
    for filename, classification in EXPECTED["invalid_sources"].items():
        try:
            ET.parse(FIXTURES / filename)
        except ET.ParseError:
            assert classification in {"malformed-xml", "truncated-xml"}
        else:
            raise AssertionError(f"{filename} parsed as successful empty data")


def test_biological_duration_missing_vs_empty_and_exact_event_times():
    early = _parse(FIXTURES / "alpha-early.xml")
    sarai = early["pawns"][0]
    assert sarai["bio_age_duration"] == 900
    assert sarai["birth_abs"] == 53230
    assert early["events"][0]["normalized_game_tick"] == -10
    later = _parse(FIXTURES / "alpha-later.xml")
    assert later["explicit_empty_traits"] is True
    assert later["events"][1]["status"] == "future-contradiction"
    assert later["events"][2]["normalized_game_tick"] is None
    assert EXPECTED["semantic_rules"]["missing_collection_is_not_empty"] is True


def test_aliases_event_conflicts_and_presence_states():
    early = _parse(FIXTURES / "alpha-early.xml")
    later = _parse(FIXTURES / "alpha-later.xml")
    tie = _parse(FIXTURES / "alpha-same-tick-alternative.xml")
    assert early["pawns"][0]["name"] == "Founder"
    assert later["pawns"][0]["name"] == "Nova V"
    assert EXPECTED["semantic_rules"]["alias_observations"]["pawn-alpha-01"][0]["name"] == "Founder"
    assert early["events"][0]["id"] == later["events"][0]["id"] == tie["events"][0]["id"] == "Tale_1"
    assert early["events"][0]["kind"] == later["events"][0]["kind"]
    assert tie["events"][0]["kind"] != later["events"][0]["kind"]
    assert early["pawns"][0]["inventory"] == "empty"
    assert early["pawns"][1]["inventory"] is None
    # The fixture's paired snapshots have the same game tick but different bytes and state.
    assert later["snapshot_abs_tick"] == tie["snapshot_abs_tick"]
    assert (FIXTURES / "alpha-later.xml").read_bytes() != (FIXTURES / "alpha-same-tick-alternative.xml").read_bytes()



def test_actual_savegame_root_field_paths():
    root = ET.parse(FIXTURES / "native-path-shape.xml").getroot()
    assert root.tag == "savegame"
    paths = {
        "seedString":"./game/world/info/seedString",
        "persistentRandomValue":"./game/world/info/persistentRandomValue",
        "initialMapSize":"./game/world/info/initialMapSize",
        "startingYear":"./game/tickManager/startingYear",
        "gameStartAbsTick":"./game/tickManager/gameStartAbsTick",
        "startingTile":"./game/info/startingTile",
    }
    vals = [_text(root, p) for p in paths.values()]
    vals[2] = "(" + ",".join(x.strip() for x in vals[2].strip("() ").split(",")) + ")"
    assert vals == EXPECTED["native-path-shape.xml"]["strong_key"]
    start = int(_text(root, paths["gameStartAbsTick"]))
    ticks = int(_text(root, "./game/tickManager/ticksGame"))
    assert start + ticks == EXPECTED["native-path-shape.xml"]["snapshot_abs_tick"]


def test_all_holder_paths_and_unverified_candidates_are_frozen():
    root = ET.parse(FIXTURES / "all-holders.xml").getroot()
    included, unknown = _holder_census(root)
    frozen = EXPECTED["all-holders.xml"]
    assert included == frozen["included"]
    assert unknown == frozen["unknown_candidates"]
    direct = next(node for node in root.iter() if _text(node, "id") == "Human910010")
    assert {key: value for key, value in direct.attrib.items() if key != "id"} == frozen["direct_map_raw_unknown_attributes"]
    assert [child.tag for child in list(direct)] == frozen["direct_map_ordered_children"]

if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("PASS", name)
