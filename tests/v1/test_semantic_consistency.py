"""Independent serialized-entry oracles for pawn and map availability."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from rimworld_save_inspector.models import QueryRequest
from rimworld_save_inspector.service import QueryService
from rimworld_save_inspector.source import load_snapshot


SEED = Path(__file__).resolve().parents[1] / "fixtures/synthetic/seed.rws"


def _service(xml: bytes) -> QueryService:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "handwritten.rws"
        path.write_bytes(xml)
        return QueryService(load_snapshot(path))


def _pawn_service(children: bytes) -> tuple[QueryService, str]:
    xml = (b"<savegame><game><maps><li><uniqueID>0</uniqueID><things>"
           b"<thing Class='Pawn'><id>P</id><def>Human</def>" + children +
           b"</thing></things></li></maps></game></savegame>")
    service = _service(xml)
    pawn = next(r for r in service.snapshot.records if r.type_name == "pawn")
    return service, pawn.record_id


def _query(service: QueryService, name: str, **filters: str) -> dict:
    return service.execute(QueryRequest(name, filters)).as_dict()


def test_handwritten_seed_child_counts_agree_with_immediate_domain_entries() -> None:
    service = _service(SEED.read_bytes())
    pawn = next(r for r in service.snapshot.records if r.type_name == "pawn" and r.saved_id == "Human1")
    record_id = pawn.record_id
    expected = {"skills": 1, "traits": 1, "hediffs": 1, "endogenes": 2,
                "xenogenes": 0, "equipment": 1, "apparel": 1, "inventory": 2}
    counts = _query(service, "pawn", record_id=record_id)["data"]["pawn"]["child_counts"]
    assert counts == expected
    domains = {
        "skills": ("pawn", {"section": "skills"}),
        "traits": ("pawn", {"section": "traits"}),
        "hediffs": ("health", {}),
        "endogenes": ("genes", {"group": "endogenes"}),
        "xenogenes": ("genes", {"group": "xenogenes"}),
        "equipment": ("equipment", {"holder": "equipment"}),
        "apparel": ("equipment", {"holder": "apparel"}),
        "inventory": ("inventory", {}),
    }
    for key, (name, extra) in domains.items():
        result = _query(service, name, record_id=record_id, **extra)
        assert result["data"]["collection_state"] == "present", key
        assert result["page"]["total"] == expected[key], key


@pytest.mark.parametrize(
    ("child", "expected_state", "expected_count"),
    [
        (b"", "absent", None),
        (b"<skills IsNull='True'/>", "explicit_null", None),
        (b"<skills IsNull='True'><skills><li><def>Shooting</def></li></skills></skills>", "invalid", None),
        (b"<skills><skills/></skills>", "present", 0),
        (b"<skills><skills><li><def>Shooting</def></li></skills></skills>", "present", 1),
    ],
)
def test_child_collection_availability_is_distinct_from_zero(
    child: bytes, expected_state: str, expected_count: int | None,
) -> None:
    service, record_id = _pawn_service(child)
    counts = _query(service, "pawn", record_id=record_id)["data"]["pawn"]["child_counts"]
    skills = _query(service, "pawn", record_id=record_id, section="skills")
    assert counts["skills"] == expected_count
    assert skills["data"]["collection_state"] == expected_state
    assert skills["page"]["total"] == expected_count
    assert len(skills["data"]["entries"]) == (expected_count or 0)
    if expected_state == "invalid":
        assert skills["coverage"]["diagnostic_counts"].get("INVALID_VALUE")


@pytest.mark.parametrize(
    ("kind", "prefix", "tag", "suffix", "entry", "name", "extra"),
    [
        ("skills", b"<skills>", b"skills", b"</skills>", b"<li><def>Shooting</def><level>bad</level></li>", "pawn", {"section": "skills"}),
        ("traits", b"<story><traits>", b"allTraits", b"</traits></story>", b"<li><def>Tough</def></li>", "pawn", {"section": "traits"}),
        ("hediffs", b"<healthTracker><hediffSet>", b"hediffs", b"</hediffSet></healthTracker>", b"<li><def>Cut</def></li>", "health", {}),
        ("endogenes", b"<genes>", b"endogenes", b"</genes>", b"<li><def>GeneA</def></li>", "genes", {"group": "endogenes"}),
        ("xenogenes", b"<genes>", b"xenogenes", b"</genes>", b"<li><def>GeneA</def></li>", "genes", {"group": "xenogenes"}),
        ("equipment", b"<equipment><equipment>", b"innerList", b"</equipment></equipment>", b"<li><id>R</id><def>Rifle</def></li>", "equipment", {"holder": "equipment"}),
        ("apparel", b"<apparel><wornApparel>", b"innerList", b"</wornApparel></apparel>", b"<li><id>H</id><def>Hat</def></li>", "equipment", {"holder": "apparel"}),
        ("inventory", b"<inventory><innerContainer>", b"innerList", b"</innerContainer></inventory>", b"<li><id>M</id><def>Medicine</def></li>", "inventory", {}),
    ],
)
@pytest.mark.parametrize(("variant", "state", "count"), [
    ("absent", "absent", None), ("null", "explicit_null", None),
    ("invalid", "invalid", None), ("empty", "present", 0), ("one", "present", 1),
])
def test_all_child_domains_count_only_available_immediate_entries(
    kind: str, prefix: bytes, tag: bytes, suffix: bytes, entry: bytes,
    name: str, extra: dict[str, str], variant: str, state: str, count: int | None,
) -> None:
    if variant == "absent":
        # Keep parent wrappers to distinguish an absent child collection.
        fragment = prefix + suffix
    else:
        attribute = b" IsNull='True'" if variant in {"null", "invalid"} else b""
        body = entry if variant in {"invalid", "one"} else b""
        fragment = prefix + b"<" + tag + attribute + b">" + body + b"</" + tag + b">" + suffix
    service, record_id = _pawn_service(fragment)
    core = _query(service, "pawn", record_id=record_id)["data"]["pawn"]
    domain = _query(service, name, record_id=record_id, **extra)
    assert core["child_counts"][kind] == count
    assert domain["data"]["collection_state"] == state
    assert domain["page"]["total"] == count
    assert len(domain["data"]["entries"]) == (count or 0)
    if variant == "invalid":
        assert domain["coverage"]["diagnostic_counts"].get("INVALID_VALUE")


@pytest.mark.parametrize(("kind", "fragment", "name", "extra"), [
    ("traits", b"<story IsNull='True'><traits><allTraits><li><def>Tough</def></li></allTraits></traits></story>", "pawn", {"section": "traits"}),
    ("hediffs", b"<healthTracker IsNull='True'><hediffSet><hediffs><li><def>Cut</def></li></hediffs></hediffSet></healthTracker>", "health", {}),
    ("endogenes", b"<genes IsNull='True'><endogenes><li><def>GeneA</def></li></endogenes></genes>", "genes", {"group": "endogenes"}),
    ("inventory", b"<inventory IsNull='True'><innerContainer><innerList><li><id>M</id><def>Medicine</def></li></innerList></innerContainer></inventory>", "inventory", {}),
])
def test_conflicting_null_parent_is_invalid_and_cannot_publish_children(
    kind: str, fragment: bytes, name: str, extra: dict[str, str],
) -> None:
    service, record_id = _pawn_service(fragment)
    core = _query(service, "pawn", record_id=record_id)["data"]["pawn"]
    domain = _query(service, name, record_id=record_id, **extra)
    assert core["child_counts"][kind] is None
    assert domain["data"]["collection_state"] == "invalid"
    assert domain["data"]["entries"] == []
    assert domain["page"]["total"] is None
    assert domain["coverage"]["diagnostic_counts"].get("INVALID_VALUE")


def test_nested_item_collection_does_not_become_pawn_skills() -> None:
    fragment = b"""<inventory><innerContainer><innerList><li><id>Box</id><def>Box</def>
    <skills><skills><li><def>Shooting</def></li></skills></skills>
    </li></innerList></innerContainer></inventory>"""
    service, record_id = _pawn_service(fragment)
    assert _query(service, "pawn", record_id=record_id)["data"]["pawn"]["child_counts"]["skills"] is None
    skills = _query(service, "pawn", record_id=record_id, section="skills")
    assert skills["data"]["collection_state"] == "absent"
    assert skills["data"]["entries"] == []
    assert not [record for record in service.snapshot.records if record.type_name == "skill"]


def test_holder_immediate_entries_include_duplicate_references_not_nested_contents() -> None:
    children = b"""<inventory><innerContainer><innerList>
      <li><id>Box</id><def>Box</def><innerContainer><innerList>
        <li><id>Nested1</id><def>Medicine</def></li>
        <li><id>Nested2</id><def>Medicine</def></li>
      </innerList></innerContainer></li>
      <li>Thing_Duplicate</li><li>Thing_Duplicate</li>
    </innerList></innerContainer></inventory>"""
    service, record_id = _pawn_service(children)
    counts = _query(service, "pawn", record_id=record_id)["data"]["pawn"]["child_counts"]
    inventory = _query(service, "inventory", record_id=record_id)
    assert counts["inventory"] == 3
    assert inventory["data"]["entry_count"] == inventory["page"]["total"] == 3
    assert [entry["entry_kind"] for entry in inventory["data"]["entries"]] == ["record", "reference", "reference"]
    assert inventory["data"]["entries"][0]["item"]["nested_holders"][0]["entry_count"] == 2


def test_direct_map_roster_ignores_off_map_and_nested_things_collections() -> None:
    xml = b"""<savegame><game><things IsNull='True'/><maps><li><uniqueID>0</uniqueID><things>
      <thing Class='Pawn'><id>P1</id><def>Human</def><things IsNull='True'/></thing>
      <thing Class='Pawn'><id>P2</id><def>Human</def></thing>
    </things></li></maps></game></savegame>"""
    service = _service(xml)
    first = service.execute(QueryRequest("pawns", limit=1)).as_dict()
    assert first["data"]["collection_state"] == "present"
    assert first["page"]["total"] == 2
    second = service.execute(QueryRequest("pawns", limit=1, cursor=first["page"]["next_cursor"])).as_dict()
    assert second["data"]["collection_state"] == "present"
    assert second["page"]["total"] == 2
    assert {first["data"]["entries"][0]["saved_id"], second["data"]["entries"][0]["saved_id"]} == {"P1", "P2"}


def test_selected_map_roster_ignores_unavailable_sibling_map() -> None:
    xml = b"""<savegame><game><maps>
      <li><uniqueID>0</uniqueID><things><thing Class='Pawn'><id>P</id><def>Human</def></thing></things></li>
      <li><uniqueID>1</uniqueID><things IsNull='True'/></li>
    </maps></game></savegame>"""
    service = _service(xml)
    selected = _query(service, "pawns", map_id="0")
    assert selected["data"]["collection_state"] == "present"
    assert selected["page"]["total"] == 1
    all_maps = _query(service, "pawns")
    assert all_maps["data"]["collection_state"] == "present"
    assert all_maps["page"]["total"] is None
    assert [entry["saved_id"] for entry in all_maps["data"]["entries"]] == ["P"]
    assert all_maps["coverage"]["status"] == "partial"
    assert all_maps["coverage"]["diagnostic_counts"]["MISSING_COLLECTION"] == 1
    assert all_maps["coverage"]["diagnostics"][-1]["details"]["state"] == "explicit_null"
    assert all_maps["data"]["derivations"][0]["rule"] == "known-direct-map-pawns-v1"
    missing = _query(service, "pawns", map_id="1")
    assert missing["data"]["collection_state"] == "explicit_null"
    assert missing["data"]["entries"] == []
    assert missing["page"]["total"] is None


def test_mixed_map_roster_keeps_unknown_total_across_pages() -> None:
    xml = b"""<savegame><game><maps>
      <li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>P1</id><def>Human</def></thing>
        <thing Class='Pawn'><id>P2</id><def>Human</def></thing>
      </things></li>
      <li><uniqueID>1</uniqueID><things IsNull='True'/></li>
    </maps></game></savegame>"""
    service = _service(xml)
    first = service.execute(QueryRequest("pawns", limit=1)).as_dict()
    second = service.execute(QueryRequest("pawns", limit=1, cursor=first["page"]["next_cursor"])).as_dict()
    assert first["page"]["total"] is second["page"]["total"] is None
    assert {first["data"]["entries"][0]["saved_id"], second["data"]["entries"][0]["saved_id"]} == {"P1", "P2"}
    assert first["coverage"]["status"] == second["coverage"]["status"] == "partial"


def test_present_empty_maps_has_known_zero_roster() -> None:
    service = _service(b"<savegame><game><maps/></game></savegame>")
    roster = _query(service, "pawns")
    assert roster["data"]["collection_state"] == "present"
    assert roster["data"]["entries"] == []
    assert roster["page"]["total"] == 0


@pytest.mark.parametrize(("maps_markup", "expected_state"), [
    (b"<maps IsNull='True'><li><uniqueID>0</uniqueID><things><thing Class='Pawn'><id>P</id><def>Human</def></thing></things></li></maps>", "invalid"),
    (b"<maps><li><uniqueID>0</uniqueID><things IsNull='True'><thing Class='Pawn'><id>P</id><def>Human</def></thing></things></li></maps>", "invalid"),
])
def test_invalid_map_parent_does_not_publish_ordinary_rows_with_unknown_total(
    maps_markup: bytes, expected_state: str,
) -> None:
    service = _service(b"<savegame><game>" + maps_markup + b"</game></savegame>")
    result = _query(service, "pawns")
    assert result["data"]["collection_state"] == expected_state
    assert result["data"]["entries"] == []
    assert result["page"]["total"] is None
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["diagnostic_counts"].get("INVALID_VALUE")
