import hashlib
import tempfile
import unittest
from pathlib import Path

from rimworld_save_inspector.models import Record, Snapshot, QueryRequest, record_id_for_path
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


class SummaryContractTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
