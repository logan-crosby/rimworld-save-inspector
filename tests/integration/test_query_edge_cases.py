"""Deterministic synthetic assertions for paging, cursors and holder forms."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from rimworld_save_inspector.errors import InspectorError
from rimworld_save_inspector.models import QueryRequest
from rimworld_save_inspector.service import QueryService
from rimworld_save_inspector.source import load_snapshot


def _load(xml: bytes):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "edge.rws"
        path.write_bytes(xml)
        return load_snapshot(path)


class QueryEdgeCaseTests(unittest.TestCase):
    def test_paging_limits_union_order_bytes_and_empty_continuation(self):
        pawns = b"".join(
            f"<thing Class='Pawn'><id>P{index:03d}</id><def>Human</def></thing>".encode()
            for index in range(205)
        )
        snapshot = _load(b"<savegame><game><maps><li><uniqueID>0</uniqueID><things>" + pawns +
                         b"</things></li></maps></game></savegame>")
        service = QueryService(snapshot)
        full = service.execute(QueryRequest("pawns", {"scope": "map"}, limit=200)).as_dict()
        expected_records = sorted((record for record in snapshot.records if record.type_name == "pawn" and record.scope == "map"),
                                  key=lambda row: (row.saved_id is None, row.saved_id or "", row.record_id))
        full_ids = [row.record_id for row in expected_records]
        self.assertEqual(len(full_ids), 205)
        self.assertEqual(full["page"]["total"], 205)
        for limit in (1, 50, 200):
            cursor = None
            union = []
            responses = []
            while True:
                response = service.execute(QueryRequest("pawns", {"scope": "map"}, limit=limit, cursor=cursor)).as_dict()
                responses.append(response)
                union.extend(row["record_id"] for row in response["data"]["entries"])
                self.assertEqual(response["page"]["total"], 205)
                self.assertEqual(response["page"]["returned"], len(response["data"]["entries"]))
                cursor = response["page"]["next_cursor"]
                if cursor is None:
                    break
            self.assertEqual(union, full_ids, f"limit={limit} pages must be the exact ordered union")
            full_bytes = len(json.dumps(full, separators=(",", ":")))
            first_bytes = len(json.dumps(responses[0], separators=(",", ":")))
            if limit < 200:
                self.assertLess(first_bytes, full_bytes)
            else:
                self.assertEqual(responses[0], full,
                                 "the same limit, filters and snapshot must return the same first page")
                self.assertEqual(responses[-1]["page"]["returned"], 5)
                self.assertLess(len(json.dumps(responses[-1], separators=(",", ":"))), full_bytes,
                                "the five-row continuation must be smaller than the 200-row page")
        empty = service.execute(QueryRequest("pawns", {"scope": "map", "pawn_def": "NoSuchDef"}, limit=1)).as_dict()
        self.assertEqual((empty["page"]["total"], empty["page"]["returned"], empty["page"]["next_cursor"]), (0, 0, None))

    def test_cursor_rejects_corrupt_snapshot_filter_limit_parser_and_encoded_tokens(self):
        xml = b"<savegame><game><maps><li><things>" + b"".join(
            f"<thing Class='Pawn'><id>P{i}</id><def>Human</def></thing>".encode() for i in range(4)
        ) + b"</things></li></maps></game></savegame>"
        snapshot = _load(xml)
        service = QueryService(snapshot)
        first = service.execute(QueryRequest("pawns", {"scope": "map"}, limit=1)).as_dict()
        token = first["page"]["next_cursor"]
        self.assertIsNotNone(token)
        fully_encoded = "".join(f"%{ord(character):02X}" for character in token)
        self.assertNotEqual(fully_encoded, token)
        bad_tokens = ("not-a-cursor", fully_encoded)
        for bad in bad_tokens:
            with self.assertRaises(InspectorError) as caught:
                service.execute(QueryRequest("pawns", {"scope": "map"}, limit=1, cursor=bad))
            self.assertEqual(caught.exception.code, "INVALID_CURSOR")
        for request in (
            QueryRequest("pawns", {"scope": "map", "pawn_def": "Human"}, limit=1, cursor=token),
            QueryRequest("pawns", {"scope": "map"}, limit=2, cursor=token),
            QueryRequest("mods", {}, limit=1, cursor=token),
        ):
            with self.assertRaises(InspectorError) as caught:
                service.execute(request)
            self.assertEqual(caught.exception.code, "INVALID_CURSOR")

        changed = replace(snapshot, snapshot_id="sha256:" + "a" * 64,
                          interpretation_id="interpretation:" + "b" * 64)
        with self.assertRaises(InspectorError) as snapshot_error:
            QueryService(changed).execute(QueryRequest("pawns", {"scope": "map"}, limit=1, cursor=token))
        self.assertEqual(snapshot_error.exception.code, "INVALID_CURSOR")

        changed_parser = replace(snapshot, parser_version="9.9.9",
                                 interpretation_id="interpretation:" + hashlib.sha256(
                                     f"{snapshot.snapshot_id}\n9.9.9\n{snapshot.profile_version}".encode()).hexdigest())
        with self.assertRaises(InspectorError) as parser_error:
            QueryService(changed_parser).execute(QueryRequest("pawns", {"scope": "map"}, limit=1, cursor=token))
        self.assertEqual(parser_error.exception.code, "INVALID_CURSOR")

    def test_nested_deep_and_reference_holder_forms_keep_edges_without_recursion(self):
        xml = b"""<savegame><game><world><worldObjects><worldObjects><li><ID>9</ID><def>Ship</def><pawns><innerList><li>Thing_DeadPawn</li></innerList></pawns></li></worldObjects></worldObjects></world>
        <maps><li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>MapPawn</id><def>Human</def><equipment><equipment><innerList><li><id>Weapon</id><def>Rifle</def></li></innerList></equipment></equipment>
        <apparel><wornApparel><innerList><li><id>Clothing</id><def>Shirt</def></li></innerList></wornApparel></apparel><inventory><innerContainer><innerList>
        <li><id>Crate</id><def>Container</def><innerContainer><contentsLookMode>Deep</contentsLookMode><innerList>
        <li><id>NestedItem</id><def>NestedThing</def><stackCount>3</stackCount></li>
        <li Class='Pawn'><id>NestedPawn</id><def>Human</def></li><li>Thing_WorldPawn</li>
        </innerList></innerContainer></li></innerList></innerContainer></inventory></thing>
        <thing Class='Corpse'><id>Corpse1</id><def>Corpse_Human</def><innerContainer><contentsLookMode>Reference</contentsLookMode><innerList><li>Thing_DeadPawn</li></innerList></innerContainer></thing>
        <thing Class='ThingWithComps'><id>MapContainer</id><def>Crate</def><innerContainer><innerList><li><id>MapHeldItem</id><def>Thing</def></li><li>Thing_WorldPawn</li></innerList></innerContainer></thing>
        </things></li></maps><transportShipManager><ships><li><shipThing><innerContainer><contentsLookMode>Deep</contentsLookMode><innerList><li Class='Pawn'><id>ShipPawn</id><def>Human</def></li></innerList></innerContainer></shipThing></li></ships></transportShipManager>
        <worldPawns><pawnsAlive><li Class='Pawn'><id>WorldPawn</id><def>Human</def></li></pawnsAlive>
        <pawnsDead><li Class='Pawn'><id>DeadPawn</id><def>Human</def></li></pawnsDead></worldPawns>
        </game></savegame>"""
        snapshot = _load(xml)
        service = QueryService(snapshot)
        held = service.execute(QueryRequest("pawns", {"scope": "held"})).as_dict()["data"]["entries"]
        ids = {row["saved_id"] for row in held}
        self.assertEqual(ids, {"NestedPawn", "WorldPawn", "DeadPawn", "ShipPawn"})
        self.assertEqual(len({row["record_id"] for row in held}), len(held))
        self.assertGreaterEqual(sum(len(row["memberships"]) for row in held), 3)
        self.assertLessEqual(len(held), 4, "references and nested holders must not recursively duplicate definitions")
        dead = next(row for row in held if row["saved_id"] == "DeadPawn")
        dead_membership_paths = [member["source_path"] for member in dead["memberships"]]
        self.assertTrue(any("worldObjects" in path and "/pawns[1]/innerList[1]" in path for path in dead_membership_paths),
                        "world-object pawn references must be recorded as memberships")
        self.assertTrue(any("/thing[2]/innerContainer[1]/innerList[1]" in path for path in dead_membership_paths),
                        "corpse Reference entries must resolve to the dead-pool definition without duplicating it")
        self.assertEqual(sum(record.type_name == "pawn" and record.saved_id == "DeadPawn" for record in snapshot.records), 1)
        pawn = next(record for record in snapshot.records if record.type_name == "pawn" and record.saved_id == "MapPawn")
        equipment = service.execute(QueryRequest("equipment", {"record_id": pawn.record_id, "holder": "equipment"})).as_dict()["data"]
        apparel = service.execute(QueryRequest("equipment", {"record_id": pawn.record_id, "holder": "apparel"})).as_dict()["data"]
        inventory = service.execute(QueryRequest("inventory", {"record_id": pawn.record_id})).as_dict()["data"]
        self.assertEqual([entry["item"]["saved_id"] for entry in equipment["entries"]], ["Weapon"])
        self.assertEqual([entry["item"]["saved_id"] for entry in apparel["entries"]], ["Clothing"])
        self.assertEqual([entry["item"]["saved_id"] for entry in inventory["entries"] if entry["item"] is not None], ["Crate"],
                         "one inventory result expands only the selected root holder; callers select an owned nested holder separately")
        self.assertEqual(inventory["entry_count"], 1, "nested definitions and text references belong to their own holder selection")
        map_container = next(record for record in snapshot.records if record.type_name == "item" and record.saved_id == "MapHeldItem")
        self.assertTrue(any(member.get("holder_id") for member in map_container.memberships),
                        "recognized map-thing innerContainer contents must become holder memberships")
        nested_item = next(record for record in snapshot.records if record.type_name == "item" and record.saved_id == "NestedItem")
        nested_membership = next(item for item in nested_item.memberships if item.get("holder_id"))
        selected = service.execute(QueryRequest("inventory", {"record_id": pawn.record_id,
                                                                  "holder_id": nested_membership["holder_id"]})).as_dict()["data"]
        selected_items = [entry["item"]["saved_id"] for entry in selected["entries"] if entry["item"] is not None]
        selected_references = [entry for entry in selected["entries"] if entry["reference"] is not None]
        self.assertEqual(selected_items, ["NestedItem"])
        self.assertEqual(len(selected_references), 2,
                         "nested full-pawn and text-only pawn references are represented as edges, not recursively embedded items")
        expected_pawn_ids = {record.record_id for record in snapshot.records
                             if record.type_name == "pawn" and record.saved_id in {"NestedPawn", "WorldPawn"}}
        self.assertEqual({entry["pawn_record_id"] for entry in selected_references}, expected_pawn_ids)
        self.assertTrue(all(entry["reference"]["status"] == "resolved" for entry in selected_references))
        inferred_holder_diagnostics = [diagnostic for diagnostic in snapshot.diagnostics
                                       if diagnostic.source_path and diagnostic.source_path.endswith("/thing[3]/innerContainer[1]")]
        self.assertTrue(inferred_holder_diagnostics,
                        "omitted look mode must be inferred as deep only for full records and the inference retained as a diagnostic")
        resolved_world = service.indexes.resolve("Thing_WorldPawn", "thing")
        self.assertEqual(resolved_world.status, "resolved")
        self.assertEqual(len(resolved_world.target_record_ids), 1,
                         "corpse, world-object, and nested-holder references must point to one definition")
        self.assertTrue(all("/" in row["source_path"] for row in held))


if __name__ == "__main__":
    unittest.main()
