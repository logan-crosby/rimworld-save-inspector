"""Hardened streaming extraction of selected save records and scalar fields."""

from __future__ import annotations

import threading
import time
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, BinaryIO, Mapping
from xml.sax.handler import ContentHandler, feature_external_ges, feature_external_pes

from ..errors import InspectorError
from ..models import Diagnostic, ParseLimits, Record, record_id_for_path

PARSER_PROFILE = "rimworld-1.6/v1"
_SCALARS = {
    "id", "ID", "loadID", "def", "kindDef", "first", "nick", "last", "name", "faction", "gender",
    "ageBiologicalTicks", "birthAbsTicks", "guestStatus", "hostFaction", "slaveFaction", "level", "passion",
    "xpSinceLastLevel", "xpSinceMidnight", "degree", "sourceGene", "suppressedBy", "healthState", "severity",
    "ageTicks", "tickAdded", "visible", "chemical", "body", "index", "pawn", "overriddenByGene", "xenotype",
    "xenotypeName", "hybrid", "iconDef", "stuff", "quality", "health", "stackCount", "pos", "uniqueID",
    "parent", "size", "gameVersion", "ticksGame", "seedString", "factionDef", "currentProj", "contentsLookMode",
    "progress", "modIds", "modNames", "li", "Class", "label", "nameInt", "worldName", "seedString",
}
_CANONICAL_TYPES = {
    "pawn": "Pawn", "item": "Item", "gene": "Gene", "hediff": "Hediff", "skill": "Skill",
    "trait": "Trait", "map": "Map", "faction": "Faction", "world_object": "WorldObject",
}
_EXPECTED_FIELDS = {
    "corpse": ("/id", "/def"),
    "pawn": ("/id", "/def", "/kindDef", "/name/first", "/name/nick", "/name/last", "/name", "/faction", "/gender", "/ageTracker/ageBiologicalTicks", "/ageTracker/birthAbsTicks", "/guest/guestStatus", "/guest/hostFaction", "/guest/slaveFaction", "/genes/xenotype", "/genes/xenotypeName", "/genes/hybrid", "/genes/iconDef"),
    "item": ("/id", "/def", "/stuff", "/quality", "/health", "/stackCount", "/pos"),
    "gene": ("/def", "/loadID", "/pawn", "/overriddenByGene"),
    "hediff": ("/def", "/loadID", "/severity", "/ageTicks", "/tickAdded", "/visible", "/chemical", "/part/body", "/part/index"),
    "skill": ("/def", "/level", "/passion", "/xpSinceLastLevel", "/xpSinceMidnight"),
    "trait": ("/def", "/degree", "/sourceGene", "/suppressedBy"),
    "map": ("/uniqueID", "/mapInfo/parent", "/mapInfo/size"),
    "faction": ("/def", "/loadID", "/name"),
    "world_object": ("/ID", "/id", "/def", "/name", "/label", "/nameInt"),
}
_REFERENCE_LEAVES = {"faction", "hostFaction", "slaveFaction", "sourceGene", "suppressedBy", "pawn", "overriddenByGene", "parent"}
_COLLECTION_NAMES = {"maps", "things", "pawnsAlive", "pawnsDead", "pawnsMothballed", "pawnsForcefullyKeptAsWorldPawns", "skills", "allTraits", "hediffs", "endogenes", "xenogenes", "equipment", "wornApparel", "inventory", "innerList", "progress", "techprints", "modIds", "modNames", "keys", "values"}
_EMPTY_ATTRS: Mapping[str, str] = MappingProxyType({})


@dataclass(slots=True)
class _Frame:
    tag: str
    path: str
    counts: dict[str, int] | None = None
    text: list[str] | None = None
    text_bytes: int = 0
    has_child: bool = False
    attrs: Mapping[str, str] = field(default_factory=lambda: _EMPTY_ATTRS)


@dataclass(slots=True)
class _Builder:
    type_name: str
    path: str
    attrs: dict[str, str]
    base_depth: int
    values: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    used_bytes: int = 0
    owner_id: str | None = None
    scope: str | None = None
    memberships: list[dict[str, Any]] = field(default_factory=list)
    record_id: str = ""
    direct_thing_count: int = 0
    direct_pawn_count: int = 0


@dataclass(frozen=True, slots=True)
class ParsedSave:
    records: tuple[Record, ...]
    globals: dict[str, Any]
    global_status: dict[str, str]
    collection_states: dict[str, str]
    diagnostics: tuple[Diagnostic, ...]
    diagnostic_counts: dict[str, int]
    game_version: str | None
    profile_version: str = PARSER_PROFILE
    auxiliary_records: tuple[Record, ...] = ()


class _Handler(ContentHandler):
    def __init__(self, limits: ParseLimits, cancel: threading.Event | None) -> None:
        super().__init__()
        self.limits = limits
        self.cancel = cancel
        self.started = time.monotonic()
        self.frames: list[_Frame] = []
        self.builders: list[_Builder] = []
        self.records: list[Record] = []
        self.auxiliary_records: list[Record] = []
        self.globals: dict[str, Any] = {}
        self.global_status: dict[str, str] = {}
        self.collections: dict[str, str] = {}
        self.diagnostics: list[Diagnostic] = []
        self.diagnostic_counts: dict[str, int] = {}
        self._retained_bytes = 0
        self.root_seen = False
        self.game_seen = False
        self._check_count = 0

    def _check(self, *, force_deadline: bool = False) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise InspectorError("CANCELLED", "Snapshot loading was cancelled.")
        self._check_count += 1
        # Cancellation stays event-granular; the deadline is sampled at most
        # 64 callbacks apart and again at EOF. BoundedReader checks every read.
        if (force_deadline or self._check_count & 63 == 0) and time.monotonic() - self.started > self.limits.deadline_seconds:
            raise InspectorError("LIMIT_EXCEEDED", "The parse wall deadline was exceeded.", {"limit": "deadline_seconds"})

    def _path(self, tag: str) -> str:
        parent = self.frames[-1] if self.frames else None
        if parent is None:
            return f"/{tag}[1]"
        if parent.counts is None:
            parent.counts = {}
        parent.counts[tag] = parent.counts.get(tag, 0) + 1
        parent.has_child = True
        if parent.text is not None:
            parent.text.clear()
        return f"{parent.path}/{tag}[{parent.counts[tag]}]"

    def _tags(self) -> tuple[str, ...]:
        return tuple(frame.tag for frame in self.frames)

    def _candidate(self, tag: str, cls: str | None) -> tuple[str, str | None]:
        # startElement has already pushed this element on the path stack.
        if tag not in {"li", "thing"} and cls != "Pawn":
            return "", None
        if tag == "li" and cls != "Pawn" and len(self.frames) > 1 and self.frames[-2].tag not in {
            "maps", "allFactions", "worldObjects", "pawnsAlive", "pawnsDead", "pawnsMothballed",
            "innerList", "hediffs", "endogenes", "xenogenes", "skills", "allTraits",
        }:
            return "", None
        tags = self._tags()
        tail = "/".join(tags[-9:])
        if tail.endswith("game/maps/li"):
            return "map", None
        if tail.endswith("game/world/factionManager/allFactions/li"):
            return "faction", None
        if tail.endswith("game/world/worldObjects/worldObjects/li"):
            return "world_object", None
        if tail.endswith("game/maps/li/things/thing") and cls == "Corpse":
            return "corpse", "map"
        map_pawn = tail.endswith("maps/li/things/thing")
        pool = _world_pool_for_tags(tags)
        world_pool_pawn = pool is not None
        known_holder_pawn = any(x in tags for x in ("equipment", "wornApparel", "inventory", "innerContainer", "pawns"))
        if cls == "Pawn" and (map_pawn or world_pool_pawn or known_holder_pawn):
            if map_pawn:
                return "pawn", "map"
            return "pawn", f"world_{pool}" if pool else "held"
        if world_pool_pawn and tag == "li" and cls is None:
            return "pawn", f"world_{pool}"
        if cls not in {"Pawn", "Corpse"} and tail.endswith("innerList/li"):
            return "item", "held"
        if tag == "li" and tail.endswith("hediffs/li"):
            return "hediff", "held"
        if tag == "li" and (tail.endswith("endogenes/li") or tail.endswith("xenogenes/li")):
            return "gene", "held"
        if tag == "li" and tail.endswith("skills/li"):
            return "skill", "held"
        if tag == "li" and tail.endswith("allTraits/li"):
            return "trait", "held"
        return "", None

    def startElement(self, name: str, attrs: Any) -> None:
        self._check()
        if len(self.frames) + 1 > self.limits.nesting_depth:
            raise InspectorError("LIMIT_EXCEEDED", "The XML nesting depth limit was exceeded.", {"limit": "nesting_depth"})
        path = self._path(name)
        is_null = attrs.get("IsNull")
        cls = attrs.get("Class")
        frame_attrs = ({key: str(value) for key, value in (("IsNull", is_null), ("Class", cls)) if value is not None}
                       if is_null is not None or cls is not None else _EMPTY_ATTRS)
        frame = _Frame(name, path, attrs=frame_attrs)
        self.frames.append(frame)
        if not self.root_seen:
            self.root_seen = True
            if name != "savegame":
                raise InspectorError("UNSUPPORTED_FORMAT", "The XML root is not an unnamespaced savegame element.")
        if name == "game" and self.frames[0].tag == "savegame":
            self.game_seen = True
        if cls is not None:
            class_bytes = len(str(cls).encode("utf-8"))
            if class_bytes > self.limits.extension_value_bytes:
                self._diagnostic("EXTENSION_OMITTED", path=path)
                cls = None
            else:
                self._retained_bytes += class_bytes
                if self._retained_bytes > self.limits.retained_value_bytes:
                    raise InspectorError("LIMIT_EXCEEDED", "The normalized value byte limit was exceeded.", {"limit": "retained_value_bytes"})
        if (name == "thing" and len(self.frames) >= 4 and self.frames[-2].tag == "things"
                and self.frames[-3].tag == "li" and self.frames[-4].tag == "maps"):
            map_builder = next((b for b in reversed(self.builders) if b.type_name == "map"), None)
            if map_builder:
                map_builder.direct_thing_count += 1
                if cls == "Pawn":
                    map_builder.direct_pawn_count += 1
        self._mark_collections(path, is_null)
        kind, scope = self._candidate(name, cls)
        if kind:
            parent_pawn = next((b for b in reversed(self.builders) if b.type_name == "pawn"), None)
            builder = _Builder(kind, path, {"Class": cls} if cls else {}, len(self.frames), owner_id=parent_pawn.record_id if parent_pawn else None, scope=scope)
            builder.record_id = record_id_for_path(kind, path)
            if kind == "pawn" and scope == "map":
                map_builder = next((b for b in reversed(self.builders) if b.type_name == "map"), None)
                if map_builder:
                    builder.memberships.append({"scope": "map", "map_id": None, "map_record_id": map_builder.record_id, "source_path": path})
            if parent_pawn and kind != "pawn":
                holder_path = self._holder_path()
                if holder_path:
                    builder.memberships.append({"scope": "held", "holder_id": record_id_for_path("holder", holder_path), "source_path": path})
            self.builders.append(builder)

    def _holder_path(self) -> str | None:
        for index in range(len(self.frames) - 1, -1, -1):
            if self.frames[index].tag in {"equipment", "wornApparel", "inventory", "innerContainer", "innerList", "pawns"}:
                return self.frames[index].path
        return None

    def _mark_collections(self, path: str, is_null: str | None) -> None:
        tag = self.frames[-1].tag
        if tag in _COLLECTION_NAMES:
            self.collections[path] = "explicit_null" if is_null == "True" else "present"

    def characters(self, content: str) -> None:
        self._check()
        if not self.frames:
            return
        frame = self.frames[-1]
        if frame.tag not in _SCALARS and frame.tag != "li":
            return
        size = len(content.encode("utf-8"))
        if not self.builders:
            frame.text_bytes += size
            if frame.text_bytes > self.limits.extension_value_bytes:
                if frame.text is not None:
                    frame.text.clear()
                if frame.text_bytes <= self.limits.extension_value_bytes + size:
                    self._diagnostic("EXTENSION_OMITTED", path=frame.path)
                frame.text_bytes = self.limits.extension_value_bytes + 1
                return
            self._retained_bytes += size
            if self._retained_bytes > self.limits.retained_value_bytes:
                raise InspectorError("LIMIT_EXCEEDED", "The normalized value byte limit was exceeded.", {"limit": "retained_value_bytes"})
            if frame.text is None:
                frame.text = []
            frame.text.append(content)
            return
        builder = self.builders[-1]
        if builder.used_bytes + size > self.limits.record_bytes:
            raise InspectorError("LIMIT_EXCEEDED", "A retained logical record exceeded its byte limit.", {"limit": "record_bytes"})
        builder.used_bytes += size
        self._retained_bytes += size
        if self._retained_bytes > self.limits.retained_value_bytes:
            raise InspectorError("LIMIT_EXCEEDED", "The normalized value byte limit was exceeded.", {"limit": "retained_value_bytes"})
        if frame.text is None:
            frame.text = []
        frame.text.append(content)
        frame.text_bytes += size

    def endElement(self, name: str) -> None:
        self._check()
        frame = self.frames[-1]
        if frame.tag != name:
            raise InspectorError("MALFORMED_XML", "The XML element stack is inconsistent.")
        text = "".join(frame.text or ()).strip()
        if self.builders and text:
            builder = self.builders[-1]
            relative = self._relative_path(builder.path, frame.path)
            is_null = frame.attrs.get("IsNull") == "True"
            if is_null and text:
                builder.values.setdefault(relative + ":invalid", []).append((text, frame.path))
            elif relative.rsplit("/", 1)[-1] in _REFERENCE_LEAVES and text == "null":
                builder.values.setdefault(relative + ":explicit_null", []).append((text, frame.path))
            else:
                builder.values.setdefault(relative, []).append((text, frame.path))
            if builder.type_name == "corpse" and frame.tag == "li":
                self.globals[frame.path] = text
        elif self.builders and not frame.has_child and frame.tag in _SCALARS:
            builder = self.builders[-1]
            relative = self._relative_path(builder.path, frame.path)
            builder.values.setdefault(relative + (":explicit_null" if frame.attrs.get("IsNull") == "True" else ":empty"), []).append(("", frame.path))
        # Collection/record wrapper elements (notably item <li> nodes) are
        # not scalar values. In particular, do not publish empty wrapper
        # text into globals: holder queries interpret scalar <li> globals as
        # reference entries and would otherwise duplicate full records.
        elif frame.tag in _SCALARS and not self.builders and not frame.has_child:
            if frame.attrs.get("IsNull") == "True" and not text:
                self.globals[frame.path] = None
                self.global_status[frame.path] = "explicit_null"
            elif frame.attrs.get("IsNull") == "True" and text:
                self.globals[frame.path] = None
                self.global_status[frame.path] = "invalid"
                self._diagnostic("INVALID_VALUE", path=frame.path)
            elif text:
                leaf = frame.tag
                if leaf in {"ticksGame"}:
                    try:
                        number = int(text, 10)
                        if not -(2**63) <= number < 2**63:
                            raise ValueError
                    except ValueError:
                        self.globals[frame.path] = text
                        self.global_status[frame.path] = "invalid"
                        self._diagnostic("INVALID_VALUE", path=frame.path)
                    else:
                        self.globals[frame.path] = text
                else:
                    self.globals[frame.path] = text
            else:
                if frame.tag in {"ticksGame"}:
                    self.globals[frame.path] = None
                    self.global_status[frame.path] = "invalid"
                    self._diagnostic("INVALID_VALUE", path=frame.path)
                else:
                    self.globals[frame.path] = ""
        if self.builders and self.builders[-1].path == frame.path:
            builder = self.builders.pop()
            record = self._finish(builder)
            if record is not None:
                (self.auxiliary_records if record.type_name == "corpse" else self.records).append(record)
            if len(self.records) + len(self.auxiliary_records) > self.limits.normalized_records:
                raise InspectorError("LIMIT_EXCEEDED", "The normalized record limit was exceeded.", {"limit": "normalized_records"})
        self.frames.pop()

    @staticmethod
    def _relative_path(base: str, path: str) -> str:
        if path == base:
            return "/text"
        return "/" + "/".join(part.rsplit("[", 1)[0] for part in path[len(base) + 1 :].split("/"))

    def _finish(self, builder: _Builder) -> Record | None:
        self._check()
        values: dict[str, Any] = {}
        statuses: dict[str, str] = {}
        raw: dict[str, str] = {}
        for path, found in builder.values.items():
            if path.endswith(":explicit_null"):
                pointer = path.removesuffix(":explicit_null")
                values[pointer] = None
                statuses[pointer] = "explicit_null"
                continue
            if path.endswith(":empty"):
                pointer = path.removesuffix(":empty")
                leaf = pointer.rsplit("/", 1)[-1]
                if leaf in {"level", "degree", "ageBiologicalTicks", "birthAbsTicks", "xpSinceLastLevel", "xpSinceMidnight", "severity", "ageTicks", "tickAdded", "index", "health", "stackCount", "ticksGame", "progress", "visible", "hybrid"} | _REFERENCE_LEAVES:
                    statuses[pointer] = "invalid"
                    self._diagnostic("INVALID_VALUE", path=found[0][1])
                else:
                    values[pointer] = ""
                continue
            if path.endswith(":invalid"):
                pointer = path.removesuffix(":invalid")
                statuses[pointer] = "invalid"
                self._diagnostic("INVALID_VALUE", path=found[0][1], detail=found[0][0])
                continue
            if len(found) > 1 and path not in {"/li"}:
                statuses[path] = "invalid"
                self._diagnostic("DUPLICATE_SINGLETON", path=found[0][1])
                continue
            value, source_path = found[0]
            leaf = path.rsplit("/", 1)[-1]
            if leaf in {"level", "degree", "ageBiologicalTicks", "birthAbsTicks", "ageTicks", "tickAdded", "index", "health", "stackCount", "ticksGame"}:
                try:
                    number = int(value, 10)
                    if not -(2**63) <= number < 2**63 or leaf == "stackCount" and number < 0:
                        raise ValueError
                except ValueError:
                    statuses[path] = "invalid"
                    self._diagnostic("INVALID_VALUE", path=source_path, detail=value)
                    continue
            elif leaf in {"xpSinceLastLevel", "xpSinceMidnight", "severity", "progress"}:
                try:
                    if not math.isfinite(float(value)):
                        raise ValueError
                    raw[path] = value
                except ValueError:
                    statuses[path] = "invalid"
                    self._diagnostic("INVALID_VALUE", path=source_path, detail=value)
                    continue
            elif leaf in {"visible", "hybrid"} and value not in {"True", "False", "true", "false"}:
                statuses[path] = "invalid"
                self._diagnostic("INVALID_VALUE", path=source_path, detail=value)
                continue
            values[path] = value
            values[path + ":source_path"] = source_path
        if builder.attrs.get("Class"):
            values["/@Class"] = builder.attrs["Class"]
        if builder.type_name == "item" and "/def" not in values:
            text = values.get("/text")
            if isinstance(text, str) and text:
                self.globals[builder.path] = text
            elif "/@Class" in values:
                self._diagnostic("UNSUPPORTED_HOLDER", path=builder.path)
            return None
        if builder.type_name == "map":
            values["/direct_thing_count"] = builder.direct_thing_count
            values["/direct_pawn_count"] = builder.direct_pawn_count
        for pointer in _EXPECTED_FIELDS.get(builder.type_name, ()):
            if pointer not in values and pointer not in statuses:
                statuses[pointer] = "absent"
        if builder.type_name == "pawn" and (builder.scope or "").startswith("world_") and "/def" not in values:
            if isinstance(values.get("/text"), str) and values["/text"].strip():
                self.globals[builder.path] = values["/text"].strip()
            elif isinstance(values.get("/id"), str) and values["/id"].strip():
                self.globals[builder.path + "/id[1]"] = values["/id"].strip()
            return None
        id_path = {"pawn": "/id", "item": "/id", "corpse": "/id", "gene": "/loadID", "hediff": "/loadID", "faction": "/loadID", "world_object": "/ID", "map": "/uniqueID", "skill": "/def", "trait": "/def"}.get(builder.type_name)
        if builder.type_name == "world_object" and "/id" in values:
            if "/ID" in values and values["/ID"] != values["/id"]:
                statuses["/saved_id"] = "invalid"
                self._diagnostic("INVALID_VALUE", path=builder.path)
                saved_id = None
            else:
                saved_id = values.get("/ID", values.get("/id"))
        else:
            saved_id = values.get(id_path) if id_path else None
        memberships = list(builder.memberships)
        if builder.type_name == "pawn" and builder.scope and not any(
            membership.get("scope") == builder.scope for membership in memberships
        ):
            memberships.append({"scope": builder.scope, "map_id": None, "pool": builder.scope.removeprefix("world_") if builder.scope.startswith("world_") else None, "source_path": builder.path})
        return Record(
            record_id=builder.record_id,
            type_name=builder.type_name,
            saved_id=saved_id,
            source_path=builder.path,
            values=values,
            field_status=statuses,
            raw_values=raw,
            owner_record_id=builder.owner_id,
            scope=builder.scope,
            memberships=tuple(memberships),
        )

    def _diagnostic(self, code: str, *, path: str, detail: str | None = None) -> None:
        self.diagnostic_counts[code] = self.diagnostic_counts.get(code, 0) + 1
        if sum(1 for d in self.diagnostics if d.code == code) < self.limits.diagnostic_examples_per_code:
            details = {}
            if detail is not None:
                encoded = detail.encode("utf-8")
                max_bytes = self.limits.diagnostic_detail_bytes
                excerpt = encoded[:max_bytes].decode("utf-8", errors="ignore")
                if len(encoded) > max_bytes:
                    excerpt += "…"
                details["raw"] = excerpt
            self.diagnostics.append(Diagnostic(code, "warning", code.replace("_", " ").lower(), path, details=details))

    def close(self) -> ParsedSave:
        self._check(force_deadline=True)
        if not self.root_seen or not self.game_seen:
            raise InspectorError("UNSUPPORTED_FORMAT", "The savegame root must contain a game element.")
        game_version = next((v for p, v in self.globals.items() if p.endswith("/gameVersion[1]")), None)
        if game_version and not game_version.startswith("1.6."):
            self._diagnostic("UNSUPPORTED_VERSION", path="/savegame[1]/meta[1]/gameVersion[1]")
        self.records.sort(key=lambda record: record.source_path)
        seen: dict[tuple[str, str], int] = {}
        for record in self.records:
            if record.saved_id is None:
                continue
            key = (record.type_name, record.saved_id)
            seen[key] = seen.get(key, 0) + 1
        for (kind, saved_id), count in seen.items():
            if count > 1:
                self._diagnostic("DUPLICATE_SAVED_ID", path="/savegame[1]" )
        return ParsedSave(tuple(self.records), dict(self.globals), dict(self.global_status), dict(self.collections), tuple(sorted(self.diagnostics, key=lambda d: (d.code, d.source_path or ""))), dict(self.diagnostic_counts), game_version, auxiliary_records=tuple(self.auxiliary_records))


def _world_pool_for_tags(tags: tuple[str, ...]) -> str | None:
    if len(tags) < 3 or tags[-1] != "li" or "worldPawns" not in tags:
        return None
    pools = {"pawnsAlive": "alive", "pawnsDead": "dead", "pawnsMothballed": "mothballed"}
    if tags[-2] in pools:
        return pools[tags[-2]]
    return None


def parse_stream(stream: BinaryIO, *, limits: ParseLimits, cancel: threading.Event | None = None) -> ParsedSave:
    """Parse through EOF using defusedxml SAX and explicit DTD/entity bans."""
    from defusedxml.sax import make_parser

    parser = make_parser()
    # defusedxml exposes these controls on its SAX parser; set all explicitly.
    if not all(hasattr(parser, name) for name in ("forbid_dtd", "forbid_entities", "forbid_external")):
        raise RuntimeError("the configured SAX parser does not expose all defusedxml security controls")
    parser.forbid_dtd = True
    parser.forbid_entities = True
    parser.forbid_external = True
    for feature in (feature_external_ges, feature_external_pes):
        try:
            parser.setFeature(feature, False)
        except (NotImplementedError, ValueError):
            pass
    handler = _Handler(limits, cancel)
    parser.setContentHandler(handler)
    parser.parse(stream)
    return handler.close()
