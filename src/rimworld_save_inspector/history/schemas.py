"""Machine-readable history response and field contracts."""

from __future__ import annotations

from typing import Any

VERSION = "1.0"
FIELD_STATUSES = ("observed", "derived", "ambiguous", "absent", "explicit-null",
                  "unsupported", "invalid")
TIME_STATUSES = ("known", "pre_arrival", "future_contradiction", "unknown", "invalid")
CLOCKS = ("absolute_tick", "game_tick", "biological_duration", "wall_clock", "unknown")
DOMAINS = ("identity", "status_and_interaction", "location", "age", "backstory",
           "traits", "skills", "health", "genes", "equipment", "apparel", "inventory",
           "relationships", "abilities", "mechanitor", "ideology", "royalty", "roles",
           "ownership", "work", "needs", "memories", "records", "events", "unknown_fields")


def _obj(properties: dict[str, Any], required: tuple[str, ...] | list[str],
         *, additional: bool | dict[str, Any] = False) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": additional}


STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
COUNT = {"type": "integer", "minimum": 0}
ANY_JSON = {"type": ["object", "array", "string", "number", "integer", "boolean", "null"]}
STRING_ARRAY = {"type": "array", "items": STRING}
JSON_MAP = {"type": "object", "additionalProperties": ANY_JSON}

SIZE_SCHEMA = _obj({
    "serialized_bytes": COUNT, "configured_limit": COUNT, "absolute_limit": COUNT,
    "semantic_complete": {"type": "boolean"}, "raw_detail_coverage": STRING,
}, ("serialized_bytes", "configured_limit", "absolute_limit", "semantic_complete",
    "raw_detail_coverage"))

CATALOG_INFO_SCHEMA = _obj({
    "namespace_id": STRING, "catalog_id": STRING, "generation_id": STRING,
    "catalog_basename": STRING, "freshness": STRING,
    "discovery_started": {"type": ["number", "null"]},
    "discovery_ended": {"type": ["number", "null"]},
    "files_reused": COUNT, "files_rehashed": COUNT,
    "groups": {"type": "object", "additionalProperties": STRING_ARRAY},
    "campaign_id": NULLABLE_STRING, "anchor_snapshot_id": STRING,
}, ("namespace_id", "catalog_id", "generation_id", "freshness"),
   additional=False)

COVERAGE_SCHEMA = _obj({
    "candidate_count": COUNT, "parsed_count": COUNT, "failed_count": COUNT,
    "removed_count": COUNT, "snapshot_count": COUNT, "duplicate_origin_count": COUNT,
    "selected_snapshot_count": COUNT, "materialized_observation_count": COUNT,
    "compatible_snapshot_count": COUNT, "incompatible_snapshot_count": COUNT,
    "review_needed_snapshot_count": COUNT,
    "domain_filter": {"type": ["array", "null"], "items": STRING},
    "unknown_clock_count": COUNT,
    "domain_status_counts": {"type": "object", "additionalProperties": {
        "type": "object", "additionalProperties": COUNT}},
    "earliest_observed_tick": {"type": ["integer", "null"]},
    "latest_observed_tick": {"type": ["integer", "null"]},
    "omitted_detail_count": COUNT,
    "journal": JSON_MAP,
}, ("candidate_count", "parsed_count", "failed_count"), additional=False)

EVIDENCE_REF_SCHEMA = _obj({
    "evidence_id": STRING, "content_id": STRING, "source_ref": STRING,
}, ("evidence_id", "content_id", "source_ref"))

CATALOG_SCHEMA = _obj({
    "history_schema_version": {"const": VERSION},
    "catalog": CATALOG_INFO_SCHEMA,
    "coverage": COVERAGE_SCHEMA,
    "sources": {"type": "array", "items": _obj({
        "path": STRING, "source_hash": NULLABLE_STRING, "status": STRING,
        "error_code": NULLABLE_STRING, "snapshot_id": NULLABLE_STRING,
        "interpretation_id": NULLABLE_STRING,
    }, ("path", "source_hash", "status", "error_code", "snapshot_id",
        "interpretation_id"))},
}, ("history_schema_version", "catalog", "coverage", "sources"))

TIMELINE_SCHEMA = _obj({
    "history_schema_version": {"const": VERSION},
    "detail": {"enum": ["context", "full"]},
    "catalog": CATALOG_INFO_SCHEMA,
    "subject": _obj({
        "pawn_id": STRING, "aliases": {"type": "array", "items": JSON_MAP},
        "first_observed_tick": {"type": ["integer", "null"]},
        "last_observed_tick": {"type": ["integer", "null"]},
        "ambiguity": {"type": "boolean"},
    }, ("pawn_id", "aliases", "first_observed_tick", "last_observed_tick", "ambiguity")),
    "overview": _obj({
        "phases": {"type": "array", "items": JSON_MAP}, "basis": STRING,
    }, ("phases", "basis")),
    "timeline": _obj({
        "lineage_status": {"enum": ["unproven", "asserted"]},
        "recorded_events": {"type": "array", "items": JSON_MAP},
        "unknown_time_events": {"type": "array", "items": JSON_MAP},
        "state_observations": {"type": "array", "items": JSON_MAP},
        "observed_differences": {"type": "array", "items": JSON_MAP},
        "inferences": {"type": "array", "items": JSON_MAP},
        "same_tick_alternatives": {"type": "array", "items": STRING_ARRAY},
        "event_conflicts": {"type": "array", "items": JSON_MAP},
        "journal_events": {"type": "array", "items": JSON_MAP},
        "journal_conflicts": {"type": "array", "items": JSON_MAP},
        "journal_sessions": {"type": "array", "items": JSON_MAP},
        "expanded_differences": {"type": "array", "items": JSON_MAP},
    }, ("lineage_status", "recorded_events", "unknown_time_events",
        "state_observations", "observed_differences", "inferences",
        "same_tick_alternatives", "event_conflicts")),
    "context": _obj({
        "membership_encoding": {"const": "context/1"},
        "facts": {"type": "object", "additionalProperties": JSON_MAP},
        "fact_index": STRING_ARRAY,
        "evidence_pairs": {"type": "array", "items": STRING_ARRAY},
        "memberships": {"type": "array", "items": JSON_MAP},
        "unknown_inventory_index": {"type": "array", "items": {
            "type": "array", "items": COUNT, "minItems": 2, "maxItems": 2}},
        "latest_dossiers": {"type": "array", "items": JSON_MAP},
        "shared": {"type": "object", "additionalProperties": JSON_MAP},
        "shared_memberships": {"type": "object", "additionalProperties": STRING},
        "expanded_observations": {"type": "array", "items": JSON_MAP},
        "recorder_joins": {"type": "array", "items": JSON_MAP},
    }, ("membership_encoding", "facts", "fact_index", "evidence_pairs",
        "memberships", "unknown_inventory_index", "latest_dossiers", "shared",
        "shared_memberships")),
    "coverage": COVERAGE_SCHEMA,
    "evidence": {"type": "array", "items": EVIDENCE_REF_SCHEMA},
    "size": SIZE_SCHEMA,
}, ("history_schema_version", "detail", "catalog", "subject", "overview",
    "timeline", "context", "coverage", "evidence", "size"))

DOSSIER_SCHEMA = _obj({
    "history_schema_version": {"const": VERSION},
    "detail": {"enum": ["context", "full"]},
    "catalog": CATALOG_INFO_SCHEMA,
    "subject": _obj({
        "pawn_id": STRING, "snapshot_id": STRING, "ambiguity": {"type": "boolean"},
    }, ("pawn_id", "snapshot_id", "ambiguity")),
    "overview": _obj({"materialized_records": COUNT}, ("materialized_records",)),
    "dossier": _obj({
        "pawns": {"type": "array", "items": JSON_MAP},
        "context": JSON_MAP, "events": {"type": "array", "items": JSON_MAP},
    }, ("pawns", "context", "events")),
    "coverage": COVERAGE_SCHEMA,
    "evidence": {"type": "array", "items": EVIDENCE_REF_SCHEMA},
    "size": SIZE_SCHEMA,
}, ("history_schema_version", "detail", "catalog", "subject", "overview",
    "dossier", "coverage", "evidence", "size"))

EVIDENCE_SCHEMA = _obj({
    "history_schema_version": {"const": VERSION},
    "catalog": _obj({"namespace_id": STRING, "generation_id": STRING},
                    ("namespace_id", "generation_id")),
    "evidence_id": STRING, "content_id": STRING, "source_ref": STRING,
    "snapshot_id": STRING, "interpretation_id": STRING, "path": STRING,
    "total_bytes": COUNT, "offset": COUNT, "next_cursor": NULLABLE_STRING,
    "node": {"type": ["object", "null"]},
    "fragment_base64": NULLABLE_STRING, "encoding": {"const": "utf-8-json"},
    "size": SIZE_SCHEMA,
}, ("history_schema_version", "catalog", "evidence_id", "content_id",
    "source_ref", "snapshot_id", "interpretation_id", "path", "total_bytes",
    "offset", "next_cursor", "node", "fragment_base64", "encoding", "size"))

EVENTS_SCHEMA = _obj({
    "history_schema_version": {"const": VERSION}, "catalog": CATALOG_INFO_SCHEMA,
    "selection": _obj({"campaign_id": NULLABLE_STRING, "anchor_snapshot_id": STRING,
                       "identity_basis": {"enum": ["validated_campaign", "snapshot_only"]},
                       "snapshot_id": NULLABLE_STRING, "saved_event_id": NULLABLE_STRING},
                      ("campaign_id", "anchor_snapshot_id", "identity_basis", "snapshot_id", "saved_event_id")),
    "events": {"type": "array", "items": _obj({
        "snapshot_id": STRING, "event_index": COUNT,
        "game_tick": {"type": ["integer", "null"]},
        "ticks_abs": {"type": ["integer", "null"]}, "event": JSON_MAP,
        "evidence": {"type": "array", "items": EVIDENCE_REF_SCHEMA},
    }, ("snapshot_id", "event_index", "game_tick", "ticks_abs", "event", "evidence"))},
    "page": _obj({"limit": {"type": "integer", "minimum": 1, "maximum": 500},
                  "returned_occurrences": COUNT, "matching_occurrences": COUNT,
                  "remaining_occurrences": COUNT, "next_cursor": NULLABLE_STRING,
                  "ordering": {"const": "snapshot_id,event_index; not chronology"},
                  "complete_selection": {"type": "boolean"}},
                 ("limit", "returned_occurrences", "matching_occurrences",
                  "remaining_occurrences", "next_cursor", "ordering", "complete_selection")),
    "coverage": COVERAGE_SCHEMA, "size": SIZE_SCHEMA,
}, ("history_schema_version", "catalog", "selection", "events", "page", "coverage", "size"))

DISCOVERY_SCHEMA = _obj({
    "discovery_schema_version": {"const": "history-discovery/1.0"},
    "catalog": JSON_MAP, "source_channel": JSON_MAP, "linked_artifacts": JSON_MAP,
    "sources": {"type": "array", "items": JSON_MAP},
    "source_outcomes": {"type": "array", "items": JSON_MAP},
    "entities": {"type": "array", "items": JSON_MAP},
    "episodes": {"type": "array", "items": JSON_MAP},
    "clock_observations": {"type": "array", "items": JSON_MAP},
    "support_and_counterevidence": {"type": "array", "items": JSON_MAP},
    "coverage": JSON_MAP, "selection": JSON_MAP, "followup_template": JSON_MAP,
    "evidence_use": JSON_MAP, "uncertainty": JSON_MAP,
    "semantic_cache_key": NULLABLE_STRING,
    "annotations": {"type": "array", "items": JSON_MAP}, "size": JSON_MAP,
}, ("discovery_schema_version", "catalog", "source_channel", "linked_artifacts", "sources",
    "source_outcomes", "entities", "episodes", "clock_observations", "support_and_counterevidence",
    "coverage", "selection", "followup_template", "evidence_use", "uncertainty",
    "semantic_cache_key", "annotations", "size"))

HISTORY_SCHEMAS = {
    "catalog": CATALOG_SCHEMA,
    "timeline": TIMELINE_SCHEMA,
    "dossier": DOSSIER_SCHEMA,
    "evidence": EVIDENCE_SCHEMA,
    "events": EVENTS_SCHEMA,
    "discovery": DISCOVERY_SCHEMA,
}


def get_history_schema() -> dict[str, Any]:
    """Describe stable field meanings, clock rules, limits and response schemas."""
    return {
        "history_schema_version": VERSION,
        "field_statuses": list(FIELD_STATUSES),
        "time_statuses": list(TIME_STATUSES),
        "clocks": list(CLOCKS),
        "domains": list(DOMAINS),
        "limits": {"context_default_bytes": 16 * 1024 * 1024,
                   "full_default_bytes": 8 * 1024 * 1024,
                   "evidence_page_bytes": 512 * 1024,
                   "absolute_response_bytes": 16 * 1024 * 1024},
        "clock_rules": {
            "absolute_tick": "integer(raw)-gameStartAbsTick",
            "game_tick": "integer(raw)",
            "biological_duration": "duration; never an event timestamp",
            "wall_clock": "provenance only; never game chronology",
            "unknown": "no normalized tick",
        },
        "schemas": HISTORY_SCHEMAS,
    }
