import hashlib

from rimworld_save_inspector.history.source import extract_history_snapshot
from rimworld_save_inspector.history.models import evidence_id_for_node, source_ref_for


SOURCE = b'''<?xml version="1.0" encoding="UTF-8"?>
<savegame version="1.6.4535" xmlns:x="urn:test">
  <game>
    <world><info><seedString>reef</seedString><persistentRandomValue>91</persistentRandomValue><initialMapSize>(250,1,250)</initialMapSize></info></world>
    <tickManager><startingYear>5500</startingYear><gameStartAbsTick>52500</gameStartAbsTick><ticksGame>730</ticksGame></tickManager>
    <info><startingTile>42,0</startingTile></info>
    <maps><li id="Map_1" name="Home"><things><thing Class="Pawn" customAttr="keep">
      <def>Human</def><kindDef>Colonist</kindDef><id>Human42</id><name><nick>Nova</nick></name><faction>Faction_27</faction>
      <inventory/><x:note>before<b code="x">inside</b>after<x:i/>tail</x:note>
    </thing></things></li></maps>
  </game>
</savegame>'''


def test_extracts_canonical_metadata_claims_clocks_and_lossless_mixed_content(tmp_path):
    path = tmp_path / "source.rws"
    path.write_bytes(SOURCE)

    result = extract_history_snapshot(path).as_dict()

    assert list(result) == [
        "snapshot_id", "interpretation_id", "metadata", "pawns", "events",
        "context", "evidence", "coverage", "diagnostics",
    ]
    assert result["snapshot_id"] == hashlib.sha256(SOURCE).hexdigest()
    assert len(result["interpretation_id"]) == 64
    assert result["metadata"]["profile"]["game_version"] == "1.6.4535"
    assert result["metadata"]["profile"]["support_status"] == "verified"
    campaign = result["metadata"]["campaign_key"]
    assert campaign["fields"]["seedString"]["value"] == "reef"
    assert campaign["fields"]["initialMapSize"]["value"] == "(250,1,250)"
    assert campaign["identity"]["status"] == "compatible"
    clocks = result["metadata"]["clocks"]
    assert clocks["ticksGame"]["raw_value"] == "730"
    assert clocks["gameStartAbsTick"]["raw_value"] == "52500"
    assert clocks["startingYear"]["unit"] == "year"
    assert clocks["ticksAbs"]["value"] == 53230
    assert clocks["ticksAbs"]["normalization_rule"] == "ticksGame_plus_gameStartAbsTick"

    assert len(result["pawns"]) == 1
    pawn = result["pawns"][0]
    assert pawn["pawn_id"] == "Human42"
    assert pawn["scope"] == "map"
    assert pawn["holder_chain"][0]["saved_id"] == "Map_1"
    assert [(alias["field"], alias["value"]) for alias in pawn["aliases"]] == [("name.nick", "Nova")]
    assert set(pawn["domains"]) == {
        "identity", "status_and_interaction", "location", "age", "backstory", "traits", "skills",
        "health", "genes", "equipment", "apparel", "inventory", "relationships", "abilities",
        "mechanitor", "ideology", "royalty", "roles", "ownership", "work", "needs", "memories",
        "records", "events", "unknown_fields",
    }
    assert pawn["domains"]["traits"]["status"] == "absent"
    assert pawn["domains"]["inventory"]["status"] == "complete"
    inventory = next(claim for claim in pawn["domains"]["inventory"]["claims"]
                     if claim["field"] == "inventory[1]")
    assert inventory["status"] == "observed"
    assert inventory["value"] == []
    faction = next(claim for claim in pawn["domains"]["identity"]["claims"]
                   if claim["field"] == "identity.faction[1]")
    assert faction["status"] == "observed"
    guest = next(claim for claim in pawn["domains"]["status_and_interaction"]["claims"]
                 if claim["field"] == "status_and_interaction.guest")
    assert guest["status"] == "absent"
    assert guest["value"] is None
    assert guest["source_ref"]
    assert guest["evidence_id"]

    note_path = "/savegame[1]/game[1]/maps[1]/li[1]/things[1]/thing[1]/{urn:test}note[1]"
    note = next(row for row in result["evidence"] if row["path"] == note_path)
    assert note["node"] == {
        "tag": "{urn:test}note",
        "attributes": [],
        "text": "before",
        "children": [
            {"node": {"tag": "b", "attributes": [["code", "x"]], "text": "inside", "children": []}, "tail": "after"},
            {"node": {"tag": "{urn:test}i", "attributes": [], "text": None, "children": []}, "tail": "tail"},
        ],
    }
    assert note["evidence_id"] == evidence_id_for_node(note["node"])
    assert note["source_ref"] == source_ref_for(result["snapshot_id"], result["interpretation_id"], note_path)
    unknown = next(row for row in result["coverage"]["unknown_fields"] if row["path"] == note_path)
    assert unknown["pawn_id"] == "Human42"
    assert unknown["record_id"] == pawn["record_id"]
    assert unknown["source_ref"] == note["source_ref"]
    assert unknown["serialized_bytes"] == len(__import__("json").dumps(note["node"], sort_keys=True,
                                                                           separators=(",", ":"), ensure_ascii=False).encode())
    unknown_claim = next(claim for claim in pawn["domains"]["unknown_fields"]["claims"]
                         if claim["raw_field"] == "/{urn:test}note[1]")
    assert unknown_claim["structural_kind"] == "unknown_pawn_field"
    assert unknown_claim["serialized_bytes"] == unknown["serialized_bytes"]
    pawn_path = "/savegame[1]/game[1]/maps[1]/li[1]/things[1]/thing[1]"
    pawn_node = next(row for row in result["evidence"] if row["path"] == pawn_path)
    assert pawn_node["node"]["attributes"] == [["Class", "Pawn"], ["customAttr", "keep"]]


def test_same_expanded_name_sibling_paths_remain_one_based_and_ordered(tmp_path):
    source = SOURCE.replace(
        b"<inventory/>",
        b"<inventory/><x:repeat>first</x:repeat><x:repeat>second</x:repeat>",
    )
    path = tmp_path / "repeated-expanded-siblings.xml"
    path.write_bytes(source)

    result = extract_history_snapshot(path).as_dict()
    pawn_path = result["pawns"][0]["source_path"]
    repeated = [row for row in result["evidence"] if row["path"].startswith(pawn_path + "/{urn:test}repeat[")]
    assert [(row["path"], row["node"]["text"]) for row in repeated] == [
        (pawn_path + "/{urn:test}repeat[1]", "first"),
        (pawn_path + "/{urn:test}repeat[2]", "second"),
    ]


def test_missing_campaign_field_is_review_needed_not_a_partial_hash(tmp_path):
    source = SOURCE.replace(b"<startingTile>42,0</startingTile>", b"")
    path = tmp_path / "missing-key.rws"
    path.write_bytes(source)

    result = extract_history_snapshot(path).as_dict()

    campaign = result["metadata"]["campaign_key"]
    assert campaign["identity"]["status"] == "review-needed"
    assert campaign["identity"]["value"] is None
    assert campaign["fields"]["startingTile"]["status"] == "absent"
    assert campaign["fields"]["startingTile"]["value"] is None


def test_extracted_snapshot_uses_readonly_views_and_detached_serialization(tmp_path):
    import pytest

    path = tmp_path / "immutable-source.xml"
    path.write_bytes(SOURCE)
    snapshot = extract_history_snapshot(path)

    with pytest.raises(TypeError):
        snapshot.pawns[0]["scope"] = "changed"
    with pytest.raises(TypeError):
        snapshot.pawns[0]["domains"]["identity"]["claims"][0]["value"] = "changed"
    serialized = snapshot.as_dict()
    serialized["pawns"][0]["scope"] = "changed"
    assert snapshot.as_dict()["pawns"][0]["scope"] == "map"


def test_invalid_immutable_campaign_component_cannot_form_strong_key(tmp_path):
    source = SOURCE.replace(b"<gameStartAbsTick>52500</gameStartAbsTick>", b"<gameStartAbsTick>not-a-tick</gameStartAbsTick>")
    path = tmp_path / "invalid-campaign.xml"
    path.write_bytes(source)
    result = extract_history_snapshot(path).as_dict()
    campaign = result["metadata"]["campaign_key"]
    assert campaign["fields"]["gameStartAbsTick"]["status"] == "invalid"
    assert campaign["identity"] == {"status": "review-needed", "value": None}


def test_profile_interpretation_is_stable_and_profile_specific():
    from rimworld_save_inspector.history.source import (
        INTERPRETATION_ID_FOR_VERIFIED_1_6,
        VERIFIED_1_6_PROFILE,
        interpretation_id_for_profile,
    )

    assert INTERPRETATION_ID_FOR_VERIFIED_1_6 == interpretation_id_for_profile(VERIFIED_1_6_PROFILE)
    assert interpretation_id_for_profile("rimworld-1.7-best-effort/v1") != INTERPRETATION_ID_FOR_VERIFIED_1_6
    assert len(INTERPRETATION_ID_FOR_VERIFIED_1_6) == 64


def test_rejects_symlinks_dtd_entities_and_malformed_xml(tmp_path):
    import pytest
    from rimworld_save_inspector.errors import InspectorError

    regular = tmp_path / "source.xml"
    regular.write_bytes(SOURCE)
    link = tmp_path / "link.xml"
    link.symlink_to(regular)
    with pytest.raises(InspectorError) as symlink_error:
        extract_history_snapshot(link)
    assert symlink_error.value.code == "UNSUPPORTED_SOURCE"

    for filename, payload, expected in (
        ("dtd.xml", b'<!DOCTYPE savegame [<!ENTITY x "y">]><savegame>&x;</savegame>', "UNSAFE_XML"),
        ("broken.xml", b"<savegame><game></savegame>", "MALFORMED_XML"),
    ):
        invalid = tmp_path / filename
        invalid.write_bytes(payload)
        with pytest.raises(InspectorError) as error:
            extract_history_snapshot(invalid)
        assert error.value.code == expected


def test_repeated_expanded_sibling_tags_keep_one_based_source_paths(tmp_path):
    source = SOURCE.replace(b"<inventory/>", b"<inventory><li>Med</li><li>Med</li></inventory>")
    path = tmp_path / "repeated.xml"
    path.write_bytes(source)
    result = extract_history_snapshot(path).as_dict()
    pawn_path = "/savegame[1]/game[1]/maps[1]/li[1]/things[1]/thing[1]"
    pawn = result["pawns"][0]
    inventory = pawn["domains"]["inventory"]["claims"]
    same_siblings = [claim for claim in inventory if claim["value"] == "Med"]
    assert [claim["raw_field"] for claim in same_siblings] == ["/inventory[1]/li[1]", "/inventory[1]/li[2]"]
    assert same_siblings[0]["source_ref"] == same_siblings[1]["source_ref"]
    assert same_siblings[0]["evidence_id"] == same_siblings[1]["evidence_id"]
    anchor = next(row for row in result["evidence"] if row["source_ref"] == same_siblings[0]["source_ref"])
    assert anchor["path"] == pawn_path
    inventory_node = next(child["node"] for child in anchor["node"]["children"]
                          if child["node"]["tag"] == "inventory")
    assert [child["node"]["text"] for child in inventory_node["children"]] == ["Med", "Med"]
    assert [child["node"]["tag"] for child in inventory_node["children"]] == ["li", "li"]


def test_recorder_checkpoint_is_typed_context_with_exact_field_provenance(tmp_path):
    source = SOURCE.replace(
        b"<game>",
        b'<game><components><li Class="Recorder.Mod.RecorderSessionComponent">'
        b"<lifeRecorderCampaignId>opaque-campaign</lifeRecorderCampaignId>"
        b"<lifeRecorderCheckpointId>checkpoint-4</lifeRecorderCheckpointId>"
        b"<lifeRecorderCheckpointSession>session-8</lifeRecorderCheckpointSession>"
        b"</li></components>",
    )
    path = tmp_path / "recorder.xml"
    path.write_bytes(source)
    result = extract_history_snapshot(path).as_dict()
    anchors = result["context"]["related_entities"]
    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor["kind"] == "recorder_checkpoint"
    claims = {claim["field"]: claim for claim in anchor["claims"]}
    assert claims["campaign_token"]["value"] == "opaque-campaign"
    assert claims["checkpoint_id"]["value"] == "checkpoint-4"
    assert claims["producer_session_id"]["value"] == "session-8"
    assert claims["source_save_sha256"]["status"] == "absent"
    assert claims["campaign_token"]["source_ref"]
    assert claims["campaign_token"]["evidence_id"]


def test_shared_map_context_and_absent_domains_have_accurate_coverage(tmp_path):
    path = tmp_path / "source.xml"
    path.write_bytes(SOURCE)
    result = extract_history_snapshot(path).as_dict()
    coverage = result["coverage"]["domains"]
    for name in ("events", "factions", "mods", "research", "scenario", "art"):
        assert coverage[name] == {"status": "absent", "observed_count": 0, "unknown_count": 0}
    assert coverage["maps"] == {"status": "complete", "observed_count": 1, "unknown_count": 0}
    assert len(result["context"]["maps"]) == 1
    shared_map = result["context"]["maps"][0]
    assert shared_map["id"].endswith("Map_1")
    assert {claim["value"] for claim in shared_map["claims"]} >= {"Map_1", "Home"}
    evidence_pairs = {(row["evidence_id"], row["source_ref"]) for row in result["evidence"]}
    assert all((claim["evidence_id"], claim["source_ref"]) in evidence_pairs
               for claim in shared_map["claims"])
    assert result["events"] == []


def test_materializes_classless_world_pool_pawns_and_inventories_custom_candidates(tmp_path):
    from pathlib import Path

    fixture = Path(__file__).parents[1] / "history_oracle" / "fixtures" / "all-holders.xml"
    path = tmp_path / "holders.xml"
    path.write_bytes(fixture.read_bytes())
    result = extract_history_snapshot(path).as_dict()
    by_id = {pawn["pawn_id"]: pawn for pawn in result["pawns"]}
    assert {"Human910001", "Human910002", "Human910003"} <= set(by_id)
    assert by_id["Human910001"]["scope"] == "world_alive"
    assert by_id["Human910002"]["scope"] == "world_dead"
    assert by_id["Human910003"]["scope"] == "world_mothballed"
    for pawn_id, pool_name in (("Human910001", "pawnsAlive"), ("Human910002", "pawnsDead"),
                               ("Human910003", "pawnsMothballed")):
        pawn = by_id[pawn_id]
        pool = pawn["holder_chain"][-1]
        assert pool["kind"].startswith("world_")
        assert pool["source_path"].endswith("/{}[1]".format(pool_name))
        assert pool["saved_id"] is None and pool["identity_status"] == "unsupported"
        assert {"evidence_id": pool["evidence_id"], "source_ref": pool["source_ref"]} in pawn["evidence_refs"]
    assert "Human910099" not in by_id
    assert any(row["structural_kind"] == "unverified_pawn_like_class" for row in result["coverage"]["unknown_fields"])
    assert by_id["Human910011"]["scope"] == "cryptosleep"
    assert by_id["Human910012"]["scope"] == "carry"
    assert by_id["Human910014"]["scope"] == "inventory"
    assert by_id["Human910015"]["scope"] == "corpse"
    assert by_id["Human910016"]["scope"] == "growth_vat"
    assert [holder["saved_id"] for holder in by_id["Human910011"]["holder_chain"]] == ["Map_Synthetic", "Casket_A"]
    evidence_by_ref = {row["source_ref"]: row for row in result["evidence"]}
    map_holder = by_id["Human910010"]["holder_chain"][0]
    map_evidence = evidence_by_ref[map_holder["source_ref"]]
    assert map_holder["identity_field"] == "/@id"
    assert map_holder["identity_status"] == "observed"
    assert map_evidence["evidence_kind"] == "xml_start_tag"
    assert map_evidence["path"] == map_holder["source_path"]
    assert map_evidence["node"]["attributes"] == [["id", "Map_Synthetic"]]
    assert map_evidence["node"]["children"] == []


def test_holder_without_saved_id_uses_pawn_occurrence_evidence_not_partial_parent(tmp_path):
    from pathlib import Path

    fixture = Path(__file__).parents[1] / "history_oracle" / "fixtures" / "all-holders.xml"
    path = tmp_path / "holder-without-id.xml"
    path.write_bytes(fixture.read_bytes().replace(b' id="Casket_A"', b""))
    result = extract_history_snapshot(path).as_dict()
    pawn = next(row for row in result["pawns"] if row["pawn_id"] == "Human910011")
    casket = pawn["holder_chain"][-1]
    assert casket["saved_id"] is None
    assert casket["identity_status"] == "unsupported"
    assert casket["identity_field"] is None
    assert {"evidence_id": casket["evidence_id"], "source_ref": casket["source_ref"]} in pawn["evidence_refs"]
    evidence = next(row for row in result["evidence"] if row["source_ref"] == casket["source_ref"])
    assert evidence["path"] == pawn["source_path"]
    assert evidence.get("evidence_kind") is None


def test_holder_child_id_is_retained_as_its_exact_xml_node(tmp_path):
    from pathlib import Path

    fixture = Path(__file__).parents[1] / "history_oracle" / "fixtures" / "all-holders.xml"
    source = fixture.read_bytes().replace(
        b'<li id="Map_Synthetic"><things>',
        b'<li><uniqueID>Map_Synthetic</uniqueID><things>',
    )
    path = tmp_path / "holder-child-id.xml"
    path.write_bytes(source)
    result = extract_history_snapshot(path).as_dict()
    pawn = next(row for row in result["pawns"] if row["pawn_id"] == "Human910010")
    map_holder = pawn["holder_chain"][0]
    evidence = next(row for row in result["evidence"] if row["source_ref"] == map_holder["source_ref"])
    assert map_holder["saved_id"] == "Map_Synthetic"
    assert map_holder["identity_status"] == "observed"
    assert map_holder["identity_field"] == "/uniqueID[1]"
    assert evidence["path"] == map_holder["source_path"] + "/uniqueID[1]"
    assert evidence["node"]["tag"] == "uniqueID"
    assert evidence["node"]["text"] == "Map_Synthetic"
    assert "evidence_kind" not in evidence


def test_nested_pawn_class_li_is_not_misclassified_as_map_holder(tmp_path):
    source = b'''<savegame version="1.6.4535"><game><maps><li id="Map_A"><things>
      <thing Class="Building_CryptosleepCasket" id="Casket_A"><innerContainer><innerList>
        <li Class="Pawn" id="Carrier_A"><def>Human</def><id>HumanCarrier</id><kindDef>Colonist</kindDef><faction>Faction_27</faction>
          <innerContainer><innerList><li Class="Pawn"><def>Human</def><id>HumanNested</id><kindDef>Colonist</kindDef><faction>Faction_27</faction></li></innerList></innerContainer>
        </li>
      </innerList></innerContainer></thing>
    </things></li></maps></game></savegame>'''
    path = tmp_path / "nested-holder.xml"
    path.write_bytes(source)

    result = extract_history_snapshot(path).as_dict()
    by_id = {pawn["pawn_id"]: pawn for pawn in result["pawns"]}

    assert [holder["kind"] for holder in by_id["HumanNested"]["holder_chain"]] == [
        "map", "cryptosleep_casket", "pawn_holder",
    ]
    assert [holder["saved_id"] for holder in by_id["HumanNested"]["holder_chain"]] == [
        "Map_A", "Casket_A", "Carrier_A",
    ]


def test_cancellation_is_checked_during_postparse_normalization(tmp_path, monkeypatch):
    import threading
    import pytest
    import rimworld_save_inspector.history.source as source_module
    from rimworld_save_inspector.errors import InspectorError

    path = tmp_path / "cancel-during-normalization.xml"
    path.write_bytes(SOURCE)
    cancel = threading.Event()
    original = source_module._metadata_tree

    def cancel_after_xml_parse(root, captures):
        tree = original(root, captures)
        cancel.set()
        return tree

    monkeypatch.setattr(source_module, "_metadata_tree", cancel_after_xml_parse)
    with pytest.raises(InspectorError) as cancelled:
        extract_history_snapshot(path, cancel=cancel)
    assert cancelled.value.code == "CANCELLED"


def test_deadline_is_checked_during_postparse_normalization(tmp_path, monkeypatch):
    import pytest
    import rimworld_save_inspector.history.source as source_module
    from rimworld_save_inspector.errors import InspectorError

    path = tmp_path / "deadline-during-normalization.xml"
    path.write_bytes(SOURCE)
    original_tree = source_module._metadata_tree
    original_monotonic = source_module.time.monotonic
    expired = False

    def monotonic():
        return original_monotonic() + (121 if expired else 0)

    def expire_after_xml_parse(root, captures):
        nonlocal expired
        tree = original_tree(root, captures)
        expired = True
        return tree

    monkeypatch.setattr(source_module.time, "monotonic", monotonic)
    monkeypatch.setattr(source_module, "_metadata_tree", expire_after_xml_parse)
    with pytest.raises(InspectorError) as elapsed:
        extract_history_snapshot(path)
    assert elapsed.value.code == "LIMIT_EXCEEDED"
    assert elapsed.value.details["limit"] == "deadline_seconds"


def test_birth_time_claim_uses_record_relative_saved_path(tmp_path):
    source = SOURCE.replace(
        b"<inventory/>",
        b"<ageTracker><birthAbsTicks>52490</birthAbsTicks><ageBiologicalTicks>900</ageBiologicalTicks></ageTracker><inventory/>",
    )
    path = tmp_path / "birth-time.xml"
    path.write_bytes(source)
    result = extract_history_snapshot(path).as_dict()
    age = result["pawns"][0]["domains"]["age"]["claims"]
    birth = next(claim for claim in age if claim["field"].endswith("birthAbsTicks[1]"))
    assert birth["time_observation"]["raw_field"] == "/ageTracker[1]/birthAbsTicks[1]"
    assert birth["time_observation"]["clock"] == "absolute_tick"
    assert birth["time_observation"]["normalized_game_tick"] == -10
    duration = next(claim for claim in age if claim["field"].endswith("ageBiologicalTicks[1]"))
    assert duration["time_observation"]["clock"] == "biological_duration"
    assert duration["time_observation"]["normalized_game_tick"] is None


def test_nested_trait_and_mood_memory_payloads_enter_their_own_domains(tmp_path):
    source = SOURCE.replace(
        b"<inventory/>",
        b"<story><traits><li><def>Kind</def></li></traits></story>"
        b"<needs><mood><thoughts><memories><li><def>MetSomeone</def></li></memories></thoughts></mood></needs>"
        b"<inventory/>",
    )
    path = tmp_path / "nested-domains.xml"
    path.write_bytes(source)

    pawn = extract_history_snapshot(path).as_dict()["pawns"][0]

    assert pawn["domains"]["traits"]["status"] == "complete"
    assert any(claim["value"] == "Kind" for claim in pawn["domains"]["traits"]["claims"])
    assert pawn["domains"]["memories"]["status"] == "complete"
    assert any(claim["value"] == "MetSomeone" for claim in pawn["domains"]["memories"]["claims"])
    assert not any(claim["value"] == "Kind" for claim in pawn["domains"]["backstory"]["claims"])
    assert not any(claim["value"] == "MetSomeone" for claim in pawn["domains"]["needs"]["claims"])


def test_social_direct_relations_link_only_to_materialized_pawn_ids(tmp_path):
    source = SOURCE.replace(
        b"<inventory/>",
        b"<social><directRelations><li><otherPawn>Human42</otherPawn><def>Friend</def><startTicks>7</startTicks></li>"
        b"<li><otherPawn>MissingPawn</otherPawn><def>Rival</def></li></directRelations></social><inventory/>",
    ).replace(b"</things>", b"<li Class=\"Verse.Pawn\"><id>Human42</id><def>Human</def><kindDef>Human</kindDef></li></things>")
    path = tmp_path / "relationships.xml"
    path.write_bytes(source)
    result = extract_history_snapshot(path).as_dict()
    pawn = next(row for row in result["pawns"] if row["pawn_id"] == "Human42")
    links = pawn["domains"]["relationships"]["links"]
    assert any(link["target_id"] == "Human42" and link["status"] == "observed" for link in links)
    assert any(link["target_id"] == "MissingPawn" and link["status"] == "unsupported" for link in links)
    rel_claims = pawn["domains"]["relationships"]["claims"]
    relation_clock = next(claim["time_observation"] for claim in rel_claims if claim["raw_field"].endswith("/startTicks[1]"))
    assert relation_clock["raw_field"] == "/social[1]/directRelations[1]/li[1]/startTicks[1]"
    assert relation_clock["clock"] == "unknown"
    assert relation_clock["normalized_game_tick"] is None


def test_ownership_root_is_a_supported_domain(tmp_path):
    source = SOURCE.replace(
        b"<inventory/>",
        b"<ownership><ownedBed>Bed_A</ownedBed><assignedGrave>Grave_B</assignedGrave></ownership><inventory/>",
    )
    path = tmp_path / "ownership.xml"
    path.write_bytes(source)
    pawn = extract_history_snapshot(path).as_dict()["pawns"][0]
    claims = pawn["domains"]["ownership"]["claims"]
    assert pawn["domains"]["ownership"]["status"] == "complete"
    assert {claim["value"] for claim in claims} >= {"Bed_A", "Grave_B"}


def test_item_title_is_not_misclassified_as_pawn_royalty(tmp_path):
    source = SOURCE.replace(
        b"<inventory/>",
        b"<apparel><li><title>Decorative item title</title></li></apparel>"
        b"<royalty><titles><li><title>Countess</title></li></titles></royalty><inventory/>",
    )
    path = tmp_path / "contextual-title.xml"
    path.write_bytes(source)
    domains = extract_history_snapshot(path).as_dict()["pawns"][0]["domains"]
    assert any(claim["value"] == "Decorative item title" for claim in domains["apparel"]["claims"])
    assert not any(claim["value"] == "Decorative item title" for claim in domains["royalty"]["claims"])
    assert any(claim["value"] == "Countess" for claim in domains["royalty"]["claims"])
    assert not any(claim["status"] == "absent" and claim["field"].endswith("royalty.title")
                   for claim in domains["royalty"]["claims"])


def test_unverified_direct_title_and_role_fields_remain_raw_unknowns(tmp_path):
    source = SOURCE.replace(
        b"<inventory/>",
        b"<title>Unverified direct title</title><role>Unverified office</role>"
        b"<story><title>Story label</title></story><inventory/>",
    )
    path = tmp_path / "unverified-title-role.xml"
    path.write_bytes(source)
    pawn = extract_history_snapshot(path).as_dict()["pawns"][0]
    assert pawn["domains"]["roles"]["status"] == "unsupported"
    assert any(row["field"].endswith("title[1]") for row in pawn["domains"]["unknown_fields"]["claims"])
    assert any(row["field"].endswith("role[1]") for row in pawn["domains"]["unknown_fields"]["claims"])
    assert any(row["value"] == "Story label" for row in pawn["domains"]["backstory"]["claims"])
    assert not any(row["value"] == "Unverified direct title" for row in pawn["domains"]["royalty"]["claims"])


def test_limits_and_cancellation_fail_instead_of_returning_partial_snapshot(tmp_path):
    import threading
    import pytest
    from rimworld_save_inspector.errors import InspectorError
    from rimworld_save_inspector.models import ParseLimits

    path = tmp_path / "limited.xml"
    two_pawns = SOURCE.replace(b"</things>", b'<thing Class="Pawn"><id>Human43</id></thing></things>')
    path.write_bytes(two_pawns)
    with pytest.raises(InspectorError) as too_many:
        extract_history_snapshot(path, limits=ParseLimits(normalized_records=1))
    assert too_many.value.code == "LIMIT_EXCEEDED"
    event = threading.Event()
    event.set()
    with pytest.raises(InspectorError) as cancelled:
        extract_history_snapshot(path, cancel=event)
    assert cancelled.value.code == "CANCELLED"


def test_tale_ids_exact_participants_and_occurrence_clocks(tmp_path):
    from pathlib import Path

    fixture = Path(__file__).parents[1] / "history_oracle" / "fixtures" / "alpha-early.xml"
    path = tmp_path / "early.xml"
    path.write_bytes(fixture.read_bytes())
    result = extract_history_snapshot(path).as_dict()
    assert result["metadata"]["campaign_key"]["identity"]["status"] == "compatible"
    assert result["metadata"]["clocks"]["ticksAbs"]["value"] == 53230
    event = result["events"][0]
    assert event["saved_event_id"] == "tale:Tale:Tale_1"
    assert event["raw_saved_id"] == "Tale_1"
    assert event["participants"][0]["pawn_id"] == "pawn-alpha-01"
    assert event["occurrence"]["raw_field"] == "/occurred[1]"
    assert event["occurrence"]["normalized_game_tick"] == -10
    assert event["occurrence"]["status"] == "pre_arrival"


def test_archive_records_keep_class_namespaced_ids_and_unknown_clocks(tmp_path):
    xml = b'''<savegame version="1.6.4535"><game><maps><map id="M"><things><li Class="Pawn"><id>Human42</id><def>Human</def></li></things></map></maps><history><archive><archivables>
      <li Class="Verse.Letter_Arrival"><ID>17</ID><def>Letter_Arrived</def><arrivalTick>12345</arrivalTick>
        <lookTargets><targets><li>Human42</li><li>Human42xy</li></targets></lookTargets><text>Pawn called Nova</text></li>
    </archivables></archive></history><letterStack><letters><li>Letter_17</li></letters></letterStack></game></savegame>'''
    path = tmp_path / "archive.xml"
    path.write_bytes(xml)
    result = extract_history_snapshot(path).as_dict()
    archive_events = [event for event in result["events"] if event["kind"] == "archive"]
    assert len(archive_events) == 1
    event = archive_events[0]
    assert event["saved_event_id"] == "archive:Verse.Letter_Arrival:17"
    assert event["raw_saved_id"] == "17"
    assert event["occurrence"]["raw_value"] == "12345"
    assert event["occurrence"]["clock"] == "unknown"
    assert event["occurrence"]["normalized_game_tick"] is None
    assert [participant["pawn_id"] for participant in event["participants"]] == ["Human42"]
