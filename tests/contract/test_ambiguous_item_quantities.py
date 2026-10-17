"""Unknown item targets must never contribute to known holder quantities."""
from pathlib import Path

import pytest

from rimworld_save_inspector import QueryRequest, QueryService, load_snapshot


def _pawn(saved_id, inventory):
    return (f'<thing Class="Pawn"><id>{saved_id}</id><def>Human</def>'
            f'<inventory><innerContainer>{inventory}</innerContainer></inventory></thing>')


def _item(units):
    return f'<li><id>SharedItem</id><def>Medicine</def><stackCount>{units}</stackCount></li>'


@pytest.mark.parametrize("owned_units,reference_count", [(None, 1), (None, 2), (7, 1)])
def test_ambiguous_candidates_do_not_become_known_units(tmp_path: Path, owned_units, reference_count):
    own = _item(owned_units) if owned_units is not None else ""
    references = '<li>Thing_SharedItem</li>' * reference_count
    # All targets are full records under supported pawn inventories. The first
    # pawn owns either no item or one known full record; other possible targets
    # cannot add units merely because they share a serialized saved ID.
    pawn_a = _pawn("A", '<innerList>' + own + references + '</innerList>')
    pawn_b = _pawn("B", '<innerList>' + _item(2) + '</innerList>')
    pawn_c = _pawn("C", '<innerList>' + _item(3) + '</innerList>')
    xml = ('<savegame><meta><gameVersion>1.6.4871 rev591</gameVersion></meta><game>'
           '<maps><li><uniqueID>0</uniqueID><things>' + pawn_a + pawn_b + pawn_c +
           '</things></li></maps></game></savegame>')
    source = tmp_path / "synthetic.rws"
    source.write_text(xml)
    service = QueryService(load_snapshot(source))
    pawn = next(r for r in service.snapshot.records if r.type_name == "pawn" and r.saved_id == "A")
    response = service.execute(QueryRequest("inventory", {"record_id": pawn.record_id,
                                                           "estimate_missing_as_one": True})).as_dict()
    data = response["data"]
    expected_owned = int(owned_units is not None)
    assert data["entry_count"] == reference_count + expected_owned
    assert data["item_record_count"] == expected_owned
    assert data["resolved_item_count"] == expected_owned
    assert data["quantity"]["explicit_stack_units"] == (owned_units or 0)
    assert data["quantity"]["unresolved_item_entries"] == reference_count
    assert data["quantity"]["estimated_stack_units"] is None
    refs = [e for e in data["entries"] if e["entry_kind"] == "reference"]
    assert len(refs) == reference_count
    assert all(e["reference"]["status"] == "ambiguous" for e in refs)
    assert all(len(e["reference"]["target_record_ids"]) >= 2 for e in refs)
    assert response["coverage"]["status"] == "partial"
