"""Independent raw XML oracles for shared RimWorld 1.6 context extraction.

The fixture is hand-authored from the documented public save layout. The
expected raw values and XML evidence nodes below are literal expectations,
not values obtained from the production parser or an older parser.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from rimworld_save_inspector.history.source import (
    EXTRACTION_VERSION,
    INTERPRETATION_ID_FOR_VERIFIED_1_6,
    VERIFIED_1_6_PROFILE,
    extract_history_snapshot,
    interpretation_id_for_profile,
)
from rimworld_save_inspector.history.models import canonical_json_bytes, evidence_id_for_node, source_ref_for


PUBLIC_1_6_CONTEXT_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<savegame version="1.6.4535">
  <meta>
    <gameVersion>1.6.4535</gameVersion>
    <modIds><li>ludeon.rimworld</li><li>ludeon.rimworld.biotech</li></modIds>
    <modNames><li>Core</li><li>Biotech</li></modNames>
  </meta>
  <game>
    <world><info><seedString>public-context-seed</seedString><persistentRandomValue>17</persistentRandomValue><initialMapSize>(250,1,250)</initialMapSize></info>
      <factionManager><allFactions>
        <li Class="Faction"><def>PlayerColony</def><loadID>Faction_1</loadID><name>North Colony</name><leader>Human42</leader></li>
      </allFactions></factionManager>
    </world>
    <tickManager><startingYear>5500</startingYear><gameStartAbsTick>52500</gameStartAbsTick><ticksGame>20</ticksGame></tickManager>
    <info><startingTile>42,0</startingTile></info>
    <researchManager>
      <currentProj>Microelectronics</currentProj>
      <completedProjects><li>Electricity</li><li>Smithing</li></completedProjects>
      <progress><li><key>Microelectronics</key><value>0.25</value></li></progress>
    </researchManager>
    <scenario><name>Crashlanded</name><summary>Raw &amp; ordered</summary><parts><li Class="ScenPart_StartingThing_Defined"><def>MealSurvivalPack</def></li></parts></scenario>
    <maps><li Class="Map" id="Map_1">
      <uniqueID>Map_1</uniqueID><mapInfo><parent>WorldObject_42</parent><size>(250,1,250)</size><tile>42</tile></mapInfo>
      <things><thing Class="ThingWithComps" id="ArtTable_1"><def>ArtSmall</def>
        <comps><li Class="RimWorld.CompArt"><authorName>Nova</authorName><savedTaleReference>7</savedTaleReference></li></comps>
      </thing><thing Class="Pawn"><def>Human</def><kindDef>Colonist</kindDef><id>Human42</id><name><nick>Nova</nick></name><faction>Faction_1</faction></thing>
      <thing Class="Pawn"><def>Human</def><kindDef>Colonist</kindDef><id>Human99</id><name><nick>Nova</nick></name><faction>Faction_1</faction></thing></things>
    </li></maps>
    <taleManager><tales><li Class="Tale_Art" id="7"><def>ArtMade</def><occurred>52510</occurred><pawn>Human42</pawn></li></tales></taleManager>
  </game>
</savegame>'''


EXPECTED_RAW_NODES = {
    "mod_id": {"tag": "li", "attributes": [], "text": "ludeon.rimworld", "children": []},
    "mod_name": {"tag": "li", "attributes": [], "text": "Core", "children": []},
    "faction_id": {"tag": "loadID", "attributes": [], "text": "Faction_1", "children": []},
    "map_id": {"tag": "uniqueID", "attributes": [], "text": "Map_1", "children": []},
    "research_project": {"tag": "li", "attributes": [], "text": "Electricity", "children": []},
    "scenario_name": {"tag": "name", "attributes": [], "text": "Crashlanded", "children": []},
    "saved_tale_reference": {"tag": "savedTaleReference", "attributes": [], "text": "7", "children": []},
}


def _write(path, source: bytes = PUBLIC_1_6_CONTEXT_XML):
    path.write_bytes(source)
    return path


def _replace_region(source: bytes, opening: bytes, closing: bytes, replacement: bytes) -> bytes:
    start = source.index(opening)
    end = source.index(closing, start) + len(closing)
    return source[:start] + replacement + source[end:]


def _evidence_by_raw_node(result):
    return {json.dumps(row["node"], sort_keys=True, separators=(",", ":")): row
            for row in result["evidence"]}


def _entity_by_claim(entities, field):
    return next(entity for entity in entities
                if any(claim["field"] == field for claim in entity["claims"]))


def test_public_1_6_context_has_shared_entities_exact_raw_evidence_and_art_links(tmp_path):
    result = extract_history_snapshot(_write(tmp_path / "public-context-1.6.rws")).as_dict()
    coverage = result["coverage"]["domains"]

    assert EXTRACTION_VERSION == "7"
    assert result["interpretation_id"] == INTERPRETATION_ID_FOR_VERIFIED_1_6
    assert interpretation_id_for_profile(VERIFIED_1_6_PROFILE) == INTERPRETATION_ID_FOR_VERIFIED_1_6
    assert result["interpretation_id"] != interpretation_id_for_profile("rimworld-1.7-best-effort/v1")
    assert set(result["context"]) == {
        "maps", "factions", "mods", "research", "scenario", "art", "related_entities",
    }

    expected_counts = {"maps": 1, "factions": 1, "mods": 4, "research": 3, "scenario": 1, "art": 1}
    for domain, expected_count in expected_counts.items():
        assert coverage[domain] == {"status": "complete", "observed_count": expected_count, "unknown_count": 0}
        assert len(result["context"][domain]) == expected_count
        for entity in result["context"][domain]:
            assert set(entity) == {"id", "kind", "claims", "links", "evidence_refs"}
            assert entity["id"] and entity["kind"]
            assert entity["evidence_refs"]

    mods = result["context"]["mods"]
    assert [(entity["kind"], entity["claims"][0]["field"], entity["claims"][0]["value"]) for entity in mods] == [
        ("mod_id", "mods.mod_id", "ludeon.rimworld"),
        ("mod_id", "mods.mod_id", "ludeon.rimworld.biotech"),
        ("mod_name", "mods.mod_name", "Core"),
        ("mod_name", "mods.mod_name", "Biotech"),
    ]
    assert _entity_by_claim(result["context"]["factions"], "faction.loadID")["id"].endswith("Faction_1")
    assert _entity_by_claim(result["context"]["maps"], "map.uniqueID")["id"].endswith("Map_1")
    assert any(claim["field"] == "research.completedProjects" and claim["value"] == "Smithing"
               for entity in result["context"]["research"] for claim in entity["claims"])
    assert any(claim["field"] == "scenario.name" and claim["value"] == "Crashlanded"
               for entity in result["context"]["scenario"] for claim in entity["claims"])

    art = result["context"]["art"][0]
    assert art["kind"] == "art_reference"
    tale_links = [link for link in art["links"] if link["kind"] == "saved_tale_reference"]
    assert len(tale_links) == 1 and tale_links[0]["target_id"] == "7"
    assert tale_links[0]["status"] == "observed"
    pawn_links = [link for link in art["links"] if link["kind"] == "pawn_reference"]
    assert len(pawn_links) == 1 and pawn_links[0]["target_id"] == "Human42"
    assert pawn_links[0]["role"] == "participant_unknown"
    assert "actor" not in str(art).lower() and "motive" not in str(art).lower()

    evidence = _evidence_by_raw_node(result)
    for label, expected_node in EXPECTED_RAW_NODES.items():
        row = evidence[json.dumps(expected_node, sort_keys=True, separators=(",", ":"))]
        assert row["evidence_id"] == evidence_id_for_node(expected_node), label
        assert row["source_ref"] == source_ref_for(result["snapshot_id"], result["interpretation_id"], row["path"])
    assert any(row["path"].endswith("/summary[1]") and row["node"]["text"] == "Raw & ordered"
               for row in result["evidence"])


@pytest.mark.parametrize(
    ("source", "expected_status"),
    [
        (_replace_region(PUBLIC_1_6_CONTEXT_XML, b"<maps>", b"</maps>", b"<maps/>") , "complete"),
        (_replace_region(PUBLIC_1_6_CONTEXT_XML, b"<maps>", b"</maps>", b"<maps><li Class=\"UnknownMap\"/></maps>"), "unsupported"),
        (_replace_region(PUBLIC_1_6_CONTEXT_XML, b"<maps>", b"</maps>", b""), "absent"),
    ],
)
def test_context_distinguishes_supported_unsupported_and_absent_maps(tmp_path, source, expected_status):
    result = extract_history_snapshot(_write(tmp_path / "map-state.rws", source)).as_dict()
    assert result["coverage"]["domains"]["maps"]["status"] == expected_status
    assert result["context"]["maps"] == []


def test_present_empty_sections_are_complete_and_absent_sections_stay_absent(tmp_path):
    source = _replace_region(PUBLIC_1_6_CONTEXT_XML, b"<maps>", b"</maps>", b"<maps/>")
    source = _replace_region(source, b"<allFactions>", b"</allFactions>", b"<allFactions/>")
    source = _replace_region(source, b"<modIds>", b"</modIds>", b"<modIds/>")
    source = _replace_region(source, b"<modNames>", b"</modNames>", b"<modNames/>")
    source = _replace_region(source, b"<researchManager>", b"</researchManager>", b"<researchManager/>")
    source = _replace_region(source, b"<scenario>", b"</scenario>", b"<scenario/>")

    result = extract_history_snapshot(_write(tmp_path / "empty-context.rws", source)).as_dict()
    for domain in ("maps", "factions", "mods", "research"):
        assert result["context"][domain] == []
        assert result["coverage"]["domains"][domain] == {
            "status": "complete", "observed_count": 0, "unknown_count": 0,
        }
    assert result["context"]["scenario"] == []
    assert result["coverage"]["domains"]["scenario"] == {
        "status": "complete", "observed_count": 0, "unknown_count": 0,
    }
    assert result["coverage"]["domains"]["art"]["status"] == "absent"


def test_context_extraction_change_changes_profile_interpretation_id():
    old_interpretation_id = hashlib.sha256(canonical_json_bytes({
        "schema_version": "history/1.0",
        "parser_version": "expat-defused/1",
        "extraction_version": "1",
        "profile_id": VERIFIED_1_6_PROFILE,
    })).hexdigest()
    assert EXTRACTION_VERSION == "7"
    version_two_interpretation_id = hashlib.sha256(canonical_json_bytes({
        "schema_version": "history/1.0",
        "parser_version": "expat-defused/1",
        "extraction_version": "2",
        "profile_id": VERIFIED_1_6_PROFILE,
    })).hexdigest()
    assert INTERPRETATION_ID_FOR_VERIFIED_1_6 != version_two_interpretation_id
    version_three_interpretation_id = hashlib.sha256(canonical_json_bytes({
        "schema_version": "history/1.0",
        "parser_version": "expat-defused/1",
        "extraction_version": "3",
        "profile_id": VERIFIED_1_6_PROFILE,
    })).hexdigest()
    assert INTERPRETATION_ID_FOR_VERIFIED_1_6 != version_three_interpretation_id
    version_four_interpretation_id = hashlib.sha256(canonical_json_bytes({
        "schema_version": "history/1.0",
        "parser_version": "expat-defused/1",
        "extraction_version": "4",
        "profile_id": VERIFIED_1_6_PROFILE,
    })).hexdigest()
    assert INTERPRETATION_ID_FOR_VERIFIED_1_6 != version_four_interpretation_id
    version_five_interpretation_id = hashlib.sha256(canonical_json_bytes({
        "schema_version": "history/1.0",
        "parser_version": "expat-defused/1",
        "extraction_version": "5",
        "profile_id": VERIFIED_1_6_PROFILE,
    })).hexdigest()
    assert INTERPRETATION_ID_FOR_VERIFIED_1_6 != version_five_interpretation_id
    version_six_interpretation_id = hashlib.sha256(canonical_json_bytes({
        "schema_version": "history/1.0",
        "parser_version": "expat-defused/1",
        "extraction_version": "6",
        "profile_id": VERIFIED_1_6_PROFILE,
    })).hexdigest()
    assert INTERPRETATION_ID_FOR_VERIFIED_1_6 != version_six_interpretation_id
    assert INTERPRETATION_ID_FOR_VERIFIED_1_6 != old_interpretation_id


def test_namespaces_repeated_children_and_mixed_text_keep_source_paths_and_order(tmp_path):
    source = PUBLIC_1_6_CONTEXT_XML.replace(
        b"<summary>Raw &amp; ordered</summary>",
        b"<summary xmlns:x=\"urn:public\">before<x:mark>middle</x:mark>after</summary>",
    ).replace(b"<completedProjects><li>Electricity</li><li>Smithing</li></completedProjects>",
              b"<completedProjects><li>Electricity</li><li>Smithing</li><li>Electricity</li></completedProjects>")
    result = extract_history_snapshot(_write(tmp_path / "ordered-context.rws", source)).as_dict()
    repeated = [row for row in result["evidence"] if "/completedProjects[1]/li[" in row["path"]]
    assert [row["path"].rsplit("[", 1)[-1][:-1] for row in repeated] == ["1", "2", "3"]
    scenario_path = result["context"]["scenario"][0]["evidence_refs"][0]["source_ref"]
    evidence = next(row for row in result["evidence"] if row["source_ref"] == scenario_path)
    summary = next(entry["node"] for entry in evidence["node"]["children"]
                   if entry["node"]["tag"] == "summary")
    assert summary == {
        "tag": "summary", "attributes": [], "text": "before",
        "children": [
            {"node": {"tag": "{urn:public}mark", "attributes": [], "text": "middle", "children": []}, "tail": "after"},
        ],
    }


def test_art_reference_without_exact_saved_id_does_not_invent_a_tale_or_pawn_link(tmp_path):
    source = PUBLIC_1_6_CONTEXT_XML.replace(b"<savedTaleReference>7</savedTaleReference>",
                                            b"<savedTaleReference>not-present</savedTaleReference>")
    result = extract_history_snapshot(_write(tmp_path / "unresolved-art.rws", source)).as_dict()
    art = result["context"]["art"][0]
    tale_link = next(link for link in art["links"] if link["kind"] == "saved_tale_reference")
    assert tale_link["target_id"] == "not-present"
    assert tale_link["status"] == "unresolved"
    assert not any(link["kind"] == "pawn_reference" for link in art["links"])


def test_unknown_custom_art_component_remains_raw_and_unsupported(tmp_path):
    source = PUBLIC_1_6_CONTEXT_XML.replace(b"RimWorld.CompArt", b"ThirdParty.PaintingArt")
    result = extract_history_snapshot(_write(tmp_path / "custom-art.rws", source)).as_dict()
    assert result["context"]["art"] == []
    assert result["coverage"]["domains"]["art"] == {
        "status": "unsupported", "observed_count": 0, "unknown_count": 1,
    }
    raw = [row for row in result["evidence"] if row["node"]["tag"] == "li" and
           ["Class", "ThirdParty.PaintingArt"] in row["node"]["attributes"]]
    assert len(raw) == 1


@pytest.mark.parametrize(
    ("replacement", "domain", "expected_count"),
    [
        ((b'Class="RimWorld.CompArt"', b'Class="ThirdParty.CompArt"'), "art", 0),
        ((b'Class="Map"', b'Class="ThirdParty.Map"'), "maps", 0),
        ((b'Class="Faction"', b'Class="ThirdParty.Faction"'), "factions", 0),
    ],
)
def test_foreign_class_with_supported_local_name_is_unsupported(tmp_path, replacement, domain, expected_count):
    source = PUBLIC_1_6_CONTEXT_XML.replace(*replacement)
    result = extract_history_snapshot(_write(tmp_path / f"foreign-{domain}.rws", source)).as_dict()

    assert result["context"][domain] == []
    assert result["coverage"]["domains"][domain] == {
        "status": "unsupported", "observed_count": expected_count, "unknown_count": 1,
    }
    if domain == "art":
        assert not any(link["kind"] == "pawn_reference"
                       for entity in result["context"]["art"] for link in entity["links"])
        assert any(row["path"].endswith("/comps[1]/li[1]") and
                   row.get("evidence_kind", "xml_node") == "xml_node" for row in result["evidence"])
    elif domain == "maps":
        assert any(row.get("evidence_kind") == "xml_start_tag" and
                   row["path"].endswith("/maps[1]/li[1]") for row in result["evidence"])


def test_art_saved_tale_reference_ignores_archive_records_with_same_id(tmp_path):
    source = PUBLIC_1_6_CONTEXT_XML.replace(
        b"</taleManager>",
        b"</taleManager><history><archive><archivables><li Class=\"Archive\"><ID>7</ID>"
        b"<lookTargets><li>Human99</li></lookTargets></li></archivables></archive></history>",
    )
    result = extract_history_snapshot(_write(tmp_path / "tale-and-archive-same-id.rws", source)).as_dict()
    links = result["context"]["art"][0]["links"]

    tale_link = next(link for link in links if link["kind"] == "saved_tale_reference")
    assert tale_link["status"] == "observed"
    assert tale_link["target_id"] == "7"
    assert [link["target_id"] for link in links if link["kind"] == "pawn_reference"] == ["Human42"]


def test_art_saved_tale_reference_does_not_resolve_to_archive_only_record(tmp_path):
    source = PUBLIC_1_6_CONTEXT_XML.replace(
        b"<taleManager><tales><li Class=\"Tale_Art\" id=\"7\"><def>ArtMade</def>"
        b"<occurred>52510</occurred><pawn>Human42</pawn></li></tales></taleManager>",
        b"<history><archive><archivables><li Class=\"Archive\"><ID>7</ID>"
        b"<lookTargets><li>Human99</li></lookTargets></li></archivables></archive></history>",
    )
    result = extract_history_snapshot(_write(tmp_path / "archive-only-same-id.rws", source)).as_dict()
    links = result["context"]["art"][0]["links"]

    tale_link = next(link for link in links if link["kind"] == "saved_tale_reference")
    assert tale_link["status"] == "unresolved"
    assert tale_link["target_id"] == "7"
    assert not any(link["kind"] == "pawn_reference" for link in links)


def test_unknown_map_extension_is_bounded_raw_evidence_and_partial_coverage(tmp_path):
    source = PUBLIC_1_6_CONTEXT_XML.replace(
        b"<things>",
        b'<customMapData xmlns:x="urn:public"><x:state>retained?</x:state></customMapData><things>',
    )
    result = extract_history_snapshot(_write(tmp_path / "unknown-map-extension.rws", source)).as_dict()

    assert result["coverage"]["domains"]["maps"] == {
        "status": "partial", "observed_count": 1, "unknown_count": 1,
    }
    extension = next(row for row in result["evidence"] if "customMapData" in row["path"])
    assert extension["node"] == {
        "tag": "customMapData", "attributes": [], "text": None,
        "children": [{"node": {"tag": "{urn:public}state", "attributes": [], "text": "retained?", "children": []},
                      "tail": None}],
    }
    assert extension.get("evidence_kind", "xml_node") == "xml_node"
    assert any(claim["status"] == "unsupported" and claim["evidence_id"] == extension["evidence_id"]
               for claim in result["context"]["maps"][0]["claims"])
    assert not any(row.get("evidence_kind") != "xml_start_tag" and
                   row["path"].endswith("/maps[1]/li[1]") for row in result["evidence"])


def test_context_records_and_pawns_share_normalized_record_limit(tmp_path):
    from rimworld_save_inspector.errors import InspectorError
    from rimworld_save_inspector.models import ParseLimits

    source = (b'<savegame version="1.6.4535"><game><maps><li><uniqueID>1</uniqueID><things>'
              b'<thing Class="Pawn"><def>Human</def><id>Human1</id><kindDef>Colonist</kindDef></thing>'
              b'</things></li></maps></game></savegame>')
    path = _write(tmp_path / "map-then-pawn-limit-1.rws", source)

    with pytest.raises(InspectorError) as failure:
        extract_history_snapshot(path, limits=ParseLimits(normalized_records=1))
    assert failure.value.code == "LIMIT_EXCEEDED"


def test_native_pawns_nested_in_retained_map_context_are_materialized_but_unknown_shapes_stay_raw(tmp_path):
    source = PUBLIC_1_6_CONTEXT_XML.replace(
        b"<things>",
        b'''<visitorManager><passingShips><li><things>
          <thing Class="Pawn"><def>Human</def><kindDef>Visitor</kindDef><id>Visitor42</id></thing>
          <thing Class="ThirdParty.Pawn"><def>Human</def><kindDef>Visitor</kindDef><id>FakeVisitor</id></thing>
          <x:thing xmlns:x="urn:foreign" Class="Pawn"><def>Human</def><kindDef>Visitor</kindDef><id>QNameVisitor</id></x:thing>
        </things></li></passingShips></visitorManager><things>''',
        1,
    )
    result = extract_history_snapshot(_write(tmp_path / "passing-ships-context-pawns.rws", source)).as_dict()

    assert "Visitor42" in {pawn["pawn_id"] for pawn in result["pawns"]}
    assert not {"FakeVisitor", "QNameVisitor"}.intersection(pawn["pawn_id"] for pawn in result["pawns"])
    retained_extension = next(row for row in result["evidence"]
                              if row["path"].endswith("/visitorManager[1]"))
    assert retained_extension["node"]["children"][0]["node"]["tag"] == "passingShips"
    assert result["coverage"]["domains"]["maps"]["status"] == "partial"
    unknown_record_kinds = {row["structural_kind"] for row in result["coverage"]["unknown_fields"]}
    assert "unverified_pawn_like_class" in unknown_record_kinds
    assert "foreign_qname_pawn_record" in unknown_record_kinds
    raw_tags = {row["node"]["tag"] for row in result["evidence"]}
    assert "{urn:foreign}thing" in raw_tags
    assert any(row["node"]["attributes"] == [["Class", "ThirdParty.Pawn"]]
               for row in result["evidence"])


def test_foreign_qname_map_size_is_raw_unsupported_not_map_metadata(tmp_path):
    source = PUBLIC_1_6_CONTEXT_XML.replace(
        b"<size>(250,1,250)</size>",
        b'<x:size xmlns:x="urn:foreign">other shape</x:size>',
    )
    result = extract_history_snapshot(_write(tmp_path / "foreign-qname-map-size.rws", source)).as_dict()
    claims = result["context"]["maps"][0]["claims"]

    assert not any(claim["field"] == "map.size" for claim in claims)
    assert result["coverage"]["domains"]["maps"] == {
        "status": "partial", "observed_count": 1, "unknown_count": 1,
    }
    unsupported = next(claim for claim in claims if claim["status"] == "unsupported")
    raw = next(row for row in result["evidence"] if row["evidence_id"] == unsupported["evidence_id"])
    assert raw["node"]["tag"] == "{urn:foreign}size"
    assert raw["node"]["text"] == "other shape"


def test_foreign_qname_pawn_fields_remain_raw_and_do_not_claim_identity_age_or_genes(tmp_path):
    source = b'''<savegame xmlns:x="urn:foreign" version="1.6.4535"><game>
      <tickManager><gameStartAbsTick>52500</gameStartAbsTick><ticksGame>50</ticksGame></tickManager>
      <maps><li Class="Map"><uniqueID>Map_1</uniqueID><things><thing Class="Pawn">
        <def>Human</def><kindDef>Colonist</kindDef><id>Human1</id><x:faction>ForeignFaction</x:faction>
        <ageTracker><x:birthAbsTicks>52450</x:birthAbsTicks></ageTracker>
        <genes><x:overriddenByGene>GeneA</x:overriddenByGene></genes>
        <x:genes><x:overriddenByGene>GeneB</x:overriddenByGene></x:genes>
      </thing></things></li></maps>
    </game></savegame>'''
    result = extract_history_snapshot(_write(tmp_path / "foreign-qname-pawn-fields.rws", source)).as_dict()
    pawn = next(row for row in result["pawns"] if row["pawn_id"] == "Human1")
    domains = pawn["domains"]
    all_claims = [claim for domain in domains.values() for claim in domain["claims"]]
    unsupported = [claim for claim in domains["unknown_fields"]["claims"]
                   if claim["status"] == "unsupported"]
    unsupported_fields = [claim["raw_field"] for claim in unsupported]

    assert not any(claim["field"].endswith("faction") and claim["status"] == "observed"
                   for claim in all_claims)
    assert any(claim["field"] == "identity.faction" and claim["status"] == "absent"
               for claim in all_claims)
    assert not any(claim.get("time_observation") and claim["time_observation"]["clock"] == "absolute_tick"
                   for claim in all_claims)
    assert not any(claim["field"].endswith("overriddenByGene") for claim in all_claims)
    assert not any(claim["field"].startswith("genes.") and "GeneB" in str(claim) for claim in all_claims)
    assert {"/{urn:foreign}faction[1]", "/ageTracker[1]/{urn:foreign}birthAbsTicks[1]",
            "/genes[1]/{urn:foreign}overriddenByGene[1]",
            "/{urn:foreign}genes[1]"}.issubset(set(unsupported_fields))
    assert domains["unknown_fields"]["status"] == "partial"


def test_foreign_qname_event_pawn_is_not_a_participant_and_is_raw_unsupported(tmp_path):
    source = PUBLIC_1_6_CONTEXT_XML.replace(
        b"<pawn>Human42</pawn>", b'<x:pawn xmlns:x="urn:foreign">Human42</x:pawn>',
    )
    result = extract_history_snapshot(_write(tmp_path / "foreign-qname-event-pawn.rws", source)).as_dict()
    event = next(row for row in result["events"] if row["raw_saved_id"] == "7")

    assert event["participants"] == []
    unsupported = next(claim for claim in event["claims"] if claim["status"] == "unsupported")
    raw = next(row for row in result["evidence"] if row["evidence_id"] == unsupported["evidence_id"])
    assert raw["node"]["tag"] == "{urn:foreign}pawn"
    assert raw["node"]["text"] == "Human42"
    assert result["coverage"]["domains"]["events"]["unknown_count"] >= 1


@pytest.mark.parametrize("class_name", ["Pawn", "Verse.Pawn", "ThirdParty.Pawn"])
def test_pawn_class_allowlist_rejects_foreign_namespace_and_keeps_raw_candidate(tmp_path, class_name):
    source = (f'<savegame version="1.6.4535"><game><maps><li><uniqueID>Map_1</uniqueID><things>'
              f'<thing Class="{class_name}"><def>Human</def><kindDef>Colonist</kindDef><id>Human1</id></thing>'
              f'</things></li></maps></game></savegame>').encode()
    result = extract_history_snapshot(_write(tmp_path / f"pawn-class-{class_name.replace('.', '-')}.rws", source)).as_dict()

    if class_name == "ThirdParty.Pawn":
        assert result["pawns"] == []
        raw = next(row for row in result["coverage"]["unknown_fields"]
                   if row["structural_kind"] == "unverified_pawn_like_class")
        evidence = next(row for row in result["evidence"] if row["evidence_id"] == raw["evidence_id"])
        assert evidence["node"]["attributes"] == [["Class", "ThirdParty.Pawn"]]
        assert result["coverage"]["domains"]["identity"]["status"] == "absent"
    else:
        assert [pawn["pawn_id"] for pawn in result["pawns"]] == ["Human1"]


def test_holder_scopes_require_exact_native_classes_and_preserve_unknown_holder_evidence(tmp_path):
    source = b'''<savegame version="1.6.4535"><game><maps><li><uniqueID>Map_1</uniqueID><things>
      <thing Class="Corpse" id="Corpse1"><innerPawn Class="Pawn"><id>CorpsePawn</id><def>Human</def><kindDef>Colonist</kindDef></innerPawn></thing>
      <thing Class="Verse.Corpse" id="VerseCorpse1"><innerPawn Class="Pawn"><id>VerseCorpsePawn</id><def>Human</def><kindDef>Colonist</kindDef></innerPawn></thing>
      <thing Class="ExampleMod.Corpse" id="ModCorpse1"><innerPawn Class="Pawn"><id>ModCorpsePawn</id><def>Human</def><kindDef>Colonist</kindDef></innerPawn></thing>
      <x:thing xmlns:x="urn:mod" Class="ExampleMod.Corpse" id="QNameCorpse1"><innerPawn Class="Pawn"><id>QNameCorpsePawn</id><def>Human</def><kindDef>Colonist</kindDef></innerPawn></x:thing>
      <thing Class="Building_CryptosleepCasket" id="Casket1"><innerContainer><innerList><li Class="Pawn"><id>CasketPawn</id><def>Human</def><kindDef>Colonist</kindDef></li></innerList></innerContainer></thing>
      <thing Class="Building_CryptosleepCasketPretender" id="FakeCasket1"><innerContainer><innerList><li Class="Pawn"><id>FakeCasketPawn</id><def>Human</def><kindDef>Colonist</kindDef></li></innerList></innerContainer></thing>
      <thing Class="Building_GrowthVat" id="Vat1"><innerContainer><innerList><li Class="Pawn"><id>VatPawn</id><def>Human</def><kindDef>Colonist</kindDef></li></innerList></innerContainer></thing>
      <thing Class="Building_GrowthVatPretender" id="FakeVat1"><innerContainer><innerList><li Class="Pawn"><id>FakeVatPawn</id><def>Human</def><kindDef>Colonist</kindDef></li></innerList></innerContainer></thing>
    </things></li></maps></game></savegame>'''
    result = extract_history_snapshot(_write(tmp_path / "native-and-custom-holder-classes.rws", source)).as_dict()
    pawns = {pawn["pawn_id"]: pawn for pawn in result["pawns"]}

    assert pawns["CorpsePawn"]["scope"] == "corpse"
    assert pawns["VerseCorpsePawn"]["scope"] == "corpse"
    assert pawns["CasketPawn"]["scope"] == "cryptosleep"
    assert pawns["VatPawn"]["scope"] == "growth_vat"
    for pawn_id in ("ModCorpsePawn", "QNameCorpsePawn", "FakeCasketPawn", "FakeVatPawn"):
        assert pawns[pawn_id]["scope"] in {"held_other", "unknown"}
        assert not any(holder["kind"] in {"corpse", "cryptosleep_casket", "growth_vat"}
                       for holder in pawns[pawn_id]["holder_chain"])

    unknown_holders = [row for row in result["coverage"]["unknown_fields"]
                       if row["structural_kind"] == "unverified_holder_class"]
    assert len(unknown_holders) == 4
    assert all(row["path"] and row["evidence_id"] and row["source_ref"] for row in unknown_holders)
    evidence = {row["evidence_id"]: row for row in result["evidence"]}
    raw_holder_nodes = [evidence[row["evidence_id"]]["node"] for row in unknown_holders]
    assert any(node["tag"] == "{urn:mod}thing" and ["id", "QNameCorpse1"] in node["attributes"]
               for node in raw_holder_nodes)
    assert any(node["attributes"] == [["Class", "ExampleMod.Corpse"], ["id", "ModCorpse1"]]
               for node in raw_holder_nodes)


@pytest.mark.parametrize(
    ("holder_class", "holder_contents", "pawn_id"),
    [
        ("Corpse", '<innerPawn Class="Pawn"><id>QNameCorpsePawn</id><def>Human</def><kindDef>Colonist</kindDef></innerPawn>', "QNameCorpsePawn"),
        ("Building_CryptosleepCasket", '<innerContainer><innerList><li Class="Pawn"><id>QNameCasketPawn</id><def>Human</def><kindDef>Colonist</kindDef></li></innerList></innerContainer>', "QNameCasketPawn"),
        ("Building_GrowthVat", '<innerContainer><innerList><li Class="Pawn"><id>QNameVatPawn</id><def>Human</def><kindDef>Colonist</kindDef></li></innerList></innerContainer>', "QNameVatPawn"),
    ],
)
def test_foreign_qname_holder_with_native_class_stays_unsupported_raw(tmp_path, holder_class, holder_contents, pawn_id):
    source = (f'<savegame version="1.6.4535"><game><maps><li><uniqueID>Map_1</uniqueID><things>'
              f'<x:thing xmlns:x="urn:foreign" Class="{holder_class}" id="Holder_{pawn_id}">'
              f'{holder_contents}</x:thing></things></li></maps></game></savegame>').encode()
    result = extract_history_snapshot(_write(tmp_path / f"foreign-qname-holder-{pawn_id}.rws", source)).as_dict()
    pawn = next(row for row in result["pawns"] if row["pawn_id"] == pawn_id)

    assert pawn["scope"] in {"held_other", "unknown"}
    assert not any(holder["kind"] in {"corpse", "cryptosleep_casket", "growth_vat"}
                   for holder in pawn["holder_chain"])
    raw_row = next(row for row in result["coverage"]["unknown_fields"]
                   if row["structural_kind"] == "unverified_holder_class")
    raw = next(row for row in result["evidence"] if row["evidence_id"] == raw_row["evidence_id"])
    assert raw_row["path"].endswith("/{urn:foreign}thing[1]")
    assert raw["node"]["tag"] == "{urn:foreign}thing"
    assert ["Class", holder_class] in raw["node"]["attributes"]
    assert ["id", f"Holder_{pawn_id}"] in raw["node"]["attributes"]


def test_foreign_qname_game_clock_does_not_become_clock_input(tmp_path):
    source = b'''<savegame xmlns:x="urn:foreign" version="1.6.4535"><game>
      <tickManager><x:gameStartAbsTick>52500</x:gameStartAbsTick><x:ticksGame>50</x:ticksGame></tickManager>
    </game></savegame>'''
    result = extract_history_snapshot(_write(tmp_path / "foreign-qname-clock.rws", source)).as_dict()

    assert result["metadata"]["clocks"]["gameStartAbsTick"]["status"] == "absent"
    assert result["metadata"]["clocks"]["ticksGame"]["status"] == "absent"
    assert result["metadata"]["clocks"]["ticksAbs"]["status"] == "unsupported"


def test_native_meta_game_version_precedes_root_attribute_and_selects_profile(tmp_path):
    source = b'<savegame version="1.7.3901 rev12"><meta><gameVersion>1.6.4871 rev591</gameVersion></meta><game/></savegame>'
    result = extract_history_snapshot(_write(tmp_path / "native-meta-game-version.rws", source)).as_dict()
    profile = result["metadata"]["profile"]

    assert profile["game_version"] == "1.6.4871 rev591"
    assert profile["support_status"] == "verified"
    assert profile["profile_id"] == VERIFIED_1_6_PROFILE
    assert result["interpretation_id"] == interpretation_id_for_profile(VERIFIED_1_6_PROFILE)
    version_evidence = next(row for row in result["evidence"]
                            if row["path"].endswith("/meta[1]/gameVersion[1]"))
    assert version_evidence["node"]["tag"] == "gameVersion"
    assert version_evidence["node"]["text"] == "1.6.4871 rev591"
    assert any(row.get("evidence_kind") == "xml_start_tag" and
               row["node"]["attributes"] == [["version", "1.7.3901 rev12"]]
               for row in result["evidence"])


@pytest.mark.parametrize(
    ("native_version", "root_version", "expected_support", "expected_profile"),
    [
        ("1.6.4535", "1.7.1", "verified", VERIFIED_1_6_PROFILE),
        ("1.6.4871 rev591", "1.7.1", "verified", VERIFIED_1_6_PROFILE),
        ("1.6.garbage", "1.6.4535", "best_effort", "rimworld-1.6-best-effort/v1"),
        ("1.6.4871suffix", "1.6.4535", "best_effort", "rimworld-1.6-best-effort/v1"),
        ("1.6.4871 revNaN", "1.6.4535", "best_effort", "rimworld-1.6-best-effort/v1"),
    ],
)
def test_native_1_6_profile_requires_complete_numeric_build_and_optional_revision(
        tmp_path, native_version, root_version, expected_support, expected_profile):
    source = (f'<savegame version="{root_version}"><meta><gameVersion>{native_version}</gameVersion></meta>'
              '<game/></savegame>').encode()
    result = extract_history_snapshot(_write(tmp_path / "strict-native-version.rws", source)).as_dict()
    profile = result["metadata"]["profile"]

    assert profile["game_version"] == native_version
    assert profile["support_status"] == expected_support
    assert profile["profile_id"] == expected_profile


@pytest.mark.parametrize(
    ("meta_xml", "root_version", "expected_version", "expected_support", "expected_profile"),
    [
        (b"<gameVersion/>", b' version="1.6.4535"', None, "unknown", "rimworld-unknown/v1"),
        (b"<gameVersion>invalid-version</gameVersion>", b' version="1.6.4535"',
         "invalid-version", "best_effort", "rimworld-invalid-version-best-effort/v1"),
        (b'<x:gameVersion xmlns:x="urn:foreign">1.6.4871 rev591</x:gameVersion>', b"",
         None, "unknown", "rimworld-unknown/v1"),
        (b"<gameVersion>1.7.3901 rev12</gameVersion>", b"",
         "1.7.3901 rev12", "best_effort", "rimworld-1.7-best-effort/v1"),
    ],
)
def test_native_meta_version_presence_empty_invalid_foreign_and_unsupported_are_truthful(
        tmp_path, meta_xml, root_version, expected_version, expected_support, expected_profile):
    source = b"<savegame" + root_version + b"><meta>" + meta_xml + b"</meta><game/></savegame>"
    result = extract_history_snapshot(_write(tmp_path / "version-source-state.rws", source)).as_dict()
    profile = result["metadata"]["profile"]

    assert profile["game_version"] == expected_version
    assert profile["support_status"] == expected_support
    assert profile["profile_id"] == expected_profile


def test_foreign_qname_pawn_record_is_not_materialized_as_pawn(tmp_path):
    source = b'''<savegame xmlns:x="urn:foreign" version="1.6.4535"><game>
      <maps><li><uniqueID>Map_1</uniqueID><things>
        <x:thing Class="Pawn"><def>Human</def><kindDef>Colonist</kindDef><id>Human1</id></x:thing>
      </things></li></maps>
    </game></savegame>'''
    result = extract_history_snapshot(_write(tmp_path / "foreign-qname-pawn-record.rws", source)).as_dict()

    assert result["pawns"] == []
    assert any(row["structural_kind"] == "foreign_qname_pawn_record"
               for row in result["coverage"]["unknown_fields"])
