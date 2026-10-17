"""Public normative companions: strict schemas, real examples and no private facts."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from rimworld_save_inspector import QueryRequest, QueryService, load_snapshot
from rimworld_save_inspector.errors import InspectorError
from rimworld_save_inspector.schemas import ERROR_SCHEMA, SUCCESS_SCHEMAS

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs/rimworld-save-inspector"


def test_public_schema_companion_matches_every_runtime_schema_constraint():
    companion = json.loads((DOCS / "responses.schema.json").read_text())
    Draft202012Validator.check_schema(companion)
    definitions = companion["$defs"]
    for query, runtime in SUCCESS_SCHEMAS.items():
        expected = {k: v for k, v in runtime.items() if k not in {"$schema", "$id", "$defs"}}
        assert definitions["response_" + query] == expected
        for name, definition in runtime["$defs"].items():
            assert definitions[name] == definition
    assert definitions["response_error"] == {
        k: v for k, v in ERROR_SCHEMA.items() if k not in {"$schema", "$id"}
    }
    assert len(companion["oneOf"]) == len(SUCCESS_SCHEMAS) + 1


def test_public_companion_validates_every_domain_and_rejects_unknown_fields():
    validator = Draft202012Validator(json.loads((DOCS / "responses.schema.json").read_text()))
    service = QueryService(load_snapshot(ROOT / "tests/fixtures/synthetic/seed.rws"))
    pawn = next(r for r in service.snapshot.records if r.type_name == "pawn" and r.scope == "map")
    base = {"record_id": pawn.record_id}
    requests = [QueryRequest(q) for q in ("inspect", "summary", "pawns", "research", "mods")]
    requests += [QueryRequest(q, base) for q in ("pawn", "health", "genes", "equipment", "inventory")]
    requests += [QueryRequest("pawn", base | {"section": section}) for section in ("skills", "traits")]
    requests += [QueryRequest("genes", base | {"group": "xenogenes"}),
                 QueryRequest("equipment", base | {"holder": "apparel"}),
                 QueryRequest("research", {"section": "techprints"})]
    for request in requests:
        value = service.execute(request).as_dict()
        validator.validate(value)
        unknown = copy.deepcopy(value)
        unknown["unexpected_public_field"] = True
        with pytest.raises(ValidationError):
            validator.validate(unknown)
    with pytest.raises(InspectorError) as caught:
        service.execute(QueryRequest("pawn", {"record_id": "pawn:missing"}))
    validator.validate(caught.value.as_dict())


def test_public_private_case_catalog_preserves_required_predicates_without_values():
    catalog = json.loads((DOCS / "private-case-catalog.json").read_text())
    assert catalog["spec_id"] == "RWSI-1.0"
    assert catalog["spec_revision"] == "2026-10-17.1"
    assert catalog["private_baseline_spec_revision"] == "2026-10-16.1"
    assert [row["check_id"] for row in catalog["assertions"]] == [f"F{i:02d}" for i in range(1, 29)]
    assert catalog["required_dynamic_comparisons"] == ["D01", "D02", "D03"]
    for row in catalog["assertions"]:
        assert set(row) == {"check_id", "scope", "predicate", "expected_source"}
        assert row["expected_source"] == "frozen_private_oracle"
        assert row["scope"] and row["predicate"]
    spec = (ROOT / "docs/design.md").read_text()
    for row in catalog["assertions"]:
        assert f'| {row["check_id"]} | {row["predicate"]} |' in spec
