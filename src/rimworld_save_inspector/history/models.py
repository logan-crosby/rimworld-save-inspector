"""Stable JSON models and identifiers for one history source snapshot."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import InitVar, dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence


TOP_LEVEL_KEYS = (
    "snapshot_id",
    "interpretation_id",
    "metadata",
    "pawns",
    "events",
    "context",
    "evidence",
    "coverage",
    "diagnostics",
)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically while preserving Unicode and array order."""
    try:
        serialized = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("history IDs require finite JSON values") from exc
    return serialized.encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _freeze(value: Any, check_work: Callable[[], None] | None = None) -> Any:
    if check_work is not None:
        check_work()
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item, check_work) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, check_work) for item in value)
    return value


def _freeze_lazy(value: Any) -> Any:
    if isinstance(value, _LazyFrozenMapping | _LazyFrozenSequence):
        return value
    if isinstance(value, Mapping):
        return _LazyFrozenMapping(value)
    if isinstance(value, (list, tuple)):
        return _LazyFrozenSequence(value)
    return value


class _LazyFrozenMapping(Mapping):
    """Read-only lazy view for trusted source-owned structures."""

    __slots__ = ("_value", "_cache")

    def __init__(self, value: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_cache", {})

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("history snapshots are immutable")

    def __getitem__(self, key: str) -> Any:
        cache = object.__getattribute__(self, "_cache")
        if key not in cache:
            cache[key] = _freeze_lazy(object.__getattribute__(self, "_value")[key])
        return cache[key]

    def __iter__(self):
        return iter(object.__getattribute__(self, "_value"))

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_value"))


class _LazyFrozenSequence(Sequence):
    """Read-only lazy sequence view for trusted source-owned structures."""

    __slots__ = ("_value", "_cache")

    def __init__(self, value: Sequence[Any]) -> None:
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_cache", {})

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("history snapshots are immutable")

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(_freeze_lazy(item) for item in object.__getattribute__(self, "_value")[index])
        cache = object.__getattribute__(self, "_cache")
        if index not in cache:
            cache[index] = _freeze_lazy(object.__getattribute__(self, "_value")[index])
        return cache[index]

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_value"))


def _thaw(value: Any) -> Any:
    # Source-owned containers already hold ordinary JSON data. Reading through
    # their lazy views would allocate and cache a wrapper for every nested item
    # solely to copy it into the expanded wire result.
    kind = type(value)
    if kind is _LazyFrozenMapping or kind is _LazyFrozenSequence:
        return _thaw(object.__getattribute__(value, "_value"))
    if kind is dict:
        return {str(key): _thaw(item) for key, item in value.items()}
    if kind is list or kind is tuple:
        return [_thaw(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_thaw(item) for item in value]
    return deepcopy(value)


def evidence_id_for_node(node: Mapping[str, Any]) -> str:
    """Return a content ID independent of the source path and snapshot."""
    return _sha256(node)


def source_ref_for(snapshot_id: str, interpretation_id: str, canonical_xml_path: str) -> str:
    """Return the exact occurrence ID for a source path in one interpretation."""
    if len(snapshot_id) != 64 or any(c not in "0123456789abcdef" for c in snapshot_id):
        raise ValueError("snapshot_id must be a lowercase SHA-256 hex digest")
    if not interpretation_id or not canonical_xml_path.startswith("/"):
        raise ValueError("interpretation and absolute canonical XML path are required")
    return _sha256([snapshot_id, interpretation_id, canonical_xml_path])


def record_id_for_occurrence(
    snapshot_id: str,
    interpretation_id: str,
    record_kind: str,
    canonical_xml_path: str,
) -> str:
    """Return a snapshot-scoped record ID; never use it as cross-save identity."""
    if not record_kind:
        raise ValueError("record_kind is required")
    return _sha256([snapshot_id, interpretation_id, record_kind, canonical_xml_path])


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    snapshot_id: str
    interpretation_id: str
    metadata: Mapping[str, Any]
    pawns: Sequence[Mapping[str, Any]]
    events: Sequence[Mapping[str, Any]]
    context: Mapping[str, Any]
    evidence: Sequence[Mapping[str, Any]]
    coverage: Mapping[str, Any]
    diagnostics: Sequence[Mapping[str, Any]]
    check_work: InitVar[Callable[[], None] | None] = None
    trusted_immutable_inputs: InitVar[bool] = False

    def __post_init__(self, check_work: Callable[[], None] | None,
                     trusted_immutable_inputs: bool) -> None:
        freeze = _freeze_lazy if trusted_immutable_inputs else lambda value: _freeze(value, check_work)
        object.__setattr__(self, "metadata", freeze(self.metadata))
        object.__setattr__(self, "pawns", freeze(self.pawns))
        object.__setattr__(self, "events", freeze(self.events))
        object.__setattr__(self, "context", freeze(self.context))
        object.__setattr__(self, "evidence", freeze(self.evidence))
        object.__setattr__(self, "coverage", freeze(self.coverage))
        object.__setattr__(self, "diagnostics", freeze(self.diagnostics))

    def as_dict(self) -> dict[str, Any]:
        """Return the exact frozen nine-key wire envelope as ordinary JSON data."""
        return {
            "snapshot_id": self.snapshot_id,
            "interpretation_id": self.interpretation_id,
            "metadata": _thaw(self.metadata),
            "pawns": _thaw(self.pawns),
            "events": _thaw(self.events),
            "context": _thaw(self.context),
            "evidence": _thaw(self.evidence),
            "coverage": _thaw(self.coverage),
            "diagnostics": _thaw(self.diagnostics),
        }


@dataclass(slots=True)
class XmlNode:
    """Bounded in-memory representation of one captured XML subtree."""

    tag: str
    path: str
    attributes: list[list[str]]
    text: str | None = None
    children: list[tuple[XmlNode, str | None]] | None = None

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = []

    def as_node(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "attributes": [list(pair) for pair in self.attributes],
            "text": self.text,
            "children": [
                {"node": child.as_node(), "tail": tail}
                for child, tail in self.children or []
            ],
        }
