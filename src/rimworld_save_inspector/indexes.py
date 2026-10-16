"""Typed saved-identity indexes and explicit membership helpers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .models import Record, Snapshot


@dataclass(frozen=True, slots=True)
class Reference:
    raw: str | None
    namespace: str | None
    normalized_id: str | None
    status: str
    target_record_ids: tuple[str, ...] = ()
    rule: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "raw": self.raw,
            "namespace": self.namespace,
            "normalized_id": self.normalized_id,
            "status": self.status,
            "target_record_ids": list(self.target_record_ids),
            "rule": self.rule,
        }


class IdentityIndexes:
    def __init__(self, records: Iterable[Record]) -> None:
        self.records = tuple(records)
        self.by_id = {record.record_id: record for record in self.records}
        self.typed: dict[str, dict[str, list[Record]]] = defaultdict(lambda: defaultdict(list))
        self.by_saved_id: dict[str, dict[str, list[Record]]] = defaultdict(lambda: defaultdict(list))
        for record in self.records:
            if record.saved_id is None:
                continue
            self.by_saved_id[record.type_name][record.saved_id].append(record)
            namespace, key = self._index_key(record)
            if namespace and key is not None:
                self.typed[namespace][key].append(record)
        self.reference_cycles = self._find_gene_cycles()

    def _find_gene_cycles(self) -> tuple[tuple[str, ...], ...]:
        graph: dict[str, str] = {}
        for record in self.records:
            if record.type_name != "gene":
                continue
            raw = record.value("/overriddenByGene")
            ref = self.resolve(raw, "gene", state=str(record.field_status.get("/overriddenByGene", "present")))
            if ref.status == "resolved" and ref.target_record_ids:
                graph[record.record_id] = ref.target_record_ids[0]
        done: set[str] = set()
        cycles: set[tuple[str, ...]] = set()
        for start in graph:
            path: list[str] = []
            positions: dict[str, int] = {}
            node = start
            while node in graph and node not in done:
                if node in positions:
                    cycle = path[positions[node]:]
                    rotations = [tuple(cycle[i:] + cycle[:i]) for i in range(len(cycle))]
                    cycles.add(min(rotations))
                    break
                positions[node] = len(path)
                path.append(node)
                node = graph[node]
            done.update(path)
        return tuple(sorted(cycles))

    @staticmethod
    def _index_key(record: Record) -> tuple[str | None, str | None]:
        if record.type_name in {"pawn", "item"}:
            return "thing", record.saved_id.removeprefix("Thing_") if record.saved_id is not None else None
        if record.type_name in {"faction", "gene", "hediff", "world_object"}:
            prefix = {"faction": "Faction_", "gene": "Gene_", "hediff": "Hediff_", "world_object": "WorldObject_"}[record.type_name]
            return record.type_name, prefix + record.saved_id if record.saved_id is not None else None
        return None, None

    def resolve(self, raw: str | None, namespace: str, *, state: str = "present") -> Reference:
        if state == "absent":
            return Reference(None, namespace, None, "absent")
        if state == "explicit_null" or (raw is not None and raw.strip() == "null"):
            return Reference(None, namespace, None, "explicit_null")
        if raw is None or not raw.strip():
            return Reference(None, namespace, None, "invalid")
        text = raw.strip()
        if namespace == "thing":
            normalized = text.removeprefix("Thing_")
            rule = "thing-prefix-v1"
        elif namespace in {"faction", "gene", "hediff", "world_object"}:
            prefix = {"faction": "Faction_", "gene": "Gene_", "hediff": "Hediff_", "world_object": "WorldObject_"}[namespace]
            normalized = text if text.startswith(prefix) else prefix + text
            rule = f"{namespace}-prefix-v1"
        else:
            return Reference(text, namespace, text, "unresolved")
        targets = tuple(record.record_id for record in self.typed.get(namespace, {}).get(normalized, ()))
        status = "resolved" if len(targets) == 1 else "ambiguous" if len(targets) > 1 else "unresolved"
        return Reference(text, namespace, normalized, status, targets, rule)

    def player_faction(self, snapshot: Snapshot) -> Reference:
        scenario_fields = [(path, value) for path, value in snapshot.globals.items() if path.endswith("/factionDef[1]")]
        if scenario_fields:
            scenario_path, scenario_value = scenario_fields[0]
            if snapshot.global_status.get(scenario_path) == "invalid":
                return Reference(None, "faction", None, "invalid")
            if snapshot.global_status.get(scenario_path) == "explicit_null":
                return Reference(None, "faction", None, "explicit_null")
            scenario_def = str(scenario_value) if scenario_value is not None else None
            candidates = [r for r in self.records if r.type_name == "faction" and r.value("/def") == scenario_def]
            raw = candidates[0].saved_id if len(candidates) == 1 else None
            if len(candidates) != 1:
                return Reference(f"Faction_{scenario_def}", "faction", f"Faction_{scenario_def}", "ambiguous" if candidates else "unresolved", tuple(r.record_id for r in candidates), "scenario-factionDef-v1")
            return self.resolve(f"Faction_{raw}", "faction")
        candidates = [r for r in self.records if r.type_name == "faction" and r.value("/def") == "PlayerColony"]
        if len(candidates) == 1:
            raw = candidates[0].saved_id
            ref = self.resolve(f"Faction_{raw}", "faction")
            return Reference(ref.raw, ref.namespace, ref.normalized_id, ref.status, ref.target_record_ids, "player-colony-fallback-v1")
        return Reference(None, "faction", None, "ambiguous" if candidates else "unresolved")


def _global_suffix(snapshot: Snapshot, suffix: str) -> str | None:
    return next((str(value) for path, value in snapshot.globals.items() if path.endswith(suffix) and value is not None), None)


def memberships_for(snapshot: Snapshot, record: Record) -> tuple[dict[str, object], ...]:
    memberships = list(record.memberships)
    if record.scope == "map" and not any(m.get("scope") == "map" for m in memberships):
        memberships.append({"scope": "map", "map_id": None, "pool": None, "holder_id": None, "source_path": record.source_path})
    elif record.scope and record.scope.startswith("world_") and not any(m.get("scope") == record.scope for m in memberships):
        memberships.append({"scope": record.scope, "map_id": None, "pool": record.scope.removeprefix("world_"), "holder_id": None, "source_path": record.source_path})
    if record.owner_record_id and not any(m.get("scope") == "held" for m in memberships):
        memberships.append({"scope": "held", "map_id": None, "pool": None, "holder_id": None, "source_path": record.source_path})
    unique: dict[tuple[object, ...], dict[str, object]] = {}
    for membership in memberships:
        key = tuple(membership.get(field) for field in ("scope", "map_id", "map_record_id", "pool", "holder_id", "source_path"))
        unique[key] = dict(membership)
    return tuple(unique.values())
