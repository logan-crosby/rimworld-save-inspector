"""Quantity selector defaults must be explicit and bound into cursors."""
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from rimworld_save_inspector import InspectorError, QueryRequest, QueryService, load_snapshot
from rimworld_save_inspector.schemas import SUCCESS_SCHEMAS

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def colony():
    service = QueryService(load_snapshot(ROOT / "tests/fixtures/synthetic/seed.rws"))
    pawn = next(r for r in service.snapshot.records if r.type_name == "pawn" and r.saved_id == "Human1")
    return service, {"record_id": pawn.record_id}


@pytest.mark.parametrize("query,extra,expected_units", [
    ("equipment", {}, 1), ("equipment", {"holder": "apparel"}, 1), ("inventory", {}, 3),
])
def test_quantity_defaults_are_explicit_and_false_matches_omission(colony, query, extra, expected_units):
    service, base = colony
    filters = base | extra
    omitted = service.execute(QueryRequest(query, filters)).as_dict()
    explicit = service.execute(QueryRequest(query, filters | {"estimate_missing_as_one": False})).as_dict()
    estimated = service.execute(QueryRequest(query, filters | {"estimate_missing_as_one": True})).as_dict()
    assert omitted == explicit
    assert omitted["query"]["filters"]["estimate_missing_as_one"] is False
    assert estimated["query"]["filters"]["estimate_missing_as_one"] is True
    assert omitted["data"]["quantity"]["estimated_stack_units"] is None
    assert estimated["data"]["quantity"]["estimated_stack_units"] == expected_units


@pytest.mark.parametrize("first,second", [(False, True), (True, False)])
def test_cursor_rejects_changed_quantity_interpretation(colony, first, second):
    service, base = colony
    page = service.execute(QueryRequest("inventory", base | {"estimate_missing_as_one": first}, limit=1)).as_dict()
    cursor = page["page"]["next_cursor"]
    assert cursor is not None  # Two handwritten inventory entries; no fixture substitution.
    with pytest.raises(InspectorError) as caught:
        service.execute(QueryRequest("inventory", base | {"estimate_missing_as_one": second}, limit=1, cursor=cursor))
    assert caught.value.code == "INVALID_CURSOR"


def test_cursor_accepts_equivalent_omitted_and_explicit_false(colony):
    service, base = colony
    first = service.execute(QueryRequest("inventory", base, limit=1)).as_dict()
    second = service.execute(QueryRequest("inventory", base | {"estimate_missing_as_one": False},
                                          limit=1, cursor=first["page"]["next_cursor"])).as_dict()
    assert second["page"]["returned"] == 1
    assert second["page"]["next_cursor"] is None
    assert second["query"]["filters"]["estimate_missing_as_one"] is False


@pytest.mark.parametrize("query", ["equipment", "inventory"])
def test_response_schema_requires_effective_quantity_selector(colony, query):
    service, base = colony
    response = service.execute(QueryRequest(query, base)).as_dict()
    response["query"]["filters"].pop("estimate_missing_as_one")
    with pytest.raises(ValidationError):
        Draft202012Validator(SUCCESS_SCHEMAS[query]).validate(response)
