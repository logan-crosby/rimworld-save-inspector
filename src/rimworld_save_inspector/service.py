"""Pure shared query API for immutable save snapshots."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from typing import Any

from .errors import InspectorError
from .indexes import IdentityIndexes, memberships_for
from .models import Diagnostic, QueryRequest, QueryResponse, Record, Snapshot, record_id_for_path, thaw

_ARRAY_QUERIES = {"pawns", "health", "genes", "equipment", "inventory", "research", "mods"}
_SCOPES = {"map", "world_alive", "world_dead", "world_mothballed", "held", "all_supported"}
_ALLOWED_FILTERS = {
    "inspect": set(), "summary": {"map_id"},
    "pawns": {"scope", "map_id", "faction", "pawn_def", "name", "guest_status"},
    "pawn": {"record_id", "section"}, "health": {"record_id"},
    "genes": {"record_id", "group"}, "equipment": {"record_id", "holder", "estimate_missing_as_one"},
    "inventory": {"record_id", "holder_id", "estimate_missing_as_one"},
    "research": {"section", "project_def"}, "mods": set(),
}
_FIELD_NAMES = {
    "pawn": {"/id": "/saved_id", "/def": "/pawn_def", "/kindDef": "/kind_def", "/name/first": "/name/first", "/name/nick": "/name/nick", "/name/last": "/name/last", "/name": "/name/simple", "/faction": "/faction", "/gender": "/gender", "/ageTracker/ageBiologicalTicks": "/age_biological_ticks", "/ageTracker/birthAbsTicks": "/birth_abs_ticks", "/guest/guestStatus": "/guest_status", "/guest/hostFaction": "/host_faction", "/guest/slaveFaction": "/slave_faction", "/healthTracker/healthState": "/health_state"},
    "item": {"/id": "/saved_id", "/def": "/item_def", "/stuff": "/stuff", "/quality": "/quality", "/health": "/hit_points", "/stackCount": "/stack_count", "/pos": "/position_raw"},
    "gene": {"/loadID": "/saved_id", "/def": "/gene_def", "/pawn": "/pawn", "/overriddenByGene": "/overridden_by"},
    "hediff": {"/loadID": "/saved_id", "/def": "/hediff_def", "/severity": "/severity", "/ageTicks": "/age_ticks", "/tickAdded": "/tick_added", "/visible": "/visible", "/chemical": "/chemical", "/part/body": "/part/body", "/part/index": "/part/index"},
    "skill": {"/def": "/skill_def", "/level": "/level", "/passion": "/passion", "/xpSinceLastLevel": "/xp_since_last_level", "/xpSinceMidnight": "/xp_since_midnight"},
    "trait": {"/def": "/trait_def", "/degree": "/degree", "/sourceGene": "/source_gene", "/suppressedBy": "/suppressed_by"},
    "map": {"/uniqueID": "/map_id", "/mapInfo/parent": "/parent", "/mapInfo/size": "/size_raw"},
    "faction": {"/def": "/def", "/loadID": "/saved_id", "/name": "/name"},
    "world_object": {"/ID": "/saved_id", "/id": "/saved_id", "/def": "/def", "/name": "/name", "/label": "/label", "/nameInt": "/name_int"},
}


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _get(record: Record | None, *paths: str, default: Any = None) -> Any:
    return default if record is None else record.value(*paths, default=default)


def _status(record: Record | None, path: str) -> str:
    if record is None:
        return "absent"
    return str(record.field_status.get(path, "present" if path in record.values else "absent"))


def _sort_records(records: list[Record]) -> list[Record]:
    def ordinal_path(path: str) -> tuple[int, ...]:
        import re
        return tuple(int(x) for x in re.findall(r"\[(\d+)\]", path))
    return sorted(records, key=lambda r: (r.saved_id is None, r.saved_id or "", r.record_id)) if all(r.type_name != "research" for r in records) else sorted(records, key=lambda r: ordinal_path(r.source_path))


class QueryService:
    def __init__(self, snapshot: Snapshot, *, snapshot_lifetime: str = "invocation") -> None:
        if snapshot_lifetime not in {"process", "invocation"}:
            raise ValueError("snapshot_lifetime must be process or invocation")
        self.snapshot = snapshot
        self.snapshot_lifetime = snapshot_lifetime
        self.indexes = IdentityIndexes(snapshot.records)
        self._records = {r.record_id: r for r in snapshot.records}
        self._records_by_type: dict[str, list[Record]] = defaultdict(list)
        self._children: dict[str, list[Record]] = defaultdict(list)
        self._items_by_holder: dict[str, list[Record]] = defaultdict(list)
        self._pawns_by_holder: dict[str, list[Record]] = defaultdict(list)
        for record in snapshot.records:
            self._records_by_type[record.type_name].append(record)
            if record.owner_record_id:
                self._children[record.owner_record_id].append(record)
            if record.type_name in {"item", "pawn"} and "/li[" in record.source_path:
                holder_path = record.source_path.rsplit("/li[", 1)[0]
                if record.type_name == "item":
                    self._items_by_holder[holder_path].append(record)
                elif any(member.get("holder_id") == record_id_for_holder(holder_path) for member in record.memberships):
                    self._pawns_by_holder[holder_path].append(record)
        self._player_faction = self.indexes.player_faction(snapshot)
        self._map_things_state = {
            record.record_id: _collection_state(snapshot, record.source_path + "/things[1]")
            for record in self._records_by_type["map"]
        }
        self._array_globals: list[tuple[str, str]] = []
        self._references_by_holder: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._metadata_globals: dict[str, tuple[str, Any]] = {}
        self._reference_memberships: dict[str, list[dict[str, Any]]] = {}
        self._holder_reference_diagnostics: list[Diagnostic] = []
        for path, raw in snapshot.globals.items():
            if "/li[" in path and any(marker in path for marker in (
                "/innerList[", "/modIds[", "/modNames[", "/researchManager[",
            )):
                self._array_globals.append((path, str(raw)))
                if "/innerList[" in path and path.rsplit("/", 1)[-1].startswith("li["):
                    self._references_by_holder[path.rsplit("/li[", 1)[0]].append((path, str(raw)))
            leaf = path.rsplit("/", 1)[-1].split("[", 1)[0]
            if leaf in {"gameVersion", "name", "seedString", "ticksGame", "currentProj"}:
                normalized_path = re.sub(r"\[\d+\]", "", path)
                for suffix in ("/gameVersion", "/info/name", "/info/seedString", "/ticksGame", "/currentProj"):
                    if normalized_path.endswith(suffix):
                        self._metadata_globals.setdefault(suffix, (path, raw))
            pool = _world_pool_reference(path) if "/worldPawns[" in path else None
            if pool is not None and isinstance(raw, str) and raw.strip():
                thing_raw = raw if raw.startswith("Thing_") else "Thing_" + raw
                reference = self.indexes.resolve(thing_raw, "thing")
                if reference.status == "resolved" and reference.target_record_ids:
                    source_path = re.sub(r"/id\[\d+\]$", "", path)
                    for target_id in reference.target_record_ids:
                        self._reference_memberships.setdefault(target_id, []).append({
                            "scope": f"world_{pool}", "map_id": None, "pool": pool,
                            "holder_id": None, "source_path": source_path,
                        })
                continue
            if not isinstance(raw, str) or not raw.strip() or not _is_supported_reference_path(path):
                continue
            reference = self.indexes.resolve(raw, "thing")
            if reference.status in {"unresolved", "ambiguous"}:
                code = "UNRESOLVED_REFERENCE" if reference.status == "unresolved" else "AMBIGUOUS_REFERENCE"
                self._holder_reference_diagnostics.append(
                    Diagnostic(code, "warning", code.replace("_", " ").lower(), path)
                )
            if reference.status != "resolved" or not reference.target_record_ids:
                continue
            tags = re_tags(path)
            pool_name = "forcefully_kept" if "pawnsForcefullyKeptAsWorldPawns" in tags else None
            for target_id in reference.target_record_ids:
                self._reference_memberships.setdefault(target_id, []).append({
                    "scope": "held", "map_id": None, "pool": pool_name,
                    "holder_id": record_id_for_holder(path.rsplit("/li[", 1)[0]), "source_path": path,
                })
        self._holder_cycles = self._find_holder_cycles()

    def _find_holder_cycles(self) -> tuple[tuple[str, ...], ...]:
        graph: dict[str, set[str]] = defaultdict(set)
        for item in self._records_by_type["item"]:
            path = item.source_path + "/innerContainer[1]/innerList[1]"
            for child in self._items_by_holder.get(path, ()):
                graph[item.record_id].add(child.record_id)
            for source_path, raw in self._references_by_holder.get(path, ()):
                reference = self.indexes.resolve(raw, "thing")
                if reference.status == "resolved" and reference.target_record_ids:
                    target = self._records.get(reference.target_record_ids[0])
                    if target is not None and target.type_name == "item":
                        graph[item.record_id].add(target.record_id)
        cycles: set[tuple[str, ...]] = set()
        done: set[str] = set()
        for start in graph:
            if start in done:
                continue
            stack: list[tuple[str, Any]] = [(start, iter(graph.get(start, ())))]
            positions = {start: 0}
            path = [start]
            while stack:
                node, children = stack[-1]
                target = next(children, None)
                if target is None:
                    stack.pop()
                    done.add(node)
                    positions.pop(node, None)
                    path.pop()
                    continue
                if target in positions:
                    cycle = path[positions[target]:]
                    cycles.add(min(tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))))
                elif target in graph and target not in done:
                    positions[target] = len(path)
                    path.append(target)
                    stack.append((target, iter(graph[target])))
        return tuple(sorted(cycles))

    def execute(self, query: QueryRequest) -> QueryResponse:
        if not isinstance(query, QueryRequest):
            raise InspectorError("INVALID_ARGUMENT", "A QueryRequest is required.", snapshot_id=self.snapshot.snapshot_id)
        name = query.name
        if name not in _ALLOWED_FILTERS:
            raise InspectorError("INVALID_ARGUMENT", "Unknown query name.", {"query": name}, snapshot_id=self.snapshot.snapshot_id)
        filters = thaw(query.filters)
        unknown = set(filters) - _ALLOWED_FILTERS[name]
        if unknown:
            raise InspectorError("INVALID_ARGUMENT", "Unknown query filter.", {"filters": sorted(unknown)}, snapshot_id=self.snapshot.snapshot_id)
        for key, value in filters.items():
            expected_type = bool if key == "estimate_missing_as_one" else str
            if not isinstance(value, expected_type):
                raise InspectorError("INVALID_ARGUMENT", f"{key} has an invalid value type.", {"filter": key}, snapshot_id=self.snapshot.snapshot_id)
        pawn_list = name == "pawn" and filters.get("section", "core") in {"skills", "traits"}
        if (name in {"inspect", "summary"} or name == "pawn" and not pawn_list) and (query.limit is not None or query.cursor is not None):
            raise InspectorError("INVALID_ARGUMENT", "Paging is not supported for this query.", snapshot_id=self.snapshot.snapshot_id)
        if query.limit is not None and (type(query.limit) is not int or query.limit < 1 or query.limit > 200):
            raise InspectorError("INVALID_ARGUMENT", "limit must be an integer from 1 through 200.", snapshot_id=self.snapshot.snapshot_id)
        normal, data, coverage, rows = self._run(name, filters)
        is_array = name in _ARRAY_QUERIES or (name == "pawn" and filters.get("section", "core") in {"skills", "traits"})
        if query.cursor is not None and not is_array:
            raise InspectorError("INVALID_ARGUMENT", "Paging is not supported for this query.", snapshot_id=self.snapshot.snapshot_id)
        effective_limit = query.limit if is_array and query.limit is not None else (50 if is_array else None)
        page = None
        if rows is not None:
            data, page = self._page(query, normal, data, rows, effective_limit or 50)
        envelope = QueryResponse(
            "1.0", self.snapshot.snapshot_id,
            {"name": name, "filters": normal, "limit": effective_limit},
            data, coverage,
            {"source_name": self.snapshot.source_name, "game_version": self.snapshot.game_version,
             "parser_version": self.snapshot.parser_version, "profile_version": self.snapshot.profile_version,
             "interpretation_id": self.snapshot.interpretation_id},
            page,
        )
        max_bytes = 524_288
        encoded = _json(envelope.as_dict())
        if len(encoded) > max_bytes and page is not None and rows is not None:
            mutable_data = thaw(envelope.data)
            entries = mutable_data.get("entries", [])
            offset = self._cursor_offset(query.cursor, name, normal, effective_limit or 50) if query.cursor else 0
            while len(encoded) > max_bytes and len(entries) > 1:
                entries.pop()
                next_offset = offset + len(entries)
                next_cursor = self._make_cursor(name, normal, effective_limit or 50, next_offset) if next_offset < len(rows) else None
                page = {"total": len(rows) if mutable_data.get("collection_state", "present") == "present" else None,
                        "returned": len(entries), "next_cursor": next_cursor}
                envelope = QueryResponse("1.0", self.snapshot.snapshot_id,
                    {"name": name, "filters": normal, "limit": effective_limit}, mutable_data, coverage,
                    {"source_name": self.snapshot.source_name, "game_version": self.snapshot.game_version,
                     "parser_version": self.snapshot.parser_version, "profile_version": self.snapshot.profile_version,
                     "interpretation_id": self.snapshot.interpretation_id}, page)
                encoded = _json(envelope.as_dict())
        if len(encoded) > max_bytes:
            raise InspectorError("RESULT_TOO_LARGE", "The response exceeds the supported JSON byte limit.",
                                 _narrower_query_details(name, normal), snapshot_id=self.snapshot.snapshot_id)
        return envelope

    def _run(self, name: str, filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]] | None]:
        if name == "inspect":
            data = self._inspect()
            return {}, data, self._coverage("all_supported"), None
        if name == "summary":
            data, normalized = self._summary(filters)
            incomplete_map = data["maps_state"] != "present" or any(item["things_state"] != "present" for item in data["maps"])
            player_unresolved = data["player_faction"]["status"] != "resolved"
            return normalized, data, self._coverage("map", force_partial=incomplete_map or player_unresolved), None
        if name == "pawns":
            return self._pawns(filters)
        if name in {"pawn", "health", "genes", "equipment", "inventory"}:
            return self._pawn_domain(name, filters)
        if name in {"research", "mods"}:
            return self._paired_domain(name, filters)
        raise InspectorError("INVALID_ARGUMENT", "Unknown query name.", {"query": name}, snapshot_id=self.snapshot.snapshot_id)

    def _inspect(self) -> dict[str, Any]:
        totals = {kind: len(self.snapshot.of_type(kind)) for kind in ("pawn", "item", "gene", "hediff", "map", "faction", "world_object", "skill", "trait", "research", "mod")}
        pawn_records = self.snapshot.of_type("pawn")
        # These aggregate counters describe the all_supported pawn population;
        # the type key above remains the required normalized record total.
        totals["record_count"] = len(pawn_records)
        totals["membership_count"] = sum(len(self._pawn_index(record)["memberships"]) for record in pawn_records)
        totals["resolved_saved_identity_count"] = sum(
            record.saved_id is not None and len(self.indexes.by_saved_id["pawn"].get(record.saved_id, ())) == 1
            for record in pawn_records
        )
        scopes = {scope: self._coverage(scope) for scope in ("map", "world_alive", "world_dead", "world_mothballed", "held", "all_supported")}
        collections = self.snapshot.collection_states
        domains: dict[str, str] = {}
        for domain in ("summary", "pawn", "health", "genes", "equipment", "inventory", "research", "mods"):
            relevant = [state for path, state in collections.items() if domain.lower() in path.lower()]
            domains[domain] = "absent" if not relevant else "partial" if any(s != "present" for s in relevant) else "present"
        return {"field_status": {}, "raw_values": {}, "derivations": [], "snapshot_id": self.snapshot.snapshot_id,
                "interpretation_id": self.snapshot.interpretation_id, "source_bytes": self.snapshot.source_bytes,
                "game_version": self.snapshot.game_version, "profile_version": self.snapshot.profile_version,
                "snapshot_lifetime": self.snapshot_lifetime, "record_totals": totals, "scope_coverage": scopes,
                "domain_availability": domains}

    def _coverage(self, scope: str, *, force_partial: bool = False, include_holder: bool = False) -> dict[str, Any]:
        pawn_candidate_affects_scope = scope in {"map", "all_supported"}
        diagnostics = [d for d in self.snapshot.diagnostics
                       if d.code != "UNSUPPORTED_PAWN_CANDIDATE" or
                       scope == "all_supported" or scope == "map" and _is_direct_map_thing(d.source_path)]
        counts = dict(self.snapshot.diagnostic_counts)
        if scope == "map":
            if self.snapshot.direct_map_candidate_count:
                counts["UNSUPPORTED_PAWN_CANDIDATE"] = self.snapshot.direct_map_candidate_count
            else:
                counts.pop("UNSUPPORTED_PAWN_CANDIDATE", None)
        elif not pawn_candidate_affects_scope:
            counts.pop("UNSUPPORTED_PAWN_CANDIDATE", None)
        if scope in {"held", "all_supported"} or include_holder:
            for diagnostic in self._holder_reference_diagnostics:
                counts[diagnostic.code] = counts.get(diagnostic.code, 0) + 1
                diagnostics.append(diagnostic)
            for cycle in self._holder_cycles:
                counts["REFERENCE_CYCLE"] = counts.get("REFERENCE_CYCLE", 0) + 1
                record = self._records.get(cycle[0])
                diagnostics.append(Diagnostic("REFERENCE_CYCLE", "warning", "holder reference cycle",
                                              record.source_path if record else None,
                                              record.record_id if record else None,
                                              {"record_ids": list(cycle)}))
        for cycle in self.indexes.reference_cycles:
            cycle_records = [self._records.get(record_id) for record_id in cycle]
            if any(record is not None and (
                scope in {"all_supported", "held"}
                or record.scope == scope
                or record.owner_record_id is not None
                and (owner := self._records.get(record.owner_record_id)) is not None
                and owner.scope == scope
            ) for record in cycle_records):
                counts["REFERENCE_CYCLE"] = counts.get("REFERENCE_CYCLE", 0) + 1
                record = next((r for r in cycle_records if r is not None), None)
                diagnostics.append(Diagnostic("REFERENCE_CYCLE", "warning", "gene override cycle", record.source_path if record else None,
                                              record.record_id if record else None, {"record_ids": list(cycle)}))
        status = "partial" if force_partial or diagnostics else "complete_for_scope"
        examples = sorted(diagnostics, key=lambda d: (d.code, d.source_path or ""))[:100]
        return {"status": status, "scope": scope, "diagnostic_counts": counts,
                "diagnostics": [{"code": d.code, "severity": d.severity, "message": d.message,
                                 "source_path": d.source_path, "record_id": d.record_id, "details": thaw(d.details)} for d in examples]}

    def _record_meta(self, record: Record) -> dict[str, Any]:
        names = _FIELD_NAMES.get(record.type_name, {})
        statuses = {names.get(path, path): state for path, state in record.field_status.items() if not record.type_name == "pawn" or not path.startswith("/genes/")}
        raw_values = {names.get(path, path): value for path, value in record.raw_values.items() if not record.type_name == "pawn" or not path.startswith("/genes/")}
        return {"record_id": record.record_id, "saved_id": record.saved_id, "source_path": record.source_path,
                "field_status": statuses, "raw_values": raw_values,
                "derivations": thaw(record.derivations)}

    def _name(self, pawn: Record) -> dict[str, Any]:
        first, nick, last = (_get(pawn, f"/name/{key}") for key in ("first", "nick", "last"))
        simple = _get(pawn, "/name")
        display = nick or " ".join(x for x in (first, last) if x) or simple or pawn.saved_id or pawn.record_id
        name_status = {f"/{field}": pawn.field_status[source] for field, source in (("first", "/name/first"), ("nick", "/name/nick"), ("last", "/name/last"), ("simple", "/name")) if source in pawn.field_status}
        return {"field_status": name_status, "raw_values": {}, "derivations": [{"field": "display", "rule": "nick-first-last-simple-id-v1", "inputs": ["first", "nick", "last", "simple", "saved_id"]}],
                "first": first, "nick": nick, "last": last, "simple": simple, "display": display}

    def _pawn_index(self, pawn: Record) -> dict[str, Any]:
        memberships = []
        for membership in memberships_for(self.snapshot, pawn):
            item = dict(membership)
            map_record_id = item.pop("map_record_id", None)
            if map_record_id and map_record_id in self._records:
                item["map_id"] = self._records[map_record_id].saved_id
            for field in ("scope", "map_id", "pool", "holder_id", "source_path"):
                item.setdefault(field, None)
            memberships.append(item)
        memberships.extend(dict(membership) for membership in self._reference_memberships.get(pawn.record_id, ()))
        return {**self._record_meta(pawn), "pawn_def": _get(pawn, "/def"), "kind_def": _get(pawn, "/kindDef"),
                "name": self._name(pawn), "faction": self.indexes.resolve(_get(pawn, "/faction"), "faction", state=_status(pawn, "/faction")).as_dict(),
                "memberships": memberships, "guest_status": _get(pawn, "/guest/guestStatus")}

    def _summary(self, filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        maps_state = self.snapshot.collection_states.get("/savegame[1]/game[1]/maps[1]", "absent")
        maps = list(self._records_by_type["map"])
        map_id = filters.get("map_id")
        normalized = {"map_id": map_id} if map_id is not None else {}
        if map_id is not None:
            matches = [m for m in maps if m.saved_id == map_id]
            if not matches and maps_state != "present":
                raise InspectorError("FILTER_UNAVAILABLE", "The map identity collection is incomplete.", snapshot_id=self.snapshot.snapshot_id)
            if not matches:
                raise InspectorError("MAP_NOT_FOUND", "No map matches the requested saved ID.", snapshot_id=self.snapshot.snapshot_id)
            if len(matches) > 1:
                raise InspectorError("AMBIGUOUS_MAP", "More than one map has the requested saved ID.", snapshot_id=self.snapshot.snapshot_id)
            maps = matches
        maps.sort(key=lambda m: _ordinal_key(m.source_path))
        player_ref = self._player_faction
        map_summaries = []
        all_pawns: list[Record] = []
        any_unavailable = maps_state != "present"
        any_unknown_map_pawn = False
        for map_record in maps:
            direct = [p for p in self._records_by_type["pawn"] if p.scope == "map" and _map_prefix(map_record.source_path, p.source_path)]
            all_pawns.extend(direct)
            unknown_map_pawn = map_record.source_path in self.snapshot.direct_map_candidate_map_paths
            any_unknown_map_pawn |= unknown_map_pawn
            things_state = self._map_things_state[map_record.record_id]
            if things_state != "present":
                any_unavailable = True
            parent_raw = _get(map_record, "/mapInfo/parent")
            parent_ref = self.indexes.resolve(parent_raw, "world_object", state=_status(map_record, "/mapInfo/parent"))
            parent = self._records.get(parent_ref.target_record_ids[0]) if parent_ref.status == "resolved" else None
            map_summaries.append({**self._record_meta(map_record), "map_id": map_record.saved_id,
                                  "parent": parent_ref.as_dict(), "parent_label": _get(parent, "/name", "/label", "/nameInt"),
                                  "parent_def": _get(parent, "/def"), "size_raw": _get(map_record, "/mapInfo/size"),
                                  "things_state": things_state,
                                  "direct_thing_count": _get(map_record, "/direct_thing_count") if things_state == "present" else None,
                                  "direct_pawn_count": (_get(map_record, "/direct_pawn_count", default=len(direct))
                                                         if things_state == "present" and not unknown_map_pawn else None)})
        if maps_state != "present":
            map_count = None
            map_summaries = []
        else:
            map_count = len(maps)
        total = None if any_unavailable or any_unknown_map_pawn else len(all_pawns)
        human = None if any_unavailable or any_unknown_map_pawn else sum(_get(p, "/def") == "Human" for p in all_pawns)
        player_human_set = [p for p in all_pawns if _get(p, "/def") == "Human" and _get(p, "/faction") == player_ref.raw]
        player_human = None if any_unavailable or any_unknown_map_pawn or player_ref.status != "resolved" else len(player_human_set)
        meta_fields = {"/game_version": "/gameVersion[1]", "/world_name": "/info/name[1]", "/world_seed": "/info/seedString[1]", "/ticks_game": "/ticksGame[1]"}
        field_status: dict[str, str] = {}
        raw_values: dict[str, str] = {}
        for output_path, suffix in meta_fields.items():
            path, raw = self._metadata_entry(suffix)
            status = self.snapshot.global_status.get(path, "absent") if path else "absent"
            value = _coerce_int(raw) if output_path == "/ticks_game" else raw
            if value is None:
                field_status[output_path] = status
            if status == "invalid" and raw is not None and output_path == "/ticks_game":
                raw_values[output_path] = str(raw)
        summary = {"field_status": field_status, "raw_values": raw_values, "derivations": [],
                   "game_version": self.snapshot.game_version, "world_name": self._metadata_entry("/info/name")[1],
                   "world_seed": self._metadata_entry("/info/seedString")[1], "ticks_game": _coerce_int(self._metadata_entry("/ticksGame")[1]),
                   "player_faction": player_ref.as_dict(), "maps_state": maps_state, "map_count": map_count, "maps": map_summaries,
                   "population": {"direct_map_pawn_records": total, "human_def_records": human, "player_human_records": player_human,
                                  "map_guest_status_counts": _partition(all_pawns, "/guest/guestStatus") if not any_unavailable else None,
                                  "map_health_state_counts": _partition(all_pawns, "/healthTracker/healthState") if not any_unavailable else None,
                                  "player_human_guest_status_counts": _partition(player_human_set, "/guest/guestStatus") if player_human is not None else None,
                                  "player_human_health_state_counts": _partition(player_human_set, "/healthTracker/healthState") if player_human is not None else None}}
        return summary, normalized

    def _pawns(self, filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        scope = filters.get("scope", "map")
        if scope not in _SCOPES:
            raise InspectorError("INVALID_ARGUMENT", "Unsupported pawn scope.", {"scope": scope}, snapshot_id=self.snapshot.snapshot_id)
        if "map_id" in filters and scope != "map":
            raise InspectorError("INVALID_ARGUMENT", "map_id is only valid with scope=map.", snapshot_id=self.snapshot.snapshot_id)
        normal = {"scope": scope}
        normal.update({k: v for k, v in filters.items() if k != "scope"})
        if filters.get("faction") == "player" and self._player_faction.status != "resolved":
            raise InspectorError("PLAYER_FACTION_UNRESOLVED", "The player faction could not be uniquely resolved.", snapshot_id=self.snapshot.snapshot_id)
        if scope == "map" and filters.get("map_id") is not None:
            maps = [m for m in self._records_by_type["map"] if m.saved_id == filters["map_id"]]
            if not maps:
                state = self.snapshot.collection_states.get("/savegame[1]/game[1]/maps[1]", "absent")
                raise InspectorError("FILTER_UNAVAILABLE" if state != "present" else "MAP_NOT_FOUND", "The requested map is unavailable.", snapshot_id=self.snapshot.snapshot_id)
            if len(maps) > 1:
                raise InspectorError("AMBIGUOUS_MAP", "More than one map has the requested saved ID.", snapshot_id=self.snapshot.snapshot_id)
            chosen_map = maps[0]
        else:
            chosen_map = None
        records = list(self._records_by_type["pawn"])
        if scope == "map":
            records = [r for r in records if r.scope == "map"]
        elif scope.startswith("world_"):
            records = [r for r in records if r.scope == scope or any(
                membership.get("scope") == scope for membership in self._reference_memberships.get(r.record_id, ()))]
        elif scope == "held":
            referenced = {record_id for record_id, memberships in self._reference_memberships.items()
                          if any(membership.get("scope") == "held" for membership in memberships)}
            records = [r for r in records if r.scope == "held" or r.owner_record_id or r.record_id in referenced]
        if chosen_map:
            records = [r for r in records if _map_prefix(chosen_map.source_path, r.source_path)]
        for key, path in (("pawn_def", "/def"), ("guest_status", "/guest/guestStatus")):
            if key in filters:
                wanted = filters[key]
                if wanted in {"absent", "explicit_null"}:
                    records = [r for r in records if _status(r, path) == wanted]
                else:
                    records = [r for r in records if _get(r, path) == wanted]
        if "faction" in filters:
            wanted = self._player_faction.raw if filters["faction"] == "player" else filters["faction"]
            records = [r for r in records if _get(r, "/faction") == wanted]
        if "name" in filters:
            needle = str(filters["name"]).casefold()
            records = [r for r in records if any(needle in str(_get(r, f"/name/{x}", "/name") or "").casefold() for x in ("first", "nick", "last"))]
        records = _sort_records(records)
        collection_state = self._scope_collection_state(scope, chosen_map)
        data = {"field_status": {}, "raw_values": {}, "derivations": [], "collection_state": collection_state,
                "entries": []}
        return normal, data, self._coverage(scope, force_partial=collection_state != "present"), records

    def _scope_collection_state(self, scope: str, map_record: Record | None) -> str:
        if scope == "map":
            states = [state for path, state in self.snapshot.collection_states.items() if path.endswith("/maps[1]") or path.endswith("/things[1]") and (map_record is None or path.startswith(map_record.source_path))]
            if not states or any(state != "present" for state in states):
                return "absent" if not states else next((s for s in states if s != "present"), "present")
            return "present"
        if scope.startswith("world_"):
            marker = {"world_alive": "pawnsAlive", "world_dead": "pawnsDead", "world_mothballed": "pawnsMothballed"}[scope]
            return next((state for path, state in self.snapshot.collection_states.items() if path.endswith(f"/{marker}[1]")), "absent")
        return "present"

    def _pawn_domain(self, name: str, filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]] | None]:
        record_id = filters.get("record_id")
        if not isinstance(record_id, str) or not record_id.startswith("pawn:"):
            raise InspectorError("INVALID_ARGUMENT", "record_id must be a pawn record ID.", snapshot_id=self.snapshot.snapshot_id)
        pawn = self._records.get(record_id)
        if pawn is None or pawn.type_name != "pawn":
            raise InspectorError("RECORD_NOT_FOUND", "The requested pawn record was not found.", snapshot_id=self.snapshot.snapshot_id)
        normalized = {"record_id": record_id}
        children = self._children.get(pawn.record_id, ())
        if name == "pawn":
            section = filters.get("section", "core")
            if section not in {"core", "skills", "traits"}:
                raise InspectorError("INVALID_ARGUMENT", "section must be core, skills, or traits.", snapshot_id=self.snapshot.snapshot_id)
            normalized["section"] = section
            if section == "core":
                child_counts = {kind: self._count_child(pawn, kind, path) for kind, path in (("skills", "skills/skills"), ("traits", "allTraits"), ("hediffs", "hediffs"), ("endogenes", "endogenes"), ("xenogenes", "xenogenes"), ("equipment", "equipment"), ("apparel", "wornApparel"), ("inventory", "inventory"))}
                core = {**self._pawn_index(pawn), "gender": _get(pawn, "/gender"), "age_biological_ticks": _coerce_int(_get(pawn, "/ageTracker/ageBiologicalTicks")),
                        "birth_abs_ticks": _coerce_int(_get(pawn, "/ageTracker/birthAbsTicks")),
                        "host_faction": self.indexes.resolve(_get(pawn, "/guest/hostFaction"), "faction", state=_status(pawn, "/guest/hostFaction")).as_dict(),
                        "slave_faction": self.indexes.resolve(_get(pawn, "/guest/slaveFaction"), "faction", state=_status(pawn, "/guest/slaveFaction")).as_dict(), "child_counts": child_counts}
                return normalized, {"field_status": {}, "raw_values": {}, "derivations": [], "pawn": core}, self._coverage(pawn.scope or "held"), None
            kind = "skill" if section == "skills" else "trait"
            entries = [self._domain_record(r) for r in _source_sort([r for r in children if r.type_name == kind])]
            state = self._child_collection_state(pawn, "skills/skills" if kind == "skill" else "allTraits")
            return normalized, {"field_status": {}, "raw_values": {}, "derivations": [], "pawn_record_id": record_id, "collection_state": state, "entries": entries}, self._coverage(pawn.scope or "held", force_partial=state != "present"), entries
        if name == "health":
            state = _get(pawn, "/healthTracker/healthState")
            entries = [self._domain_record(r) for r in _source_sort([r for r in children if r.type_name == "hediff"])]
            coll_state = self._child_collection_state(pawn, "hediffs")
            return normalized, {"field_status": {"/health_state": _status(pawn, "/healthTracker/healthState")} if state is None else {}, "raw_values": {}, "derivations": [], "pawn_record_id": record_id, "health_state": state, "collection_state": coll_state, "entries": entries}, self._coverage(pawn.scope or "held", force_partial=coll_state != "present"), entries
        if name == "genes":
            group = filters.get("group", "endogenes")
            if group not in {"endogenes", "xenogenes"}:
                raise InspectorError("INVALID_ARGUMENT", "group must be endogenes or xenogenes.", snapshot_id=self.snapshot.snapshot_id)
            normalized["group"] = group
            genes = [r for r in children if r.type_name == "gene"]
            chosen = _source_sort([r for r in genes if f"/{group}[" in r.source_path])
            entries = [self._domain_record(r) for r in chosen]
            header_status = {f"/{key}": pawn.field_status[path] for key, path in (("xenotype", "/genes/xenotype"), ("xenotype_name", "/genes/xenotypeName"), ("hybrid", "/genes/hybrid"), ("icon_def", "/genes/iconDef")) if path in pawn.field_status}
            header = {"field_status": header_status, "raw_values": {}, "derivations": [], "xenotype": _get(pawn, "/genes/xenotype"),
                      "xenotype_name": _get(pawn, "/genes/xenotypeName"), "hybrid": _coerce_bool(_get(pawn, "/genes/hybrid")), "icon_def": _get(pawn, "/genes/iconDef")}
            counts = {"endogenes": sum("/endogenes[" in r.source_path for r in genes), "xenogenes": sum("/xenogenes[" in r.source_path for r in genes)}
            return normalized, {"field_status": {}, "raw_values": {}, "derivations": [], "pawn_record_id": record_id, "header": header, "group": group,
                               "group_counts": counts, "collection_state": self._child_collection_state(pawn, group)}, self._coverage(pawn.scope or "held", force_partial=self._child_collection_state(pawn, group) != "present"), entries
        if name in {"equipment", "inventory"}:
            holder = filters.get("holder", "equipment") if name == "equipment" else filters.get("holder_id")
            if name == "equipment" and holder not in {"equipment", "apparel"}:
                raise InspectorError("INVALID_ARGUMENT", "holder must be equipment or apparel.", snapshot_id=self.snapshot.snapshot_id)
            normalized["holder" if name == "equipment" else "holder_id"] = holder
            selected_holder_path = None
            if name == "inventory" and holder is not None:
                selected_holder_path = self._owned_holder_path(pawn, holder)
                if selected_holder_path is None:
                    raise InspectorError("HOLDER_NOT_OWNED", "The requested holder is not reachable from this pawn.", snapshot_id=self.snapshot.snapshot_id)
            token = holder if name == "equipment" else "inventory"
            if selected_holder_path is None:
                selected_holder_path = _root_holder_path(pawn, token)
            if name == "inventory" and holder is not None:
                state = str(self.snapshot.collection_states.get(selected_holder_path, "absent"))
            elif name == "inventory":
                outer_state = self._child_collection_state(pawn, "inventory")
                state = (outer_state if outer_state != "present"
                         else self._child_collection_state(pawn, "inventory/innerContainer/innerList"))
            else:
                state = self._child_collection_state(
                    pawn, "equipment/equipment/innerList" if holder == "equipment" else "apparel/wornApparel/innerList")
            items = _source_sort(self._items_by_holder.get(selected_holder_path, []))
            item_entries = [{"entry_kind": "record", "source_path": r.source_path, "item": self._item(r), "reference": None, "pawn_record_id": None} for r in items]
            reference_entries = []
            for source_path, raw_reference in sorted(self._references_by_holder.get(selected_holder_path, ()), key=lambda pair: _ordinal_key(pair[0])):
                reference = self.indexes.resolve(raw_reference, "thing")
                target_id = reference.target_record_ids[0] if reference.status == "resolved" else None
                target = self._records.get(target_id) if target_id else None
                reference_entries.append({"entry_kind": "reference", "source_path": source_path, "item": None,
                                          "reference": reference.as_dict(),
                                          "pawn_record_id": target_id if target is not None and target.type_name == "pawn" else None})
            for held_pawn in self._pawns_by_holder.get(selected_holder_path, []):
                raw_reference = "Thing_" + held_pawn.saved_id if held_pawn.saved_id else None
                reference = self.indexes.resolve(raw_reference, "thing") if raw_reference else self.indexes.resolve(None, "thing", state="absent")
                reference_entries.append({"entry_kind": "reference", "source_path": held_pawn.source_path,
                                          "item": None, "reference": reference.as_dict(),
                                          "pawn_record_id": held_pawn.record_id})
            item_entries = sorted(item_entries + reference_entries, key=lambda e: _ordinal_key(e["source_path"]))
            item_identities = {r.record_id: r for r in items}
            for entry in reference_entries:
                for target_id in entry["reference"]["target_record_ids"]:
                    target = self._records.get(target_id)
                    if target is not None and target.type_name == "item":
                        item_identities[target_id] = target
            counts = [(_get(r, "/stackCount"), _status(r, "/stackCount")) for r in item_identities.values()]
            missing = sum(state == "absent" for _, state in counts)
            null_count = sum(state == "explicit_null" for _, state in counts)
            invalid = sum(state == "invalid" for _, state in counts)
            explicit = sum(_coerce_int(v) for v, item_state in counts if item_state == "present" and _coerce_int(v) is not None)
            unresolved = sum(entry["reference"]["status"] not in {"resolved", "explicit_null"}
                             or entry["reference"]["status"] == "resolved" and not entry["reference"]["target_record_ids"]
                             for entry in reference_entries)
            estimate = None
            if filters.get("estimate_missing_as_one", False) and not null_count and not invalid and not unresolved:
                estimate = explicit + missing
            selected_holder_id = holder if name == "inventory" and holder is not None else _root_holder_id(pawn, token)
            holder_data = {"holder_id": selected_holder_id, "collection_state": state, "entry_count": len(item_entries) if state == "present" else None}
            quantity = {"explicit_stack_units": explicit if state == "present" else None, "missing_stack_count_records": missing if state == "present" else None,
                        "null_stack_count_records": null_count if state == "present" else None, "invalid_stack_count_records": invalid if state == "present" else None,
                        "unresolved_item_entries": unresolved if state == "present" else None, "estimated_stack_units": estimate if state == "present" else None}
            data = {"field_status": {}, "raw_values": {}, "derivations": [], "pawn_record_id": record_id, "holder": holder_data,
                    "collection_state": state, "entry_count": len(item_entries) if state == "present" else None,
                    "item_record_count": len(items) if state == "present" else None,
                    "resolved_item_count": len(item_identities) if state == "present" else None,
                    "quantity": quantity, "entries": item_entries}
            return normalized, data, self._coverage(pawn.scope or "held", force_partial=state != "present", include_holder=True), item_entries
        raise InspectorError("INVALID_ARGUMENT", "Unknown pawn query.", snapshot_id=self.snapshot.snapshot_id)

    def _domain_record(self, record: Record) -> dict[str, Any]:
        common = self._record_meta(record)
        mappings = {
            "skill": {"skill_def": "/def", "level": ("/level", _coerce_int), "passion": "/passion", "xp_since_last_level": ("/xpSinceLastLevel", _coerce_float), "xp_since_midnight": ("/xpSinceMidnight", _coerce_float)},
            "trait": {"trait_def": "/def", "degree": ("/degree", _coerce_int), "source_gene": ("/sourceGene", "gene"), "suppressed_by": ("/suppressedBy", "gene")},
            "hediff": {"hediff_def": "/def", "saved_class": "/@Class", "severity": ("/severity", _coerce_float), "age_ticks": ("/ageTicks", _coerce_int), "tick_added": ("/tickAdded", _coerce_int), "visible": ("/visible", _coerce_bool), "chemical": "/chemical"},
            "gene": {"gene_def": "/def", "pawn": ("/pawn", "thing"), "overridden_by": ("/overriddenByGene", "gene")},
        }
        for key, spec in mappings.get(record.type_name, {}).items():
            if isinstance(spec, tuple) and len(spec) == 2 and callable(spec[1]):
                common[key] = spec[1](_get(record, spec[0]))
            elif isinstance(spec, tuple):
                common[key] = self.indexes.resolve(_get(record, spec[0]), spec[1], state=_status(record, spec[0])).as_dict()
            else:
                common[key] = _get(record, spec)
        if record.type_name == "hediff":
            body = _get(record, "/part/body")
            index = _coerce_int(_get(record, "/part/index"))
            part_status = {f"/{name}": record.field_status[path] for name, path in (("body", "/part/body"), ("index", "/part/index")) if path in record.field_status}
            common["part"] = None if body is None and index is None else {"field_status": part_status, "raw_values": {}, "derivations": [], "body": body, "index": index}
        return common

    def _child_collection_state(self, pawn: Record, marker: str) -> str:
        marker_tags = tuple(marker.split("/"))
        candidates = [(path, state) for path, state in self.snapshot.collection_states.items()
                      if path.startswith(pawn.source_path + "/") and re_tags(path)[-len(marker_tags):] == marker_tags]
        return candidates[0][1] if candidates else "absent"

    def _owned_holder_path(self, pawn: Record, holder_id: str) -> str | None:
        root_path = _root_holder_path(pawn, "inventory")
        candidates = {root_path}
        for item in self._records_by_type["item"]:
            if not item.source_path.startswith(root_path + "/li["):
                continue
            path = item.source_path + "/innerContainer[1]/innerList[1]"
            if path in self.snapshot.collection_states:
                candidates.add(path)
        for path in candidates:
            if record_id_for_holder(path) == holder_id:
                return path
        return None

    def _count_child(self, pawn: Record, type_name: str, marker: str) -> int | None:
        state = self._child_collection_state(pawn, marker)
        if state != "present":
            return None
        marker_tags = tuple(marker.split("/"))
        return sum(r.type_name == type_name and _contains_tag_sequence(re_tags(r.source_path), marker_tags)
                   for r in self._children.get(pawn.record_id, ()))

    def _item(self, record: Record) -> dict[str, Any]:
        nested_path = record.source_path + "/innerContainer[1]/innerList[1]"
        nested_state = self.snapshot.collection_states.get(nested_path)
        nested_holders = ([{"holder_id": record_id_for_holder(nested_path), "collection_state": str(nested_state),
                            "entry_count": self._holder_entry_count(nested_path) if nested_state == "present" else None}]
                          if nested_state is not None else [])
        return {**self._record_meta(record), "item_def": _get(record, "/def"), "saved_class": _get(record, "/@Class"),
                "stuff": _get(record, "/stuff"), "quality": _get(record, "/quality"), "hit_points": _coerce_int(_get(record, "/health")),
                "stack_count": _coerce_int(_get(record, "/stackCount")), "position_raw": _get(record, "/pos"), "nested_holders": nested_holders}

    def _holder_entry_count(self, path: str) -> int:
        return (len(self._items_by_holder.get(path, ())) + len(self._pawns_by_holder.get(path, ()))
                + len(self._references_by_holder.get(path, ())))

    def _paired_domain(self, name: str, filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        # Parallel arrays are extracted as independent source sides; no zip truncation is permitted.
        prefix = "/savegame[1]/game[1]/researchManager[1]" if name == "research" else "/savegame[1]/meta[1]"
        section = filters.get("section", "progress") if name == "research" else None
        if name == "research" and section not in {"progress", "techprints"}:
            raise InspectorError("INVALID_ARGUMENT", "section must be progress or techprints.", snapshot_id=self.snapshot.snapshot_id)
        normalized = {"section": section} if name == "research" else {}
        if name == "mods":
            key_pairs = self._ordered_globals("/meta[1]/modIds[1]/li[")
            value_pairs = self._ordered_globals("/meta[1]/modNames[1]/li[")
            key_state = _collection_state(self.snapshot, "/meta[1]/modIds[1]")
            value_state = _collection_state(self.snapshot, "/meta[1]/modNames[1]")
        else:
            collection = "progress" if section == "progress" else "techprints"
            key_pairs = self._ordered_globals(f"/researchManager[1]/{collection}[1]/keys[1]/li[")
            value_pairs = self._ordered_globals(f"/researchManager[1]/{collection}[1]/values[1]/li[")
            key_state = _collection_state(self.snapshot, f"{prefix}/{collection}[1]/keys[1]")
            value_state = _collection_state(self.snapshot, f"{prefix}/{collection}[1]/values[1]")
        keys = [v for _, v in key_pairs]
        vals = [v for _, v in value_pairs]
        rows: list[dict[str, Any]] = []
        if key_state == value_state == "present" and len(keys) == len(vals):
            for ordinal, (key, val) in enumerate(zip(keys, vals)):
                if name == "mods":
                    rows.append({"record_id": "mod:" + hashlib.sha256(key_pairs[ordinal][0].encode()).hexdigest(), "saved_id": key,
                                 "source_path": key_pairs[ordinal][0], "field_status": {}, "raw_values": {}, "derivations": [],
                                 "ordinal": ordinal, "package_id": key, "display_name": val,
                                 "id_source_path": key_pairs[ordinal][0], "name_source_path": value_pairs[ordinal][0]})
                elif section == "progress":
                    rows.append({"record_id": "research:" + hashlib.sha256(key_pairs[ordinal][0].encode()).hexdigest(), "saved_id": key,
                                 "source_path": key_pairs[ordinal][0], "field_status": {}, "raw_values": {"/progress_points": val}, "derivations": [],
                                 "ordinal": ordinal, "project_def": key, "progress_points": _coerce_float(val),
                                 "key_source_path": key_pairs[ordinal][0], "value_source_path": value_pairs[ordinal][0]})
                else:
                    rows.append({"record_id": "research:" + hashlib.sha256(key_pairs[ordinal][0].encode()).hexdigest(), "saved_id": key,
                                 "source_path": key_pairs[ordinal][0], "field_status": {}, "raw_values": {"/techprint_count": val}, "derivations": [],
                                 "ordinal": ordinal, "project_def": key, "techprint_count": _coerce_int(val),
                                 "key_source_path": key_pairs[ordinal][0], "value_source_path": value_pairs[ordinal][0]})
            alignment = "paired"
        else:
            alignment = "unpaired"
            rows.extend({"side": "key" if name == "research" else "id", "ordinal": i, "value": v, "source_path": p, "field_status": {}, "raw_values": {}, "derivations": []} for i, (p, v) in enumerate(key_pairs))
            rows.extend({"side": "value" if name == "research" else "name", "ordinal": i, "value": v, "source_path": p, "field_status": {}, "raw_values": {}, "derivations": []} for i, (p, v) in enumerate(value_pairs))
        if name == "research" and "project_def" in filters:
            if alignment != "paired":
                raise InspectorError("FILTER_UNAVAILABLE", "The research arrays are not positionally aligned.", snapshot_id=self.snapshot.snapshot_id)
            rows = [r for r in rows if r.get("project_def") == filters["project_def"]]
            normalized["project_def"] = filters["project_def"]
        current = self._metadata_entry("/currentProj")[1] if name == "research" else None
        collection_state = "present" if key_state == value_state == "present" else key_state if key_state != "present" else value_state
        data = {"field_status": {}, "raw_values": {}, "derivations": [], "alignment": alignment,
                "collection_state": collection_state, "entries": rows}
        if name == "research":
            data.update({"current_project": current, "section": section,
                         "key_count": len(keys) if key_state == "present" else None,
                         "value_count": len(vals) if value_state == "present" else None})
        else:
            data.update({"id_count": len(keys) if key_state == "present" else None,
                         "name_count": len(vals) if value_state == "present" else None})
        return normalized, data, self._coverage("all_supported", force_partial=alignment == "unpaired" or collection_state != "present"), rows

    def _metadata_entry(self, suffix: str) -> tuple[str | None, Any]:
        return self._metadata_globals.get(re.sub(r"\[\d+\]", "", suffix), (None, None))

    def _ordered_globals(self, marker: str) -> list[tuple[str, str]]:
        return sorted((pair for pair in self._array_globals if marker in pair[0]), key=lambda pair: _ordinal_key(pair[0]))

    def _page(self, query: QueryRequest, filters: dict[str, Any], data: dict[str, Any], rows: list[dict[str, Any]] | list[Record], limit: int) -> tuple[dict[str, Any], dict[str, Any]]:
        offset = self._cursor_offset(query.cursor, query.name, filters, limit) if query.cursor else 0
        if offset > len(rows):
            raise InspectorError("INVALID_CURSOR", "The cursor offset is outside the result set.", snapshot_id=self.snapshot.snapshot_id)
        end = min(offset + limit, len(rows))
        selected = rows[offset:end]
        if query.name == "pawns":
            selected = [self._pawn_index(record) for record in selected]
        page_data = {**data, "entries": selected}
        next_cursor = self._make_cursor(query.name, filters, limit, end) if end < len(rows) else None
        result_page = {"total": len(rows), "returned": len(selected), "next_cursor": next_cursor}
        while selected and len(_json({"data": page_data, "page": result_page})) > 524_288:
            selected = selected[:-1]
            end = offset + len(selected)
            page_data = {**data, "entries": selected}
            next_cursor = self._make_cursor(query.name, filters, limit, end) if end < len(rows) else None
            result_page = {"total": len(rows), "returned": len(selected), "next_cursor": next_cursor}
        total: int | None = len(rows) if data.get("collection_state", "present") == "present" else None
        result_page["total"] = total
        if not selected and len(rows) > offset:
            raise InspectorError("RESULT_TOO_LARGE", "A single result entry exceeds the response byte limit.",
                                 _narrower_query_details(query.name, filters), snapshot_id=self.snapshot.snapshot_id)
        return page_data, result_page

    def _cursor_payload(self, name: str, filters: dict[str, Any], limit: int, offset: int) -> dict[str, Any]:
        sort_rule = "saved-order-v1" if name in {"research", "mods"} else "source-ordinal-v1" if name in {"health", "genes", "equipment", "inventory"} or name == "pawn" and filters.get("section") in {"skills", "traits"} else "saved-id-record-id-v1"
        return {"version": 1, "snapshot_id": self.snapshot.snapshot_id, "interpretation_id": self.snapshot.interpretation_id,
                "query": name, "filters": filters, "limit": limit, "sort": sort_rule, "offset": offset}

    def _make_cursor(self, name: str, filters: dict[str, Any], limit: int, offset: int) -> str:
        payload = self._cursor_payload(name, filters, limit, offset)
        checksum = hashlib.sha256(_json(payload)).hexdigest()
        packed = _json({"payload": payload, "sha256": checksum})
        return base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")

    def _cursor_offset(self, cursor: str, name: str, filters: dict[str, Any], limit: int) -> int:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            parsed = json.loads(raw)
            payload = parsed["payload"]
            if parsed["sha256"] != hashlib.sha256(_json(payload)).hexdigest():
                raise ValueError("checksum")
            expected = self._cursor_payload(name, filters, limit, payload["offset"])
            if payload != expected or not isinstance(payload["offset"], int) or payload["offset"] < 0:
                raise ValueError("binding")
            return payload["offset"]
        except Exception as exc:
            raise InspectorError("INVALID_CURSOR", "The cursor is malformed or does not match this query.", snapshot_id=self.snapshot.snapshot_id) from exc


def _ordinal_key(path: str) -> tuple[int, ...]:
    import re
    return tuple(int(x) for x in re.findall(r"\[(\d+)\]", path))


def _source_sort(records: list[Record]) -> list[Record]:
    return sorted(records, key=lambda r: _ordinal_key(r.source_path))


def _ordered_globals(snapshot: Snapshot, marker: str) -> list[tuple[str, str]]:
    pairs = [(path, str(value)) for path, value in snapshot.globals.items() if marker in path]
    return sorted(pairs, key=lambda pair: _ordinal_key(pair[0]))


def _collection_state(snapshot: Snapshot, path: str) -> str:
    if path in snapshot.collection_states:
        return str(snapshot.collection_states[path])
    matches = [state for candidate, state in snapshot.collection_states.items() if candidate.endswith(path)]
    return str(matches[0]) if matches else "absent"


def _item_in_holder(record: Record, holder: Any) -> bool:
    tags = re_tags(record.source_path)
    wanted = {"equipment": ("equipment", "equipment", "innerList", "li"),
              "apparel": ("apparel", "wornApparel", "innerList", "li"),
              "inventory": ("inventory", "innerContainer", "innerList", "li")}.get(str(holder))
    return wanted is not None and tags[-4:] == wanted


def _root_holder_id(pawn: Record, holder: Any) -> str:
    return record_id_for_holder(_root_holder_path(pawn, holder))


def _root_holder_path(pawn: Record, holder: Any) -> str:
    suffix = {"equipment": "equipment[1]/equipment[1]/innerList[1]", "apparel": "apparel[1]/wornApparel[1]/innerList[1]",
              "inventory": "inventory[1]/innerContainer[1]/innerList[1]"}.get(str(holder), "inventory[1]/innerContainer[1]/innerList[1]")
    return pawn.source_path + "/" + suffix


def record_id_for_holder(path: str) -> str:
    return record_id_for_path("holder", path)


def re_tags(path: str) -> tuple[str, ...]:
    import re
    return tuple(re.findall(r"/([^/\[]+)\[\d+\]", path))


def _contains_tag_sequence(tags: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    return any(tags[index:index + len(sequence)] == sequence for index in range(len(tags) - len(sequence) + 1))


def _is_supported_reference_path(path: str) -> bool:
    tags = re_tags(path)
    if not tags or tags[-1] != "li":
        return False
    joined = "/".join(tags)
    return ("pawnsForcefullyKeptAsWorldPawns" in tags
            or "innerContainer" in tags and ("things" in tags or "inventory" in tags or "transportShipManager" in tags)
            or "pawns" in tags and ("worldObjects" in tags or "transportShipManager" in tags))


def _world_pool_reference(path: str) -> str | None:
    tags = re_tags(path)
    if len(tags) < 3 or not (tags[-1] == "li" or tags[-1] == "id" and tags[-2] == "li"):
        return None
    if "worldPawns" not in tags:
        return None
    for marker, pool in (("pawnsAlive", "alive"), ("pawnsDead", "dead"), ("pawnsMothballed", "mothballed")):
        if marker in tags:
            return pool
    return None


def _map_prefix(map_path: str, pawn_path: str) -> bool:
    return pawn_path.startswith(map_path + "/things[")


def _is_direct_map_thing(path: str | None) -> bool:
    return path is not None and re_tags(path)[-4:] == ("maps", "li", "things", "thing")


def _global(snapshot: Snapshot, suffix: str) -> Any:
    normalized_suffix = re.sub(r"\[\d+\]", "", suffix)
    return next((v for path, v in snapshot.globals.items()
                 if re.sub(r"\[\d+\]", "", path).endswith(normalized_suffix)), None)


def _global_entry(snapshot: Snapshot, suffix: str) -> tuple[str | None, Any]:
    normalized_suffix = re.sub(r"\[\d+\]", "", suffix)
    return next(((path, value) for path, value in snapshot.globals.items()
                 if re.sub(r"\[\d+\]", "", path).endswith(normalized_suffix)), (None, None))


def _narrower_query_details(name: str, filters: dict[str, Any]) -> dict[str, str]:
    available: dict[str, tuple[str, ...]] = {
        "summary": ("map_id",),
        "pawns": ("map_id", "faction", "pawn_def", "name", "guest_status"),
        "equipment": ("holder",),
        "inventory": ("holder_id",),
        "research": ("project_def",),
    }
    selectors = [selector for selector in available.get(name, ()) if selector not in filters]
    if selectors:
        return {"narrower_query": "Try a supported selector: " + ", ".join(selectors) + "."}
    return {"narrower_query": "No narrower query is available in v1."}


def _partition(records: list[Record], path: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in records:
        value = _get(record, path)
        state = _status(record, path)
        key = f"stored:{value}" if value is not None else state
        counts[key] += 1
    return dict(sorted(counts.items()))


def _state_for(record: Record, path: str, has_records: bool) -> str:
    if has_records:
        return "present"
    return "present" if any(key.startswith(path) for key in record.values) else "absent"


def _coerce_int(value: Any) -> int | None:
    try:
        text = str(value).strip()
        if not text or any(ch in text for ch in ".eE"):
            return None
        number = int(text, 10)
        return number if -(2**63) <= number < 2**63 else None
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    if value in {"True", "true"}:
        return True
    if value in {"False", "false"}:
        return False
    return None
