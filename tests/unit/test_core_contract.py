import hashlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rimworld_save_inspector.models import ParseLimits, Record, Snapshot, QueryRequest, record_id_for_path
from rimworld_save_inspector.parser.core import parse_stream
from rimworld_save_inspector.service import QueryService
from rimworld_save_inspector.source import load_snapshot
from rimworld_save_inspector.indexes import IdentityIndexes
from rimworld_save_inspector.errors import InspectorError


def _load_xml(directory: str, filename: str, xml: bytes):
    path = Path(directory) / filename
    path.write_bytes(xml)
    return load_snapshot(path)


class IdentityContractTests(unittest.TestCase):
    def test_record_identity_is_type_prefixed_sha256_of_canonical_path(self):
        path = "/savegame[1]/game[1]/maps[1]/li[1]/things[1]/thing[42]"
        expected = "pawn:" + hashlib.sha256(path.encode("utf-8")).hexdigest()
        self.assertEqual(record_id_for_path("pawn", path), expected)


class HolderGraphTests(unittest.TestCase):
    def test_holder_reference_cycle_and_ambiguous_identity_are_reported(self):
        xml = b"""<savegame><game><maps><li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>Carrier</id><def>Human</def><inventory><innerContainer><innerList>
        <li><id>Box</id><def>Crate</def><innerContainer><innerList><li>Thing_Box</li>
        <li>Thing_Duplicate</li><li>Thing_Missing</li></innerList></innerContainer></li>
        <li><id>Duplicate</id><def>Gem</def></li><li><id>Duplicate</id><def>Gem</def></li>
        </innerList></innerContainer></inventory></thing>
        </things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "holder-cycle.rws", xml)
        service = QueryService(snapshot)
        pawn = next(r for r in snapshot.records if r.type_name == "pawn")
        root = service.execute(QueryRequest("inventory", {"record_id": pawn.record_id})).data
        nested_id = root["entries"][0]["item"]["nested_holders"][0]["holder_id"]
        nested = service.execute(QueryRequest("inventory", {"record_id": pawn.record_id,
                                                           "holder_id": nested_id})).as_dict()
        self.assertEqual([e["reference"]["status"] for e in nested["data"]["entries"]],
                         ["resolved", "ambiguous", "unresolved"])
        self.assertGreaterEqual(nested["coverage"]["diagnostic_counts"].get("REFERENCE_CYCLE", 0), 1)
        self.assertEqual(nested["coverage"]["diagnostic_counts"]["AMBIGUOUS_REFERENCE"], 1)
        self.assertEqual(nested["coverage"]["diagnostic_counts"]["UNRESOLVED_REFERENCE"], 1)

    def test_unsupported_pawn_holder_does_not_attach_child_records_to_parent(self):
        xml = b"""<savegame><game><maps><li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>Parent</id><def>Human</def><carryTracker><innerContainer><innerList>
        <li Class='Pawn'><id>UnknownChild</id><def>Human</def><genes><endogenes>
        <li><loadID>1</loadID><def>GeneA</def></li></endogenes></genes></li>
        </innerList></innerContainer></carryTracker></thing>
        </things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "unsupported-holder.rws", xml)
        self.assertEqual([r.saved_id for r in snapshot.records if r.type_name == "pawn"], ["Parent"])
        self.assertFalse(any(r.type_name == "gene" for r in snapshot.records))
        self.assertEqual(snapshot.direct_map_candidate_count, 0)
        self.assertEqual(snapshot.diagnostic_counts["UNSUPPORTED_PAWN_CANDIDATE"], 1)
        self.assertEqual(snapshot.diagnostic_counts["UNSUPPORTED_HOLDER"], 1)
        summary = QueryService(snapshot).execute(QueryRequest("summary")).data
        self.assertEqual(summary["maps"][0]["direct_pawn_count"], 1)

    def test_reference_mode_after_list_does_not_define_structured_child(self):
        xml = b"""<savegame><game><maps><li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>Carrier</id><def>Human</def><inventory><innerContainer><innerList>
        <li><id>Shared</id><def>Gem</def></li><li><id>Missing</id></li>
        </innerList><contentsLookMode>Reference</contentsLookMode></innerContainer></inventory></thing>
        <thing Class='Pawn'><id>Owner</id><def>Human</def><inventory><innerContainer>
        <contentsLookMode>Deep</contentsLookMode><innerList><li><id>Shared</id><def>Gem</def></li></innerList>
        </innerContainer></inventory></thing>
        </things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "reference-after-list.rws", xml)
        service = QueryService(snapshot)
        pawn = next(r for r in snapshot.records if r.type_name == "pawn" and r.saved_id == "Carrier")
        self.assertFalse(any(r.type_name == "item" and r.source_path.startswith(pawn.source_path)
                             for r in snapshot.records))
        entries = service.execute(QueryRequest("inventory", {"record_id": pawn.record_id})).data["entries"]
        self.assertEqual([e["reference"]["status"] for e in entries], ["resolved", "unresolved"])
        self.assertEqual([e["entry_kind"] for e in entries], ["reference", "reference"])
        self.assertNotIn("INFERRED_HOLDER_MODE", snapshot.diagnostic_counts)

    def test_corpse_world_object_and_ship_adapters_keep_one_pawn_definition(self):
        xml = b"""<savegame><game><world><worldObjects><worldObjects><li><ID>1</ID><def>Ship</def>
        <pawns><innerList><li>Thing_WorldOne</li></innerList></pawns></li></worldObjects></worldObjects></world>
        <maps><li><uniqueID>0</uniqueID><things><thing Class='Corpse'><id>Dead1</id><def>Corpse_Human</def>
        <innerContainer><contentsLookMode>Reference</contentsLookMode><innerList><li>Thing_WorldOne</li></innerList></innerContainer></thing>
        </things></li></maps><transportShipManager><ships><li><shipThing><innerContainer>
        <contentsLookMode>Deep</contentsLookMode><innerList><li Class='Pawn'><id>ShipOne</id><def>Human</def></li>
        </innerList></innerContainer></shipThing></li></ships></transportShipManager>
        <worldPawns><pawnsAlive><li Class='Pawn'><id>WorldOne</id><def>Human</def></li></pawnsAlive></worldPawns>
        </game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "holder-adapters.rws", xml)
        service = QueryService(snapshot)
        pawns = [r for r in snapshot.records if r.type_name == "pawn"]
        self.assertEqual({r.saved_id for r in pawns}, {"ShipOne", "WorldOne"})
        self.assertEqual(len(snapshot.auxiliary_records), 1)
        held = service.execute(QueryRequest("pawns", {"scope": "held"})).data["entries"]
        self.assertEqual({row["saved_id"] for row in held}, {"ShipOne", "WorldOne"})
        world = next(row for row in held if row["saved_id"] == "WorldOne")
        self.assertGreaterEqual(len([m for m in world["memberships"] if m["scope"] == "held"]), 2)
        ship = next(r for r in pawns if r.saved_id == "ShipOne")
        self.assertEqual(ship.memberships[0]["holder_id"],
                         record_id_for_path("holder", ship.source_path.rsplit("/li[", 1)[0]))
        self.assertNotIn("INFERRED_HOLDER_MODE", snapshot.diagnostic_counts)

    def test_immediate_inventory_edges_nested_selector_and_inferred_mode(self):
        xml = b"""<savegame><game><maps><li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>Carrier</id><def>Human</def><inventory><innerContainer><innerList>
        <li><id>Crate</id><def>Box</def><stackCount>2</stackCount><innerContainer><innerList>
        <li><id>Gem</id><def>Gem</def><stackCount>5</stackCount></li><li>Thing_Elsewhere</li>
        </innerList></innerContainer></li></innerList></innerContainer></inventory></thing>
        <thing Class='ThingWithComps'><id>MapCrate</id><def>Box</def><innerContainer><innerList>
        <li><id>MapGem</id><def>Gem</def></li><li>Thing_Elsewhere</li>
        </innerList></innerContainer></thing>
        </things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "holder-graph.rws", xml)
        service = QueryService(snapshot)
        pawn = next(r for r in snapshot.records if r.type_name == "pawn")
        root = service.execute(QueryRequest("inventory", {"record_id": pawn.record_id})).data
        self.assertEqual([e["item"]["saved_id"] for e in root["entries"]], ["Crate"])
        self.assertEqual(root["entry_count"], 1)
        self.assertEqual(root["quantity"]["explicit_stack_units"], 2)
        nested = root["entries"][0]["item"]["nested_holders"]
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0]["entry_count"], 2)
        child = service.execute(QueryRequest("inventory", {"record_id": pawn.record_id,
                                                          "holder_id": nested[0]["holder_id"]})).data
        self.assertEqual([e["item"]["saved_id"] if e["item"] else None for e in child["entries"]], ["Gem", None])
        self.assertEqual(child["entry_count"], 2)
        self.assertEqual(child["quantity"]["explicit_stack_units"], 5)
        map_gem = next(r for r in snapshot.records if r.type_name == "item" and r.saved_id == "MapGem")
        self.assertEqual(len(map_gem.memberships), 1)
        self.assertEqual(map_gem.memberships[0]["scope"], "held")
        self.assertEqual(map_gem.memberships[0]["holder_id"], record_id_for_path("holder", map_gem.source_path.rsplit("/li[", 1)[0]))
        self.assertEqual(snapshot.diagnostic_counts.get("INFERRED_HOLDER_MODE"), 3)

    def test_foreign_nested_holder_selector_is_rejected(self):
        xml = b"""<savegame><game><maps><li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>A</id><def>Human</def><inventory><innerContainer><innerList>
        <li><id>ABox</id><def>Box</def><innerContainer><innerList><li><id>AGem</id><def>Gem</def></li></innerList></innerContainer></li>
        </innerList></innerContainer></inventory></thing>
        <thing Class='Pawn'><id>B</id><def>Human</def><inventory><innerContainer><innerList>
        <li><id>BBox</id><def>Box</def><innerContainer><innerList><li><id>BGem</id><def>Gem</def></li></innerList></innerContainer></li>
        </innerList></innerContainer></inventory></thing>
        </things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "foreign-holder.rws", xml)
        service = QueryService(snapshot)
        pawns = {r.saved_id: r for r in snapshot.records if r.type_name == "pawn"}
        b_gem = next(r for r in snapshot.records if r.type_name == "item" and r.saved_id == "BGem")
        foreign_id = record_id_for_path("holder", b_gem.source_path.rsplit("/li[", 1)[0])
        with self.assertRaises(InspectorError) as raised:
            service.execute(QueryRequest("inventory", {"record_id": pawns["A"].record_id, "holder_id": foreign_id}))
        self.assertEqual(raised.exception.code, "HOLDER_NOT_OWNED")


class SummaryContractTests(unittest.TestCase):
    def test_summary_metadata_and_parallel_arrays_preserve_source_matching_and_order(self):
        snapshot = Snapshot(
            "sha256:" + "a" * 64, "interpretation:" + "b" * 64, 1, "tiny.rws", "1.6.1",
            "rimworld-1.6/v1", "0.1.0", (),
            globals={
                "/savegame[1]/game[1]/other[1]/name[1]": "unrelated",
                "/savegame[1]/game[1]/world[1]/info[1]/name[3]": "first matching world",
                "/savegame[1]/game[1]/world[1]/info[1]/name[1]": "later matching world",
                "/savegame[1]/meta[1]/modIds[1]/li[2]": "second.id",
                "/savegame[1]/meta[1]/modIds[1]/li[1]": "first.id",
                "/savegame[1]/meta[1]/modNames[1]/li[2]": "Second",
                "/savegame[1]/meta[1]/modNames[1]/li[1]": "First",
            },
            collection_states={
                "/savegame[1]/meta[1]/modIds[1]": "present",
                "/savegame[1]/meta[1]/modNames[1]": "present",
            },
        )
        service = QueryService(snapshot)
        self.assertEqual(service.execute(QueryRequest("summary")).data["world_name"], "first matching world")
        self.assertEqual(
            [(entry["package_id"], entry["display_name"]) for entry in service.execute(QueryRequest("mods")).data["entries"]],
            [("first.id", "First"), ("second.id", "Second")],
        )

    def test_summary_uses_saved_direct_thing_and_pawn_counts(self):
        map_path = "/savegame[1]/game[1]/maps[1]/li[1]"
        map_record = Record(
            record_id=record_id_for_path("map", map_path), type_name="map", saved_id="0", source_path=map_path,
            values={"/uniqueID": "0", "/direct_thing_count": 2, "/direct_pawn_count": 1},
        )
        pawn_path = map_path + "/things[1]/thing[1]"
        pawn = Record(record_id=record_id_for_path("pawn", pawn_path), type_name="pawn", saved_id="Human1",
                      source_path=pawn_path, values={"/def": "Human", "/faction": "Faction_7"}, scope="map")
        snapshot = Snapshot("sha256:" + "a" * 64, "interpretation:" + "b" * 64, 1, "tiny.rws", "1.6.1",
                            "rimworld-1.6/v1", "0.1.0", (map_record, pawn),
                            globals={"/savegame[1]/game[1]/scenario[1]/playerFaction[1]/factionDef[1]": "PlayerColony"},
                            collection_states={"/savegame[1]/game[1]/maps[1]": "present",
                                              map_path + "/things[1]": "present"})
        result = QueryService(snapshot).execute(QueryRequest("summary")).as_dict()
        self.assertEqual(result["data"]["maps"][0]["direct_thing_count"], 2)
        self.assertEqual(result["data"]["maps"][0]["direct_pawn_count"], 1)

    def test_streaming_loader_counts_direct_map_things_and_emits_stable_identity(self):
        xml = b"""<savegame><meta><gameVersion>1.6.1234</gameVersion></meta><game>
        <world><info><name>Test</name><seedString>seed</seedString></info></world>
        <maps><li><uniqueID>7</uniqueID><things>
        <thing Class='Pawn'><id>Human1</id><def>Human</def><faction>Faction_7</faction></thing>
        <thing Class='Corpse'><id>Corpse1</id><def>Corpse_Human</def></thing>
        </things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "tiny.rws", xml)
        result = QueryService(snapshot).execute(QueryRequest("summary")).as_dict()
        map_result = result["data"]["maps"][0]
        self.assertEqual(snapshot.snapshot_id, "sha256:" + hashlib.sha256(xml).hexdigest())
        self.assertEqual(map_result["direct_thing_count"], 2)
        self.assertEqual(map_result["direct_pawn_count"], 1)
        self.assertEqual(result["data"]["population"]["direct_map_pawn_records"], 1)

    def test_all_domain_queries_share_the_same_loaded_snapshot(self):
        xml = b"""<savegame><meta><gameVersion>1.6.1234</gameVersion><modIds><li>core</li></modIds><modNames><li>Core</li></modNames></meta>
        <game><tickManager><ticksGame>90</ticksGame></tickManager><scenario><playerFaction><factionDef>PlayerColony</factionDef></playerFaction></scenario>
        <world><info><name>TestWorld</name><seedString>seed</seedString></info><factionManager><allFactions><li><def>PlayerColony</def><loadID>7</loadID></li></allFactions></factionManager>
        <worldPawns><pawnsDead><li Class='Pawn'><id>Human2</id><def>Human</def></li></pawnsDead></worldPawns>
        <worldObjects><worldObjects><li><ID>9</ID><def>Settlement</def><name>Landing</name></li></worldObjects></worldObjects></world>
        <maps><li><uniqueID>0</uniqueID><mapInfo><parent>WorldObject_9</parent><size>(10, 1, 10)</size></mapInfo><things><thing Class='Pawn'>
        <id>Human1</id><def>Human</def><kindDef>Colonist</kindDef><name><first>Alex</first><nick>Lex</nick><last>Example</last></name><faction>Faction_7</faction>
        <skills><skills><li><def>Shooting</def><level>4</level><passion>Minor</passion><xpSinceLastLevel>15.5</xpSinceLastLevel></li></skills></skills>
        <story><traits><allTraits><li><def>Tough</def><degree>1</degree></li></allTraits></traits></story>
        <healthTracker><hediffSet><hediffs><li Class='Hediff_Injury'><loadID>10</loadID><def>Cut</def><severity>0.25</severity><visible>True</visible><part><body>Human</body><index>3</index></part></li></hediffs></hediffSet></healthTracker>
        <genes><endogenes><li><def>GeneA</def><loadID>1</loadID><pawn>Thing_Human1</pawn><overriddenByGene>Gene_2</overriddenByGene></li><li><def>GeneB</def><loadID>2</loadID><pawn>Thing_Human1</pawn><overriddenByGene>null</overriddenByGene></li></endogenes><xenogenes/><xenotype>Baseliner</xenotype><hybrid>False</hybrid></genes>
        <equipment><equipment><innerList><li Class='ThingWithComps'><id>Rifle1</id><def>Rifle</def></li></innerList></equipment></equipment>
        <apparel><wornApparel><innerList><li Class='ThingWithComps'><id>Hat1</id><def>Hat</def><stackCount>1</stackCount></li></innerList></wornApparel></apparel>
        <inventory><innerContainer><innerList><li Class='ThingWithComps'><id>Medicine1</id><def>Medicine</def><stackCount>2</stackCount></li></innerList></innerContainer></inventory>
        </thing><thing Class='Corpse'><id>Corpse1</id><def>Corpse_Human</def><innerContainer><contentsLookMode>Reference</contentsLookMode><innerList><li>Thing_Human2</li></innerList></innerContainer></thing></things></li></maps>
        <researchManager><currentProj>SolarPower</currentProj><progress><keys><li>SolarPower</li></keys><values><li>25.5</li></values></progress><techprints><keys/><values/></techprints></researchManager>
        </game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "domains.rws", xml)
        service = QueryService(snapshot)
        pawn_id = next(r.record_id for r in snapshot.records if r.type_name == "pawn")
        self.assertEqual(service.execute(QueryRequest("inspect")).data["record_totals"]["pawn"], 2)
        self.assertEqual(service.execute(QueryRequest("summary")).data["maps"][0]["parent_label"], "Landing")
        self.assertEqual(service.execute(QueryRequest("pawns")).data["entries"][0]["name"]["display"], "Lex")
        held = service.execute(QueryRequest("pawns", {"scope": "held"})).data["entries"]
        self.assertEqual([entry["saved_id"] for entry in held], ["Human2"])
        self.assertEqual(service.execute(QueryRequest("pawn", {"record_id": pawn_id})).data["pawn"]["pawn_def"], "Human")
        self.assertEqual(service.execute(QueryRequest("pawn", {"record_id": pawn_id, "section": "skills"})).data["entries"][0]["skill_def"], "Shooting")
        self.assertEqual(service.execute(QueryRequest("health", {"record_id": pawn_id})).data["entries"][0]["part"]["index"], 3)
        self.assertEqual(service.execute(QueryRequest("genes", {"record_id": pawn_id})).data["entries"][0]["overridden_by"]["status"], "resolved")
        self.assertEqual(service.execute(QueryRequest("equipment", {"record_id": pawn_id})).data["entries"][0]["item"]["item_def"], "Rifle")
        self.assertEqual(service.execute(QueryRequest("inventory", {"record_id": pawn_id})).data["quantity"]["explicit_stack_units"], 2)
        self.assertEqual(service.execute(QueryRequest("research")).data["entries"][0]["progress_points"], 25.5)
        self.assertEqual(service.execute(QueryRequest("mods")).data["entries"][0]["package_id"], "core")

    def test_frozen_seed_service_matches_world_metadata_and_holder_record_union(self):
        fixture = Path(__file__).parents[1] / "fixtures/synthetic/seed.rws"
        snapshot = load_snapshot(fixture)
        service = QueryService(snapshot)
        summary = service.execute(QueryRequest("summary")).as_dict()["data"]
        inspect = service.execute(QueryRequest("inspect")).as_dict()["data"]
        pawn = next(record for record in snapshot.records if record.type_name == "pawn" and record.scope == "map")
        pawn_list = service.execute(QueryRequest("pawns")).as_dict()["data"]
        pawn_entry = pawn_list["entries"][0]
        world_dead = service.execute(QueryRequest("pawns", {"scope": "world_dead"})).as_dict()["data"]["entries"]
        held = service.execute(QueryRequest("pawns", {"scope": "held"})).as_dict()["data"]["entries"]
        all_supported = service.execute(QueryRequest("pawns", {"scope": "all_supported"})).as_dict()["data"]["entries"]

        self.assertEqual(
            (summary["map_count"], summary["maps"][0]["map_id"],
             summary["maps"][0]["direct_thing_count"], summary["maps"][0]["direct_pawn_count"],
             summary["world_name"], summary["world_seed"], summary["ticks_game"]),
            (1, "0", 2, 1, "SyntheticWorld", "fixture", 120000),
        )
        self.assertEqual(set(pawn_list), {"field_status", "raw_values", "derivations", "collection_state", "entries"})
        self.assertEqual([m["scope"] for m in pawn_entry["memberships"]], ["map"])
        self.assertEqual(pawn_entry["memberships"][0]["map_id"], "0")
        self.assertEqual(set(pawn_entry["memberships"][0]), {"scope", "map_id", "pool", "holder_id", "source_path"})
        self.assertEqual(inspect["record_totals"]["pawn"], 2)
        self.assertEqual(inspect["record_totals"]["record_count"], 2)
        self.assertEqual(inspect["record_totals"]["resolved_saved_identity_count"], 2)
        self.assertGreaterEqual(inspect["record_totals"]["membership_count"], 2)
        self.assertEqual([entry["saved_id"] for entry in world_dead], ["Human2"])
        self.assertEqual([entry["saved_id"] for entry in held], ["Human2"])
        self.assertEqual({entry["saved_id"] for entry in all_supported}, {"Human1", "Human2"})
        self.assertEqual(len(all_supported), 2)
        self.assertIn("held", {membership["scope"] for membership in held[0]["memberships"]})
        self.assertEqual(set(pawn_entry), {
            "record_id", "saved_id", "source_path", "field_status", "raw_values", "derivations",
            "pawn_def", "kind_def", "name", "faction", "memberships", "guest_status",
        })

        equipment = service.execute(QueryRequest("equipment", {"record_id": pawn.record_id})).as_dict()["data"]
        apparel = service.execute(QueryRequest("equipment", {"record_id": pawn.record_id, "holder": "apparel"})).as_dict()["data"]
        inventory = service.execute(QueryRequest("inventory", {"record_id": pawn.record_id, "estimate_missing_as_one": True})).as_dict()["data"]
        self.assertEqual([e["item"]["saved_id"] for e in equipment["entries"] if e["entry_kind"] == "record"], ["Rifle1"])
        self.assertEqual(equipment["entry_count"], 1)
        self.assertEqual([e["item"]["saved_id"] for e in apparel["entries"] if e["entry_kind"] == "record"], ["Hat1"])
        self.assertEqual(apparel["entry_count"], 1)
        self.assertEqual([e["item"]["saved_id"] for e in inventory["entries"] if e["entry_kind"] == "record"], ["Medicine1", "Food1"])
        self.assertEqual(inventory["entry_count"], 2)
        self.assertEqual(inventory["item_record_count"], 2)
        self.assertEqual(inventory["quantity"]["explicit_stack_units"], 2)
        self.assertEqual(inventory["quantity"]["missing_stack_count_records"], 1)
        self.assertEqual(inventory["quantity"]["estimated_stack_units"], 3)


class ReferenceAndPagingTests(unittest.TestCase):
    def test_pawn_first_page_materializes_only_returned_rows(self):
        pawns = tuple(
            Record(record_id=record_id_for_path("pawn", path), type_name="pawn", saved_id=f"Pawn_{index}",
                   source_path=path, values={"/def": "Human"}, scope="map")
            for index in range(100)
            for path in (f"/savegame[1]/game[1]/maps[1]/li[1]/things[1]/thing[{index + 1}]",)
        )
        snapshot = Snapshot("sha256:" + "a" * 64, "interpretation:" + "b" * 64, 1, "many.rws", "1.6.1",
                            "rimworld-1.6/v1", "0.1.0", pawns,
                            collection_states={
                                "/savegame[1]/game[1]/maps[1]": "present",
                                "/savegame[1]/game[1]/maps[1]/li[1]/things[1]": "present",
                            })
        service = QueryService(snapshot)
        with patch.object(service, "_pawn_index", wraps=service._pawn_index) as materialize:
            result = service.execute(QueryRequest("pawns", {"scope": "map"}, limit=1))
        self.assertEqual(result.page["total"], 100)
        self.assertEqual(len(result.data["entries"]), 1)
        self.assertEqual(materialize.call_count, 1)


    def test_deadline_is_checked_at_eof_for_short_documents(self):
        class SlowReader(io.BytesIO):
            def read(self, size=-1):
                time.sleep(0.01)
                return super().read(size)

        with self.assertRaises(InspectorError) as caught:
            parse_stream(SlowReader(b"<savegame><game/></savegame>"), limits=ParseLimits(deadline_seconds=0.001))
        self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")

    def test_candidate_fast_path_keeps_classed_non_li_held_pawns(self):
        xml = b"""<savegame><game><maps><li><things>
        <thing Class='Pawn'><id>Parent</id><def>Human</def><inventory><pawns>
        <other Class='Pawn'><id>Child</id><def>Human</def></other>
        </pawns></inventory></thing></things></li></maps>
        <unrelated><li><id>Ignored</id><def>Human</def></li></unrelated>
        </game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "candidate-fast-path.rws", xml)
        pawns = [record for record in snapshot.records if record.type_name == "pawn"]
        self.assertEqual([record.saved_id for record in pawns], ["Parent", "Child"])
        self.assertEqual(pawns[1].owner_record_id, pawns[0].record_id)

    def test_direct_map_corpse_adapter_is_internal_and_excludes_nested_held_corpses(self):
        xml = b"""<savegame><game><maps><li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>Human1</id><def>Human</def></thing>
        <thing Class='Corpse'><id>Corpse1</id><def>Corpse_Human</def><innerContainer>
        <contentsLookMode>Reference</contentsLookMode><innerList><li>Thing_Human1</li></innerList>
        </innerContainer></thing><thing Class='ThingWithComps'><id>Thing1</id><def>Crate</def></thing>
        </things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "corpse-adapter.rws", xml)
        self.assertEqual(len(snapshot.auxiliary_records), 1)
        self.assertEqual(snapshot.auxiliary_records[0].type_name, "corpse")
        self.assertEqual(snapshot.auxiliary_records[0].saved_id, "Corpse1")
        self.assertEqual(snapshot.auxiliary_records[0].value("/def"), "Corpse_Human")
        self.assertFalse(any(record.type_name == "corpse" for record in snapshot.records))
        self.assertEqual([value for path, value in snapshot.globals.items() if "/innerContainer[" in path], ["Thing_Human1"])
        held = QueryService(snapshot).execute(QueryRequest("pawns", {"scope": "held"})).data["entries"]
        self.assertEqual([entry["saved_id"] for entry in held], ["Human1"])

    def test_world_pool_references_resolve_memberships_without_inventing_records(self):
        xml = b"""<savegame><game><world><worldPawns>
        <pawnsAlive><li><id>Alive1</id><def>Human</def><name><nick>Alive</nick></name></li></pawnsAlive>
        <pawnsDead><li><id>Human1</id></li></pawnsDead>
        <pawnsMothballed><li><id>Moth1</id><def>Human</def><name><nick>Moth</nick></name></li></pawnsMothballed>
        <pawnsForcefullyKeptAsWorldPawns><li>Thing_Human1</li></pawnsForcefullyKeptAsWorldPawns>
        </worldPawns></world><maps><li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>Human1</id><def>Human</def></thing>
        <thing Class='Corpse'><id>Corpse1</id><def>Corpse_Human</def><innerContainer>
        <contentsLookMode>Reference</contentsLookMode><innerList><li>Thing_Human1</li></innerList>
        </innerContainer></thing></things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "world-pool-references.rws", xml)
        service = QueryService(snapshot)

        self.assertEqual(sum(record.type_name == "pawn" for record in snapshot.records), 3)
        self.assertEqual([p["saved_id"] for p in service.execute(QueryRequest("pawns", {"scope": "world_dead"})).data["entries"]], ["Human1"])
        self.assertEqual([p["saved_id"] for p in service.execute(QueryRequest("pawns", {"scope": "world_alive"})).data["entries"]], ["Alive1"])
        self.assertEqual([p["saved_id"] for p in service.execute(QueryRequest("pawns", {"scope": "world_mothballed"})).data["entries"]], ["Moth1"])
        memberships = service.execute(QueryRequest("pawns", {"scope": "all_supported"})).data["entries"]
        human = next(p for p in memberships if p["saved_id"] == "Human1")
        self.assertEqual({m["scope"] for m in human["memberships"]}, {"map", "world_dead", "held"})
        self.assertEqual(next(m for m in human["memberships"] if m["scope"] == "world_dead")["pool"], "dead")

    def test_holder_reference_resolves_item_and_includes_its_stack_once(self):
        xml = b"""<savegame><game><maps><li><uniqueID>0</uniqueID><things><thing Class='Pawn'>
        <id>Human1</id><def>Human</def><inventory><innerContainer><innerList>
        <li>Thing_Rifle1</li></innerList></innerContainer></inventory>
        <equipment><equipment><innerList><li Class='ThingWithComps'><id>Rifle1</id><def>Rifle</def><stackCount>3</stackCount></li></innerList></equipment></equipment>
        </thing></things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "held-reference.rws", xml)
        service = QueryService(snapshot)
        pawn = next(r for r in snapshot.records if r.type_name == "pawn")
        result = service.execute(QueryRequest("inventory", {"record_id": pawn.record_id})).data
        self.assertEqual(result["resolved_item_count"], 1)
        self.assertEqual(result["quantity"]["explicit_stack_units"], 3)
        self.assertEqual(result["entries"][0]["reference"]["status"], "resolved")

    def test_negative_stack_count_is_invalid(self):
        xml = b"<savegame><game><maps><li><things><thing Class='Pawn'><id>Human1</id><def>Human</def><inventory><innerContainer><innerList><li Class='ThingWithComps'><id>Rifle1</id><def>Rifle</def><stackCount>-1</stackCount></li></innerList></innerContainer></inventory></thing></things></li></maps></game></savegame>"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "negative-stack.rws"
            path.write_bytes(xml)
            snapshot = load_snapshot(path)
        item = next(r for r in snapshot.records if r.type_name == "item")
        self.assertEqual(item.field_status.get("/stackCount"), "invalid")
        self.assertEqual(item.value("/stackCount"), None)

    def test_real_secure_parser_rejects_doctype(self):
        xml = b"<!DOCTYPE savegame [<!ENTITY x 'expanded'>]><savegame><gameVersion>&x;</gameVersion></savegame>"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe.rws"
            path.write_bytes(xml)
            with self.assertRaises(InspectorError) as caught:
                load_snapshot(path)
        self.assertEqual(caught.exception.code, "UNSAFE_XML")

    def test_duplicate_saved_ids_resolve_as_ambiguous_and_strip_only_thing_prefix(self):
        pawns = tuple(Record(record_id=f"pawn:{letter * 64}", type_name="pawn", saved_id="Human7",
                             source_path=f"/savegame[1]/game[1]/maps[1]/li[1]/things[1]/thing[{i}]")
                      for i, letter in enumerate(("a", "b"), 1))
        indexes = IdentityIndexes(pawns)
        ref = indexes.resolve("Thing_Human7", "thing")
        self.assertEqual(ref.status, "ambiguous")
        self.assertEqual(ref.normalized_id, "Human7")
        unknown = indexes.resolve("Other_Human7", "thing")
        self.assertEqual(unknown.normalized_id, "Other_Human7")
        self.assertEqual(unknown.status, "unresolved")

    def test_cursor_is_bound_to_the_normalized_filter_set(self):
        path = "/savegame[1]/game[1]/maps[1]/li[1]/things[1]/thing[1]"
        first = Record(record_id=record_id_for_path("pawn", path), type_name="pawn", saved_id="Human1",
                       source_path=path, values={"/name/nick": "Alice", "/def": "Human"}, scope="map")
        second_path = path.replace("thing[1]", "thing[2]")
        second = Record(record_id=record_id_for_path("pawn", second_path), type_name="pawn", saved_id="Human2",
                        source_path=second_path, values={"/name/nick": "Bob", "/def": "Human"}, scope="map")
        snapshot = Snapshot("sha256:" + "a" * 64, "interpretation:" + "b" * 64, 1, "tiny.rws", "1.6.1",
                            "rimworld-1.6/v1", "0.1.0", (first, second), collection_states={
                                "/savegame[1]/game[1]/maps[1]": "present",
                                "/savegame[1]/game[1]/maps[1]/li[1]/things[1]": "present"})
        service = QueryService(snapshot)
        first_page = service.execute(QueryRequest("pawns", limit=1))
        self.assertEqual(first_page.page["total"], 2)
        with self.assertRaises(InspectorError) as caught:
            service.execute(QueryRequest("pawns", filters={"scope": "map", "name": "Alice"}, limit=1,
                                         cursor=first_page.page["next_cursor"]))
        self.assertEqual(caught.exception.code, "INVALID_CURSOR")

    def test_scalar_literal_null_invalid_number_and_explicit_null_stay_distinct(self):
        xml = b"""<savegame><game><maps><li><uniqueID>0</uniqueID><things><thing Class='Pawn'>
        <id>Human1</id><def>Human</def><name><nick>null</nick></name><ageTracker><ageBiologicalTicks>oops</ageBiologicalTicks></ageTracker>
        <guest><hostFaction IsNull='True'/></guest></thing></things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "presence.rws", xml)
        pawn = next(record for record in snapshot.records if record.type_name == "pawn")
        self.assertEqual(pawn.value("/name/nick"), "null")
        self.assertEqual(pawn.field_status["/ageTracker/ageBiologicalTicks"], "invalid")
        self.assertEqual(pawn.field_status["/guest/hostFaction"], "explicit_null")
        self.assertEqual(pawn.field_status["/guest/guestStatus"], "absent")

    def test_single_oversized_hediff_names_that_no_narrower_v1_query_exists(self):
        pawn_path = "/savegame[1]/game[1]/maps[1]/li[1]/things[1]/thing[1]"
        pawn = Record(record_id=record_id_for_path("pawn", pawn_path), type_name="pawn", saved_id="Human1",
                      source_path=pawn_path, scope="map")
        hediff_path = pawn_path + "/healthTracker[1]/hediffSet[1]/hediffs[1]/li[1]"
        hediff = Record(record_id=record_id_for_path("hediff", hediff_path), type_name="hediff", saved_id="1",
                        source_path=hediff_path, owner_record_id=pawn.record_id,
                        values={"/chemical": "x" * 530_000})
        snapshot = Snapshot("sha256:" + "a" * 64, "interpretation:" + "b" * 64, 1, "large.rws", "1.6.1",
                            "rimworld-1.6/v1", "0.1.0", (pawn, hediff), collection_states={
                                pawn_path + "/healthTracker[1]/hediffSet[1]/hediffs[1]": "present"})
        with self.assertRaises(InspectorError) as caught:
            QueryService(snapshot).execute(QueryRequest("health", {"record_id": pawn.record_id}))
        self.assertEqual(caught.exception.code, "RESULT_TOO_LARGE")
        self.assertIn("narrower_query", caught.exception.details)
        self.assertIn("no narrower query is available in v1", caught.exception.details["narrower_query"].lower())


class ResearchDecodingContractTests(unittest.TestCase):
    def _research(self, value_xml: bytes, *, kind: str, current_xml: bytes = b"<currentProj>ProjectA</currentProj>",
                  key_xml: bytes = b"<li>ProjectA</li>"):
        values_name = b"values"
        element = ("progress" if kind == "progress" else "techprints").encode()
        xml = (b"<savegame><game><researchManager>" + current_xml + b"<" + element + b"><keys>" + key_xml + b"</keys><" +
               values_name + b">" + value_xml + b"</" + values_name + b"></" + element + b"></researchManager></game></savegame>")
        with tempfile.TemporaryDirectory() as directory:
            snapshot = _load_xml(directory, "research-state.rws", xml)
        return QueryService(snapshot).execute(QueryRequest("research", {"section": kind})).as_dict()

    def test_paired_research_values_distinguish_zero_invalid_nonfinite_empty_and_explicit_null(self):
        cases = (
            (b"<li>0</li>", 0, None, "0"),
            (b"<li>not-a-number</li>", None, "invalid", "not-a-number"),
            (b"<li>NaN</li>", None, "invalid", "NaN"),
            (b"<li>Infinity</li>", None, "invalid", "Infinity"),
            (b"<li/>", None, "invalid", ""),
            (b"<li IsNull='True'/>", None, "explicit_null", None),
        )
        for kind in ("progress", "techprints"):
            for value_xml, expected_value, expected_status, expected_raw in cases:
                with self.subTest(kind=kind, value_xml=value_xml):
                    result = self._research(value_xml, kind=kind)
                    data = result["data"]
                    row = data["entries"][0]
                    field = "/progress_points" if kind == "progress" else "/techprint_count"
                    normalized = "progress_points" if kind == "progress" else "techprint_count"
                    self.assertEqual(data["alignment"], "paired")
                    self.assertEqual(row[normalized], expected_value)
                    if expected_status is None:
                        self.assertNotIn(field, row["field_status"])
                    else:
                        self.assertEqual(row["field_status"][field], expected_status)
                    self.assertIn(field, row["raw_values"])
                    self.assertEqual(row["raw_values"][field], expected_raw)
                    if expected_status == "invalid":
                        self.assertEqual(result["coverage"]["diagnostic_counts"].get("INVALID_VALUE"), 1)
                    else:
                        self.assertNotIn("INVALID_VALUE", result["coverage"]["diagnostic_counts"])

    def test_current_project_distinguishes_absent_explicit_null_and_present_empty(self):
        cases = (
            (b"", None, "absent"),
            (b"<currentProj IsNull='True'/>", None, "explicit_null"),
            (b"<currentProj/>", "", None),
        )
        for current_xml, expected_value, expected_status in cases:
            with self.subTest(current_xml=current_xml):
                result = self._research(b"<li>0</li>", kind="progress", current_xml=current_xml)
                data = result["data"]
                self.assertEqual(data["current_project"], expected_value)
                if expected_status is None:
                    self.assertNotIn("/current_project", data["field_status"])
                else:
                    self.assertEqual(data["field_status"]["/current_project"], expected_status)

    def test_nullable_research_project_key_is_not_stringified(self):
        result = self._research(b"<li>0</li>", kind="progress", key_xml=b"<li IsNull='True'/>")
        row = result["data"]["entries"][0]
        self.assertIsNone(row["project_def"])
        self.assertIsNone(row["saved_id"])
        self.assertEqual(row["field_status"], {"/project_def": "explicit_null"})

    def test_paired_mod_nulls_keep_nullable_string_status(self):
        xml = b"""<savegame><meta><modIds><li>alpha</li><li IsNull='True'/></modIds>
        <modNames><li IsNull='True'/><li/></modNames></meta><game/></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            service = QueryService(_load_xml(directory, "mods-null.rws", xml))
        rows = service.execute(QueryRequest("mods")).as_dict()["data"]["entries"]
        self.assertIsNone(rows[0]["display_name"])
        self.assertEqual(rows[0]["field_status"], {"/display_name": "explicit_null"})
        self.assertIsNone(rows[1]["package_id"])
        self.assertEqual(rows[1]["field_status"], {"/package_id": "explicit_null"})
        self.assertEqual(rows[1]["display_name"], "")
        self.assertNotIn("/display_name", rows[1]["field_status"])


if __name__ == "__main__":
    unittest.main()
