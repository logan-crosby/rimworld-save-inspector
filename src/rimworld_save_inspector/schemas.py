"""Strict JSON Schema contracts for the v1 command line response API.

The schema objects are ordinary Python dictionaries so callers can validate
responses with any Draft 2020-12 implementation. They are also copied into
``schemas/*.json`` package resources during wheel builds.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def _object(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


def _array(item: Any) -> dict[str, Any]:
    return {"type": "array", "items": item}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


def _nullable(schema: Any) -> dict[str, Any]:
    if isinstance(schema, dict) and "type" in schema:
        result = deepcopy(schema)
        result["type"] = [schema["type"], "null"]
        return result
    return {"anyOf": [schema, {"type": "null"}]}


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}
BOOLEAN = {"type": "boolean"}
NULLABLE_STRING = _nullable(STRING)
NULLABLE_INTEGER = _nullable(INTEGER)
NULLABLE_NUMBER = _nullable(NUMBER)
NULLABLE_BOOLEAN = _nullable(BOOLEAN)
JSON_SCALAR = {"type": ["string", "number", "integer", "boolean", "null"]}
ID = {"type": "string", "pattern": "^[a-z_]+:[0-9a-f]{64}$"}
SNAPSHOT_ID = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
INTERPRETATION_ID = {"type": "string", "pattern": "^interpretation:[0-9a-f]{64}$"}

_DEFS: dict[str, Any] = {
    "ValueMeta": _object({
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING},
        "derivations": _array(_ref("Derivation")),
    }),
    "Derivation": _object({"field": STRING, "rule": STRING, "inputs": _array(STRING)}),
    "Record": _object({"record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
                        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
                        "raw_values": {"type": "object", "additionalProperties": STRING},
                        "derivations": _array(_ref("Derivation"))}),
    "Reference": _object({
        "raw": NULLABLE_STRING, "namespace": NULLABLE_STRING, "normalized_id": NULLABLE_STRING,
        "status": {"enum": ["absent", "explicit_null", "invalid", "unresolved", "ambiguous", "resolved"]},
        "target_record_ids": _array(ID), "rule": NULLABLE_STRING,
    }),
    "Membership": _object({"scope": STRING, "map_id": NULLABLE_STRING, "pool": NULLABLE_STRING,
                            "holder_id": NULLABLE_STRING, "source_path": STRING}),
    "Name": _object({
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "first": NULLABLE_STRING, "nick": NULLABLE_STRING, "last": NULLABLE_STRING,
        "simple": NULLABLE_STRING, "display": STRING,
    }),
    "PawnIndex": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "pawn_def": NULLABLE_STRING, "kind_def": NULLABLE_STRING, "name": _ref("Name"), "faction": _ref("Reference"),
        "memberships": _array(_ref("Membership")), "guest_status": NULLABLE_STRING,
    }),
    "PawnCore": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "pawn_def": NULLABLE_STRING, "kind_def": NULLABLE_STRING, "name": _ref("Name"), "faction": _ref("Reference"),
        "memberships": _array(_ref("Membership")), "guest_status": NULLABLE_STRING,
        "gender": NULLABLE_STRING, "age_biological_ticks": NULLABLE_INTEGER, "birth_abs_ticks": NULLABLE_INTEGER,
        "host_faction": _ref("Reference"), "slave_faction": _ref("Reference"),
        "child_counts": {"type": "object", "properties": {key: NULLABLE_INTEGER for key in ("skills", "traits", "hediffs", "endogenes", "xenogenes", "equipment", "apparel", "inventory")}, "required": ["skills", "traits", "hediffs", "endogenes", "xenogenes", "equipment", "apparel", "inventory"], "additionalProperties": False},
    }),
    "Skill": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "skill_def": NULLABLE_STRING, "level": NULLABLE_INTEGER, "passion": NULLABLE_STRING,
        "xp_since_last_level": NULLABLE_NUMBER, "xp_since_midnight": NULLABLE_NUMBER,
    }),
    "Trait": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "trait_def": NULLABLE_STRING, "degree": NULLABLE_INTEGER, "source_gene": _ref("Reference"), "suppressed_by": _ref("Reference"),
    }),
    "BodyPart": _object({
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "body": NULLABLE_STRING, "index": NULLABLE_INTEGER,
    }),
    "Hediff": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "hediff_def": NULLABLE_STRING, "saved_class": NULLABLE_STRING, "severity": NULLABLE_NUMBER,
        "age_ticks": NULLABLE_INTEGER, "tick_added": NULLABLE_INTEGER, "visible": NULLABLE_BOOLEAN,
        "chemical": NULLABLE_STRING, "part": _nullable(_ref("BodyPart")),
    }),
    "Gene": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "gene_def": NULLABLE_STRING, "pawn": _ref("Reference"), "overridden_by": _ref("Reference"),
    }),
    "GeneHeader": _object({
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "xenotype": NULLABLE_STRING, "xenotype_name": NULLABLE_STRING, "hybrid": NULLABLE_BOOLEAN, "icon_def": NULLABLE_STRING,
    }),
    "Item": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "item_def": NULLABLE_STRING, "saved_class": NULLABLE_STRING, "stuff": NULLABLE_STRING, "quality": NULLABLE_STRING,
        "hit_points": NULLABLE_INTEGER, "stack_count": NULLABLE_INTEGER, "position_raw": NULLABLE_STRING,
        "nested_holders": _array(_ref("HolderSummary")),
    }),
    "HolderSummary": _object({"holder_id": STRING, "collection_state": STRING, "entry_count": NULLABLE_INTEGER}),
    "ItemEntry": {
        "oneOf": [
            _object({"entry_kind": {"const": "record"}, "source_path": STRING, "item": _ref("Item"), "reference": {"type": "null"}, "pawn_record_id": {"type": "null"}}),
            _object({"entry_kind": {"const": "reference"}, "source_path": STRING, "item": {"type": "null"}, "reference": _ref("Reference"), "pawn_record_id": NULLABLE_STRING}),
        ]
    },
    "Quantity": _object({
        "explicit_stack_units": NULLABLE_INTEGER, "missing_stack_count_records": NULLABLE_INTEGER,
        "null_stack_count_records": NULLABLE_INTEGER, "invalid_stack_count_records": NULLABLE_INTEGER,
        "unresolved_item_entries": NULLABLE_INTEGER, "estimated_stack_units": NULLABLE_INTEGER,
    }),
    "Progress": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "ordinal": INTEGER, "project_def": NULLABLE_STRING, "progress_points": NULLABLE_NUMBER,
        "key_source_path": STRING, "value_source_path": STRING,
    }),
    "Techprint": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "ordinal": INTEGER, "project_def": NULLABLE_STRING, "techprint_count": NULLABLE_INTEGER,
        "key_source_path": STRING, "value_source_path": STRING,
    }),
    "Mod": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "ordinal": INTEGER, "package_id": NULLABLE_STRING, "display_name": NULLABLE_STRING,
        "id_source_path": STRING, "name_source_path": STRING,
    }),
    "Unpaired": _object({
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "side": STRING, "ordinal": INTEGER, "value": NULLABLE_STRING, "source_path": STRING,
    }),
    "MapSummary": _object({
        "record_id": ID, "saved_id": NULLABLE_STRING, "source_path": STRING,
        "field_status": {"type": "object", "additionalProperties": {"enum": ["present", "absent", "explicit_null", "invalid"]}},
        "raw_values": {"type": "object", "additionalProperties": STRING}, "derivations": _array(_ref("Derivation")),
        "map_id": NULLABLE_STRING, "parent": _ref("Reference"), "parent_label": NULLABLE_STRING, "parent_def": NULLABLE_STRING,
        "size_raw": NULLABLE_STRING, "things_state": STRING, "direct_thing_count": NULLABLE_INTEGER, "direct_pawn_count": NULLABLE_INTEGER,
    }),
    "Coverage": _object({
        "status": {"enum": ["complete_for_scope", "partial"]}, "scope": STRING,
        "diagnostic_counts": {"type": "object", "additionalProperties": INTEGER},
        "diagnostics": _array(_ref("Diagnostic")),
    }),
    "Diagnostic": _object({
        "code": STRING, "severity": STRING, "message": STRING, "source_path": NULLABLE_STRING,
        "record_id": _nullable(ID), "details": {"type": "object"},
    }),
    "Population": _object({
        "direct_map_pawn_records": NULLABLE_INTEGER, "human_def_records": NULLABLE_INTEGER, "player_human_records": NULLABLE_INTEGER,
        "map_guest_status_counts": _nullable({"type": "object", "additionalProperties": INTEGER}),
        "map_health_state_counts": _nullable({"type": "object", "additionalProperties": INTEGER}),
        "player_human_guest_status_counts": _nullable({"type": "object", "additionalProperties": INTEGER}),
        "player_human_health_state_counts": _nullable({"type": "object", "additionalProperties": INTEGER}),
    }),
}

_META_PROPS = {
    "field_status": _DEFS["ValueMeta"]["properties"]["field_status"],
    "raw_values": _DEFS["ValueMeta"]["properties"]["raw_values"],
    "derivations": _DEFS["ValueMeta"]["properties"]["derivations"],
}


def _data(properties: dict[str, Any]) -> dict[str, Any]:
    return _object({**_META_PROPS, **properties})


_DATA: dict[str, Any] = {
    "inspect": _data({
        "snapshot_id": SNAPSHOT_ID, "interpretation_id": INTERPRETATION_ID, "source_bytes": INTEGER,
        "game_version": NULLABLE_STRING, "profile_version": STRING,
        "snapshot_lifetime": {"enum": ["process", "invocation"]},
        "record_totals": {"type": "object", "properties": {key: INTEGER for key in ("pawn", "item", "gene", "hediff", "map", "faction", "world_object", "skill", "trait", "research", "mod", "record_count", "membership_count", "resolved_saved_identity_count")}, "required": ["pawn", "item", "gene", "hediff", "map", "faction", "world_object", "skill", "trait", "research", "mod", "record_count", "membership_count", "resolved_saved_identity_count"], "additionalProperties": False},
        "scope_coverage": {"type": "object", "properties": {key: _ref("Coverage") for key in ("map", "world_alive", "world_dead", "world_mothballed", "held", "all_supported")}, "required": ["map", "world_alive", "world_dead", "world_mothballed", "held", "all_supported"], "additionalProperties": False},
        "domain_availability": {"type": "object", "properties": {key: {"enum": ["present", "partial", "absent"]} for key in ("summary", "pawn", "health", "genes", "equipment", "inventory", "research", "mods")}, "required": ["summary", "pawn", "health", "genes", "equipment", "inventory", "research", "mods"], "additionalProperties": False},
    }),
    "summary": _data({
        "game_version": NULLABLE_STRING, "world_name": NULLABLE_STRING, "world_seed": NULLABLE_STRING, "ticks_game": NULLABLE_INTEGER,
        "player_faction": _ref("Reference"), "maps_state": STRING, "map_count": NULLABLE_INTEGER,
        "maps": _array(_ref("MapSummary")), "population": _ref("Population"),
    }),
    "pawns": _data({"collection_state": STRING, "entries": _array(_ref("PawnIndex"))}),
    "pawn": _data({"pawn": _ref("PawnCore")}),
    "pawn_skills": _data({"pawn_record_id": ID, "collection_state": STRING, "entries": _array(_ref("Skill"))}),
    "pawn_traits": _data({"pawn_record_id": ID, "collection_state": STRING, "entries": _array(_ref("Trait"))}),
    "health": _data({"pawn_record_id": ID, "health_state": NULLABLE_STRING, "collection_state": STRING, "entries": _array(_ref("Hediff"))}),
    "genes": _data({
        "pawn_record_id": ID, "header": _ref("GeneHeader"), "group": {"enum": ["endogenes", "xenogenes"]},
        "group_counts": _object({"endogenes": NULLABLE_INTEGER, "xenogenes": NULLABLE_INTEGER}),
        "collection_state": STRING, "entries": _array(_ref("Gene")),
    }),
    "equipment": _data({
        "pawn_record_id": ID, "holder": _ref("HolderSummary"), "collection_state": STRING,
        "entry_count": NULLABLE_INTEGER, "item_record_count": NULLABLE_INTEGER, "resolved_item_count": NULLABLE_INTEGER,
        "quantity": _ref("Quantity"), "entries": _array(_ref("ItemEntry")),
    }),
    "inventory": _data({
        "pawn_record_id": ID, "holder": _ref("HolderSummary"), "collection_state": STRING,
        "entry_count": NULLABLE_INTEGER, "item_record_count": NULLABLE_INTEGER, "resolved_item_count": NULLABLE_INTEGER,
        "quantity": _ref("Quantity"), "entries": _array(_ref("ItemEntry")),
    }),
    "research": _data({
        "current_project": NULLABLE_STRING, "section": {"enum": ["progress", "techprints"]},
        "alignment": {"enum": ["paired", "unpaired"]}, "collection_state": STRING,
        "key_count": NULLABLE_INTEGER, "value_count": NULLABLE_INTEGER,
        "entries": _array({"oneOf": [_ref("Progress"), _ref("Techprint"), _ref("Unpaired")]}),
    }),
    "mods": _data({
        "alignment": {"enum": ["paired", "unpaired"]}, "collection_state": STRING,
        "id_count": NULLABLE_INTEGER, "name_count": NULLABLE_INTEGER,
        "entries": _array({"oneOf": [_ref("Mod"), _ref("Unpaired")]}),
    }),
}

_SUCCESS_PROPERTIES = {
    "schema_version": {"const": "1.0"}, "snapshot_id": SNAPSHOT_ID,
    "query": _object({"name": STRING, "filters": {"type": "object", "additionalProperties": JSON_SCALAR}, "limit": _nullable({"type": "integer", "minimum": 1, "maximum": 200})}),
    "data": None,
    "coverage": _ref("Coverage"),
    "provenance": _object({"source_name": STRING, "game_version": NULLABLE_STRING, "parser_version": STRING,
                            "profile_version": STRING, "interpretation_id": INTERPRETATION_ID,
                            "source_path": STRING}, required=["source_name", "game_version", "parser_version", "profile_version", "interpretation_id"]),
    "page": _nullable(_object({"total": NULLABLE_INTEGER, "returned": INTEGER, "next_cursor": NULLABLE_STRING})),
}


def _success_schema(name: str) -> dict[str, Any]:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://rimworld-save-inspector.invalid/schemas/{name}-success-v1.json",
        **_object(deepcopy(_SUCCESS_PROPERTIES)),
        "$defs": _DEFS,
    }
    allowed_filters: dict[str, Any] = {
        "inspect": {}, "summary": {"map_id": STRING},
        "pawns": {"scope": {"enum": ["map", "world_alive", "world_dead", "world_mothballed", "held", "all_supported"]}, "map_id": STRING, "faction": STRING, "pawn_def": STRING, "name": STRING, "guest_status": STRING},
        "pawn": {"record_id": ID, "section": {"enum": ["core", "skills", "traits"]}},
        "health": {"record_id": ID}, "genes": {"record_id": ID, "group": {"enum": ["endogenes", "xenogenes"]}},
        "equipment": {"record_id": ID, "holder": {"enum": ["equipment", "apparel"]}, "estimate_missing_as_one": BOOLEAN},
        "inventory": {"record_id": ID, "holder_id": NULLABLE_STRING, "estimate_missing_as_one": BOOLEAN},
        "research": {"section": {"enum": ["progress", "techprints"]}, "project_def": STRING}, "mods": {},
    }
    filter_required = {
        "inspect": [], "summary": [], "pawns": ["scope"], "pawn": ["record_id", "section"],
        "health": ["record_id"], "genes": ["record_id", "group"],
        "equipment": ["record_id", "holder"], "inventory": ["record_id", "holder_id"],
        "research": ["section"], "mods": [],
    }
    schema["properties"]["query"] = _object({
        "name": {"const": name},
        "filters": _object(allowed_filters[name], required=filter_required[name]),
        "limit": _SUCCESS_PROPERTIES["query"]["properties"]["limit"],
    })
    if name == "pawn":
        # The query section selects one of three exact data objects. `anyOf`
        # avoids an empty skills/traits page matching both array variants.
        schema["properties"]["data"] = {"anyOf": [_DATA["pawn"], _DATA["pawn_skills"], _DATA["pawn_traits"]]}
    else:
        schema["properties"]["data"] = _DATA[name]
    if name in {"pawns", "health", "genes", "equipment", "inventory", "research", "mods"}:
        schema["properties"]["query"]["properties"]["limit"] = {"type": "integer", "minimum": 1, "maximum": 200}
        schema["properties"]["page"] = _object({"total": NULLABLE_INTEGER, "returned": INTEGER, "next_cursor": NULLABLE_STRING})
    elif name == "pawn":
        # Pawn skill and trait sections are paged; core is scalar.
        schema["properties"]["page"] = _nullable(_object({"total": NULLABLE_INTEGER, "returned": INTEGER, "next_cursor": NULLABLE_STRING}))
    else:
        schema["properties"]["query"]["properties"]["limit"] = {"type": "null"}
        schema["properties"]["page"] = {"type": "null"}
    if name == "pawn":
        page_schema = _object({"total": NULLABLE_INTEGER, "returned": INTEGER, "next_cursor": NULLABLE_STRING})
        limit_schema = {"type": "integer", "minimum": 1, "maximum": 200}
        schema["allOf"] = [
            {
                "if": {"properties": {"query": {"properties": {"filters": {
                    "properties": {"section": {"const": section}}, "required": ["section"]
                }}}}},
                "then": {"properties": {"data": _DATA[data_name], "page": page_schema,
                                            "query": {"properties": {"limit": limit_schema}}}},
            }
            for section, data_name in (("skills", "pawn_skills"), ("traits", "pawn_traits"))
        ] + [
            {
                "if": {"properties": {"query": {"properties": {"filters": {
                    "properties": {"section": {"const": "core"}}, "required": ["section"]
                }}}}},
                "then": {"properties": {"data": _DATA["pawn"], "page": {"type": "null"},
                                            "query": {"properties": {"limit": {"type": "null"}}}}},
            }
        ]
    return schema


SUCCESS_SCHEMAS: dict[str, dict[str, Any]] = {
    name: _success_schema(name) for name in ("inspect", "summary", "pawns", "pawn", "health", "genes", "equipment", "inventory", "research", "mods")
}

ERROR_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://rimworld-save-inspector.invalid/schemas/error-v1.json",
    **_object({
        "schema_version": {"const": "1.0"}, "snapshot_id": _nullable(SNAPSHOT_ID),
        "error": _object({
            "code": {"enum": ["INVALID_ARGUMENT", "UNSUPPORTED_SOURCE", "UNSUPPORTED_FORMAT", "MALFORMED_XML", "UNSAFE_XML", "MCP_EXTRA_REQUIRED", "SOURCE_NOT_FOUND", "SOURCE_UNREADABLE", "SOURCE_CHANGED", "LIMIT_EXCEEDED", "SNAPSHOT_NOT_LOADED", "SNAPSHOT_LOADING", "SNAPSHOT_MISMATCH", "INVALID_CURSOR", "RECORD_NOT_FOUND", "MAP_NOT_FOUND", "AMBIGUOUS_MAP", "HOLDER_NOT_OWNED", "PLAYER_FACTION_UNRESOLVED", "FILTER_UNAVAILABLE", "RESULT_TOO_LARGE", "CANCELLED", "INTERNAL_ERROR"]},
            "message": STRING, "details": {"type": "object"}, "retryable": BOOLEAN,
        }),
    }),
    "allOf": [{
        "if": {
            "properties": {"error": {"properties": {"code": {"enum": ["SOURCE_CHANGED", "SNAPSHOT_LOADING", "CANCELLED"]}}, "required": ["code"]}},
            "required": ["error"],
        },
        "then": {"properties": {"error": {"properties": {"retryable": {"const": True}}}}},
        "else": {"properties": {"error": {"properties": {"retryable": {"const": False}}}}},
    }],
}
