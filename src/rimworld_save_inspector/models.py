"""Immutable public and normalized domain models."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    if isinstance(value, set):
        return frozenset(freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, frozenset)):
        return [thaw(v) for v in value]
    return value


def record_id_for_path(type_name: str, canonical_xml_path: str) -> str:
    if not type_name or not canonical_xml_path.startswith("/"):
        raise ValueError("record type and absolute canonical XML path are required")
    return f"{type_name}:{hashlib.sha256(canonical_xml_path.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ParseLimits:
    input_bytes: int = 536_870_912
    read_chunk_bytes: int = 65_536
    nesting_depth: int = 256
    record_bytes: int = 16_777_216
    normalized_records: int = 250_000
    retained_value_bytes: int = 134_217_728
    deadline_seconds: float = 120.0
    extension_value_bytes: int = 65_536
    diagnostic_examples_per_code: int = 20
    diagnostic_detail_bytes: int = 1_024
    result_bytes: int = 524_288

    def __post_init__(self) -> None:
        maxima = {
            "input_bytes": 536_870_912, "read_chunk_bytes": 65_536, "nesting_depth": 256,
            "record_bytes": 16_777_216, "normalized_records": 250_000,
            "retained_value_bytes": 134_217_728, "extension_value_bytes": 65_536,
            "diagnostic_examples_per_code": 20, "diagnostic_detail_bytes": 1_024,
            "result_bytes": 524_288,
        }
        for name in (
            "input_bytes", "read_chunk_bytes", "nesting_depth", "record_bytes",
            "normalized_records", "retained_value_bytes", "extension_value_bytes",
            "diagnostic_examples_per_code", "diagnostic_detail_bytes", "result_bytes",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
            if getattr(self, name) > maxima[name]:
                raise ValueError(f"{name} cannot exceed the specified default")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        if self.deadline_seconds > 120:
            raise ValueError("deadline_seconds cannot exceed the specified default")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: str
    message: str
    source_path: str | None = None
    record_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", freeze(self.details))


@dataclass(frozen=True, slots=True)
class Record:
    record_id: str
    type_name: str
    saved_id: str | None
    source_path: str
    values: Mapping[str, Any] = field(default_factory=dict)
    field_status: Mapping[str, str] = field(default_factory=dict)
    raw_values: Mapping[str, str] = field(default_factory=dict)
    derivations: tuple[Mapping[str, Any], ...] = ()
    owner_record_id: str | None = None
    scope: str | None = None
    memberships: tuple[Mapping[str, Any], ...] = ()
    ordinal: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", freeze(self.values))
        object.__setattr__(self, "field_status", freeze(self.field_status))
        object.__setattr__(self, "raw_values", freeze(self.raw_values))
        object.__setattr__(self, "derivations", tuple(freeze(x) for x in self.derivations))
        object.__setattr__(self, "memberships", tuple(freeze(x) for x in self.memberships))

    def value(self, *paths: str, default: Any = None) -> Any:
        for path in paths:
            if path in self.values:
                return self.values[path]
        return default


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_id: str
    interpretation_id: str
    source_bytes: int
    source_name: str
    game_version: str | None
    profile_version: str
    parser_version: str
    records: tuple[Record, ...]
    globals: Mapping[str, Any] = field(default_factory=dict)
    global_status: Mapping[str, str] = field(default_factory=dict)
    collection_states: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()
    diagnostic_counts: Mapping[str, int] = field(default_factory=dict)
    auxiliary_records: tuple[Record, ...] = ()
    direct_map_candidate_count: int = 0
    direct_map_candidate_map_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "globals", freeze(self.globals))
        object.__setattr__(self, "global_status", freeze(self.global_status))
        object.__setattr__(self, "collection_states", freeze(self.collection_states))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "diagnostic_counts", freeze(self.diagnostic_counts))
        object.__setattr__(self, "auxiliary_records", tuple(self.auxiliary_records))

    def of_type(self, type_name: str) -> tuple[Record, ...]:
        return tuple(record for record in self.records if record.type_name == type_name)


@dataclass(frozen=True, slots=True)
class QueryRequest:
    name: str
    filters: Mapping[str, Any] = field(default_factory=dict)
    limit: int | None = None
    cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "filters", freeze(self.filters))


@dataclass(frozen=True, slots=True)
class QueryResponse:
    schema_version: str
    snapshot_id: str
    query: Mapping[str, Any]
    data: Mapping[str, Any]
    coverage: Mapping[str, Any]
    provenance: Mapping[str, Any]
    page: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        for name in ("query", "data", "coverage", "provenance"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        if self.page is not None:
            object.__setattr__(self, "page", freeze(self.page))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "query": thaw(self.query),
            "data": thaw(self.data),
            "coverage": thaw(self.coverage),
            "provenance": thaw(self.provenance),
            "page": thaw(self.page) if self.page is not None else None,
        }
