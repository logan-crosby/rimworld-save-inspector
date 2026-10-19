"""Secure, bounded one-pass XML source extraction for pawn-history snapshots."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any
from xml.sax.handler import ContentHandler

from defusedxml.expatreader import DefusedExpatParser

from ..errors import InspectorError
from ..models import ParseLimits
from .domains import (
    DOMAIN_NAMES,
    EvidenceCollector,
    build_pawn_domains,
    context_claim,
    context_entity,
    semantic_name,
)
from .models import HistorySnapshot, XmlNode, canonical_json_bytes, record_id_for_occurrence

SCHEMA_VERSION = "history/1.0"
PARSER_VERSION = "expat-defused/1"
# v4 gates semantic fields and pawn classes by exact QName/class. v5 also
# materializes validated native Pawn descendants inside retained map-context
# captures, such as visitor-manager passing ships. v6 restricts holder
# semantics to exact native classes and preserves lookalike classes as raw.
# v7 selects profiles from native meta/gameVersion before the legacy root attribute.
EXTRACTION_VERSION = "7"
VERIFIED_1_6_PROFILE = "rimworld-1.6/v1"
INTERPRETATION_ID_FOR_VERIFIED_1_6 = None  # initialized after helper definition

_NATIVE_HOLDER_CLASS_KINDS = {
    "Corpse": "corpse",
    "Verse.Corpse": "corpse",
    "Building_CryptosleepCasket": "cryptosleep_casket",
    "RimWorld.Building_CryptosleepCasket": "cryptosleep_casket",
    "Building_GrowthVat": "growth_vat",
    "RimWorld.Building_GrowthVat": "growth_vat",
}


def _native_holder_kind(class_name: str, tag: str) -> str | None:
    """Return a holder semantic only for an exact validated native class."""
    if tag.startswith("{"):
        return None
    return _NATIVE_HOLDER_CLASS_KINDS.get(class_name)


def _unverified_holder_lookalike(class_name: str, tag: str) -> bool:
    """Select likely holder subclasses for bounded raw retention, never semantics."""
    if not class_name:
        return False
    if class_name in _NATIVE_HOLDER_CLASS_KINDS:
        return tag.startswith("{")
    basename = class_name.rsplit(".", 1)[-1]
    return (basename == "Corpse" or basename.startswith("Building_CryptosleepCasket")
            or basename.startswith("Building_GrowthVat"))


def interpretation_id_for_profile(profile_id: str) -> str:
    """Stable interpretation fingerprint available without opening a save."""
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("profile_id must be a non-empty string")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "extraction_version": EXTRACTION_VERSION,
        "profile_id": profile_id,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


INTERPRETATION_ID_FOR_VERIFIED_1_6 = interpretation_id_for_profile(VERIFIED_1_6_PROFILE)


class HistoryLimits:
    """Small public limits adapter that preserves the v1 source limits contract."""

    __slots__ = ("input_bytes", "read_chunk_bytes", "nesting_depth", "record_bytes", "normalized_records", "retained_value_bytes", "deadline_seconds")

    def __init__(self, base: ParseLimits | None = None) -> None:
        base = base or ParseLimits()
        self.input_bytes = base.input_bytes
        self.read_chunk_bytes = base.read_chunk_bytes
        self.nesting_depth = base.nesting_depth
        self.record_bytes = base.record_bytes
        self.normalized_records = base.normalized_records
        self.retained_value_bytes = base.retained_value_bytes
        self.deadline_seconds = base.deadline_seconds


class _TreeHandler(ContentHandler):
    """Bounded SAX tree builder preserving QName-expanded paths and mixed text."""

    def __init__(self, limits: HistoryLimits, cancel: threading.Event | None) -> None:
        super().__init__()
        self.limits = limits
        self.cancel = cancel
        self.started = time.monotonic()
        self.stack: list[XmlNode] = []
        self.stack_local_names: list[str] = []
        self.record_markers: list[bool] = []
        self.child_counts: list[dict[str, int]] = []
        self.root: XmlNode | None = None
        self.capture_root: XmlNode | None = None
        self.capture_kind: str | None = None
        self.capture_ancestors: tuple[XmlNode, ...] = ()
        self.captures: list[tuple[str, XmlNode, tuple[XmlNode, ...]]] = []
        self.context_starts: list[tuple[str, XmlNode, tuple[XmlNode, ...]]] = []
        self.seen_context_domains: set[str] = set()
        self.seen_event_families: set[str] = set()
        self.capture_bytes = 0
        self.retained_bytes = 0
        self.record_sizes: list[int] = []
        self.logical_records = 0
        self._callbacks = 0

    @staticmethod
    def _expanded(name: tuple[str, str] | str) -> str:
        if isinstance(name, tuple):
            uri, local = name
            return "{{{}}}{}".format(uri, local) if uri else local
        return name

    def _check(self, *, force_deadline: bool = False) -> None:
        self._callbacks += 1
        if self.cancel is not None and self.cancel.is_set():
            raise InspectorError("CANCELLED", "History extraction was cancelled.")
        if (force_deadline or self._callbacks % 64 == 0) and time.monotonic() - self.started > self.limits.deadline_seconds:
            raise InspectorError("LIMIT_EXCEEDED", "The history extraction deadline was exceeded.", {"limit": "deadline_seconds"})

    @staticmethod
    def _is_holder_ancestor(node: XmlNode, ancestors: list[str], name: str) -> bool:
        class_name = dict(node.attributes).get("Class", "")
        return (
            (name in {"li", "map", "Map"} and "maps" in ancestors)
            or (name == "li" and "caravans" in ancestors)
            or name == "transportShip"
            or _native_holder_kind(class_name, node.tag) is not None
        )

    @staticmethod
    def _context_capture_kind(local: str, class_name: str, ancestors: list[str], tag: str) -> str | None:
        parent = ancestors[-1] if ancestors else ""
        if local == "scenario" and "game" in ancestors and "scenario" not in ancestors:
            return "context_scenario"
        if "maps" in ancestors and "things" not in ancestors:
            if local in {"def", "name", "nameInt", "label",
                         "mapName", "parent", "size", "tile", "biome", "terrain"}:
                return "context_map_field"
        if "allFactions" in ancestors and local in {"def", "loadID", "name", "leader"}:
            return "context_faction_field"
        if "meta" in ancestors and local in {"li"} and parent in {"modIds", "modNames"}:
            return "context_mod_entry"
        if "researchManager" in ancestors:
            if local == "currentProj" or (local == "li" and parent in {
                "completedProjects", "knownProjects", "progress", "techprints", "finishedResearch", "keys", "values",
            }):
                return "context_research_entry"
        if class_name in {"CompArt", "RimWorld.CompArt"} and "game" in ancestors:
            return "context_art_component"
        if (local == "li" and class_name and class_name not in {"CompArt", "RimWorld.CompArt"}
                and "art" in class_name.rsplit(".", 1)[-1].lower()
                and parent == "comps" and "game" in ancestors):
            return "context_unknown_art_component"
        if "maps" in ancestors and "things" not in ancestors:
            known_map_fields = {"def", "name", "nameInt", "label", "mapName", "parent", "size",
                                "tile", "biome", "terrain", "uniqueID", "id", "ID"}
            map_list_positions = [index for index, name in enumerate(ancestors)
                                  if name in {"li", "map", "Map"} and index > 0
                                  and ancestors[index - 1] == "maps"]
            direct_map_child = bool(map_list_positions and len(ancestors) == map_list_positions[-1] + 1)
            map_info_child = parent in {"info", "mapInfo"}
            expanded_name = tag.startswith("{")
            if (expanded_name and local not in known_map_fields) or (
                    direct_map_child and local not in known_map_fields | {
                        "info", "mapInfo", "things", "terrainGrid", "edificeGrid", "roofGrid",
                        "fertilityGrid", "temperatureGrid", "fogGrid", "areaManager", "zoneManager",
                        "regionGrid", "resourceCounter", "designationManager", "pathGrid", "reachability",
                        "waterInfo", "weatherManager", "pawns", "listerThings", "cellIndices",
                    }) or (map_info_child and local not in known_map_fields):
                return "context_map_extension"
        return None

    def startElementNS(self, name: tuple[str, str], qname: str | None, attrs: Any) -> None:
        self._check()
        if len(self.stack) >= self.limits.nesting_depth:
            raise InspectorError("LIMIT_EXCEEDED", "The XML nesting limit was exceeded.", {"limit": "nesting_depth"})
        tag = self._expanded(name)
        local = semantic_name(tag)
        if self.stack:
            counts = self.child_counts[-1]
            sibling_count = counts.get(tag, 0) + 1
            counts[tag] = sibling_count
            path = self.stack[-1].path + "/" + tag + "[{}]".format(sibling_count)
        else:
            path = "/" + tag + "[1]"
        ordered_attrs = [[self._expanded(attr), attrs.getValue(attr)] for attr in attrs.getNames()]
        node = XmlNode(tag=tag, path=path, attributes=ordered_attrs)
        retain_holder_identity = (
            self.capture_root is None and bool(self.stack) and local in {"uniqueID", "id", "ID"}
            and self._is_holder_ancestor(self.stack[-1], self.stack_local_names[:-1], self.stack_local_names[-1])
        )
        ancestor_names = self.stack_local_names
        class_name = dict(ordered_attrs).get("Class", "")
        qualified_local = tag.rsplit("}", 1)[-1]
        if local == "maps" and "game" in ancestor_names:
            self.seen_context_domains.add("maps")
        if local in {"factions", "factionManager", "allFactions"} and "game" in ancestor_names:
            self.seen_context_domains.add("factions")
        if local in {"modIds", "modNames"} and "meta" in ancestor_names:
            self.seen_context_domains.add("mods")
        if local == "researchManager" and "game" in ancestor_names:
            self.seen_context_domains.add("research")
        if local == "scenario" and "game" in ancestor_names:
            self.seen_context_domains.add("scenario")
        unknown_art_component = (class_name and class_name not in {"CompArt", "RimWorld.CompArt"}
                                 and "art" in class_name.rsplit(".", 1)[-1].lower()
                                 and ancestor_names[-1:] == ["comps"] and "game" in ancestor_names)
        if ((class_name in {"CompArt", "RimWorld.CompArt"} and "game" in ancestor_names)
                or unknown_art_component or local == "savedTaleReference"):
            self.seen_context_domains.add("art")
        if local in {"tales", "playLog", "playLogs", "battleLog", "battleLogs", "letterStack", "letters", "art"}:
            self.seen_event_families.add(local)
        if local == "archivables" and {"history", "archive"}.issubset(ancestor_names):
            self.seen_event_families.add("archive")
        class_pawn = class_name in {"Pawn", "Verse.Pawn"}
        classless_world_pool = local == "li" and bool(self.stack) and self.stack_local_names[-1] in {"pawnsAlive", "pawnsDead", "pawnsMothballed"}
        starts_record = class_pawn and local in {"thing", "pawn", "li", "innerPawn"} or classless_world_pool
        foreign_qname_pawn = (tag.startswith("{") and tag.rsplit("}", 1)[-1] in {"thing", "pawn", "li"}
                              and "Pawn" in class_name and "maps" in ancestor_names)
        if local == "li" and ancestor_names and ancestor_names[-1] == "archivables" and {"history", "archive"}.issubset(ancestor_names):
            candidate_capture = "archive"
        elif local == "li" and ancestor_names and ancestor_names[-1] == "tales":
            candidate_capture = "tale"
        elif class_name.endswith("RecorderSessionComponent"):
            candidate_capture = "recorder"
        elif (_unverified_holder_lookalike(class_name, tag)
              and qualified_local in {"thing", "corpse", "innerPawn"}):
            # Retain bounded raw context for likely modded holder lookalikes;
            # this never grants them native holder semantics.
            candidate_capture = "unsupported_holder"
        elif foreign_qname_pawn:
            candidate_capture = "candidate"
        elif class_pawn or classless_world_pool:
            candidate_capture = "pawn"
        elif local == "meta":
            candidate_capture = "metadata"
        elif local == "tickManager" and "game" in ancestor_names:
            candidate_capture = "metadata"
        elif local == "info" and ("meta" in ancestor_names or ancestor_names[-2:] == ["game", "world"] or ancestor_names[-1:] == ["game"]):
            candidate_capture = "metadata"
        elif local == "thing" and "maps" in ancestor_names and "Pawn" in class_name:
            candidate_capture = "candidate"
        else:
            candidate_capture = self._context_capture_kind(local, class_name, ancestor_names, tag)
        starting_capture = self.capture_root is None and candidate_capture is not None
        context_start_kind = None
        if ancestor_names and ancestor_names[-1] == "maps" and local in {"li", "map", "Map"}:
            context_start_kind = "map_record_start"
        elif ancestor_names and ancestor_names[-1] == "allFactions" and local == "li":
            context_start_kind = "faction_record_start"
        needs_sizes = retain_holder_identity or context_start_kind is not None or self.capture_root is not None or starting_capture or starts_record
        attr_bytes = sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in ordered_attrs) if needs_sizes else 0
        structural_bytes = len(tag.encode("utf-8")) + len(path.encode("utf-8")) + 24 if needs_sizes else 0
        if retain_holder_identity:
            self.retained_bytes += attr_bytes + structural_bytes
            if self.retained_bytes > self.limits.retained_value_bytes:
                raise InspectorError("LIMIT_EXCEEDED", "The retained history value limit was exceeded.", {"limit": "retained_value_bytes", "retained_bytes": self.retained_bytes})
        if context_start_kind is not None:
            self.logical_records += 1
            if self.logical_records > self.limits.normalized_records:
                raise InspectorError("LIMIT_EXCEEDED", "The logical history record count limit was exceeded.", {"limit": "normalized_records"})
            self.retained_bytes += attr_bytes + structural_bytes
            self.context_starts.append((context_start_kind, node, tuple(self.stack)))
        if self.capture_root is not None:
            parent = self.stack[-1]
            if parent.children is None:
                parent.children = []
            parent.children.append((node, None))
        elif starting_capture:
            self.capture_root = node
            self.capture_kind = candidate_capture
            self.capture_bytes = 0
            self.capture_ancestors = tuple(self.stack)
            for ancestor in self.capture_ancestors:
                self.retained_bytes += len(ancestor.tag.encode("utf-8")) + len(ancestor.path.encode("utf-8"))
                self.retained_bytes += sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in ancestor.attributes)
        elif self.root is None:
            self.root = node
        elif not self.stack:
            raise InspectorError("MALFORMED_XML", "The source contains multiple root elements.")
        if self.capture_root is not None:
            self.retained_bytes += attr_bytes + structural_bytes
            self.capture_bytes += attr_bytes + structural_bytes
            if self.capture_bytes > self.limits.record_bytes:
                raise InspectorError("LIMIT_EXCEEDED", "A captured history record exceeded its byte limit.", {"limit": "record_bytes"})
        if self.retained_bytes > self.limits.retained_value_bytes:
            raise InspectorError("LIMIT_EXCEEDED", "The retained history value limit was exceeded.", {"limit": "retained_value_bytes", "retained_bytes": self.retained_bytes})
        if self.record_sizes and self.capture_root is not None:
            for index in range(len(self.record_sizes)):
                self.record_sizes[index] += attr_bytes + structural_bytes
                if self.record_sizes[index] > self.limits.record_bytes:
                    raise InspectorError("LIMIT_EXCEEDED", "A logical pawn record exceeded its byte limit.", {"limit": "record_bytes"})
        if local in {"thing", "pawn", "li", "innerPawn"} and (class_pawn or classless_world_pool):
            self.logical_records += 1
            if self.logical_records > self.limits.normalized_records:
                raise InspectorError("LIMIT_EXCEEDED", "The logical record count limit was exceeded.", {"limit": "normalized_records"})
        if starts_record:
            if attr_bytes + structural_bytes > self.limits.record_bytes:
                raise InspectorError("LIMIT_EXCEEDED", "A logical pawn record exceeded its byte limit.", {"limit": "record_bytes"})
            self.record_sizes.append(attr_bytes + structural_bytes)
        self.stack.append(node)
        self.stack_local_names.append(local)
        self.record_markers.append(starts_record)
        self.child_counts.append({})

    def characters(self, content: str) -> None:
        if not content or not self.stack:
            return
        self._check()
        encoded = len(content.encode("utf-8"))
        if self.capture_root is None:
            if (self.stack_local_names[-1] in {"uniqueID", "id", "ID"} and len(self.stack) >= 2
                    and self._is_holder_ancestor(self.stack[-2], self.stack_local_names[:-2], self.stack_local_names[-2])):
                self.retained_bytes += encoded
                if self.retained_bytes > self.limits.retained_value_bytes:
                    raise InspectorError("LIMIT_EXCEEDED", "The retained history value limit was exceeded.", {"limit": "retained_value_bytes", "retained_bytes": self.retained_bytes})
                node = self.stack[-1]
                node.text = (node.text or "") + content
            return
        self.retained_bytes += encoded
        if self.retained_bytes > self.limits.retained_value_bytes:
            raise InspectorError("LIMIT_EXCEEDED", "The retained history value limit was exceeded.", {"limit": "retained_value_bytes", "retained_bytes": self.retained_bytes})
        node = self.stack[-1]
        if node.children:
            previous, _ = node.children[-1]
            node.children[-1] = (previous, (node.children[-1][1] or "") + content)
        else:
            node.text = (node.text or "") + content
        for idx in range(len(self.record_sizes)):
            self.record_sizes[idx] += encoded
        self.capture_bytes += encoded
        if self.capture_bytes > self.limits.record_bytes:
            raise InspectorError("LIMIT_EXCEEDED", "A captured history record exceeded its byte limit.", {"limit": "record_bytes"})
        if self.record_sizes and self.record_sizes[-1] > self.limits.record_bytes:
            raise InspectorError("LIMIT_EXCEEDED", "A logical pawn record exceeded its byte limit.", {"limit": "record_bytes"})

    def endElementNS(self, name: tuple[str, str], qname: str | None) -> None:
        self._check()
        if not self.stack:
            raise InspectorError("MALFORMED_XML", "Unexpected closing XML element.")
        ended = self.stack[-1]
        ended_name = self.stack_local_names[-1]
        if (self.capture_root is None and ended_name in {"uniqueID", "id", "ID"} and len(self.stack) >= 2
                and self._is_holder_ancestor(self.stack[-2], self.stack_local_names[:-2], self.stack_local_names[-2])):
            parent = self.stack[-2]
            if parent.children is None:
                parent.children = []
            parent.children.append((ended, None))
        self.stack.pop()
        self.stack_local_names.pop()
        starts_record = self.record_markers.pop()
        self.child_counts.pop()
        if starts_record and self.record_sizes:
            self.record_sizes.pop()
        if self.capture_root is ended:
            self.captures.append((self.capture_kind or "candidate", ended, self.capture_ancestors))
            self.capture_root = None
            self.capture_kind = None
            self.capture_ancestors = ()
            self.capture_bytes = 0


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _profile_for(version: str | None) -> tuple[str, str]:
    normalized_version = version.strip() if isinstance(version, str) else ""
    # A verified profile requires the complete native build form. A prefix
    # check would incorrectly trust malformed metadata such as "1.6.garbage".
    if re.fullmatch(r"1\.6\.[0-9]+(?: rev[0-9]+)?", normalized_version):
        return VERIFIED_1_6_PROFILE, "verified"
    if normalized_version:
        major_minor = ".".join(normalized_version.split(".")[:2])
        return "rimworld-{}-best-effort/v1".format(major_minor), "best_effort"
    return "rimworld-unknown/v1", "unknown"


def _find(node: XmlNode | None, path: tuple[str, ...]) -> XmlNode | None:
    current = node
    for segment in path:
        if current is None:
            return None
        matches = [child for child, _ in current.children or [] if semantic_name(child.tag) == segment]
        current = matches[0] if matches else None
    return current


def _text(node: XmlNode | None) -> str | None:
    if node is None:
        return None
    if node.children:
        return None
    return node.text


def _claim(field: str, node: XmlNode | None, parent: XmlNode | None, collector: EvidenceCollector,
           *, unit: str | None = None, status_if_absent: str = "absent") -> dict[str, Any]:
    evidence_node = node or parent
    if evidence_node is None:
        return {"field": field, "value": None, "raw_value": None, "status": status_if_absent,
                "raw_field": "/" + field.rsplit(".", 1)[-1],
                "unit": unit, "code": None, "display_label": None, "normalization_rule": None,
                "time_observation": None, "evidence_id": "", "source_ref": ""}
    pair = collector.add(evidence_node)
    raw = _text(node)
    value: Any = raw.strip() if raw is not None else None
    status = "observed" if node is not None else status_if_absent
    if node is not None and value is None:
        status = "explicit-null"
    raw_field = node.path[len(parent.path):] if node is not None and parent is not None else (
        "/" + field.rsplit(".", 1)[-1] if node is None else node.path)
    return {"field": field, "raw_field": raw_field or "/", "value": value, "raw_value": raw, "status": status,
            "unit": unit, "code": None, "display_label": None, "normalization_rule": None,
            "time_observation": None, **pair}


def _normalize_campaign_claim(claim: dict[str, Any], field: str) -> None:
    if claim["status"] != "observed" or not isinstance(claim["value"], str):
        return
    raw = claim["value"].strip()
    try:
        if field in {"persistentRandomValue", "startingYear", "gameStartAbsTick"}:
            number = int(raw, 10)
            claim["value"] = str(number) if field == "persistentRandomValue" else number
            claim["normalization_rule"] = "canonical_base10_integer"
            if field == "startingYear":
                claim["unit"] = "year"
            elif field == "gameStartAbsTick":
                claim["unit"] = "ticks"
        elif field == "initialMapSize":
            dimensions = [int(value.strip(), 10) for value in raw.strip("() ").split(",")]
            if len(dimensions) != 3 or any(value < 1 for value in dimensions):
                raise ValueError
            claim["value"] = "({})".format(",".join(str(value) for value in dimensions))
            claim["normalization_rule"] = "canonical_three_integer_map_size"
        elif field == "startingTile":
            components = [int(value.strip(), 10) for value in raw.strip("() ").split(",")]
            if not 1 <= len(components) <= 2 or any(value < 0 for value in components):
                raise ValueError
            claim["value"] = ",".join(str(value) for value in components)
            claim["normalization_rule"] = "canonical_integer_tile_and_optional_layer"
        elif field == "seedString":
            if not raw:
                raise ValueError
            claim["value"] = raw
            if raw != claim["raw_value"]:
                claim["normalization_rule"] = "trim_outer_whitespace"
    except ValueError:
        claim["value"] = None
        claim["status"] = "invalid"


def _metadata(root: XmlNode, snapshot_id: str, interpretation_id: str, version: str | None,
              collector: EvidenceCollector) -> dict[str, Any]:
    game = _find(root, ("game",))
    info = _find(root, ("game", "world", "info"))
    info = info or _find(root, ("meta",))
    tick_manager = _find(root, ("game", "tickManager"))
    tick_manager = tick_manager or _find(root, ("meta",))
    general_info = _find(root, ("game", "info"))
    general_info = general_info or _find(root, ("meta",))
    profile_id, support = _profile_for(version)
    profile = {"schema_version": SCHEMA_VERSION, "parser_version": PARSER_VERSION,
               "extraction_version": EXTRACTION_VERSION, "game_version": version,
               "support_status": support, "profile_id": profile_id}
    fields: dict[str, Any] = {}
    field_specs = {
        "seedString": (info, "seedString", None),
        "persistentRandomValue": (info, "persistentRandomValue", None),
        "initialMapSize": (info, "initialMapSize", None),
        "startingYear": (tick_manager, "startingYear", "year"),
        "gameStartAbsTick": (tick_manager, "gameStartAbsTick", "ticks"),
        "startingTile": (general_info, "startingTile", None),
    }
    for field, (container, name, unit) in field_specs.items():
        node = next((c for c, _ in container.children or [] if semantic_name(c.tag) == name), None) if container else None
        fields[field] = _claim("campaign_key." + field, node, container, collector, unit=unit)
        _normalize_campaign_claim(fields[field], field)
    normalized = [fields[name]["value"] for name in field_specs]
    identity = {"status": "compatible", "value": hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()} \
        if all(fields[name]["status"] == "observed" and fields[name]["value"] is not None for name in field_specs) \
        else {"status": "review-needed", "value": None}

    clocks: dict[str, Any] = {}
    for name, container in (("ticksGame", tick_manager), ("gameStartAbsTick", tick_manager), ("startingYear", tick_manager)):
        node = next((c for c, _ in container.children or [] if semantic_name(c.tag) == name), None) if container else None
        claim = _claim("clocks." + name, node, container, collector, unit="year" if name == "startingYear" else "ticks")
        if name != "startingYear":
            try:
                claim["value"] = int(str(claim["value"]).strip(), 10) if claim["value"] is not None else None
                if claim["value"] is None:
                    claim["status"] = "invalid" if node is not None else "absent"
            except (TypeError, ValueError):
                claim["value"], claim["status"] = None, "invalid"
        clocks[name] = claim
    ticks_game, start_abs = clocks["ticksGame"]["value"], clocks["gameStartAbsTick"]["value"]
    premise_refs = [{"evidence_id": clocks[n]["evidence_id"], "source_ref": clocks[n]["source_ref"]}
                    for n in ("ticksGame", "gameStartAbsTick")]
    derived_value = ticks_game + start_abs if isinstance(ticks_game, int) and isinstance(start_abs, int) else None
    base_node = game or root
    pair = collector.add(base_node)
    clocks["ticksAbs"] = {"field": "clocks.ticksAbs", "value": derived_value,
                          "raw_value": None, "status": "derived" if derived_value is not None else "unsupported",
                          "unit": "ticks", "code": None, "display_label": None,
                          "normalization_rule": "ticksGame_plus_gameStartAbsTick" if derived_value is not None else None,
                          "time_observation": None, **pair, "premise_refs": premise_refs}
    return {"profile": profile, "campaign_key": {"fields": fields, "identity": identity}, "clocks": clocks}


def _walk_nodes(root: XmlNode):
    stack = [(root, ())]
    while stack:
        node, ancestors = stack.pop()
        yield node, ancestors
        stack.extend((child, ancestors + (node,)) for child, _ in reversed(node.children or []))


def _metadata_tree(root: XmlNode, captures: list[tuple[str, XmlNode, tuple[XmlNode, ...]]]) -> XmlNode:
    """Rebuild only the small metadata branches needed by metadata extraction."""
    sparse = XmlNode(root.tag, root.path, [list(pair) for pair in root.attributes])
    retained: dict[str, XmlNode] = {root.path: sparse}
    for kind, captured, ancestors in captures:
        if kind != "metadata":
            continue
        for original in (*ancestors, captured):
            target = retained.get(original.path)
            if target is None:
                target = XmlNode(original.tag, original.path, [list(pair) for pair in original.attributes])
                retained[original.path] = target
                parent_path = original.path.rsplit("/", 1)[0]
                parent = retained[parent_path]
                if parent.children is None:
                    parent.children = []
                parent.children.append((target, None))
            if original is captured:
                # Replace the placeholder with the retained metadata subtree.
                retained[original.path] = captured
                parent_path = original.path.rsplit("/", 1)[0]
                if parent_path in retained:
                    parent = retained[parent_path]
                    parent.children = [(captured if child.path == original.path else child, tail)
                                       for child, tail in parent.children or []]
    return sparse


def _classify_pawn(node: XmlNode, ancestors: tuple[XmlNode, ...]) -> tuple[str, list[dict[str, Any]], str | None] | None:
    attrs = dict(node.attributes)
    class_name = attrs.get("Class", "")
    if node.tag.startswith("{") or semantic_name(node.tag) not in {"thing", "pawn", "li", "innerPawn"}:
        return None
    saved_id = next((_text(child) for child, _ in node.children or [] if semantic_name(child.tag) in {"id", "ID"}), None)
    if class_name and class_name not in {"Pawn", "Verse.Pawn"}:
        return None
    if not class_name:
        ancestor_names = [semantic_name(x.tag) for x in ancestors]
        known_pool = any(name in {"pawnsAlive", "pawnsDead", "pawnsMothballed"} for name in ancestor_names) \
            and "worldPawns" in ancestor_names and "world" in ancestor_names
        direct = {semantic_name(child.tag): _text(child) for child, _ in node.children or []}
        if not known_pool or not (direct.get("id") and direct.get("def") and direct.get("kindDef")):
            return None
    if not saved_id:
        return ("unknown", [], None)
    ancestors: list[XmlNode] = []
    # The caller annotates holder ancestry after the tree is walked.
    return ("map", [], saved_id.strip())


def _collect_pawns(root: XmlNode, snapshot_id: str, interpretation_id: str,
                   collector: EvidenceCollector, metadata: dict[str, Any],
                   captures: list[tuple[str, XmlNode, tuple[XmlNode, ...]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start_abs = metadata["clocks"]["gameStartAbsTick"]["value"]
    snapshot_ticks = metadata["clocks"]["ticksGame"]["value"]
    materialized_ids: set[str] = set()
    candidates: list[tuple[XmlNode, tuple[XmlNode, ...], tuple[str, list[dict[str, Any]], str | None]]] = []
    seen_paths: set[str] = set()
    for capture_kind, capture, capture_ancestors in captures:
        if capture_kind not in {"pawn", "candidate", "context_map_extension", "unsupported_holder"}:
            continue
        for node, relative_ancestors in _walk_nodes(capture):
            collector.check_work()
            class_value = next((value for key, value in node.attributes if key == "Class"), "")
            if class_value not in {"Pawn", "Verse.Pawn"} and node is not capture:
                continue
            if node.path in seen_paths:
                continue
            seen_paths.add(node.path)
            path_tags = [semantic_name(part.rsplit("[", 1)[0]) for part in node.path.split("/")[1:]]
            if "pawnsForcefullyKeptAsWorldPawns" in path_tags:
                continue
            ancestors = capture_ancestors + relative_ancestors
            classified = _classify_pawn(node, ancestors)
            if classified is not None and classified[2]:
                materialized_ids.add(classified[2])
            if classified is None:
                continue
            candidates.append((node, ancestors, classified))
    for node, ancestors, classified in candidates:
            scope, holders, pawn_id = classified
            path_tags = [semantic_name(part.rsplit("[", 1)[0]) for part in node.path.split("/")[1:]]
            if "pawnsDead" in path_tags:
                scope = "world_dead"
            elif "pawnsMothballed" in path_tags:
                scope = "world_mothballed"
            elif "pawnsAlive" in path_tags:
                scope = "world_alive"
            elif "caravan" in path_tags or "Caravan" in path_tags:
                scope = "caravan"
            elif "transporters" in path_tags or "transportPods" in path_tags:
                scope = "transport"
            elif any(_unverified_holder_lookalike(dict(holder.attributes).get("Class", ""), holder.tag)
                     for holder in ancestors):
                scope = "held_other"
            elif any(_native_holder_kind(dict(holder.attributes).get("Class", ""), holder.tag) == "cryptosleep_casket"
                     for holder in ancestors):
                scope = "cryptosleep"
            elif any(_native_holder_kind(dict(holder.attributes).get("Class", ""), holder.tag) == "corpse"
                     for holder in ancestors):
                scope = "corpse"
            elif any(_native_holder_kind(dict(holder.attributes).get("Class", ""), holder.tag) == "growth_vat"
                     for holder in ancestors):
                scope = "growth_vat"
            elif "carryTracker" in path_tags:
                scope = "carry"
            elif "inventory" in path_tags:
                scope = "inventory"
            elif "innerContainer" in path_tags:
                scope = "held_other"
            elif "maps" in path_tags:
                scope = "map"
            else:
                scope = "unknown"
            node_pair = collector.add(node)
            holder_chain: list[dict[str, Any]] = []
            for holder_index, holder in enumerate(ancestors):
                collector.check_work()
                holder_name = semantic_name(holder.tag)
                holder_path_tags = [semantic_name(part.rsplit("[", 1)[0]) for part in holder.path.split("/")[1:]]
                holder_class = dict(holder.attributes).get("Class", "")
                parent_name = semantic_name(ancestors[holder_index - 1].tag) if holder_index else None
                native_holder_kind = _native_holder_kind(holder_class, holder.tag)
                if native_holder_kind is not None:
                    kind = native_holder_kind
                elif holder_class in {"Pawn", "Verse.Pawn"}:
                    kind = "pawn_holder"
                elif holder_name in {"li", "map", "Map"} and parent_name == "maps":
                    kind = "map"
                elif holder_name in {"pawnsAlive", "pawnsDead", "pawnsMothballed"} and "worldPawns" in holder_path_tags:
                    kind = "world_{}_pool".format(holder_name.removeprefix("pawns").lower())
                elif holder_name == "li" and "caravans" in holder_path_tags:
                    kind = "caravan"
                elif holder_name == "transportShip":
                    kind = "transport"
                elif holder_name in {"caravan", "Caravan"}:
                    kind = "caravan"
                elif holder_name in {"corpse", "Corpse"}:
                    kind = "corpse"
                elif holder_name in {"cryptosleepCasket", "casket", "growthVat", "vat", "transporters"}:
                    kind = holder_name
                else:
                    continue
                holder_id = dict(holder.attributes).get("id") or dict(holder.attributes).get("ID")
                identity_field = None
                identity_status = "unsupported"
                if holder_id is None:
                    identity_node = next((c for c, _ in holder.children or []
                                          if semantic_name(c.tag) in {"id", "ID", "uniqueID"}), None)
                    holder_id = _text(identity_node)
                    if identity_node is not None:
                        identity_field = identity_node.path[len(holder.path):] or "/"
                        holder_pair = collector.add(identity_node)
                        identity_status = "observed"
                    else:
                        holder_pair = node_pair
                else:
                    attr_name = "id" if "id" in dict(holder.attributes) else "ID"
                    identity_field = "/@{}".format(attr_name)
                    holder_pair = collector.add_start_tag_attribute(holder, attr_name)
                    identity_status = "observed"
                holder_chain.append({"kind": kind, "saved_id": holder_id.strip() if holder_id else None,
                                     "source_path": holder.path, "identity_field": identity_field,
                                     "identity_status": identity_status, **holder_pair})
            if scope in {"world_alive", "world_dead", "world_mothballed"}:
                # Keep the actual pool locator in the holder chain. Its complete
                # identity is structural, so the full Pawn occurrence supports
                # membership while the pool itself remains explicitly unkeyed.
                pool_name = {"world_alive": "pawnsAlive", "world_dead": "pawnsDead",
                             "world_mothballed": "pawnsMothballed"}[scope]
                pool = next((holder for holder in ancestors if semantic_name(holder.tag) == pool_name), None)
                if pool is not None and not any(row["source_path"] == pool.path for row in holder_chain):
                    holder_chain.append({"kind": "world_{}_pool".format(scope.removeprefix("world_")),
                                         "saved_id": None, "source_path": pool.path,
                                         "identity_field": None, "identity_status": "unsupported",
                                         **node_pair})
            aliases = []
            for alias, alias_ancestors in _walk_nodes(node):
                alias_name = semantic_name(alias.tag)
                if alias_name not in {"first", "nick", "last", "title", "label", "nameInt"}:
                    continue
                ancestor_names = {semantic_name(parent.tag) for parent in alias_ancestors}
                if alias_name in {"first", "nick", "last", "label", "nameInt"} and "name" not in ancestor_names:
                    continue
                if alias_name == "title" and not ancestor_names.intersection({"royalty", "titles"}):
                    continue
                alias_value = _text(alias)
                if alias_value is None:
                    continue
                pair = collector.add(alias)
                aliases.append({"field": "name." + alias_name, "value": alias_value.strip(),
                                "raw_field": alias.path[len(node.path):] or "/",
                                "raw_value": alias_value, "status": "observed", **pair})
            rows.append({"pawn_id": pawn_id, "record_id": record_id_for_occurrence(snapshot_id, interpretation_id, "pawn", node.path),
                         "source_path": node.path, "scope": scope, "holder_chain": holder_chain, "aliases": aliases,
                         "status": "materialized" if pawn_id else "candidate_unverified",
                         "domains": build_pawn_domains(node, scope=scope, holder_chain=holders, evidence=collector,
                                                        game_start_abs=start_abs, snapshot_ticks=snapshot_ticks,
                                                        pawn_ids=materialized_ids),
                         "evidence_refs": [node_pair]})
    return rows


def _recorder_context(captures: list[tuple[str, XmlNode, tuple[XmlNode, ...]]], collector: EvidenceCollector, snapshot_id: str,
                      interpretation_id: str) -> list[dict[str, Any]]:
    """Read only the explicitly serialized Life Recorder checkpoint anchor."""
    rows = []
    field_names = {
        "campaign_token": "lifeRecorderCampaignId",
        "checkpoint_id": "lifeRecorderCheckpointId",
        "producer_session_id": "lifeRecorderCheckpointSession",
        "source_save_sha256": "sourceSaveSha256",
    }
    for capture_kind, component, _ in captures:
        collector.check_work()
        if capture_kind != "recorder":
            continue
        class_name = dict(component.attributes).get("Class", "")
        if not class_name.endswith("RecorderSessionComponent"):
            continue
        node_pair = collector.add(component)
        claims = []
        by_name = {semantic_name(child.tag): child for child, _ in component.children or []}
        for semantic, saved_name in field_names.items():
            collector.check_work()
            field_node = by_name.get(saved_name)
            evidence_node = field_node or component
            pair = collector.add(evidence_node)
            raw = _text(field_node)
            claims.append({
                "field": semantic,
                "raw_field": "/{}[1]".format(saved_name),
                "value": raw.strip() if raw is not None else None,
                "raw_value": raw,
                "status": "observed" if field_node is not None else "absent",
                "unit": None, "code": None, "display_label": None,
                "normalization_rule": None, "time_observation": None, **pair,
            })
        record_id = record_id_for_occurrence(snapshot_id, interpretation_id, "recorder_checkpoint", component.path)
        rows.append({"id": record_id, "kind": "recorder_checkpoint", "claims": claims,
                     "links": [], "evidence_refs": [node_pair]})
    return rows


def _relative_path(node: XmlNode, root_path: str) -> str:
    return node.path[len(root_path):] or "/"


def _attribute_claim(field: str, node: XmlNode, attribute: str,
                     collector: EvidenceCollector) -> dict[str, Any] | None:
    raw = dict(node.attributes).get(attribute)
    if raw is None:
        return None
    pair = collector.add_start_tag_attribute(node, attribute)
    return {"field": field, "raw_field": "/@{}".format(attribute),
            "value": raw, "raw_value": raw, "status": "observed",
            "unit": None, "code": None, "display_label": None,
            "normalization_rule": None, "time_observation": None, **pair}


def _simple_context_claim(field: str, node: XmlNode, root_path: str,
                          collector: EvidenceCollector) -> dict[str, Any]:
    return context_claim(field, node, collector, raw_field=_relative_path(node, root_path))


def _context_owner(ancestors: tuple[XmlNode, ...], kind: str) -> XmlNode | None:
    names = [semantic_name(node.tag) for node in ancestors]
    if kind == "map":
        indexes = [index for index, name in enumerate(names)
                   if name in {"li", "map", "Map"} and index > 0 and names[index - 1] == "maps"]
    elif kind == "faction":
        indexes = [index for index, name in enumerate(names)
                   if name == "li" and index > 0 and names[index - 1] == "allFactions"]
    else:
        return None
    return ancestors[indexes[-1]] if indexes else None


def _context_entities(captures: list[tuple[str, XmlNode, tuple[XmlNode, ...]]],
                      context_starts: list[tuple[str, XmlNode, tuple[XmlNode, ...]]],
                      collector: EvidenceCollector, snapshot_id: str, interpretation_id: str,
                      events: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Extract shared context from bounded records and scalar source nodes."""
    context: dict[str, list[dict[str, Any]]] = {name: [] for name in
                                                ("maps", "factions", "mods", "research", "scenario", "art")}
    states: dict[str, dict[str, Any]] = {}
    map_starts = {node.path: node for kind, node, _ in context_starts if kind == "map_record_start"}
    faction_starts = {node.path: node for kind, node, _ in context_starts if kind == "faction_record_start"}
    fields_by_owner: dict[tuple[str, str], list[XmlNode]] = {}
    for capture_kind, node, ancestors in captures:
        if capture_kind == "context_map_field":
            owner = _context_owner(ancestors, "map")
            if owner is not None:
                fields_by_owner.setdefault(("map", owner.path), []).append(node)
        elif capture_kind == "context_faction_field":
            owner = _context_owner(ancestors, "faction")
            if owner is not None:
                fields_by_owner.setdefault(("faction", owner.path), []).append(node)

    maps_supported = 0
    maps_unknown = 0
    for path, root in sorted(map_starts.items()):
        class_name = dict(root.attributes).get("Class", "")
        if class_name and class_name != "Map":
            collector.add_start_tag_attribute(root, "Class")
            maps_unknown += 1
            continue
        claims = []
        refs = []
        for attribute in ("Class", "id", "name"):
            claim = _attribute_claim("map.attribute." + attribute, root, attribute, collector)
            if claim:
                claims.append(claim)
                refs.append({"evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"]})
        for node in sorted(fields_by_owner.get(("map", path), ()), key=lambda item: item.path):
            field = "map." + semantic_name(node.tag)
            claims.append(_simple_context_claim(field, node, root.path, collector))
        for node, _ in root.children or ():
            if semantic_name(node.tag) in {"uniqueID", "id", "ID"}:
                claims.append(_simple_context_claim("map." + semantic_name(node.tag), node, root.path, collector))
        if not claims:
            maps_unknown += 1
            continue
        saved_id = next((claim["value"] for claim in claims
                         if claim["field"] in {"map.uniqueID", "map.id", "map.ID", "map.attribute.id"}
                         and isinstance(claim["value"], str) and claim["value"]), None)
        entity_id = "map:" + saved_id if saved_id is not None else record_id_for_occurrence(
            snapshot_id, interpretation_id, "map", root.path)
        if dict(root.attributes).get("Class"):
            refs.append(collector.add_start_tag_attribute(root, "Class"))
        for capture_kind, node, ancestors in captures:
            if capture_kind != "context_map_extension":
                continue
            owner = _context_owner(ancestors, "map")
            if owner is None or owner.path != path:
                continue
            pair = collector.add(node)
            refs.append(pair)
            claims.append({"field": "map.unknown_extension", "raw_field": _relative_path(node, root.path),
                           "value": None, "raw_value": None, "status": "unsupported", "unit": None,
                           "code": None, "display_label": None, "normalization_rule": None,
                           "time_observation": None, **pair})
            maps_unknown += 1
        context["maps"].append(context_entity(entity_id, "map", claims, evidence_refs=refs))
        maps_supported += 1
    states["maps"] = {"status": "unsupported" if maps_unknown and not maps_supported else
                       "partial" if maps_unknown else "complete",
                       "observed_count": maps_supported, "unknown_count": maps_unknown}

    faction_supported = 0
    faction_unknown = 0
    for path, root in sorted(faction_starts.items()):
        class_name = dict(root.attributes).get("Class", "")
        if class_name and class_name != "Faction":
            collector.add_start_tag_attribute(root, "Class")
            faction_unknown += 1
            continue
        claims = []
        refs = []
        class_claim = _attribute_claim("faction.attribute.Class", root, "Class", collector)
        if class_claim:
            claims.append(class_claim)
            refs.append({"evidence_id": class_claim["evidence_id"], "source_ref": class_claim["source_ref"]})
        for node in sorted(fields_by_owner.get(("faction", path), ()), key=lambda item: item.path):
            claims.append(_simple_context_claim("faction." + semantic_name(node.tag), node, root.path, collector))
        saved_id = next((claim["value"] for claim in claims if claim["field"] == "faction.loadID"
                         and isinstance(claim["value"], str) and claim["value"]), None)
        entity_id = "faction:" + saved_id if saved_id is not None else record_id_for_occurrence(
            snapshot_id, interpretation_id, "faction", root.path)
        if dict(root.attributes).get("Class"):
            refs.append(collector.add_start_tag_attribute(root, "Class"))
        context["factions"].append(context_entity(entity_id, "faction", claims, evidence_refs=refs))
        faction_supported += 1
    states["factions"] = {"status": "unsupported" if faction_unknown and not faction_supported else
                           "partial" if faction_unknown else "complete",
                           "observed_count": faction_supported, "unknown_count": faction_unknown}

    mod_captures = [(node, ancestors) for kind, node, ancestors in captures if kind == "context_mod_entry"]
    # `meta` is already captured as one bounded metadata record. Reuse that
    # retained subtree so mod entries keep exact order without duplicating it.
    for kind, meta, _ in captures:
        if kind != "metadata" or semantic_name(meta.tag) != "meta":
            continue
        for container, container_ancestors in _walk_nodes(meta):
            list_name = semantic_name(container.tag)
            if list_name not in {"modIds", "modNames"}:
                continue
            for child, _ in container.children or []:
                if semantic_name(child.tag) == "li":
                    mod_captures.append((child, container_ancestors + (container,)))
    mod_unknown = 0
    for node, ancestors in sorted(mod_captures, key=lambda row: row[0].path):
        parent = semantic_name(ancestors[-1].tag) if ancestors else ""
        label = "mod_id" if parent == "modIds" else "mod_name"
        claim = _simple_context_claim("mods." + label, node, node.path, collector)
        raw = claim["value"]
        if not isinstance(raw, str) or not raw:
            mod_unknown += 1
        entity_id = record_id_for_occurrence(snapshot_id, interpretation_id, "mod_" + label, node.path)
        context["mods"].append(context_entity(entity_id, label, [claim]))
    states["mods"] = {"status": "partial" if mod_unknown else "complete",
                       "observed_count": len(context["mods"]), "unknown_count": mod_unknown}

    research_groups: dict[str, dict[str, Any]] = {}
    research_unknown = 0
    for kind, node, _ in captures:
        if kind != "context_research_entry":
            continue
        tag = semantic_name(node.tag)
        parent = semantic_name(node.path.rsplit("/", 1)[0].rsplit("/", 1)[-1].rsplit("[", 1)[0])
        leaves = [leaf for leaf, ancestors in _walk_nodes(node) if leaf is not node and not leaf.children]
        if tag == "li" and parent == "progress":
            key = next((leaf for leaf in leaves if semantic_name(leaf.tag) == "key"), None)
            group_key = _text(key) if key is not None else None
            if not group_key:
                research_unknown += 1
                group_key = None
            group_id = "research:" + group_key if group_key else record_id_for_occurrence(
                snapshot_id, interpretation_id, "research_entry", node.path)
            group = research_groups.setdefault(group_id, {"claims": [], "refs": []})
            for leaf in leaves:
                claim = _simple_context_claim("research.progress." + semantic_name(leaf.tag), leaf, node.path, collector)
                group["claims"].append(claim)
                group["refs"].append({"evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"]})
        else:
            raw = _text(node)
            field = "research.current_project" if tag == "currentProj" else "research." + parent
            claim = _simple_context_claim(field, node, node.path, collector)
            if not raw:
                research_unknown += 1
                group_id = record_id_for_occurrence(snapshot_id, interpretation_id, "research_entry", node.path)
            else:
                group_id = "research:" + raw
            group = research_groups.setdefault(group_id, {"claims": [], "refs": []})
            group["claims"].append(claim)
            group["refs"].append({"evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"]})
    for entity_id, group in sorted(research_groups.items()):
        context["research"].append(context_entity(entity_id, "research_project", group["claims"],
                                                  evidence_refs=group["refs"]))
    states["research"] = {"status": "partial" if research_unknown else "complete",
                           "observed_count": len(context["research"]), "unknown_count": research_unknown}

    scenario_captures = [(node, ancestors) for kind, node, ancestors in captures if kind == "context_scenario"]
    scenario_unknown = 0
    for root, _ in sorted(scenario_captures, key=lambda row: row[0].path):
        claims = []
        for node, _ in _walk_nodes(root):
            if node is root or node.children:
                continue
            relative = _relative_path(node, root.path).strip("/")
            segments = [part.rsplit("[", 1)[0] for part in relative.split("/") if part]
            field = "scenario." + ".".join(segments)
            claims.append(_simple_context_claim(field, node, root.path, collector))
        # An explicitly empty scenario has complete zero-count coverage; retain
        # its exact source node as evidence without inventing an entity.
        if not claims:
            collector.add(root)
            continue
        # Retain the bounded scenario record as well as its scalar claims. This
        # preserves mixed content and QName-bearing fields that are not claims.
        refs = [collector.add(root)]
        refs.extend({"evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"]}
                    for claim in claims)
        context["scenario"].append(context_entity(record_id_for_occurrence(
            snapshot_id, interpretation_id, "scenario", root.path), "scenario", claims, evidence_refs=refs))
    states["scenario"] = {"status": "partial" if scenario_unknown else "complete",
                           "observed_count": len(context["scenario"]), "unknown_count": scenario_unknown}

    event_by_raw_id: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        raw_id = event.get("raw_saved_id")
        if event.get("kind") == "tale" and isinstance(raw_id, str):
            event_by_raw_id.setdefault(raw_id, []).append(event)
    art_unknown = 0
    for kind, node, _ in captures:
        if kind == "context_unknown_art_component":
            collector.add(node)
            art_unknown += 1
    for component, ancestors in ((node, ancestors) for kind, node, ancestors in captures
                                 if kind == "context_art_component"):
        component_nodes = list(_walk_nodes(component))
        references = [node for node, field_ancestors in component_nodes
                      if (semantic_name(node.tag) == "savedTaleReference"
                          or (semantic_name(node.tag) == "tale" and
                              any(semantic_name(ancestor.tag) == "taleRef" for ancestor in field_ancestors)))
                      and not node.children]
        # Keep only components with an explicit saved tale reference relevant to this context domain.
        if not references:
            continue
        class_claim = _attribute_claim("art.component_class", component, "Class", collector)
        claims = [class_claim] if class_claim else []
        refs = ([{"evidence_id": class_claim["evidence_id"], "source_ref": class_claim["source_ref"]}]
                if class_claim else [])
        for node, _ in component_nodes:
            if node is component or node.children or node in references:
                continue
            field = "art." + semantic_name(node.tag)
            claim = _simple_context_claim(field, node, component.path, collector)
            claims.append(claim)
            refs.append({"evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"]})
        links = []
        for ref_node in references:
            ref_field = "art.saved_tale_reference" if semantic_name(ref_node.tag) == "savedTaleReference" else "art.taleRef.tale"
            claim = _simple_context_claim(ref_field, ref_node, component.path, collector)
            claims.append(claim)
            refs.append({"evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"]})
            raw_id = ref_node.text
            matching = event_by_raw_id.get(raw_id or "", [])
            if len(matching) == 1:
                event = matching[0]
                links.append({"kind": "saved_tale_reference", "target_id": raw_id,
                              "raw_saved_id": raw_id, "status": "observed",
                              "evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"]})
                for participant in event["participants"]:
                    pawn_id = participant["pawn_id"]
                    links.append({"kind": "pawn_reference", "target_id": pawn_id,
                                  "role": "participant_unknown", "status": "observed",
                                  "evidence_id": participant["evidence_id"], "source_ref": participant["source_ref"]})
            else:
                if len(matching) > 1:
                    art_unknown += 1
                links.append({"kind": "saved_tale_reference", "target_id": raw_id,
                              "raw_saved_id": raw_id, "status": "ambiguous" if len(matching) > 1 else "unresolved",
                              "evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"]})
        component_pair = collector.add(component)
        refs.append(component_pair)
        entity_id = record_id_for_occurrence(snapshot_id, interpretation_id, "art_reference", component.path)
        context["art"].append(context_entity(entity_id, "art_reference", claims, links=links,
                                             evidence_refs=refs))
    states["art"] = {"status": "unsupported" if art_unknown and not context["art"] else
                      "partial" if art_unknown else "complete",
                      "observed_count": len(context["art"]), "unknown_count": art_unknown}
    return context, states


def _events(root: XmlNode, collector: EvidenceCollector, snapshot_id: str, interpretation_id: str,
            metadata: dict[str, Any], pawn_ids: set[str],
            captures: list[tuple[str, XmlNode, tuple[XmlNode, ...]]]) -> list[dict[str, Any]]:
    rows = []
    start_abs = metadata["clocks"]["gameStartAbsTick"]["value"]
    snapshot_ticks = metadata["clocks"]["ticksGame"]["value"]
    for capture_kind, tale, _ in captures:
        collector.check_work()
        if capture_kind != "tale":
            continue
        children = {semantic_name(child.tag): child for child, _ in tale.children or []}
        id_node = children.get("ID") or children.get("id")
        raw_id = dict(tale.attributes).get("id") or _text(id_node)
        def_node = children.get("def")
        occurred_node = children.get("occurred")
        evidence_pair = collector.add(tale)
        refs = [evidence_pair]
        claims = []
        for child, _ in tale.children or []:
            if semantic_name(child.tag):
                continue
            pair = collector.add(child)
            refs.append(pair)
            raw = _text(child)
            claims.append({"field": "event.unknown_field", "value": None, "raw_field": child.path[len(tale.path):] or "/",
                           "raw_value": raw, "status": "unsupported", "unit": None, "code": None,
                           "display_label": None, "normalization_rule": None, "time_observation": None,
                           "structural_kind": "unknown_event_field", **pair})
        if def_node is not None:
            pair = collector.add(def_node)
            refs.append(pair)
            raw_kind = _text(def_node)
            claims.append({"field": "event.kind", "value": raw_kind.strip() if raw_kind else None,
                           "raw_field": def_node.path[len(tale.path):] or "/",
                           "raw_value": raw_kind, "status": "observed" if raw_kind is not None else "explicit-null",
                           "unit": None, "code": raw_kind.strip() if raw_kind else None,
                           "display_label": None, "normalization_rule": None,
                           "time_observation": None, **pair})
        participants = []
        pawn_ref = children.get("pawn")
        if pawn_ref is not None:
            raw_pawn = _text(pawn_ref)
            pair = collector.add(pawn_ref)
            refs.append(pair)
            if raw_pawn and raw_pawn.strip():
                participants.append({"pawn_id": raw_pawn.strip(), "role": "participant_unknown",
                                     "status": "observed", **pair})
        time_observation = None
        if occurred_node is not None:
            raw_time = _text(occurred_node)
            pair = collector.add(occurred_node)
            refs.append(pair)
            try:
                absolute = int(raw_time.strip(), 10) if raw_time is not None else None
            except ValueError:
                absolute = None
            if absolute is not None and start_abs is not None:
                normalized = absolute - start_abs
                status = "pre_arrival" if normalized < 0 else "known"
                if snapshot_ticks is not None and normalized > snapshot_ticks:
                    status = "future_contradiction"
            else:
                normalized = None
                status = "invalid" if raw_time is not None else "unknown"
            time_observation = {"raw_value": raw_time, "raw_field": "/occurred[1]",
                                "clock": "absolute_tick", "normalized_game_tick": normalized,
                                "normalization_rule": "absolute_tick_minus_gameStartAbsTick" if normalized is not None else None,
                                "status": status, **pair}
        class_name = dict(tale.attributes).get("Class", "Tale")
        rows.append({"kind": "tale", "saved_event_id": "tale:{}:{}".format(class_name, raw_id) if raw_id else None,
                     "raw_saved_id": raw_id, "record_id": record_id_for_occurrence(snapshot_id, interpretation_id, "tale", tale.path),
                     "participants": participants, "occurrence": time_observation, "claims": claims,
                     "evidence_refs": refs})
    for capture_kind, item, _ in captures:
        collector.check_work()
        if capture_kind != "archive":
            continue
        children = {semantic_name(child.tag): child for child, _ in item.children or []}
        id_node = children.get("ID")
        raw_id = _text(id_node)
        if not raw_id:
            continue
        class_name = dict(item.attributes).get("Class", "unknown")
        item_pair = collector.add(item)
        refs = [item_pair]
        claims = []
        participants = []
        occurrence = None
        for field_node, field_ancestors in _walk_nodes(item):
            collector.check_work()
            if field_node is item or field_node.children:
                continue
            field_name = semantic_name(field_node.tag)
            raw = _text(field_node)
            pair = collector.add(field_node)
            refs.append(pair)
            relative_path = field_node.path[len(item.path):] or "/"
            is_time_field = bool(field_name) and field_name.lower().endswith(("tick", "time", "ticks", "occurred"))
            time_observation = None
            if is_time_field:
                time_observation = {"raw_value": raw, "raw_field": relative_path, "clock": "unknown",
                                    "normalized_game_tick": None, "normalization_rule": None,
                                    "status": "unknown", **pair}
                if occurrence is None:
                    occurrence = time_observation
            claims.append({"field": ("archive." if field_name else "archive.unknown_field") + relative_path.lstrip("/"),
                           "raw_field": relative_path,
                           "value": raw.strip() if raw is not None else None, "raw_value": raw,
                           "status": ("unsupported" if not field_name else
                                      "observed" if raw is not None else "explicit-null"),
                           "unit": None, "code": raw.strip() if field_name == "def" and raw else None,
                           "display_label": None, "normalization_rule": None,
                           "time_observation": time_observation, **pair})
            if "lookTargets" in [semantic_name(x.tag) for x in field_ancestors] and field_name and raw and raw.strip() in pawn_ids:
                participants.append({"pawn_id": raw.strip(), "role": "target_unknown", "status": "observed", **pair})
        rows.append({"kind": "archive", "saved_event_id": "archive:{}:{}".format(class_name, raw_id),
                     "raw_saved_id": raw_id, "record_id": record_id_for_occurrence(snapshot_id, interpretation_id, "archive", item.path),
                     "participants": participants, "occurrence": occurrence, "claims": claims,
                     "evidence_refs": refs})
    return rows


def extract_history_snapshot(path: os.PathLike[str] | str, limits: ParseLimits | None = None,
                             cancel: threading.Event | None = None) -> HistorySnapshot:
    """Parse exactly one local regular save, failing closed on security/bounds errors."""
    effective = HistoryLimits(limits)
    source = Path(path)
    try:
        before_path = source.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise InspectorError("SOURCE_NOT_FOUND", "The configured save could not be opened.") from exc
    except OSError as exc:
        raise InspectorError("SOURCE_UNREADABLE", "The configured save could not be inspected.") from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise InspectorError("UNSUPPORTED_SOURCE", "The source must be a regular non-symlink file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise InspectorError("SOURCE_UNREADABLE", "The configured save could not be read safely.") from exc
    try:
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd_before = os.fstat(stream.fileno())
            if not stat.S_ISREG(fd_before.st_mode) or _identity(before_path) != _identity(fd_before):
                raise InspectorError("SOURCE_CHANGED", "The source path changed before extraction.")
            parser = DefusedExpatParser(namespaceHandling=1, forbid_dtd=True, forbid_entities=True, forbid_external=True)
            parser.setFeature(__import__("xml.sax.handler", fromlist=["feature_namespaces"]).feature_namespaces, True)
            handler = _TreeHandler(effective, cancel)
            parser.setContentHandler(handler)
            digest = hashlib.sha256()
            source_bytes = 0
            started = time.monotonic()
            while True:
                if cancel is not None and cancel.is_set():
                    raise InspectorError("CANCELLED", "History extraction was cancelled.")
                if time.monotonic() - started > effective.deadline_seconds:
                    raise InspectorError("LIMIT_EXCEEDED", "The history extraction deadline was exceeded.", {"limit": "deadline_seconds"})
                block = stream.read(effective.read_chunk_bytes)
                if not block:
                    break
                source_bytes += len(block)
                if source_bytes > effective.input_bytes:
                    raise InspectorError("LIMIT_EXCEEDED", "The input byte limit was exceeded.", {"limit": "input_bytes"})
                digest.update(block)
                parser.feed(block)
            parser.close()
            fd_after = os.fstat(stream.fileno())
            if _identity(fd_before) != _identity(fd_after):
                raise InspectorError("SOURCE_CHANGED", "The source changed during extraction.")
        path_after = source.stat(follow_symlinks=False)
        if _identity(before_path) != _identity(path_after):
            raise InspectorError("SOURCE_CHANGED", "The source path changed during extraction.")
        if handler.root is None:
            raise InspectorError("MALFORMED_XML", "The XML source was empty.")
        snapshot_id = digest.hexdigest()
        metadata_root = _metadata_tree(handler.root, handler.captures)
        native_version_node = _find(metadata_root, ("meta", "gameVersion"))
        root_version = dict(handler.root.attributes).get("version")
        if native_version_node is not None:
            raw_version = _text(native_version_node)
            version = raw_version if raw_version is not None and raw_version.strip() else None
        else:
            version = root_version
        profile_id, _ = _profile_for(version)
        interpretation_id = interpretation_id_for_profile(profile_id)
        collector = EvidenceCollector(snapshot_id, interpretation_id,
                                     check_work=lambda: handler._check(force_deadline=True))
        if native_version_node is not None:
            collector.add(native_version_node)
        if root_version is not None:
            collector.add_start_tag_attribute(handler.root, "version")
        metadata = _metadata(metadata_root, snapshot_id, interpretation_id, version, collector)
        pawns = _collect_pawns(handler.root, snapshot_id, interpretation_id, collector, metadata, handler.captures)
        coverage_domains = {name: {"status": "absent", "observed_count": 0, "unknown_count": 0} for name in DOMAIN_NAMES}
        for pawn in pawns:
            collector.check_work()
            for name, domain in pawn["domains"].items():
                collector.check_work()
                row = coverage_domains[name]
                row["observed_count"] += sum(claim.get("status") in {"observed", "derived"}
                                              for claim in domain["claims"]) + sum(
                                                  link.get("status") == "observed" for link in domain["links"])
                row["unknown_count"] += sum(claim.get("status") in {"unsupported", "ambiguous", "invalid"}
                                             for claim in domain["claims"])
                if domain["status"] in {"unsupported", "partial", "invalid"}:
                    row["unknown_count"] += 1
        for name in DOMAIN_NAMES:
            statuses = [pawn["domains"][name]["status"] for pawn in pawns]
            if statuses:
                distinct = set(statuses)
                coverage_domains[name]["status"] = next(iter(distinct)) if len(distinct) == 1 else "partial"
        unknown_fields: list[dict[str, Any]] = []
        for pawn in pawns:
            collector.check_work()
            for claim in pawn["domains"]["unknown_fields"]["claims"]:
                collector.check_work()
                raw_field = claim.get("raw_field") or "/"
                is_attribute = raw_field.startswith("/@")
                path = pawn["source_path"] + raw_field
                serialized_bytes = claim.get("serialized_bytes", 0)
                unknown_fields.append({"path": path,
                                       "structural_kind": claim.get("structural_kind", "unknown_pawn_attribute" if is_attribute else "unknown_pawn_field"),
                                       "serialized_bytes": serialized_bytes,
                                       "record_id": pawn["record_id"], "pawn_id": pawn["pawn_id"],
                                       "evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"]})
        for capture_kind, capture, _ in handler.captures:
            if capture_kind not in {"candidate", "pawn", "context_map_extension", "unsupported_holder"}:
                continue
            for node, _ in _walk_nodes(capture):
                collector.check_work()
                attrs = dict(node.attributes)
                class_name = attrs.get("Class", "")
                if capture_kind == "unsupported_holder" and node.path == capture.path:
                    pair = collector.add(node)
                    unknown_fields.append({"path": node.path,
                                           "structural_kind": "unverified_holder_class",
                                           "serialized_bytes": len(canonical_json_bytes(node.as_node())),
                                           "record_id": None, "pawn_id": None,
                                           **pair})
                    continue
                direct = {semantic_name(child.tag): _text(child) for child, _ in node.children or []}
                has_pawn_shape = direct.get("id") and direct.get("def") and direct.get("kindDef")
                unverified_class = bool(class_name) and class_name not in {"Pawn", "Verse.Pawn"}
                foreign_record_qname = bool(class_name in {"Pawn", "Verse.Pawn"} and node.tag.startswith("{"))
                if unverified_class or (has_pawn_shape and foreign_record_qname):
                    pair = collector.add(node)
                    unknown_fields.append({"path": node.path,
                                           "structural_kind": "foreign_qname_pawn_record" if foreign_record_qname else "unverified_pawn_like_class",
                                           "serialized_bytes": len(canonical_json_bytes(node.as_node())),
                                           "record_id": None, "pawn_id": None,
                                           **pair})
        coverage = {"source_status": "parsed", "domains": coverage_domains, "unknown_fields": unknown_fields,
                    "limits": {"input_bytes": effective.input_bytes, "nesting_depth": effective.nesting_depth,
                               "logical_record_bytes": effective.record_bytes,
                               "retained_value_bytes": effective.retained_value_bytes,
                               "deadline_seconds": effective.deadline_seconds}}
        events: list[dict[str, Any]] = _events(handler.root, collector, snapshot_id, interpretation_id, metadata,
                                                {str(pawn["pawn_id"]) for pawn in pawns if pawn["pawn_id"]}, handler.captures)
        context, context_states = _context_entities(
            handler.captures, handler.context_starts, collector, snapshot_id, interpretation_id, events)
        context["related_entities"] = _recorder_context(handler.captures, collector, snapshot_id, interpretation_id)
        unknown_event_fields = sum(claim.get("status") == "unsupported"
                                   for event in events for claim in event.get("claims", ()))
        coverage_domains["events"] = {"status": "partial" if events or handler.seen_event_families else "absent",
                                       "observed_count": len(events),
                                       "unknown_count": len(handler.seen_event_families - {"tales", "archive"})
                                       + unknown_event_fields}
        for name in ("maps", "factions", "mods", "research", "scenario", "art"):
            if name in handler.seen_context_domains:
                coverage_domains[name] = context_states.get(
                    name, {"status": "unsupported", "observed_count": 0, "unknown_count": 1})
            else:
                coverage_domains[name] = {"status": "absent", "observed_count": 0, "unknown_count": 0}
        additional_records = len(events)
        for name in ("mods", "research", "scenario", "art", "related_entities"):
            for _ in context[name]:
                collector.check_work()
                additional_records += 1
        if handler.logical_records + additional_records > effective.normalized_records:
            raise InspectorError("LIMIT_EXCEEDED", "The logical history record count limit was exceeded.",
                                 {"limit": "normalized_records"})
        if context["related_entities"]:
            coverage_domains["related_entities"] = {"status": "partial", "observed_count": len(context["related_entities"]), "unknown_count": 1}
        else:
            coverage_domains["related_entities"] = {"status": "absent", "observed_count": 0, "unknown_count": 0}
        diagnostics = []
        if not pawns:
            diagnostics.append({"code": "NO_MATERIALIZED_PAWNS", "severity": "info", "message": "No verified Pawn-class records were materialized.",
                                "path": None, "evidence_id": None, "source_ref": None, "details": {"candidate_count": 0}})
        handler._check(force_deadline=True)
        return HistorySnapshot(snapshot_id, interpretation_id, metadata, pawns, events, context,
                               collector.rows, coverage, diagnostics,
                               check_work=lambda: handler._check(force_deadline=True),
                               trusted_immutable_inputs=True)
    except InspectorError:
        raise
    except Exception as exc:
        from defusedxml.common import DefusedXmlException
        from xml.sax import SAXParseException
        if isinstance(exc, DefusedXmlException):
            raise InspectorError("UNSAFE_XML", "DTD, entity, or external-reference XML is forbidden.") from exc
        if isinstance(exc, SAXParseException):
            raise InspectorError("MALFORMED_XML", "The XML document could not be parsed safely.") from exc
        raise InspectorError("SOURCE_UNREADABLE", "The history source could not be parsed.") from exc
