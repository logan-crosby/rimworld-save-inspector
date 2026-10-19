"""Bounded first-pass discovery over an immutable history catalog generation."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..errors import InspectorError
from ..history import catalog as history_catalog
from .core import (
    DISCOVERY_VERSION,
    EVIDENCE_RUBRIC_VERSION,
    OfflineSemanticAdapter,
    _check_annotations,
    _is_object,
    _sha256,
    semantic_cache_key,
)


ABSOLUTE_LIMIT = 16 * 1024 * 1024
_PAWN_DECOMPRESS_LIMIT = 8 * 1024 * 1024
_EVENT_DECOMPRESS_LIMIT = 4 * 1024 * 1024
_CONTEXT_DECOMPRESS_LIMIT = 2 * 1024 * 1024
_MAX_ART_EXAMPLES_PER_SNAPSHOT = 3
_MAX_ART_REFS_PER_ENTITY = 8
_MAX_FACT_VALUE_BYTES = 12 * 1024
_MAX_CANDIDATES_PER_ENTITY = 384
_MAX_HIGHLIGHTS_PER_DOMAIN = {
    "genes": 12, "memories": 6, "relationships": 6, "health": 6,
    "backstory": 4, "traits": 4, "skills": 4, "work": 4,
    "equipment": 4, "inventory": 4, "ownership": 4,
}
_DEFAULT_HIGHLIGHTS_PER_DOMAIN = 3
_MAX_EPISODES = 240
_MAX_OCCURRENCES_PER_EPISODE = 8
_MAX_EVENT_FACTS_PER_EPISODE = 24
_MAX_MEMBERSHIPS_PER_EVENT_FACT = 8
_MAX_ALIASES_PER_ENTITY = 24
_MAX_CONFLICT_FIELDS_PER_ENTITY = 64
_MAX_LATEST_ALTERNATIVES = 32
_INDEX_PRIORITY = (
    "health", "memories", "relationships", "genes", "backstory", "traits", "skills",
    "work", "equipment", "ownership", "inventory", "age", "abilities", "roles",
    "status_and_interaction", "location", "apparel", "needs", "mechanitor", "ideology",
    "royalty", "records", "events", "unknown_fields",
)
_IMPORTANT_TERMS = (
    "death", "died", "birth", "injur", "scar", "disease", "conflict", "relation",
    "memory", "gene", "override", "suppress", "active", "role", "skill", "work",
    "owner", "bond", "ability", "record",
)
_OCCURRENCE_INDEX = re.compile(r"\[(\d+)\]")
_CLAIM_KEYS = {
    "field", "raw_field", "value", "raw_value", "status", "unit", "code", "display_label",
    "normalization_rule", "time_observation", "evidence_id", "source_ref", "premise_refs",
}
_EVIDENCE_KEY = {"evidence_id", "source_ref", "evidence_refs", "record_id", "source_path",
                 "snapshot_id", "interpretation_id", "raw_field", "raw_value", "path"}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _token(parts: list[str]) -> str:
    return "h1." + base64.urlsafe_b64encode(_json_bytes(parts)).decode("ascii").rstrip("=")


def _evidence_retrieval_id(namespace: str, generation_id: str,
                           content_id: str, source_ref: str) -> str:
    return _token([namespace, generation_id, content_id, source_ref])


def _entity_id(campaign_id: str | None, snapshot_id: str,
               pawn_id: str | None, record_id: str) -> tuple[str, str]:
    if campaign_id and pawn_id:
        key = ["campaign", campaign_id, pawn_id]
        basis = "validated_campaign_and_saved_pawn_id"
    else:
        key = ["snapshot", snapshot_id, pawn_id or record_id]
        basis = "snapshot_only"
    return "pawn:" + _sha256(key), basis


def _new_entity(entity_id: str, campaign_id: str | None, pawn_id: str | None,
                join_basis: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id, "campaign_id": campaign_id, "saved_id": pawn_id,
        "join_basis": join_basis, "observation_count": 0, "snapshot_ids": set(),
        "alias_values": {}, "domain_status_counts": defaultdict(Counter),
        "domain_claim_counts": Counter(), "claim_status_counts": defaultdict(Counter),
        "candidates": {}, "candidate_evictions": 0, "latest_game_tick": None,
        "latest_observations": [], "latest_tie_total": 0, "unknown_clock_observation_count": 0,
        "conflicting_fields": {}, "conflict_field_evictions": 0,
        "payload_limit_observation_count": 0,
    }


def _domain_priority(domain: str) -> int:
    try:
        return _INDEX_PRIORITY.index(domain)
    except ValueError:
        return len(_INDEX_PRIORITY)


def _claim_extensions(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in claim.items() if key not in _CLAIM_KEYS}


def _semantic_fact(domain: str, claim: Mapping[str, Any]) -> dict[str, Any]:
    return {"domain": domain, "field": claim.get("field"), "value": claim.get("value"),
            "status": claim.get("status", "unknown"), "unit": claim.get("unit"),
            "code": claim.get("code"), "display_label": claim.get("display_label"),
            "normalization_rule": claim.get("normalization_rule"),
            "extensions": _claim_extensions(claim)}


def _fact_key(domain: str, claim: Mapping[str, Any]) -> str:
    return _sha256(_semantic_fact(domain, claim))


def _claim_score(domain: str, field: Any, status: Any) -> int:
    score = (len(_INDEX_PRIORITY) - _domain_priority(domain)) * 10
    text = str(field).casefold()
    score += sum(8 for term in _IMPORTANT_TERMS if term in text)
    score += {"observed": 8, "derived": 5, "ambiguous": 4, "unsupported": 2,
              "invalid": 1, "absent": 0, "explicit-null": 0}.get(str(status), 0)
    return score


def _occurrence_index(claim: Mapping[str, Any]) -> int | None:
    raw_path = claim.get("raw_field") or claim.get("field")
    matches = _OCCURRENCE_INDEX.findall(str(raw_path))
    return int(matches[-1]) if matches else None


def _row_membership(snapshot_id: str, game_tick: int | None, record_id: str,
                    interpretation_id: str, domain: str,
                    claim: Mapping[str, Any], namespace: str, generation_id: str) -> dict[str, Any]:
    evidence = []
    evidence_id, source_ref = claim.get("evidence_id"), claim.get("source_ref")
    if isinstance(evidence_id, str) and isinstance(source_ref, str):
        evidence.append({"content_id": evidence_id, "source_ref": source_ref,
                         "retrieval_id": _evidence_retrieval_id(namespace, generation_id,
                                                                 evidence_id, source_ref)})
    return {"snapshot_id": snapshot_id, "game_tick": game_tick, "record_id": record_id,
            "interpretation_id": interpretation_id, "raw_field": claim.get("raw_field"),
            "occurrence_index": _occurrence_index(claim), "raw_value": claim.get("raw_value"),
            "time_observation": claim.get("time_observation"),
            "extensions": _claim_extensions(claim), "evidence": evidence}


def _observe_conflict(entity: dict[str, Any], domain: str, claim: Mapping[str, Any],
                      membership: Mapping[str, Any]) -> None:
    field = claim.get("field")
    if not isinstance(field, str) or field.rsplit(".", 1)[-1] in {"name", "nickname", "title"}:
        return  # Historical names and titles are aliases, never identity conflicts.
    signature = _sha256([claim.get("status"), claim.get("value")])
    key = domain + ":" + field
    if key not in entity["conflicting_fields"] and len(entity["conflicting_fields"]) >= _MAX_CONFLICT_FIELDS_PER_ENTITY:
        entity["conflict_field_evictions"] += 1
        return
    entry = entity["conflicting_fields"].setdefault(key, {"domain": domain, "field": field,
                                                           "variants": {}, "observation_count": 0})
    entry["observation_count"] += 1
    if signature not in entry["variants"] and len(entry["variants"]) < 3:
        entry["variants"][signature] = {"status": claim.get("status"),
                                        "value": claim.get("value"),
                                        "evidence_examples": []}
    variant = entry["variants"].get(signature)
    if variant is not None and len(variant["evidence_examples"]) < 2:
        variant["evidence_examples"].append({"snapshot_id": membership["snapshot_id"],
                                             "record_id": membership["record_id"],
                                             "evidence": deepcopy(membership["evidence"]),
                                             "raw_field": membership["raw_field"],
                                             "raw_value": membership["raw_value"]})


def _add_claim_candidate(entity: dict[str, Any], domain: str,
                         claim: Mapping[str, Any], tick: int | None) -> None:
    key = _fact_key(domain, claim)
    existing = entity["candidates"].get(key)
    if existing is not None:
        existing["last_known_tick"] = max(existing["last_known_tick"], tick) if tick is not None and existing["last_known_tick"] is not None else (tick if tick is not None else existing["last_known_tick"])
        return
    try:
        fact_size = len(_json_bytes(_semantic_fact(domain, claim)))
    except (TypeError, ValueError):
        entity["claim_status_counts"][domain]["invalid_json_value"] += 1
        return
    if fact_size > _MAX_FACT_VALUE_BYTES:
        entity["claim_status_counts"][domain]["oversize_value"] += 1
        return
    candidate = {"fact_id": key, "semantic": _semantic_fact(domain, claim),
                 "score": _claim_score(domain, claim.get("field"), claim.get("status")),
                 "last_known_tick": tick}
    if len(entity["candidates"]) < _MAX_CANDIDATES_PER_ENTITY:
        entity["candidates"][key] = candidate
        return
    worst_key, worst = min(entity["candidates"].items(),
                           key=lambda pair: (pair[1]["score"], pair[1]["fact_id"]))
    if (candidate["score"], candidate["fact_id"]) > (worst["score"], worst["fact_id"]):
        del entity["candidates"][worst_key]
        entity["candidate_evictions"] += 1
        entity["candidates"][key] = candidate
    else:
        entity["candidate_evictions"] += 1


def _selected_fact_keys(entity: Mapping[str, Any]) -> dict[str, set[str]]:
    latest_tick = entity["latest_game_tick"]
    by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in entity["candidates"].values():
        semantic = candidate["semantic"]
        domain = semantic["domain"]
        latest = latest_tick is not None and candidate["last_known_tick"] == latest_tick
        by_domain[domain].append({**candidate, "latest": latest})
    selected: dict[str, set[str]] = {}
    for domain, candidates in by_domain.items():
        candidates.sort(key=lambda row: (not row["latest"], -row["score"],
                                        str(row["semantic"].get("field")), row["fact_id"]))
        count = _MAX_HIGHLIGHTS_PER_DOMAIN.get(domain, _DEFAULT_HIGHLIGHTS_PER_DOMAIN)
        selected[domain] = {row["fact_id"] for row in candidates[:count]}
    return selected


def _freshness_report(catalog_path: str | Path, freshness: str,
                      generation_id: str | None) -> dict[str, Any]:
    if freshness not in {"metadata", "strict", "frozen"}:
        raise InspectorError("INVALID_ARGUMENT", "Freshness must be metadata, strict, or frozen.")
    if generation_id is not None:
        return history_catalog.inspect_catalog(catalog_path, generation_id=generation_id,
                                               freshness="frozen")
    if freshness == "frozen":
        return history_catalog.inspect_catalog(catalog_path, freshness="frozen")
    return history_catalog.refresh_catalog(catalog_path, freshness=freshness)


def _where(anchor_snapshot_id: str | None, campaign_id: str | None) -> tuple[str, tuple[Any, ...]]:
    if anchor_snapshot_id is not None and campaign_id is None:
        return " WHERE s.snapshot_id=?", (anchor_snapshot_id,)
    if anchor_snapshot_id is not None:
        return " WHERE s.campaign_id=?", (campaign_id,)
    return "", ()


def _source_outcomes(report: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = []
    counts = Counter()
    for source in report.get("sources", []):
        if not _is_object(source):
            continue
        status = str(source.get("status", "unknown"))
        counts[status] += 1
        rows.append({"source": source.get("path"), "source_hash": source.get("source_hash"),
                     "status": status, "error_code": source.get("error_code"),
                     "snapshot_id": source.get("snapshot_id"),
                     "interpretation_id": source.get("interpretation_id"),
                     "source_channel": "save_xml_catalog"})
    rows.sort(key=lambda row: (str(row["source"]), str(row["source_hash"])))
    return rows, dict(sorted(counts.items()))


def _count_where(db, sql: str, args: tuple[Any, ...]) -> int:
    row = db.execute(sql, args).fetchone()
    return int(row[0]) if row else 0


def _event_semantics(event: Mapping[str, Any]) -> dict[str, Any]:
    claims = []
    for claim in event.get("claims", []):
        if _is_object(claim):
            claims.append({key: claim.get(key) for key in (
                "field", "value", "status", "unit", "code", "display_label", "normalization_rule")})
    # Claims are a semantic multiset: normalize ordering but retain duplicates.
    claims.sort(key=_json_bytes)
    participants = []
    for participant in event.get("participants", []):
        if _is_object(participant):
            participants.append({key: participant.get(key) for key in ("pawn_id", "role", "status")})
    return {"kind": event.get("kind"), "saved_event_id": event.get("saved_event_id"),
            "claims": claims, "participants": participants,
            "occurrence": {key: event.get("occurrence", {}).get(key)
                           for key in ("raw_value", "raw_field", "clock", "normalized_game_tick", "status")}
            if _is_object(event.get("occurrence")) else None}


def _episode_key(event: Mapping[str, Any], snapshot_id: str, campaign_id: str | None,
                 event_index: int) -> tuple[str, str, str]:
    semantics = _event_semantics(event)
    variant_id = _sha256(semantics)
    saved_event_id = event.get("saved_event_id")
    if isinstance(saved_event_id, str) and saved_event_id and campaign_id:
        identity = _sha256(["campaign", campaign_id, saved_event_id, variant_id])
        basis = "validated_campaign_and_saved_event_id"
    else:
        identity = _sha256(["snapshot", snapshot_id, event_index, variant_id])
        basis = "snapshot_only"
    return identity, basis, variant_id


def _event_score(event: Mapping[str, Any]) -> int:
    text = json.dumps(_event_semantics(event), ensure_ascii=False, sort_keys=True).casefold()
    score = 20 if event.get("kind") not in {None, "unknown"} else 0
    score += 12 * sum(term in text for term in ("death", "died", "birth", "injury", "wounded", "conflict", "bond"))
    occurrence = event.get("occurrence")
    if _is_object(occurrence) and occurrence.get("status") in {"known", "pre_arrival", "future_contradiction"}:
        score += 6
    score += min(10, len(event.get("participants", [])))
    return score


def _retain_top_episode(candidates: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    key = candidate["episode_key"]
    if key in candidates:
        return
    if len(candidates) < _MAX_EPISODES:
        candidates[key] = candidate
        return
    worst_key, worst = min(candidates.items(), key=lambda pair: (pair[1]["score"], pair[0]))
    if (candidate["score"], key) > (worst["score"], worst_key):
        del candidates[worst_key]
        candidates[key] = candidate


def _selected_packet(result: Mapping[str, Any]) -> dict[str, Any]:
    evidence_rows = []
    for entity in result.get("entities", []):
        for bundle in entity.get("domain_highlights", {}).values():
            for fact in bundle.get("fact_bundles", []):
                for member in fact.get("memberships", []):
                    for evidence in member.get("evidence", []):
                        evidence_rows.append({"evidence_id": evidence["content_id"],
                                              "source_ref": evidence["source_ref"]})
    for episode in result.get("episodes", []):
        for occurrence in episode.get("occurrences", []):
            for evidence in occurrence.get("evidence", []):
                evidence_rows.append({"evidence_id": evidence["content_id"],
                                      "source_ref": evidence["source_ref"]})
        for claim in episode.get("claims", []):
            for member in claim.get("memberships", []):
                for evidence in member.get("evidence", []):
                    evidence_rows.append({"evidence_id": evidence["content_id"],
                                          "source_ref": evidence["source_ref"]})
    packet_claims = []
    for entity in result.get("entities", []):
        for bundle in entity.get("domain_highlights", {}).values():
            for fact in bundle.get("fact_bundles", []):
                for member in fact.get("memberships", []):
                    for evidence in member.get("evidence", []):
                        packet_claims.append({"field": fact.get("field"), "value": fact.get("value"),
                                              "status": fact.get("status"), "evidence_id": evidence["content_id"],
                                              "source_ref": evidence["source_ref"]})
    for episode in result.get("episodes", []):
        for claim in episode.get("claims", []):
            for member in claim.get("memberships", []):
                for evidence in member.get("evidence", []):
                    packet_claims.append({"episode_id": episode.get("episode_id"),
                                          "field": claim.get("field"), "value": claim.get("value"),
                                          "status": claim.get("status"),
                                          "evidence_id": evidence["content_id"],
                                          "source_ref": evidence["source_ref"]})
    return {"schema": "discovery-evidence-packet/1.0",
            "rubric": {"version": EVIDENCE_RUBRIC_VERSION},
            "catalog": result.get("catalog", {}),
            "source_channel": result.get("source_channel", {}),
            "sources": result.get("sources", []),
            "entities": result.get("entities", []), "episodes": result.get("episodes", []),
            "claims": packet_claims,
            "support_and_counterevidence": result.get("support_and_counterevidence", []),
            "clock_observations": result.get("clock_observations", []),
            "linked_artifacts": result.get("linked_artifacts", {}),
            "uncertainty": result.get("uncertainty", {}), "coverage": result.get("coverage", {}),
            "evidence_use": {"included": _dedupe_evidence(evidence_rows), "used": []}}


def _event_followup_selectors(db, snapshots: Mapping[str, Mapping[str, Any]],
                              generation_id: str, episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = Counter(occurrence.get("snapshot_id")
                       for episode in episodes for occurrence in episode.get("occurrences", []))
    selectors = []
    for row in db.execute("SELECT snapshot_id,count(*) AS total FROM events GROUP BY snapshot_id ORDER BY snapshot_id"):
        snapshot_id = row["snapshot_id"]
        omitted = max(0, int(row["total"]) - selected[snapshot_id])
        if not omitted:
            continue
        snapshot = snapshots.get(snapshot_id, {})
        selectors.append({"snapshot_id": snapshot_id, "campaign_id": snapshot.get("campaign_id"),
                          "omitted_record_count": omitted,
                          "selector": {"python": "rimworld_save_inspector.history.service.get_events",
                                       "mcp": "get_history_events", "availability": "available",
                                       "params": {"snapshot_id": snapshot_id,
                                                  "generation_id": generation_id,
                                                  "limit": 50, "freshness": "frozen"}}})
    return selectors


def _linked_artifact_summary(db, snapshots: Mapping[str, Mapping[str, Any]],
                             generation_id: str, namespace: str) -> dict[str, Any]:
    sources = []
    totals = Counter()
    decode_limited = 0
    evidence_presence: dict[tuple[str, str], bool] = {}

    def project_rows(value: Any) -> tuple[list[dict[str, Any]], int, int]:
        rows = value if isinstance(value, (list, tuple)) else []
        selected = []
        for raw in rows[:_MAX_ART_REFS_PER_ENTITY]:
            if not _is_object(raw):
                continue
            row = deepcopy(dict(raw))
            evidence_id, source_ref = row.get("evidence_id"), row.get("source_ref")
            if isinstance(evidence_id, str) and isinstance(source_ref, str):
                pair = (evidence_id, source_ref)
                if pair not in evidence_presence:
                    evidence_presence[pair] = db.execute(
                        "SELECT 1 FROM evidence_occurrences WHERE evidence_id=? AND source_ref=? LIMIT 1",
                        pair).fetchone() is not None
                if evidence_presence[pair]:
                    row["retrieval_id"] = _evidence_retrieval_id(namespace, generation_id,
                                                                  evidence_id, source_ref)
                    row["retrieval_status"] = "available"
                else:
                    row["retrieval_status"] = "not_in_selected_generation"
            selected.append(row)
        return selected, len(rows), max(0, len(rows) - len(selected))

    for snapshot_id in sorted(snapshots):
        row = db.execute("SELECT payload FROM snapshot_context WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            context, status = {}, "context_unavailable"
        else:
            try:
                context = history_catalog.unpack(row["payload"], limit=_CONTEXT_DECOMPRESS_LIMIT)
                status = "observed"
            except InspectorError as exc:
                if exc.code != "LIMIT_EXCEEDED":
                    raise
                context, status = {}, "context_payload_over_decode_cap"
                decode_limited += 1
        art = context.get("art", []) if _is_object(context) else []
        if not isinstance(art, (list, tuple)):
            art = []
            status = "invalid_art_context"
        examples = []
        selected_claim_count = selected_link_count = selected_evidence_ref_count = 0
        total_claim_count = total_link_count = total_evidence_ref_count = 0
        for index, item in enumerate(art):
            if not _is_object(item):
                continue
            claim_total = len(item.get("claims", [])) if isinstance(item.get("claims", []), (list, tuple)) else 0
            link_total = len(item.get("links", [])) if isinstance(item.get("links", []), (list, tuple)) else 0
            ref_total = len(item.get("evidence_refs", [])) if isinstance(item.get("evidence_refs", []), (list, tuple)) else 0
            total_claim_count += claim_total
            total_link_count += link_total
            total_evidence_ref_count += ref_total
            if index >= _MAX_ART_EXAMPLES_PER_SNAPSHOT:
                continue
            claims, claim_count, _ = project_rows(item.get("claims", []))
            links, link_count, _ = project_rows(item.get("links", []))
            refs, ref_count, _ = project_rows(item.get("evidence_refs", []))
            selected_claim_count += len(claims)
            selected_link_count += len(links)
            selected_evidence_ref_count += len(refs)
            examples.append({"id": deepcopy(item.get("id")), "kind": deepcopy(item.get("kind")),
                             "record_id": deepcopy(item.get("record_id")),
                             "claims": claims, "links": links, "evidence_refs": refs,
                             "selection": {"claim_total_count": claim_count,
                                           "claim_selected_count": len(claims),
                                           "claim_omitted_count": max(0, claim_count - len(claims)),
                                           "link_total_count": link_count,
                                           "link_selected_count": len(links),
                                           "link_omitted_count": max(0, link_count - len(links)),
                                           "evidence_ref_total_count": ref_count,
                                           "evidence_ref_selected_count": len(refs),
                                           "evidence_ref_omitted_count": max(0, ref_count - len(refs))}})
        count = len(art)
        selected_art = len(examples)
        totals["art_record_count"] += count
        totals["selected_art_entity_count"] += selected_art
        totals["omitted_art_entity_count"] += max(0, count - selected_art)
        totals["claim_count"] += total_claim_count
        totals["selected_claim_count"] += selected_claim_count
        totals["link_count"] += total_link_count
        totals["selected_link_count"] += selected_link_count
        totals["evidence_ref_count"] += total_evidence_ref_count
        totals["selected_evidence_ref_count"] += selected_evidence_ref_count
        pawn_row = db.execute("SELECT pawn_id FROM pawns WHERE snapshot_id=? AND pawn_id IS NOT NULL "
                              "AND pawn_id!='' ORDER BY pawn_id LIMIT 1", (snapshot_id,)).fetchone()
        evidence_followups = []
        for example in examples:
            for group in ("claims", "links", "evidence_refs"):
                for ref in example[group]:
                    token = ref.get("retrieval_id")
                    if isinstance(token, str) and token not in {row["params"]["evidence_id"]
                                                                for row in evidence_followups}:
                        evidence_followups.append({"python": "rimworld_save_inspector.history.service.get_evidence",
                                                  "mcp": "get_history_evidence", "availability": "available",
                                                  "params": {"evidence_id": token}})
        if pawn_row is not None:
            followup = {"python": "rimworld_save_inspector.history.service.get_dossier",
                        "mcp": "get_pawn_dossier", "availability": "available",
                        "params": {"generation_id": generation_id, "snapshot_id": snapshot_id,
                                   "pawn_id": pawn_row["pawn_id"], "detail": "context"}}
        elif evidence_followups:
            followup = {"python": None, "mcp": None,
                        "availability": "available_for_exact_evidence_refs_only",
                        "evidence": evidence_followups}
        else:
            followup = {"python": None, "mcp": None,
                        "availability": "unavailable_no_pawn_or_evidence_selector",
                        "evidence": []}
        sources.append({"snapshot_id": snapshot_id, "campaign_id": snapshots[snapshot_id].get("campaign_id"),
                        "status": status if count else ("no_art_entries_observed" if status == "observed" else status),
                        "art_record_count": count, "selected_art_entity_count": selected_art,
                        "omitted_art_entity_count": max(0, count - selected_art),
                        "art_examples": examples,
                        "selection": {"example_cap": _MAX_ART_EXAMPLES_PER_SNAPSHOT,
                                      "claim_link_evidence_cap_per_entity": _MAX_ART_REFS_PER_ENTITY,
                                      "policy": "first source-order entities; display only"},
                        "claim_count": total_claim_count, "selected_claim_count": selected_claim_count,
                        "omitted_claim_count": max(0, total_claim_count - selected_claim_count),
                        "link_count": total_link_count, "selected_link_count": selected_link_count,
                        "omitted_link_count": max(0, total_link_count - selected_link_count),
                        "evidence_ref_count": total_evidence_ref_count,
                        "selected_evidence_ref_count": selected_evidence_ref_count,
                        "omitted_evidence_ref_count": max(0, total_evidence_ref_count - selected_evidence_ref_count),
                        "followup": followup})
    return {"status": "bounded_snapshot_context_summary",
            "types": ["tales", "messages", "logs", "art"],
            "provenance": "source snapshot context; generic entity shape preserved for selected art rows",
            "coverage": {"snapshot_count": len(snapshots), **dict(totals),
                         "art_snapshot_count": sum(bool(row["art_record_count"]) for row in sources),
                         "context_decode_limited_snapshot_count": decode_limited,
                         "example_cap_per_snapshot": _MAX_ART_EXAMPLES_PER_SNAPSHOT,
                         "claim_link_evidence_cap_per_entity": _MAX_ART_REFS_PER_ENTITY},
            "sources": sources}


def _dedupe_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {(row["evidence_id"], row["source_ref"]): row for row in rows}
    return [unique[key] for key in sorted(unique)]


def _displayed_evidence(result: Mapping[str, Any]) -> list[dict[str, str]]:
    found: set[tuple[str, str]] = set()
    stack: list[Any] = [result.get("entities", []), result.get("episodes", []),
                        result.get("support_and_counterevidence", [])]
    while stack:
        value = stack.pop()
        if isinstance(value, Mapping):
            evidence_id = value.get("content_id") or value.get("evidence_id")
            source_ref = value.get("source_ref")
            if isinstance(evidence_id, str) and isinstance(source_ref, str):
                found.add((evidence_id, source_ref))
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
    return [{"evidence_id": evidence_id, "source_ref": source_ref}
            for evidence_id, source_ref in sorted(found)]


def _skeptic_followups(result: Mapping[str, Any], missing_evidence_count: int,
                       limit: int = 5) -> list[dict[str, Any]]:
    followups = []
    for entity in result.get("entities", []):
        latest = entity.get("latest_observation_by_game_tick", {})
        if latest.get("status") == "same_tick_alternatives":
            followups.append({"uncertainty": "same_game_tick_branch_alternatives",
                              "source": {"entity_id": entity["entity_id"],
                                         "snapshot_ids": [row["snapshot_id"] for row in latest.get("alternatives", [])]},
                              "selector": entity.get("followup")})
            break
    omitted_domains = []
    for entity in result.get("entities", []):
        for domain, coverage in entity.get("domain_coverage", {}).items():
            if coverage.get("omitted_occurrence_count", 0):
                omitted_domains.append((coverage["omitted_occurrence_count"], entity["entity_id"], domain,
                                        entity.get("saved_id")))
    if omitted_domains:
        count, entity_id, domain, pawn_id = max(omitted_domains)
        subject = next(item for item in result.get("entities", []) if item["entity_id"] == entity_id)
        selector = deepcopy(subject.get("followup", {}))
        if selector.get("mcp") == "get_pawn_timeline":
            selector.setdefault("params", {})["domains"] = [domain]
        else:
            selector["availability"] = "available_with_snapshot_selector"
        followups.append({"uncertainty": "additional_domain_facts_not_in_first_pass_highlights",
                          "source": {"entity_id": entity_id, "domain": domain,
                                     "omitted_occurrence_count": count},
                          "selector": selector})
    if result.get("coverage", {}).get("omitted_episode_record_count", 0):
        selectors = result.get("coverage", {}).get("event_followup_selectors", [])
        followups.append({"uncertainty": "omitted_or_unsampled_event_records",
                          "source": {"omitted_record_count": result["coverage"]["omitted_episode_record_count"],
                                     "selector_count": len(selectors),
                                     "covered_snapshot_ids": [row["snapshot_id"] for row in selectors]},
                          "selector": {"python": "rimworld_save_inspector.history.service.get_events",
                                       "mcp": "get_history_events",
                                       "availability": "available_for_each_omitted_source",
                                       "selectors": selectors}})
    if missing_evidence_count:
        entity = next((item for item in result.get("entities", []) if item.get("saved_id")), None)
        source_snapshot = (entity.get("latest_observation_by_game_tick", {}).get("alternatives", [{}])[0].get("snapshot_id")
                          if entity and entity.get("latest_observation_by_game_tick", {}).get("alternatives") else None)
        followups.append({"uncertainty": "claims_without_exact_evidence_pair",
                          "source": {"claim_occurrence_count": missing_evidence_count,
                                     "entity_id": entity.get("entity_id") if entity else None,
                                     "snapshot_id": source_snapshot},
                          "selector": {"python": "rimworld_save_inspector.history.service.get_dossier",
                                       "mcp": "get_pawn_dossier",
                                       "availability": "available_with_record_selector" if entity and source_snapshot else "no_exact_record_selector",
                                       "params": {"generation_id": result["catalog"]["generation_id"],
                                                  "snapshot_id": source_snapshot,
                                                  "pawn_id": entity.get("saved_id") if entity else None}}})
    if result.get("coverage", {}).get("payload_limit_record_count", 0):
        example = next(iter(result["coverage"].get("payload_limit_examples", [])), {})
        if example.get("kind") == "event":
            selector = {"python": "rimworld_save_inspector.history.service.get_events",
                        "mcp": "get_history_events", "availability": "available",
                        "params": {"anchor_snapshot_id": example.get("snapshot_id"),
                                   "snapshot_id": example.get("snapshot_id"),
                                   "generation_id": result["catalog"]["generation_id"],
                                   "limit": 50, "freshness": "frozen"}}
        elif example.get("kind") == "pawn":
            selector = {"python": "rimworld_save_inspector.history.service.get_dossier",
                        "mcp": "get_pawn_dossier", "availability": "available_with_snapshot_selector",
                        "params": {"generation_id": result["catalog"]["generation_id"],
                                   "snapshot_id": example.get("snapshot_id"),
                                   "pawn_id": example.get("pawn_id")}}
        else:
            selector = {"availability": "no_exact_record_selector"}
        followups.append({"uncertainty": "record_payload_exceeded_discovery_decode_cap",
                          "source": {"record_count": result["coverage"]["payload_limit_record_count"],
                                     "example_record": example},
                          "selector": selector})
    return followups[:limit]


def _measure_result(result: dict[str, Any], limit: int,
                    selectors: Mapping[str, Any]) -> dict[str, Any]:
    result["size"] = {"serialized_bytes": 0, "configured_limit": limit,
                       "absolute_limit": ABSOLUTE_LIMIT, "semantic_completeness": "selected_highlights_only",
                       "record_population_complete": True}
    for _ in range(3):
        measured = len(_json_bytes(result))
        if result["size"]["serialized_bytes"] == measured:
            break
        result["size"]["serialized_bytes"] = measured
    measured = len(_json_bytes(result))
    if measured > limit:
        raise InspectorError("RESULT_TOO_LARGE", "The discovery result exceeds the configured response limit.",
                             {"required_bytes": measured, "configured_limit": limit,
                              "selectors": dict(selectors)})
    return result


def discover_catalog(catalog_path: str | Path, *, anchor_snapshot_id: str | None = None,
                     freshness: str = "metadata", response_limit: int = ABSOLUTE_LIMIT,
                     generation_id: str | None = None,
                     skeptic_limit: int = 5,
                     semantic_adapter: OfflineSemanticAdapter | None = None) -> dict[str, Any]:
    """Return bounded discovery summaries while streaming a pinned catalog generation.

    Snapshot envelopes and global raw evidence are never decompressed. Pawn and
    event rows are decoded one at a time with fixed uncompressed-size limits.
    Display selection caps do not remove catalog records; follow-up selectors
    bind every query to this immutable generation.
    """
    if isinstance(response_limit, bool) or not isinstance(response_limit, int) or response_limit < 1:
        raise InspectorError("INVALID_ARGUMENT", "response_limit must be a positive byte count.")
    if isinstance(skeptic_limit, bool) or not isinstance(skeptic_limit, int) or not 0 <= skeptic_limit <= 20:
        raise InspectorError("INVALID_ARGUMENT", "skeptic_limit must be between 0 and 20.")
    limit = min(response_limit, ABSOLUTE_LIMIT)
    report = _freshness_report(catalog_path, freshness, generation_id)
    selected_generation = report["catalog"]["generation_id"]
    namespace, selected_generation, db = history_catalog.open_generation(catalog_path, selected_generation)
    try:
        anchor_campaign = None
        if anchor_snapshot_id is not None:
            anchor = db.execute("SELECT campaign_id FROM snapshots WHERE snapshot_id=?", (anchor_snapshot_id,)).fetchone()
            if anchor is None:
                raise InspectorError("SNAPSHOT_NOT_FOUND", "The anchor snapshot is not in this generation.")
            anchor_campaign = anchor["campaign_id"]
        where_sql, where_args = _where(anchor_snapshot_id, anchor_campaign)
        snapshot_rows = db.execute(
            "SELECT s.snapshot_id,s.campaign_id,s.game_tick,s.ticks_abs,s.interpretation_id,p.profile_id "
            "FROM snapshots s LEFT JOIN snapshot_profiles p ON p.snapshot_id=s.snapshot_id" +
            where_sql + " ORDER BY s.campaign_id,s.game_tick,s.snapshot_id", where_args)
        snapshots: dict[str, dict[str, Any]] = {}
        for row in snapshot_rows:
            snapshots[row["snapshot_id"]] = {"snapshot_id": row["snapshot_id"],
                                              "campaign_id": row["campaign_id"],
                                              "game_tick": row["game_tick"] if isinstance(row["game_tick"], int) else None,
                                              "ticks_abs": row["ticks_abs"] if isinstance(row["ticks_abs"], int) else None,
                                              "interpretation_id": row["interpretation_id"],
                                              "profile_id": row["profile_id"]}

        entities: dict[str, dict[str, Any]] = {}
        selected_keys: dict[str, dict[str, set[str]]] = {}
        global_domain_status: dict[str, Counter] = defaultdict(Counter)
        global_domain_claims: Counter = Counter()
        missing_evidence_count = 0
        payload_limit_record_count = 0
        payload_limit_examples: list[dict[str, Any]] = []
        total_pawn_rows = _count_where(db, "SELECT count(*) FROM pawns p JOIN snapshots s ON s.snapshot_id=p.snapshot_id" + where_sql.replace("s.", "s."), where_args)
        pawn_sql = ("SELECT p.snapshot_id,p.pawn_id,p.record_id,p.payload,s.campaign_id,s.game_tick,s.ticks_abs,s.interpretation_id "
                    "FROM pawns p JOIN snapshots s ON s.snapshot_id=p.snapshot_id" + where_sql +
                    " ORDER BY s.campaign_id,p.pawn_id,s.game_tick,s.snapshot_id,p.record_id")
        for row in db.execute(pawn_sql, where_args):
            meta = snapshots[row["snapshot_id"]]
            entity_id, join_basis = _entity_id(row["campaign_id"], row["snapshot_id"], row["pawn_id"], row["record_id"])
            entity = entities.setdefault(entity_id, _new_entity(entity_id, row["campaign_id"], row["pawn_id"], join_basis))
            entity["observation_count"] += 1
            entity["snapshot_ids"].add(row["snapshot_id"])
            tick = row["game_tick"] if isinstance(row["game_tick"], int) and not isinstance(row["game_tick"], bool) else None
            if tick is None:
                entity["unknown_clock_observation_count"] += 1
            elif entity["latest_game_tick"] is None or tick > entity["latest_game_tick"]:
                entity["latest_game_tick"] = tick
                entity["latest_observations"] = [{"snapshot_id": row["snapshot_id"], "record_id": row["record_id"],
                                                  "game_tick": tick}]
                entity["latest_tie_total"] = 1
            elif tick == entity["latest_game_tick"]:
                entity["latest_tie_total"] += 1
                if len(entity["latest_observations"]) < _MAX_LATEST_ALTERNATIVES:
                    entity["latest_observations"].append({"snapshot_id": row["snapshot_id"],
                                                          "record_id": row["record_id"], "game_tick": tick})
            try:
                pawn = history_catalog.unpack(row["payload"], limit=_PAWN_DECOMPRESS_LIMIT)
            except InspectorError as exc:
                if exc.code != "LIMIT_EXCEEDED":
                    raise
                entity["payload_limit_observation_count"] += 1
                payload_limit_record_count += 1
                if len(payload_limit_examples) < 8:
                    payload_limit_examples.append({"kind": "pawn", "snapshot_id": row["snapshot_id"],
                                                   "pawn_id": row["pawn_id"], "record_id": row["record_id"]})
                continue
            if not _is_object(pawn):
                entity["payload_limit_observation_count"] += 1
                payload_limit_record_count += 1
                if len(payload_limit_examples) < 8:
                    payload_limit_examples.append({"kind": "pawn", "snapshot_id": row["snapshot_id"],
                                                   "pawn_id": row["pawn_id"], "record_id": row["record_id"]})
                continue
            for alias in pawn.get("aliases", []):
                if not _is_object(alias):
                    continue
                value = alias.get("value") or alias.get("name")
                if isinstance(value, str) and value:
                    alias_key = _sha256(value)
                    if alias_key not in entity["alias_values"] and len(entity["alias_values"]) >= _MAX_ALIASES_PER_ENTITY:
                        continue
                    entry = entity["alias_values"].setdefault(alias_key, {"value": value, "count": 0, "examples": []})
                    entry["count"] += 1
                    if len(entry["examples"]) < 2:
                        entry["examples"].append({"snapshot_id": row["snapshot_id"],
                                                  "record_id": row["record_id"],
                                                  "evidence": _refs_from_claim(alias, namespace, selected_generation)})
            domains = pawn.get("domains", {})
            if not isinstance(domains, Mapping):
                continue
            for domain, body in domains.items():
                if not _is_object(body):
                    global_domain_status[domain]["invalid_shape"] += 1
                    continue
                status = body.get("status", "unknown")
                entity["domain_status_counts"][domain][str(status)] += 1
                global_domain_status[domain][str(status)] += 1
                claim_rows = body.get("claims", [])
                if not isinstance(claim_rows, (list, tuple)):
                    entity["claim_status_counts"][domain]["invalid_claim_array"] += 1
                    continue
                for claim in claim_rows:
                    if not _is_object(claim):
                        entity["claim_status_counts"][domain]["invalid_claim"] += 1
                        continue
                    entity["domain_claim_counts"][domain] += 1
                    global_domain_claims[domain] += 1
                    entity["claim_status_counts"][domain][str(claim.get("status", "unknown"))] += 1
                    if not (isinstance(claim.get("evidence_id"), str) and
                            isinstance(claim.get("source_ref"), str)):
                        missing_evidence_count += 1
                    membership = _row_membership(row["snapshot_id"], tick, row["record_id"],
                                                 row["interpretation_id"], domain, claim,
                                                 namespace, selected_generation)
                    _observe_conflict(entity, domain, claim, membership)
                    _add_claim_candidate(entity, domain, claim, tick)

        for entity_id, entity in entities.items():
            selected_keys[entity_id] = _selected_fact_keys(entity)

        episode_candidates: dict[str, dict[str, Any]] = {}
        total_event_rows = _count_where(db, "SELECT count(*) FROM events e JOIN snapshots s ON s.snapshot_id=e.snapshot_id" + where_sql, where_args)
        event_where, event_args = _where(anchor_snapshot_id, anchor_campaign)
        event_where = event_where.replace("s.snapshot_id", "s.snapshot_id").replace("s.campaign_id", "s.campaign_id")
        event_sql = ("SELECT e.snapshot_id,e.event_index,e.payload,s.campaign_id,s.game_tick,s.interpretation_id "
                     "FROM events e JOIN snapshots s ON s.snapshot_id=e.snapshot_id" + event_where +
                     " ORDER BY s.campaign_id,s.game_tick,s.snapshot_id,e.event_index")
        for row in db.execute(event_sql, event_args):
            try:
                event = history_catalog.unpack(row["payload"], limit=_EVENT_DECOMPRESS_LIMIT)
            except InspectorError as exc:
                if exc.code != "LIMIT_EXCEEDED":
                    raise
                payload_limit_record_count += 1
                if len(payload_limit_examples) < 8:
                    payload_limit_examples.append({"kind": "event", "snapshot_id": row["snapshot_id"],
                                                   "event_index": row["event_index"]})
                continue
            if not _is_object(event):
                continue
            episode_key, identity_status, variant_id = _episode_key(event, row["snapshot_id"],
                                                                    row["campaign_id"], row["event_index"])
            candidate = {"episode_key": episode_key, "identity_status": identity_status,
                         "variant_id": variant_id, "score": _event_score(event),
                         "saved_event_id": event.get("saved_event_id"),
                         "campaign_id": row["campaign_id"], "kind": event.get("kind", "unknown")}
            _retain_top_episode(episode_candidates, candidate)

        selected_occurrences: dict[str, list[dict[str, Any]]] = {key: [] for key in episode_candidates}
        selected_event_claims: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in episode_candidates}
        event_participants: dict[str, list[dict[str, Any]]] = {key: [] for key in episode_candidates}
        event_occurrence_counts: Counter = Counter()
        event_claim_counts: Counter = Counter()
        event_participant_counts: Counter = Counter()
        for row in db.execute(event_sql, event_args):
            if row["snapshot_id"] not in snapshots:
                continue
            try:
                event = history_catalog.unpack(row["payload"], limit=_EVENT_DECOMPRESS_LIMIT)
            except InspectorError as exc:
                if exc.code != "LIMIT_EXCEEDED":
                    raise
                continue
            if not _is_object(event):
                continue
            episode_key, _, _ = _episode_key(event, row["snapshot_id"], row["campaign_id"], row["event_index"])
            if episode_key not in episode_candidates:
                continue
            occurrence = event.get("occurrence")
            tick = row["game_tick"] if isinstance(row["game_tick"], int) and not isinstance(row["game_tick"], bool) else None
            event_record_id = event.get("record_id") or f"{row['snapshot_id']}:event:{row['event_index']}"
            refs = []
            for ref in event.get("evidence_refs", []):
                if _is_object(ref):
                    refs.extend(_refs_from_claim(ref, namespace, selected_generation))
            event_occurrence_counts[episode_key] += 1
            if len(selected_occurrences[episode_key]) < _MAX_OCCURRENCES_PER_EPISODE:
                selected_occurrences[episode_key].append({"snapshot_id": row["snapshot_id"], "game_tick": tick,
                                                           "event_index": row["event_index"],
                                                           "record_id": event_record_id,
                                                           "occurrence": deepcopy(occurrence), "evidence": refs})
            for claim in event.get("claims", []):
                if not _is_object(claim):
                    continue
                if not (isinstance(claim.get("evidence_id"), str) and
                        isinstance(claim.get("source_ref"), str)):
                    missing_evidence_count += 1
                key = _fact_key("event", claim)
                event_claim_counts[(episode_key, key)] += 1
                if key not in selected_event_claims[episode_key] and len(selected_event_claims[episode_key]) >= _MAX_EVENT_FACTS_PER_EPISODE:
                    continue
                event_fact = selected_event_claims[episode_key].setdefault(key, {
                    "field": claim.get("field"), "value": deepcopy(claim.get("value")),
                    "status": claim.get("status"), "unit": claim.get("unit"),
                    "code": claim.get("code"), "display_label": claim.get("display_label"),
                    "normalization_rule": claim.get("normalization_rule"), "memberships": [],
                    "total_occurrence_count": 0, "omitted_membership_count": 0})
                event_fact["total_occurrence_count"] += 1
                if len(event_fact["memberships"]) < _MAX_MEMBERSHIPS_PER_EVENT_FACT:
                    event_fact["memberships"].append(_row_membership(row["snapshot_id"], tick,
                                                                      event_record_id, row["interpretation_id"],
                                                                      "event", claim, namespace, selected_generation))
                else:
                    event_fact["omitted_membership_count"] += 1
            for participant in event.get("participants", []):
                if not _is_object(participant):
                    continue
                pawn_id = participant.get("pawn_id")
                event_participant_counts[episode_key] += 1
                if len(event_participants[episode_key]) >= 32:
                    continue
                participant_entity, _ = _entity_id(row["campaign_id"], row["snapshot_id"],
                                                   pawn_id if isinstance(pawn_id, str) else None,
                                                   str(pawn_id or event_record_id))
                event_participants[episode_key].append({"pawn_id": pawn_id,
                                                        "entity_id": participant_entity if participant_entity in entities else None,
                                                        "role": participant.get("role"),
                                                        "status": participant.get("status"),
                                                        "snapshot_id": row["snapshot_id"],
                                                        "evidence": _refs_from_claim(participant, namespace, selected_generation)})

        # Second pass: reconstruct every source membership for each displayed fact,
        # retaining exact path/index/raw-value and generation-bound evidence tokens.
        occurrence_counts: dict[tuple[str, str], int] = Counter()
        total_membership_json_bytes = 0
        retain_memberships = True
        provisional = _base_result(report, namespace, selected_generation, snapshots,
                                   entities, selected_keys, episode_candidates,
                                   selected_occurrences, selected_event_claims,
                                   event_participants, source_outcomes=None,
                                   global_domain_status=global_domain_status,
                                   global_domain_claims=global_domain_claims,
                                   total_pawn_rows=total_pawn_rows,
                                   total_event_rows=total_event_rows)
        provisional["coverage"]["payload_limit_record_count"] = payload_limit_record_count
        provisional["coverage"]["payload_limit_examples"] = payload_limit_examples
        provisional["coverage"]["claim_without_evidence_pair_count"] = missing_evidence_count
        provisional["catalog"]["anchor_snapshot_id"] = anchor_snapshot_id
        for episode in provisional["episodes"]:
            key = episode["episode_id"].removeprefix("episode:")
            observed = event_occurrence_counts.get(key, 0)
            episode["total_occurrence_count"] = observed
            episode["selected_occurrence_count"] = len(episode["occurrences"])
            episode["omitted_occurrence_count"] = max(0, observed - len(episode["occurrences"]))
            episode["participant_count"] = event_participant_counts.get(key, 0)
            episode["omitted_participant_count"] = max(0, episode["participant_count"] - len(episode["participants"]))
            episode["selection"] = {"occurrence_cap": _MAX_OCCURRENCES_PER_EPISODE,
                                    "claim_fact_cap": _MAX_EVENT_FACTS_PER_EPISODE,
                                    "membership_cap_per_fact": _MAX_MEMBERSHIPS_PER_EVENT_FACT,
                                    "display_only": True}
        event_followups = _event_followup_selectors(db, snapshots, selected_generation,
                                                    provisional["episodes"])
        provisional["coverage"]["event_followup_selectors"] = event_followups
        provisional["linked_artifacts"] = _linked_artifact_summary(db, snapshots,
                                                                    selected_generation, namespace)
        provisional["evidence_use"]["queried"] = {
            "source_outcomes": len(provisional["source_outcomes"]),
            "snapshot_records": len(snapshots), "pawn_records": total_pawn_rows,
            "event_records": total_event_rows,
            "pawn_payload_reads": total_pawn_rows * 2,
            "event_payload_reads": total_event_rows * 2,
            "snapshot_payloads_decompressed": 0,
            "global_evidence_nodes_decompressed": 0}
        base_size = len(_json_bytes(provisional))
        for row in db.execute(pawn_sql, where_args):
            meta = snapshots[row["snapshot_id"]]
            entity_id, _ = _entity_id(row["campaign_id"], row["snapshot_id"], row["pawn_id"], row["record_id"])
            if entity_id not in selected_keys:
                continue
            try:
                pawn = history_catalog.unpack(row["payload"], limit=_PAWN_DECOMPRESS_LIMIT)
            except InspectorError as exc:
                if exc.code == "LIMIT_EXCEEDED":
                    continue
                raise
            if not _is_object(pawn):
                continue
            for domain, body in pawn.get("domains", {}).items():
                if not _is_object(body):
                    continue
                selected = selected_keys[entity_id].get(domain, set())
                for claim in body.get("claims", []):
                    if not _is_object(claim):
                        continue
                    fact_id = _fact_key(domain, claim)
                    if fact_id not in selected:
                        continue
                    member = _row_membership(row["snapshot_id"], meta["game_tick"], row["record_id"],
                                             row["interpretation_id"], domain, claim,
                                             namespace, selected_generation)
                    target = _find_fact(provisional["entities"], entity_id, domain, fact_id)
                    if target is None:
                        continue
                    pair = (entity_id, fact_id)
                    index = occurrence_counts[pair]
                    occurrence_counts[pair] += 1
                    encoded_size = len(_json_bytes(member)) + (1 if index else 0)
                    total_membership_json_bytes += encoded_size
                    if retain_memberships and base_size + total_membership_json_bytes <= limit:
                        target["memberships"].append(member)
                    else:
                        retain_memberships = False

        selected_occurrences_by_domain: Counter = Counter()
        for entity in provisional["entities"]:
                for domain, bundle in entity["domain_highlights"].items():
                    total_claims = entity["domain_coverage"][domain]["claim_count"]
                    selected_occurrences_count = sum(occurrence_counts.get((entity["entity_id"], fact["fact_id"]), 0)
                                                     for fact in bundle["fact_bundles"])
                    selected_occurrences_by_domain[domain] += selected_occurrences_count
                    bundle["selected_occurrence_count"] = selected_occurrences_count
                    bundle["omitted_occurrence_count"] = max(0, total_claims - selected_occurrences_count)
                    entity["domain_coverage"][domain]["selected_fact_count"] = len(bundle["fact_bundles"])
                    entity["domain_coverage"][domain]["selected_occurrence_count"] = selected_occurrences_count
                    entity["domain_coverage"][domain]["omitted_occurrence_count"] = max(0, total_claims - selected_occurrences_count)

        for domain, domain_row in provisional["coverage"]["domains"].items():
            selected_count = selected_occurrences_by_domain.get(domain, 0)
            domain_row["selected_occurrence_count"] = selected_count
            domain_row["omitted_occurrence_count"] = max(0, domain_row["claim_occurrence_count"] - selected_count)

        result = provisional
        included_evidence = _displayed_evidence(result)
        result["evidence_use"]["included"] = included_evidence
        result["evidence_use"]["used"] = []
        result["evidence_use"]["used_by_annotations"] = []
        result["evidence_use"]["excluded"] = {
            domain: {"claim_occurrence_count": row["claim_occurrence_count"],
                     "selected_occurrence_count": row["selected_occurrence_count"],
                     "omitted_occurrence_count": row["omitted_occurrence_count"],
                     "reason": "bounded display selection; original catalog record remains available"}
            for domain, row in result["coverage"]["domains"].items()
            if row["omitted_occurrence_count"]}
        if result["coverage"]["omitted_episode_record_count"]:
            result["evidence_use"]["excluded"]["events"] = {
                "record_count": result["coverage"]["omitted_episode_record_count"],
                "reason": "bounded episode and occurrence display; catalog rows remain stored"}
        if payload_limit_record_count:
            result["evidence_use"]["excluded"]["payload_limit"] = {
                "record_count": payload_limit_record_count,
                "reason": "payload exceeded the per-record decompression cap"}
        result["evidence_use"]["unresolved"] = _skeptic_followups(result, missing_evidence_count,
                                                                      skeptic_limit)
        if semantic_adapter is not None:
            packet = _selected_packet(result)
            result["semantic_cache_key"] = semantic_cache_key(result, semantic_adapter)
            result["annotations"] = _check_annotations(semantic_adapter.annotate(deepcopy(packet)), packet)
            for index, annotation in enumerate(result["annotations"]):
                if annotation.get("citation_status") != "verified":
                    continue
                result["evidence_use"]["used_by_annotations"].append({
                    "annotation_index": index,
                    "evidence": [{"evidence_id": evidence_id, "source_ref": source_ref}
                                 for evidence_id, source_ref in zip(annotation.get("support", []),
                                                                    annotation.get("source_refs", []))]})
        measured_size = len(_json_bytes(result))
        if not retain_memberships or measured_size > limit:
            measured = max(measured_size, base_size + total_membership_json_bytes)
            raise InspectorError("RESULT_TOO_LARGE", "The discovery result exceeds the configured response limit.",
                                 {"required_bytes": measured, "configured_limit": limit,
                                  "selectors": {"anchor_snapshot_id": anchor_snapshot_id,
                                                "generation_id": selected_generation,
                                                "freshness": "frozen",
                                                "available": ["get_timeline", "get_dossier", "get_evidence"]}})
        result["size"] = {"serialized_bytes": 0, "configured_limit": limit,
                           "absolute_limit": ABSOLUTE_LIMIT,
                           "semantic_completeness": "selected_highlights_only",
                           "record_population_complete": True}
        for _ in range(3):
            measured = len(_json_bytes(result))
            if result["size"]["serialized_bytes"] == measured:
                break
            result["size"]["serialized_bytes"] = measured
        if len(_json_bytes(result)) > limit:
            raise InspectorError("RESULT_TOO_LARGE", "The discovery result exceeds the configured response limit.",
                                 {"required_bytes": len(_json_bytes(result)), "configured_limit": limit,
                                  "selectors": {"anchor_snapshot_id": anchor_snapshot_id,
                                                "generation_id": selected_generation}})
        return result
    finally:
        db.close()


def _refs_from_claim(claim: Mapping[str, Any], namespace: str, generation_id: str) -> list[dict[str, Any]]:
    evidence_id, source_ref = claim.get("evidence_id"), claim.get("source_ref")
    if not isinstance(evidence_id, str) or not isinstance(source_ref, str):
        return []
    return [{"content_id": evidence_id, "source_ref": source_ref,
             "retrieval_id": _evidence_retrieval_id(namespace, generation_id, evidence_id, source_ref)}]


def _base_result(report: Mapping[str, Any], namespace: str, generation_id: str,
                 snapshots: Mapping[str, Mapping[str, Any]],
                 entities: Mapping[str, dict[str, Any]],
                 selected_keys: Mapping[str, Mapping[str, set[str]]],
                 episode_candidates: Mapping[str, Mapping[str, Any]],
                 episode_occurrences: Mapping[str, list[dict[str, Any]]],
                 episode_claims: Mapping[str, Mapping[str, Mapping[str, Any]]],
                 episode_participants: Mapping[str, list[dict[str, Any]]],
                 *, source_outcomes: list[dict[str, Any]] | None,
                 global_domain_status: Mapping[str, Counter], global_domain_claims: Counter,
                 total_pawn_rows: int, total_event_rows: int) -> dict[str, Any]:
    outcomes, status_counts = _source_outcomes(report)
    if source_outcomes is not None:
        outcomes = source_outcomes
    result_entities = []
    selector_template = {"python": "rimworld_save_inspector.history.service.get_timeline",
                         "mcp": "get_pawn_timeline", "availability": "available",
                         "params": {"freshness": "frozen", "generation_id": generation_id,
                                    "pawn_id": "<saved pawn ID>",
                                    "anchor_snapshot_id": "<snapshot in validated campaign>"}}
    for entity_id, entity in sorted(entities.items()):
        selected = selected_keys[entity_id]
        domain_coverage = {}
        for domain in sorted(set(entity["domain_status_counts"]) | set(entity["domain_claim_counts"])):
            domain_coverage[domain] = {"status_counts": dict(sorted(entity["domain_status_counts"][domain].items())),
                                       "claim_count": entity["domain_claim_counts"].get(domain, 0),
                                       "claim_status_counts": dict(sorted(entity["claim_status_counts"][domain].items())),
                                       "selected_fact_count": 0, "selected_occurrence_count": 0,
                                       "omitted_occurrence_count": entity["domain_claim_counts"].get(domain, 0)}
        highlights = {}
        for domain, keys in sorted(selected.items(), key=lambda pair: (_domain_priority(pair[0]), pair[0])):
            facts = [entity["candidates"][key] for key in keys if key in entity["candidates"]]
            facts.sort(key=lambda row: (not (entity["latest_game_tick"] is not None and
                                             row["last_known_tick"] == entity["latest_game_tick"]),
                                        -row["score"], str(row["semantic"].get("field")), row["fact_id"]))
            highlights[domain] = {"bundle_kind": "gene_vector_bundle" if domain == "genes" else "fact_bundle",
                                  "fact_bundles": [{**deepcopy(row["semantic"]), "fact_id": row["fact_id"],
                                                    "memberships": []} for row in facts],
                                  "selected_occurrence_count": 0,
                                  "omitted_occurrence_count": entity["domain_claim_counts"].get(domain, 0)}
        latest_tick = entity["latest_game_tick"]
        alternatives = deepcopy(entity["latest_observations"])
        observed_snapshot_id = min(entity["snapshot_ids"]) if entity["snapshot_ids"] else None
        aliases = sorted(entity["alias_values"].values(), key=lambda row: row["value"].casefold())
        if entity["campaign_id"] is not None:
            followup = {"python": "rimworld_save_inspector.history.service.get_timeline",
                        "mcp": "get_pawn_timeline", "availability": "available",
                        "params": {"freshness": "frozen", "generation_id": generation_id,
                                   "pawn_id": entity["saved_id"],
                                   "anchor_snapshot_id": alternatives[0]["snapshot_id"] if alternatives else observed_snapshot_id}}
        else:
            followup = {"python": "rimworld_save_inspector.history.service.get_dossier",
                        "mcp": "get_pawn_dossier", "availability": "available",
                        "params": {"generation_id": generation_id,
                                   "snapshot_id": observed_snapshot_id,
                                   "pawn_id": entity["saved_id"]}}
        conflict_rows = []
        for entry in entity["conflicting_fields"].values():
            if len(entry["variants"]) > 1:
                conflict_rows.append({"domain": entry["domain"], "field": entry["field"],
                                      "status": "conflicting_observations",
                                      "observation_count": entry["observation_count"],
                                      "variants": list(entry["variants"].values()),
                                      "interpretation": "observed variants; continuity, cause, and motive are not inferred"})
        result_entities.append({"entity_id": entity_id, "kind": "pawn", "campaign_id": entity["campaign_id"],
                                "saved_id": entity["saved_id"], "join_basis": entity["join_basis"],
                                "aliases": aliases, "observation_count": entity["observation_count"],
                                "observed_snapshot_count": len(entity["snapshot_ids"]),
                                "continuity_status": "unproven" if entity["observation_count"] > 1 else "single_observation",
                                "latest_observation_by_game_tick": {
                                    "status": ("unknown_clock" if latest_tick is None else
                                               "same_tick_alternatives" if entity["latest_tie_total"] > 1 else
                                               "known_tick_only_unknown_observations_also_present" if entity["unknown_clock_observation_count"] else "known_tick"),
                                    "game_tick": latest_tick,
                                    "alternatives": alternatives,
                                    "alternative_count": entity["latest_tie_total"],
                                    "unknown_clock_observation_count": entity["unknown_clock_observation_count"]},
                                "domain_coverage": domain_coverage, "domain_highlights": highlights,
                                "conflicting_fields": conflict_rows,
                                "payload_limit_observation_count": entity["payload_limit_observation_count"],
                                "selection": {"policy": "latest known game tick, then domain priority and field salience",
                                              "candidate_cap": _MAX_CANDIDATES_PER_ENTITY,
                                              "candidate_evictions": entity["candidate_evictions"],
                                              "display_only": True},
                                "followup": followup})
    episodes = []
    for key, candidate in sorted(episode_candidates.items(), key=lambda pair: (-pair[1]["score"], pair[0])):
        episodes.append({"episode_id": "episode:" + key, "saved_event_id": candidate["saved_event_id"],
                         "kind": candidate["kind"], "campaign_id": candidate["campaign_id"],
                         "identity_status": candidate["identity_status"], "rank_hint": candidate["score"],
                         "rank_hint_calibrated": False,
                         "claims": list(episode_claims[key].values()),
                         "participants": episode_participants[key],
                         "occurrences": episode_occurrences[key],
                         "join_basis": candidate["identity_status"]})
    source_count = len(outcomes)
    domain_summary = {}
    for domain in sorted(set(global_domain_status) | set(global_domain_claims)):
        domain_summary[domain] = {"pawn_observation_status_counts": dict(sorted(global_domain_status[domain].items())),
                                  "claim_occurrence_count": global_domain_claims.get(domain, 0),
                                  "selected_occurrence_count": 0,
                                  "omitted_occurrence_count": global_domain_claims.get(domain, 0)}
    source_ids = [{"snapshot_id": row["snapshot_id"], "interpretation_id": row["interpretation_id"],
                   "profile_id": row["profile_id"], "campaign_id": row["campaign_id"],
                   "source_channel": "save_xml_catalog", "game_tick": row["game_tick"],
                   "ticks_abs": row["ticks_abs"]}
                  for row in snapshots.values()]
    source_ids.sort(key=lambda row: (str(row["campaign_id"]), str(row["game_tick"]), row["snapshot_id"]))
    clock_observations = [{"snapshot_id": row["snapshot_id"], "source_channel": "save_xml_catalog",
                           "game_tick": row["game_tick"], "ticks_abs": row["ticks_abs"],
                           "calendar_label": None, "biological_duration": "separate_pawn_claims",
                           "observed_at": None, "wall_clock_used_for_ordering": False}
                          for row in source_ids]
    return {"discovery_schema_version": DISCOVERY_VERSION,
            "catalog": {"namespace_id": namespace, "generation_id": generation_id,
                        "freshness": report.get("catalog", {}).get("freshness", "frozen"),
                        "anchor_snapshot_id": report.get("catalog", {}).get("anchor_snapshot_id")},
            "source_channel": {"id": "save_xml_catalog", "status": "save-derived evidence only",
                               "available_channels": {"save_xml_catalog": "included",
                                                      "verified_recorder": "not_included",
                                                      "defs_annotation": "not_included"},
                               "recorder_channel": "not_included", "preinstallation_recorder_claims": "abstain",
                               "resolver_version": DISCOVERY_VERSION},
            "linked_artifacts": {"status": "not_materialized_in_first_pass",
                                 "types": ["tales", "messages", "logs", "art"],
                                 "provenance": "retrieve through snapshot dossier and cited evidence",
                                 "followup": {"python": "rimworld_save_inspector.history.service.get_dossier",
                                              "mcp": "get_pawn_dossier", "availability": "available_with_snapshot_and_pawn_selectors"}},
            "sources": source_ids, "source_outcomes": outcomes,
            "entities": result_entities, "episodes": episodes,
            "clock_observations": clock_observations,
            "support_and_counterevidence": [variant for entity in result_entities
                                             for variant in entity["conflicting_fields"]],
            "coverage": {"source_outcome_count": source_count,
                         "source_outcome_status_counts": status_counts,
                         "unaccounted_source_count": 0,
                         "snapshot_count": len(snapshots),
                         "pawn_record_count": total_pawn_rows,
                         "entity_count": len(result_entities),
                         "event_record_count": total_event_rows,
                         "selected_episode_count": len(episodes),
                         "omitted_episode_record_count": max(0, total_event_rows - sum(len(e["occurrences"]) for e in episodes)),
                         "domains": domain_summary,
                         "no_pruning": {"catalog_records_pruned": False,
                                         "pawn_records_retrievable": True,
                                         "event_records_retrievable": True,
                                         "low_ranked_fallback_status": "available_via_paged_event_lookup",
                                         "followup_tools": {"python": ["get_timeline", "get_dossier", "get_evidence",
                                                                          "get_events"],
                                                            "mcp": ["get_pawn_timeline", "get_pawn_dossier",
                                                                    "get_history_evidence", "get_history_events"]},
                                         "event_followup_availability": "available_for_validated_campaigns"}},
            "selection": {"highlight_policy": "deterministic domain and field priority; current known tick is a hint",
                          "per_domain_caps": {**_MAX_HIGHLIGHTS_PER_DOMAIN,
                                              "default": _DEFAULT_HIGHLIGHTS_PER_DOMAIN},
                          "gene_vector_separate_from_role": True,
                          "omitted_claims_remain_in_catalog": True},
            "followup_template": selector_template,
            "evidence_use": {"used": [], "included": [], "excluded": {}, "queried": {},
                             "unresolved": [], "failed": [row for row in outcomes if row["status"] != "parsed"]},
            "uncertainty": {"ancestry": "unproven", "unknown_ticks_are_not_ordered": True,
                            "names_are_aliases_not_join_keys": True,
                            "semantic_entailment": "not_assessed"},
            "semantic_cache_key": None, "annotations": []}


def _find_fact(entities: list[dict[str, Any]], entity_id: str,
               domain: str, fact_id: str) -> dict[str, Any] | None:
    entity = next((row for row in entities if row["entity_id"] == entity_id), None)
    if entity is None:
        return None
    bundle = entity["domain_highlights"].get(domain)
    if bundle is None:
        return None
    return next((fact for fact in bundle["fact_bundles"] if fact["fact_id"] == fact_id), None)
