"""Deterministic evidence-first discovery over HistorySnapshot mappings.

The module never opens save files and never changes authoritative history facts.
Semantic adapters are opt-in local callbacks over a deterministic evidence packet.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from ..history.identity import campaign_identity


DISCOVERY_VERSION = "history-discovery/1.0"
EVIDENCE_RUBRIC_VERSION = "discovery-evidence-rubric/1.0"
_TOP_LEVEL_KEYS = (
    "snapshot_id", "interpretation_id", "metadata", "pawns", "events",
    "context", "evidence", "coverage", "diagnostics",
)
_SOURCE_OUTCOMES = {"parsed", "failed", "excluded", "unavailable"}
_FORBIDDEN_ANNOTATION_PATTERNS = (
    re.compile(r"\b(?:caused|causes|because of|led to|resulted in|explains)\b", re.I),
    re.compile(r"\b(?:motive|intended|intentionally|consented|voluntarily chose)\b", re.I),
    re.compile(r"\b(?:proves? (?:death|departure|resurrection)|absence proves?)\b", re.I),
)
RUBRIC = {
    "version": EVIDENCE_RUBRIC_VERSION,
    "rules": [
        "Every factual clause needs source-linked evidence supporting each material proposition.",
        "Join entities only by stable saved identity and validated campaign identity; names are aliases, never join keys.",
        "Keep contradictory observations and participant perspectives separate.",
        "Separate recorded events, state observations, differences, and inferences.",
        "Do not infer motive, consent, moral character, death, resurrection, or causation from absence, labels, or correlation.",
        "Keep role and xenotype labels separate from active effects, overridden genes, and suppression.",
        "Label each source channel explicitly; recorder observations are separate from save XML and pre-install recorder questions abstain.",
        "Unknown, ambiguous, unsupported, and incomplete coverage require abstention and a bounded follow-up.",
        "Account for each source outcome; never silently prune low-ranked records.",
    ],
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _is_object(value: Any) -> bool:
    return isinstance(value, Mapping)


def _validate_snapshot(snapshot: Mapping[str, Any]) -> None:
    if not _is_object(snapshot) or set(snapshot) != set(_TOP_LEVEL_KEYS):
        raise ValueError("history snapshot must have the exact nine-key envelope")
    if not isinstance(snapshot.get("snapshot_id"), str) or len(snapshot["snapshot_id"]) != 64:
        raise ValueError("snapshot_id must be a 64-character source digest")
    if not isinstance(snapshot.get("interpretation_id"), str) or not snapshot["interpretation_id"]:
        raise ValueError("interpretation_id is required")
    for key in ("metadata", "context", "coverage"):
        if not _is_object(snapshot.get(key)):
            raise ValueError(f"{key} must be an object")
    for key in ("pawns", "events", "evidence", "diagnostics"):
        if not isinstance(snapshot.get(key), (list, tuple)):
            raise ValueError(f"{key} must be an array")


def _campaign(snapshot: Mapping[str, Any]) -> tuple[str | None, str]:
    value = campaign_identity(snapshot["metadata"])
    return (value, "validated") if value is not None else (None, "review-needed")


def _iter_claims(snapshot: Mapping[str, Any]) -> Iterable[tuple[str, str, Mapping[str, Any]]]:
    """Yield (owner record id, domain, claim) without flattening away provenance."""
    snapshot_id = snapshot["snapshot_id"]
    metadata = snapshot["metadata"]
    for group, values in (("metadata", metadata), ("context", snapshot["context"])):
        yield from _walk_claims(values, f"{snapshot_id}:{group}", group)
    for pawn in snapshot["pawns"]:
        if not _is_object(pawn):
            continue
        owner = str(pawn.get("record_id") or f"{snapshot_id}:pawn:{pawn.get('pawn_id')}")
        for index, alias in enumerate(pawn.get("aliases", [])):
            if _is_object(alias):
                yield owner, "aliases", alias
        yield from _walk_claims(pawn.get("domains", {}), owner, "pawn")
    for index, event in enumerate(snapshot["events"]):
        if not _is_object(event):
            continue
        owner = str(event.get("record_id") or f"{snapshot_id}:event:{index + 1}")
        yield from _walk_claims(event.get("claims", []), owner, "event")


def _walk_claims(value: Any, owner: str, domain: str) -> Iterable[tuple[str, str, Mapping[str, Any]]]:
    if _is_object(value):
        if "field" in value and "status" in value:
            yield owner, domain, value
            return
        claims = value.get("claims")
        if isinstance(claims, (list, tuple)):
            for claim in claims:
                if _is_object(claim):
                    yield owner, domain, claim
        for key, nested in value.items():
            if key != "claims" and _is_object(nested):
                yield from _walk_claims(nested, owner, str(key))
            elif key != "claims" and isinstance(nested, (list, tuple)):
                for item in nested:
                    if _is_object(item):
                        yield from _walk_claims(item, owner, str(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            if _is_object(item):
                if "field" in item and "status" in item:
                    yield owner, domain, item
                else:
                    yield from _walk_claims(item, owner, domain)


def _claim_record(snapshot: Mapping[str, Any], owner: str, domain: str,
                  claim: Mapping[str, Any]) -> dict[str, Any]:
    ref = {key: claim.get(key) for key in ("evidence_id", "source_ref")}
    record = {
        **deepcopy(dict(claim)),
        "snapshot_id": snapshot["snapshot_id"],
        "interpretation_id": snapshot["interpretation_id"],
        "owner_record_id": owner,
        "domain": domain,
        "field": claim.get("field"),
        "value": deepcopy(claim.get("value")),
        "raw_value": deepcopy(claim.get("raw_value")),
        "status": claim.get("status", "unknown"),
        "unit": claim.get("unit"),
        "code": claim.get("code"),
        "display_label": claim.get("display_label"),
        "normalization_rule": claim.get("normalization_rule"),
        "time_observation": deepcopy(claim.get("time_observation")),
        "evidence_id": ref["evidence_id"],
        "source_ref": ref["source_ref"],
    }
    if claim.get("premise_refs") is not None:
        record["premise_refs"] = deepcopy(claim["premise_refs"])
    return record


def _clock_observations(claims: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for claim in claims:
        field = claim.get("field")
        observation = claim.get("time_observation")
        if _is_object(observation):
            clock = observation.get("clock", "unknown")
            result.append({"field": field, "value": deepcopy(claim.get("value")),
                           **deepcopy(dict(observation))})
        elif field in {"clock.ticksGame", "clock.gameStartAbsTick", "clock.ticksAbs"}:
            clock = {"clock.ticksGame": "game_tick", "clock.gameStartAbsTick": "absolute_tick",
                     "clock.ticksAbs": "absolute_tick"}[field]
            result.append({"field": field, "value": deepcopy(claim.get("value")), "clock": clock,
                           "normalized_game_tick": None, "status": claim.get("status", "unknown"),
                           "evidence_id": claim.get("evidence_id"), "source_ref": claim.get("source_ref")})
        elif field == "clock.startingYear":
            result.append({"field": field, "value": deepcopy(claim.get("value")), "clock": "calendar_year",
                           "normalized_game_tick": None, "status": claim.get("status", "unknown"),
                           "evidence_id": claim.get("evidence_id"), "source_ref": claim.get("source_ref")})
        elif isinstance(field, str) and any(term in field.lower() for term in ("biologicalage", "biological_age", "age.biological")):
            result.append({"field": field, "value": deepcopy(claim.get("value")), "clock": "biological_duration",
                           "normalized_game_tick": None, "status": claim.get("status", "unknown"),
                           "evidence_id": claim.get("evidence_id"), "source_ref": claim.get("source_ref")})
    return sorted(result, key=lambda row: (str(row.get("field")), str(row.get("source_ref"))))


def _evidence_ledger(snapshot: Mapping[str, Any], claims: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    evidence = []
    for item in snapshot["evidence"]:
        if _is_object(item):
            evidence.append({key: item.get(key) for key in ("evidence_id", "source_ref", "snapshot_id", "path")})
    cited = {(c.get("evidence_id"), c.get("source_ref")) for c in claims
             if c.get("evidence_id") is not None or c.get("source_ref") is not None}
    used = [item for item in evidence if (item.get("evidence_id"), item.get("source_ref")) in cited]
    unused = [item for item in evidence if (item.get("evidence_id"), item.get("source_ref")) not in cited]
    missing = [claim for claim in claims if not claim.get("evidence_id") or not claim.get("source_ref")]
    evidence_pairs = {(item.get("evidence_id"), item.get("source_ref")) for item in evidence}
    mismatched = [claim for claim in claims if claim.get("evidence_id") and claim.get("source_ref")
                  and (claim.get("evidence_id"), claim.get("source_ref")) not in evidence_pairs]
    return {"total": len(evidence), "used": used, "unused": unused,
            "claims_missing_provenance": missing, "claims_without_matching_evidence": mismatched}


def _record(snapshot: Mapping[str, Any], kind: str, saved_id: str, identity: str,
            campaign_id: str | None, payload: Mapping[str, Any]) -> dict[str, Any]:
    record_id = payload.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        record_id = f"{snapshot['snapshot_id']}:{kind}:{identity}"
    return {"record_id": record_id,
            "snapshot_id": snapshot["snapshot_id"], "interpretation_id": snapshot["interpretation_id"],
            "kind": kind, "saved_id": saved_id, "campaign_id": campaign_id,
            "payload": deepcopy(dict(payload))}


def _snapshot_result(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    _validate_snapshot(snapshot)
    sid = snapshot["snapshot_id"]
    campaign_id, campaign_status = _campaign(snapshot)
    records: list[dict[str, Any]] = []
    pawns = []
    for pawn in snapshot["pawns"]:
        if not _is_object(pawn):
            continue
        pawn_id = pawn.get("pawn_id")
        identity = str(pawn_id) if isinstance(pawn_id, str) and pawn_id else str(pawn.get("record_id", "unknown"))
        item = _record(snapshot, "pawn", identity, identity, campaign_id, pawn)
        item["identity_status"] = "saved_id" if isinstance(pawn_id, str) and pawn_id else "snapshot_only"
        records.append(item)
        pawns.append(item)
    events = []
    for index, event in enumerate(snapshot["events"]):
        if not _is_object(event):
            continue
        saved_id = event.get("saved_event_id")
        if not isinstance(saved_id, str) or not saved_id:
            saved_id = str(event.get("record_id") or f"{event.get('kind', 'event')}:{index + 1}")
            identity_status = "snapshot_only"
        else:
            identity_status = "saved_event_id"
        item = _record(snapshot, "event", saved_id, saved_id, campaign_id, event)
        item["identity_status"] = identity_status
        events.append(item)
        records.append(item)
    claims = [_claim_record(snapshot, owner, domain, claim)
              for owner, domain, claim in _iter_claims(snapshot)]
    refs = {(item.get("evidence_id"), item.get("source_ref")) for item in snapshot["evidence"] if _is_object(item)}
    provenance_gaps = [claim for claim in claims
                       if (claim.get("evidence_id"), claim.get("source_ref")) not in refs]
    records.sort(key=lambda row: row["record_id"])
    return {
        "discovery_schema_version": DISCOVERY_VERSION,
        "source": {"snapshot_id": sid, "interpretation_id": snapshot["interpretation_id"],
                   "campaign_id": campaign_id, "campaign_identity_status": campaign_status},
        "records": records,
        "entities": [{"entity_id": f"snapshot:{sid}:pawn:{p['saved_id']}", "kind": "pawn",
                      "saved_id": p["saved_id"], "campaign_id": campaign_id,
                      "snapshot_ids": [sid], "record_ids": [p["record_id"]],
                      "aliases": _pawn_aliases(p["payload"])} for p in pawns],
        "episodes": ([{"episode_id": f"snapshot:{sid}:event:{e['saved_id']}", "kind": "recorded_event",
                       "saved_event_id": e["saved_id"], "campaign_id": campaign_id,
                       "identity_status": e["identity_status"],
                       "snapshot_id": sid, "record_ids": [e["record_id"]],
                       "participants": deepcopy(e["payload"].get("participants", [])),
                       "occurrences": [deepcopy(e["payload"].get("occurrence"))]}
                      for e in events] +
                     [{"episode_id": f"snapshot:{sid}:observation:{p['saved_id']}", "kind": "state_observation",
                       "saved_pawn_id": p["saved_id"], "campaign_id": campaign_id,
                       "snapshot_id": sid, "record_ids": [p["record_id"]],
                       "evidence_refs": deepcopy(p["payload"].get("evidence_refs", [])),
                       "scope": p["payload"].get("scope"),
                       "status": deepcopy(p["payload"].get("status"))}
                      for p in pawns]),
        "claims": claims,
        "support_and_counterevidence": [],
        "perspectives": [{"snapshot_id": claim["snapshot_id"], "subject_record_id": claim["owner_record_id"],
                           "field": claim["field"], "value": deepcopy(claim["value"]),
                           "status": claim["status"], "evidence_id": claim["evidence_id"],
                           "source_ref": claim["source_ref"],
                           "interpretation": "recorded perspective; motive and objective cause are not inferred"}
                          for claim in claims if claim["domain"] == "memories"],
        "clock_observations": _clock_observations(claims),
        "uncertainty": {"campaign_identity_status": campaign_status,
                        "unresolved_claims": [c for c in claims if c["status"] in {"ambiguous", "unsupported", "invalid"}],
                        "provenance_gaps": provenance_gaps,
                        "branch_status": "single_snapshot; ancestry not asserted"},
        "coverage": {"source_status": deepcopy(snapshot["coverage"].get("source_status", "unknown")),
                     "domains": deepcopy(snapshot["coverage"].get("domains", {})),
                     "limits": deepcopy(snapshot["coverage"].get("limits", [])),
                     "evidence_count": len(snapshot["evidence"]), "record_count": len(records)},
        "evidence_use": _evidence_ledger(snapshot, claims),
        "ranked_record_ids": [row["record_id"] for row in records],
        "ranking": {"method": "stable_saved_identity_then_record_id", "calibrated": False,
                    "is_hint_only": True, "prunes_records": False},
        "source_outcomes": [],
        "annotations": [],
        "semantic_cache_key": None,
        "authoritative_facts_unchanged": True,
    }


def _pawn_aliases(pawn: Mapping[str, Any]) -> list[dict[str, Any]]:
    aliases = []
    for alias in pawn.get("aliases", []):
        if _is_object(alias):
            aliases.append(deepcopy(dict(alias)))
    return aliases


def discover_snapshot(snapshot: Mapping[str, Any], *, semantic_adapter: "OfflineSemanticAdapter | None" = None) -> dict[str, Any]:
    """Return a source-linked first-pass index for one exact history mapping."""
    result = _snapshot_result(snapshot)
    if semantic_adapter is not None:
        packet = build_evidence_packet(result)
        key = semantic_cache_key(result, semantic_adapter)
        result["semantic_cache_key"] = key
        raw = semantic_adapter.annotate(deepcopy(packet))
        result["annotations"] = _check_annotations(raw, packet)
    return result


def discover_collection(snapshots: Iterable[Mapping[str, Any]],
                        source_outcomes: Iterable[Mapping[str, Any]] | None = None, *,
                        semantic_adapter: "OfflineSemanticAdapter | None" = None) -> dict[str, Any]:
    """Discover across snapshots, retaining every parsed record and supplied outcome."""
    per_snapshot = [_snapshot_result(snapshot) for snapshot in snapshots]
    outcome_rows = [_validate_outcome(row) for row in (source_outcomes or [])]
    if source_outcomes is None:
        outcome_rows = [{"source_id": row["source"]["snapshot_id"], "status": "parsed",
                         "snapshot_id": row["source"]["snapshot_id"]} for row in per_snapshot]
    accounted = {row.get("snapshot_id") for row in outcome_rows if row["status"] == "parsed"}
    missing = [row["source"]["snapshot_id"] for row in per_snapshot
               if row["source"]["snapshot_id"] not in accounted]
    for sid in missing:
        outcome_rows.append({"source_id": sid, "status": "parsed", "snapshot_id": sid,
                             "accounting_note": "snapshot supplied without a parsed source outcome"})

    records = [record for result in per_snapshot for record in result["records"]]
    entities, pawn_record_to_entity = _join_pawns(per_snapshot)
    observations = _join_observations(per_snapshot, pawn_record_to_entity)
    joined_events, event_record_to_entity = _join_events(per_snapshot, pawn_record_to_entity)
    episodes = joined_events + observations
    claims = [claim for result in per_snapshot for claim in result["claims"]]
    claim_entity_map = {**pawn_record_to_entity, **event_record_to_entity}
    support = _support_and_counterevidence(claims, claim_entity_map, episodes)
    perspectives = [{"snapshot_id": claim["snapshot_id"],
                     "subject_entity_id": pawn_record_to_entity.get(claim["owner_record_id"]),
                     "subject_record_id": claim["owner_record_id"], "field": claim["field"],
                     "value": deepcopy(claim["value"]), "status": claim["status"],
                     "evidence_id": claim["evidence_id"], "source_ref": claim["source_ref"],
                     "interpretation": "recorded perspective; motive and objective cause are not inferred"}
                    for claim in claims if claim["domain"] == "memories"]
    clock_observations = sorted([item for result in per_snapshot for item in result["clock_observations"]],
                                key=lambda row: (row["snapshot_id"] if "snapshot_id" in row else "", str(row["field"]), str(row.get("source_ref"))))
    for observation in clock_observations:
        if "snapshot_id" not in observation:
            # The source reference remains primary; this key is an explicit container pointer.
            observation["snapshot_id"] = next((c["snapshot_id"] for c in claims
                                               if c.get("source_ref") == observation.get("source_ref")), None)

    for row in records:
        row["rank_hint"] = _rank_hint(row)
    records.sort(key=lambda row: (-row["rank_hint"], row["record_id"]))
    ranked_ids = [row["record_id"] for row in records]
    claim_groups = _claim_groups(claims, pawn_record_to_entity)
    incomplete_domains = []
    for result in per_snapshot:
        domains = result["coverage"].get("domains", {})
        if isinstance(domains, Mapping):
            incomplete_domains.extend({"snapshot_id": result["source"]["snapshot_id"], "domain": name,
                                       "status": item.get("status") if _is_object(item) else item}
                                      for name, item in domains.items()
                                      if (_is_object(item) and item.get("status") in {"partial", "unsupported", "invalid"})
                                      or (not _is_object(item) and item in {"partial", "unsupported", "invalid"}))
    result = {
        "discovery_schema_version": DISCOVERY_VERSION,
        "sources": [row["source"] for row in per_snapshot],
        "records": records,
        "entities": entities,
        "episodes": episodes,
        "claims": claims,
        "support_and_counterevidence": support,
        "perspectives": perspectives,
        "clock_observations": clock_observations,
        "uncertainty": {
            "campaigns_with_unproven_ancestry": [row["campaign_id"] for row in _campaign_groups(per_snapshot)
                                                 if len(row["snapshot_ids"]) > 1],
            "unresolved_claims": [claim for claim in claims if claim["status"] in {"ambiguous", "unsupported", "invalid"}],
            "incomplete_domains": incomplete_domains,
            "unjoined_pawn_records": [record["record_id"] for record in records
                                       if record["kind"] == "pawn" and record["record_id"] not in pawn_record_to_entity],
        },
        "coverage": {"source_count": len({row.get("source_id") for row in outcome_rows}),
                     "outcome_count": len(outcome_rows), "unaccounted_source_count": 0,
                     "outcome_status_counts": _count_statuses(outcome_rows),
                     "parsed_snapshot_count": len(per_snapshot),
                     "missing_snapshot_outcomes_repaired": missing,
                     "incomplete_domains": incomplete_domains},
        "source_outcomes": outcome_rows,
        "evidence_use": _merge_evidence_ledgers(per_snapshot),
        "ranked_record_ids": ranked_ids,
        "ranking": {"method": "evidence_links_then_record_id", "calibrated": False,
                    "is_hint_only": True, "prunes_records": False,
                    "included_record_count": len(records), "source_record_count": len(records)},
        "semantic_cache_key": None,
        "annotations": [],
        "authoritative_facts_unchanged": True,
    }
    if semantic_adapter is not None:
        packet = build_evidence_packet(result)
        result["semantic_cache_key"] = semantic_cache_key(result, semantic_adapter)
        result["annotations"] = _check_annotations(semantic_adapter.annotate(deepcopy(packet)), packet)
    return result


def _validate_outcome(row: Mapping[str, Any]) -> dict[str, Any]:
    if not _is_object(row) or not isinstance(row.get("source_id"), str) or not row["source_id"]:
        raise ValueError("each source outcome requires a source_id")
    if row.get("status") not in _SOURCE_OUTCOMES:
        raise ValueError("source outcome status must be parsed, failed, excluded, or unavailable")
    return deepcopy(dict(row))


def _join_pawns(results: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    record_to_entity: dict[str, str] = {}
    for result in results:
        source = result["source"]
        for record in result["records"]:
            if record["kind"] != "pawn":
                continue
            if source["campaign_identity_status"] == "validated" and record["identity_status"] == "saved_id":
                key = ("campaign", source["campaign_id"], record["saved_id"])
            else:
                key = ("snapshot", record["snapshot_id"], record["saved_id"])
            groups[key].append(record)
    entities = []
    for key, members in sorted(groups.items(), key=lambda pair: str(pair[0])):
        entity_id = "pawn:" + _sha256(list(key))
        for member in members:
            record_to_entity[member["record_id"]] = entity_id
        alias_values = []
        for member in members:
            alias_values.extend(_pawn_aliases(member["payload"]))
        entities.append({"entity_id": entity_id, "kind": "pawn", "saved_id": members[0]["saved_id"],
                         "campaign_id": members[0]["campaign_id"],
                         "snapshot_ids": sorted({m["snapshot_id"] for m in members}),
                         "record_ids": sorted(m["record_id"] for m in members),
                         "aliases": _dedupe_json(alias_values),
                         "join_basis": "validated_campaign_and_saved_pawn_id" if key[0] == "campaign" else "snapshot_only"})
    return entities, record_to_entity


def _join_events(results: Sequence[Mapping[str, Any]],
                 pawn_record_to_entity: Mapping[str, str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        for episode in result["episodes"]:
            if episode["kind"] != "recorded_event":
                continue
            if (episode["identity_status"] == "saved_event_id"
                    and result["source"]["campaign_identity_status"] == "validated"):
                key = ("campaign", episode["campaign_id"], episode["saved_event_id"])
            else:
                key = ("snapshot", result["source"]["snapshot_id"], episode["saved_event_id"])
            groups[key].append(episode)
    output = []
    record_to_episode: dict[str, str] = {}
    for key, members in sorted(groups.items(), key=lambda pair: str(pair[0])):
        participants = []
        for member in members:
            sid = member["snapshot_id"]
            for participant in member.get("participants", []):
                if not _is_object(participant):
                    continue
                pawn_id = participant.get("pawn_id")
                record_id = next((record["record_id"] for result in results
                                  if result["source"]["snapshot_id"] == sid
                                  for record in result["records"]
                                  if record["kind"] == "pawn" and record["saved_id"] == pawn_id), None)
                participants.append({**deepcopy(dict(participant)),
                                     "entity_id": pawn_record_to_entity.get(record_id) if record_id else None,
                                     "snapshot_id": sid})
        episode_id = "episode:" + _sha256(list(key))
        for member in members:
            for record_id in member["record_ids"]:
                record_to_episode[record_id] = episode_id
        output.append({"episode_id": episode_id,
                       "kind": "recorded_event",
                       "saved_event_id": members[0]["saved_event_id"],
                       "identity_status": members[0]["identity_status"],
                       "campaign_id": members[0]["campaign_id"],
                       "record_ids": sorted(record for member in members for record in member["record_ids"]),
                       "snapshot_ids": sorted({sid for member in members for sid in _episode_snapshots(member)}),
                       "occurrences": _dedupe_json([occ for member in members for occ in member["occurrences"]]),
                       "participants": participants,
                       "join_basis": "validated_campaign_and_saved_event_id" if key[0] == "campaign" else "snapshot_only"})
    return output, record_to_episode


def _episode_snapshots(episode: Mapping[str, Any]) -> list[str]:
    if episode.get("snapshot_id"):
        return [episode["snapshot_id"]]
    return sorted({record_id[:64] for record_id in episode.get("record_ids", [])})


def _join_observations(results: Sequence[Mapping[str, Any]],
                       pawn_record_to_entity: Mapping[str, str]) -> list[dict[str, Any]]:
    output = []
    for result in results:
        for episode in result["episodes"]:
            if episode["kind"] != "state_observation":
                continue
            record_id = episode["record_ids"][0]
            output.append({**deepcopy(episode), "entity_id": pawn_record_to_entity.get(record_id),
                           "join_basis": "snapshot_observation_of_saved_pawn_id"})
    return sorted(output, key=lambda row: (row["snapshot_id"], row["saved_pawn_id"]))


def _campaign_groups(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, set[str]] = defaultdict(set)
    for result in results:
        source = result["source"]
        if source.get("campaign_id") and source.get("campaign_identity_status") == "validated":
            groups[source["campaign_id"]].add(source["snapshot_id"])
    return [{"campaign_id": key, "snapshot_ids": sorted(ids)} for key, ids in sorted(groups.items())]


def _claim_groups(claims: Sequence[Mapping[str, Any]], record_to_entity: Mapping[str, str]) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for claim in claims:
        identity = record_to_entity.get(claim["owner_record_id"], claim["owner_record_id"])
        groups[(identity, str(claim.get("field")))].append(claim)
    return groups


def _support_and_counterevidence(claims: Sequence[Mapping[str, Any]],
                                 record_to_entity: Mapping[str, str],
                                 episodes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for (identity, field), variants in sorted(_claim_groups(claims, record_to_entity).items()):
        if field in {"identity.name", "name", "pawn.name"}:
            continue  # Name changes are aliases, not contradictory identity evidence.
        by_value: dict[bytes, list[Mapping[str, Any]]] = defaultdict(list)
        for claim in variants:
            by_value[_canonical_bytes([claim.get("status"), claim.get("value")])].append(claim)
        values = []
        for _, occurrences in sorted(by_value.items(), key=lambda item: item[0]):
            first = occurrences[0]
            values.append({"value": deepcopy(first.get("value")), "status": first.get("status"),
                           "occurrences": [{"snapshot_id": row["snapshot_id"], "evidence_id": row.get("evidence_id"),
                                            "source_ref": row.get("source_ref"), "raw_value": deepcopy(row.get("raw_value"))}
                                           for row in occurrences]})
        output.append({"entity_id": identity, "field": field,
                       "status": "conflicting_observations" if len(values) > 1 else "supported_observation",
                       "supporting_variants": values,
                       "counterevidence_present": len(values) > 1,
                       "interpretation": "observations are preserved; cause or branch continuity is not inferred"})
    return output


def _rank_hint(record: Mapping[str, Any]) -> int:
    payload = record.get("payload", {})
    refs = payload.get("evidence_refs", []) if _is_object(payload) else []
    return len(refs) if isinstance(refs, (list, tuple)) else 0


def _dedupe_json(items: Sequence[Any]) -> list[Any]:
    found: dict[bytes, Any] = {}
    for item in items:
        found.setdefault(_canonical_bytes(item), deepcopy(item))
    return [found[key] for key in sorted(found)]


def _count_statuses(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(_SOURCE_OUTCOMES)}
    for row in outcomes:
        counts[row["status"]] += 1
    return counts


def _merge_evidence_ledgers(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"total": sum(row["evidence_use"]["total"] for row in results),
            "used": [item for row in results for item in row["evidence_use"]["used"]],
            "unused": [item for row in results for item in row["evidence_use"]["unused"]],
            "claims_missing_provenance": [item for row in results for item in row["evidence_use"]["claims_missing_provenance"]],
            "claims_without_matching_evidence": [item for row in results for item in row["evidence_use"]["claims_without_matching_evidence"]]}


def build_evidence_packet(discovery: Mapping[str, Any]) -> dict[str, Any]:
    """Create stable, source-linked input for an optional local semantic adapter."""
    return {
        "schema": "discovery-evidence-packet/1.0",
        "rubric": deepcopy(RUBRIC),
        "sources": deepcopy(discovery.get("sources", [discovery.get("source")])) ,
        "entities": deepcopy(discovery.get("entities", [])),
        "episodes": deepcopy(discovery.get("episodes", [])),
        "claims": deepcopy(discovery.get("claims", [])),
        "support_and_counterevidence": deepcopy(discovery.get("support_and_counterevidence", [])),
        "clock_observations": deepcopy(discovery.get("clock_observations", [])),
        "uncertainty": deepcopy(discovery.get("uncertainty", {})),
        "coverage": deepcopy(discovery.get("coverage", {})),
        "evidence_use": deepcopy(discovery.get("evidence_use", {})),
    }


class OfflineSemanticAdapter:
    """Explicit opt-in wrapper for a caller-supplied, local-only annotator.

    The wrapped object must expose ``annotate(packet)``. This class performs no
    network access and keeps annotations detached from the authoritative result.
    """

    def __init__(self, annotator: Any, *, model_id: str,
                 provider_id: str = "offline", options: Mapping[str, Any] | None = None):
        if not callable(getattr(annotator, "annotate", None)):
            raise TypeError("offline annotator must provide annotate(packet)")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model_id is required for reproducible semantic cache keys")
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("provider_id is required for reproducible semantic cache keys")
        self.annotator = annotator
        self.model_id = model_id
        self.provider_id = provider_id
        self.options = deepcopy(dict(options or {}))

    def annotate(self, packet: Mapping[str, Any]) -> Any:
        return self.annotator.annotate(packet)


def semantic_cache_key(discovery: Mapping[str, Any], adapter: OfflineSemanticAdapter) -> str:
    sources = discovery.get("sources") or [discovery.get("source")]
    profiles = sorted({str(source.get("interpretation_id")) for source in sources if _is_object(source)})
    source_ids = sorted({str(source.get("snapshot_id")) for source in sources if _is_object(source)})
    channels = sorted({str(source.get("source_channel")) for source in sources
                       if _is_object(source) and source.get("source_channel") is not None})
    channel = discovery.get("source_channel")
    if isinstance(channel, Mapping) and channel.get("id") is not None:
        channels = sorted(set(channels) | {str(channel["id"])})
    catalog = discovery.get("catalog") if _is_object(discovery.get("catalog")) else {}
    options = deepcopy(adapter.options)
    return _sha256({"source_ids": source_ids, "profiles": profiles,
                    "catalog_generation": catalog.get("generation_id"),
                    "source_channels": channels,
                    "resolver_version": (channel.get("resolver_version")
                                         if isinstance(channel, Mapping) else DISCOVERY_VERSION),
                    "rubric_version": EVIDENCE_RUBRIC_VERSION, "rubric": RUBRIC,
                    "model_id": adapter.model_id, "provider_id": adapter.provider_id,
                    "prompt_version": options.get("prompt_version", options.get("prompt_id")),
                    "options": options})


def _check_annotations(raw: Any, packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not _is_object(raw) or not isinstance(raw.get("claims", []), (list, tuple)):
        return [{"status": "rejected", "reason": "adapter result must contain a claims array"}]
    ledger = packet.get("evidence_use", {})
    citation_population = ledger.get("included", ledger.get("used", []))
    allowed = {(item.get("evidence_id"), item.get("source_ref"))
               for item in citation_population if _is_object(item)}
    result = []
    for item in raw.get("claims", []):
        if not _is_object(item):
            result.append({"status": "rejected", "reason": "annotation claim must be an object"})
            continue
        support = item.get("support", [])
        refs = item.get("source_refs", [])
        pairs = set(zip(support, refs)) if isinstance(support, (list, tuple)) and isinstance(refs, (list, tuple)) else set()
        text = item.get("text") if isinstance(item.get("text"), str) else ""
        if any(pattern.search(text) for pattern in _FORBIDDEN_ANNOTATION_PATTERNS):
            status, reason = "rejected", "annotation contains unsupported causal or motive language"
        elif not pairs or not pairs.issubset(allowed) or len(pairs) != len(support) or len(pairs) != len(refs):
            status, reason = "rejected", "every support ID and source reference must match packet evidence"
        else:
            status, reason = "needs_semantic_review", None
        annotation = {"status": status, "citation_status": "verified" if status == "needs_semantic_review" else "rejected",
                      "semantic_entailment": "unchecked" if status == "needs_semantic_review" else "not_assessed",
                      "text": text, "support": list(support) if isinstance(support, (list, tuple)) else [],
                      "source_refs": list(refs) if isinstance(refs, (list, tuple)) else []}
        if reason:
            annotation["reason"] = reason
        result.append(annotation)
    return result
