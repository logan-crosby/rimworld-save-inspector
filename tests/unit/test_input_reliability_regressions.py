from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rimworld_save_inspector.errors import InspectorError
from rimworld_save_inspector.models import QueryRequest
from rimworld_save_inspector.service import QueryService
from rimworld_save_inspector.source import load_snapshot


class InputReliabilityRegressionTests(unittest.TestCase):
    def test_raw_url_is_rejected_before_path_normalization(self):
        with self.assertRaises(InspectorError) as caught:
            load_snapshot("https://example.invalid/save.rws")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_SOURCE")

    def test_absolute_literal_path_with_url_like_component_remains_a_local_file(self):
        xml = b"<savegame><game/></savegame>"
        with tempfile.TemporaryDirectory() as directory:
            literal = Path(directory) / "https:" / "example.invalid" / "save.rws"
            literal.parent.mkdir(parents=True)
            literal.write_bytes(xml)
            self.assertEqual(load_snapshot(str(literal)).source_bytes, len(xml))

    def test_unknown_pawn_like_map_thing_makes_map_population_partial_and_unknown(self):
        xml = b"""<savegame><game><scenario><playerFaction><factionDef>PlayerColony</factionDef></playerFaction></scenario>
        <world><factionManager><allFactions><li><def>PlayerColony</def><loadID>7</loadID></li></allFactions></factionManager>
        <worldPawns><pawnsDead/></worldPawns></world>
        <maps><li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>Known</id><def>Human</def><faction>Faction_7</faction></thing>
        <thing Class='AlienPawnV99'><id>Unknown</id><def>Human</def><faction>Faction_7</faction></thing>
        </things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.rws"
            path.write_bytes(xml)
            snapshot = load_snapshot(path)

        self.assertNotIn("Unknown", {record.saved_id for record in snapshot.of_type("pawn")})
        self.assertEqual(snapshot.diagnostic_counts.get("UNSUPPORTED_PAWN_CANDIDATE"), 1)
        service = QueryService(snapshot)
        pawns = service.execute(QueryRequest("pawns")).as_dict()
        summary = service.execute(QueryRequest("summary")).as_dict()
        self.assertEqual(pawns["coverage"]["status"], "partial")
        self.assertEqual(pawns["coverage"]["diagnostic_counts"]["UNSUPPORTED_PAWN_CANDIDATE"], 1)
        dead = service.execute(QueryRequest("pawns", {"scope": "world_dead"})).as_dict()
        self.assertEqual(dead["coverage"]["status"], "complete_for_scope")
        self.assertNotIn("UNSUPPORTED_PAWN_CANDIDATE", dead["coverage"]["diagnostic_counts"])
        self.assertEqual(summary["coverage"]["status"], "partial")
        self.assertIsNone(summary["data"]["maps"][0]["direct_pawn_count"])
        population = summary["data"]["population"]
        self.assertIsNone(population["direct_map_pawn_records"])
        self.assertIsNone(population["human_def_records"])
        self.assertIsNone(population["player_human_records"])

    def test_explicit_null_map_things_keeps_collection_state(self):
        xml = b"<savegame><game><maps><li><uniqueID>0</uniqueID><things IsNull='True'/></li></maps></game></savegame>"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "null-things.rws"
            path.write_bytes(xml)
            snapshot = load_snapshot(path)

        service = QueryService(snapshot)
        summary = service.execute(QueryRequest("summary")).as_dict()["data"]
        pawns = service.execute(QueryRequest("pawns")).as_dict()["data"]
        self.assertEqual(summary["maps"][0]["things_state"], "explicit_null")
        self.assertIsNone(summary["maps"][0]["direct_thing_count"])
        self.assertIsNone(summary["maps"][0]["direct_pawn_count"])
        self.assertEqual(pawns["collection_state"], "explicit_null")
        self.assertEqual(pawns["entries"], [])

    def test_unpaired_parallel_values_retain_lexical_strings_and_zero_based_ordinals(self):
        xml = b"""<savegame><meta><modIds><li>A</li><li>B</li></modIds><modNames><li>Alpha</li></modNames></meta>
        <game><researchManager><progress><keys><li>ProjectA</li><li>ProjectB</li></keys><values><li>1.25</li></values></progress></researchManager></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unpaired.rws"
            path.write_bytes(xml)
            service = QueryService(load_snapshot(path))

        progress = service.execute(QueryRequest("research")).as_dict()["data"]
        mods = service.execute(QueryRequest("mods")).as_dict()["data"]
        self.assertEqual([(row["side"], row["ordinal"], row["value"]) for row in progress["entries"]],
                         [("key", 0, "ProjectA"), ("key", 1, "ProjectB"), ("value", 0, "1.25")])
        self.assertEqual([(row["side"], row["ordinal"], row["value"]) for row in mods["entries"]],
                         [("id", 0, "A"), ("id", 1, "B"), ("name", 0, "Alpha")])

    def test_pawn_record_in_unknown_holder_is_diagnosed_but_not_supported(self):
        xml = b"""<savegame><game><maps><li><things><thing Class='ThingWithComps'><id>Container</id><def>Crate</def>
        <mysteryPawns><li Class='Pawn'><id>Hidden</id><def>Human</def></li></mysteryPawns>
        </thing></things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unknown-holder.rws"
            path.write_bytes(xml)
            snapshot = load_snapshot(path)

        self.assertNotIn("Hidden", {record.saved_id for record in snapshot.of_type("pawn")})
        self.assertEqual(snapshot.diagnostic_counts.get("UNSUPPORTED_PAWN_CANDIDATE"), 1)
        self.assertEqual(snapshot.diagnostic_counts.get("UNSUPPORTED_HOLDER"), 1)
        held = QueryService(snapshot).execute(QueryRequest("pawns", {"scope": "held"})).as_dict()
        self.assertEqual(held["coverage"]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
