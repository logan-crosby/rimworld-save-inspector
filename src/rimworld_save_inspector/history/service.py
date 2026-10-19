"""Shared dossier, timeline, and retained-evidence query service."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..errors import InspectorError
from . import catalog
from .chronology import order_observations
from .identity import normalize_pawn_id

HISTORY_VERSION = "1.0"
CONTEXT_LIMIT = 16 * 1024 * 1024
FULL_LIMIT = 8 * 1024 * 1024
EVIDENCE_PAGE_LIMIT = 512 * 1024
ABSOLUTE_LIMIT = 16 * 1024 * 1024


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _fail(code: str, message: str, **details: Any) -> InspectorError:
    return InspectorError(code, message, details)


def _token(parts: list[str]) -> str:
    return "h1." + base64.urlsafe_b64encode(_json(parts)).decode("ascii").rstrip("=")


def _untoken(token: str, size: int) -> list[str]:
    if not isinstance(token, str) or not token.startswith("h1.") or len(token) > 2048:
        raise _fail("INVALID_CURSOR", "The history identifier is invalid.")
    try:
        padded = token[3:] + "=" * (-len(token[3:]) % 4)
        parts = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _fail("INVALID_CURSOR", "The history identifier is invalid.") from exc
    if not isinstance(parts, list) or len(parts) != size or not all(isinstance(p, str) for p in parts):
        raise _fail("INVALID_CURSOR", "The history identifier is invalid.")
    return parts


def _size_result(result: dict[str, Any], limit: int, *,
                 semantic_complete: bool = True) -> dict[str, Any]:
    if limit > ABSOLUTE_LIMIT or limit <= 0:
        raise _fail("INVALID_ARGUMENT", "The response limit is outside the supported range.")
    result["size"] = {"serialized_bytes": 0, "configured_limit": limit,
                      "absolute_limit": ABSOLUTE_LIMIT, "semantic_complete": semantic_complete,
                      "raw_detail_coverage": "retained_by_evidence"}
    for _ in range(4):
        measured = len(_json(result)) + 1  # CLI JSON line terminator
        if result["size"]["serialized_bytes"] == measured:
            break
        result["size"]["serialized_bytes"] = measured
    measured = len(_json(result)) + 1
    if measured > limit:
        raise _fail("RESULT_TOO_LARGE", "The history result exceeds the configured response limit.",
                    required_bytes=measured, configured_limit=limit,
                    selectors={"pawn_id": result.get("subject", {}).get("pawn_id"),
                               "detail": result.get("detail")})
    return result


def _selector(pawn_id: str | None, name: str | None) -> None:
    if (pawn_id is None) == (name is None):
        raise _fail("INVALID_ARGUMENT", "Exactly one pawn_id or name selector is required.")
    if pawn_id is not None and not normalize_pawn_id(pawn_id):
        raise _fail("INVALID_ARGUMENT", "A saved pawn ID is required.")
    if name is not None and not name.strip():
        raise _fail("INVALID_ARGUMENT", "A nonempty historical name is required.")


def _prepare(path: Path, freshness: str,
             generation_id: str | None = None
             ) -> tuple[dict[str, Any], str, str, sqlite3.Connection]:
    if generation_id is not None:
        report = catalog.inspect_catalog(path, generation_id=generation_id,
                                         freshness="frozen")
    elif freshness == "frozen":
        report = catalog.inspect_catalog(path, freshness="frozen")
    elif freshness in {"metadata", "strict"}:
        report = catalog.refresh_catalog(path, freshness=freshness)
    else:
        raise _fail("INVALID_ARGUMENT", "Freshness must be metadata, strict, or frozen.")
    namespace, selected_generation_id, db = catalog.open_generation(
        path, report["catalog"]["generation_id"])
    return report, namespace, selected_generation_id, db


def _metadata_clocks(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    clocks = payload.get("metadata", {}).get("clocks", {})
    game = clocks.get("ticksGame", {}).get("value")
    absolute = clocks.get("ticksAbs", {}).get("value")
    return game if isinstance(game, int) and not isinstance(game, bool) else None, (
        absolute if isinstance(absolute, int) and not isinstance(absolute, bool) else None)


def _snapshots(db: sqlite3.Connection, campaign_id: str) -> list[dict[str, Any]]:
    try:
        rows = db.execute(
            "SELECT s.snapshot_id,s.campaign_id,s.game_tick,s.ticks_abs,"
            "c.payload AS context_payload FROM snapshots s "
            "LEFT JOIN snapshot_context c ON c.snapshot_id=s.snapshot_id "
            "WHERE s.campaign_id=? ORDER BY s.game_tick,s.snapshot_id", (campaign_id,)
        )
    except sqlite3.OperationalError as exc:
        if "no such table: snapshot_context" not in str(exc):
            raise _fail("INVALID_CATALOG", "The catalog context index is malformed.") from exc
        layout = db.execute("SELECT value FROM meta WHERE key='storage_layout_version'").fetchone()
        if layout is not None:
            try:
                version = json.loads(layout["value"])
            except (TypeError, ValueError) as error:
                raise _fail("INVALID_CATALOG", "The catalog storage layout is invalid.") from error
            if not isinstance(version, int) or isinstance(version, bool) or version >= 2:
                raise _fail("INVALID_CATALOG", "The catalog context index is missing.") from exc
        rows = db.execute(
            "SELECT snapshot_id,campaign_id,game_tick,ticks_abs,payload AS context_payload "
            "FROM snapshots WHERE campaign_id=? ORDER BY game_tick,snapshot_id", (campaign_id,)
        )
        legacy = True
    else:
        legacy = False
    result = []
    for row in rows:
        if row["context_payload"] is None:
            raise _fail("INVALID_CATALOG", "A catalog snapshot is missing its context index.")
        decoded = catalog.unpack(row["context_payload"])
        if not isinstance(decoded, dict):
            raise _fail("INVALID_CATALOG", "The catalog context index is invalid.")
        context = decoded.get("context", {}) if legacy else decoded
        result.append({"snapshot_id": row["snapshot_id"], "campaign_id": row["campaign_id"],
                       "tick": row["game_tick"], "ticks_abs": row["ticks_abs"],
                       "payload": {"context": context}})
    return result


def _campaign(db: sqlite3.Connection, anchor_snapshot_id: str | None) -> tuple[str, str]:
    if anchor_snapshot_id is not None:
        row = db.execute("SELECT campaign_id FROM snapshots WHERE snapshot_id=?",
                         (anchor_snapshot_id,)).fetchone()
        if row is None:
            raise _fail("SNAPSHOT_NOT_FOUND", "The anchor snapshot is not in this generation.")
        if row["campaign_id"] is None:
            raise _fail("REVIEW_NEEDED", "The anchor has no validated campaign key.")
        return row["campaign_id"], anchor_snapshot_id
    groups = db.execute("SELECT DISTINCT campaign_id FROM snapshots").fetchall()
    validated = [row[0] for row in groups if row[0] is not None]
    if len(validated) != 1 or len(groups) != 1:
        raise _fail("AMBIGUOUS_ANCHOR", "Select an anchor snapshot from a compatible group.",
                    campaigns=validated)
    campaign_id = validated[0]
    candidates = db.execute(
        "SELECT snapshot_id,game_tick FROM snapshots WHERE campaign_id=? "
        "ORDER BY game_tick DESC,snapshot_id", (campaign_id,)).fetchall()
    if not candidates or candidates[0]["game_tick"] is None:
        raise _fail("AMBIGUOUS_ANCHOR", "The collection has no unique validated latest tick.")
    highest = candidates[0]["game_tick"]
    winners = [row["snapshot_id"] for row in candidates if row["game_tick"] == highest]
    if len(winners) != 1:
        raise _fail("AMBIGUOUS_ANCHOR", "The latest tick has branch alternatives.",
                    candidates=winners)
    return campaign_id, winners[0]


def _pawn_rows(db: sqlite3.Connection, snapshot_ids: list[str],
               pawn_id: str | None = None) -> list[dict[str, Any]]:
    rows = []
    for sid in snapshot_ids:
        if pawn_id is None:
            query = "SELECT snapshot_id,pawn_id,record_id,payload FROM pawns WHERE snapshot_id=?"
            values = (sid,)
        else:
            query = ("SELECT snapshot_id,pawn_id,record_id,payload FROM pawns "
                     "WHERE pawn_id=? AND snapshot_id=?")
            values = (pawn_id, sid)
        for row in db.execute(query, values):
            rows.append({"snapshot_id": sid, "pawn_id": row["pawn_id"],
                         "record_id": row["record_id"], "pawn": catalog.unpack(row["payload"])})
    return rows


def _alias_values(pawn: dict[str, Any]) -> list[str]:
    values = []
    for alias in pawn.get("aliases", []):
        if isinstance(alias, dict):
            name = alias.get("value") or alias.get("name")
        else:
            name = alias
        if isinstance(name, str) and name:
            values.append(name)
    identity = pawn.get("domains", {}).get("identity", {})
    for claim in identity.get("claims", []):
        if "name" in str(claim.get("field", "")).lower() and isinstance(claim.get("value"), str):
            values.append(claim["value"])
    return values


def _select_pawn(rows: list[dict[str, Any]], pawn_id: str | None, name: str | None
                 ) -> tuple[str, list[dict[str, Any]]]:
    if pawn_id is not None:
        selected = normalize_pawn_id(pawn_id)
    else:
        candidates = sorted({row["pawn_id"] for row in rows
                             if row["pawn_id"] and any(value.casefold() == name.casefold()
                                                       for value in _alias_values(row["pawn"]))})
        if not candidates:
            raise _fail("PAWN_NOT_FOUND", "The historical name is not present in this campaign.")
        if len(candidates) != 1:
            raise _fail("AMBIGUOUS_PAWN", "The historical name identifies multiple pawns.",
                        candidates=candidates)
        selected = candidates[0]
    matches = [row for row in rows if row["pawn_id"] == selected]
    if not matches:
        raise _fail("PAWN_NOT_FOUND", "The saved pawn ID is not present in this campaign.")
    return selected, matches


def _claims(pawn: dict[str, Any], *, domains: set[str] | None = None):
    for domain, body in pawn.get("domains", {}).items():
        if domains is not None and domain not in domains:
            continue
        if not isinstance(body, dict):
            continue
        for claim in body.get("claims", []):
            if isinstance(claim, dict):
                yield domain, claim


def _qualified_field(domain: str, field: Any) -> str:
    value = str(field)
    return value if value.startswith(domain + ".") else domain + "." + value


def _semantic(claim: dict[str, Any], domain: str) -> dict[str, Any]:
    return {key: value for key, value in {
        "domain": domain, "field": claim.get("field"), "status": claim.get("status"),
        "value": claim.get("value"), "unit": claim.get("unit"), "code": claim.get("code"),
        "display_label": claim.get("display_label"),
        "normalization_rule": claim.get("normalization_rule"),
    }.items()}


def _fact_key(semantic: dict[str, Any]) -> str:
    return hashlib.sha256(_json([semantic.get(key) for key in (
        "domain", "field", "status", "value", "unit", "code",
        "display_label", "normalization_rule",
    )])).hexdigest()


def _inverse_claim(fact: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    """Expand one fact membership into the exact logical source claim."""
    claim = {key: fact[key] for key in (
        "field", "value", "status", "unit", "code", "display_label",
        "normalization_rule",
    )}
    claim.update({key: member[key] for key in (
        "raw_value", "evidence_id", "source_ref",
    )})
    claim["time_observation"] = member.get("time_observation")
    claim.update(member.get("extensions", {}))
    return claim


def expand_context_memberships(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Invert context/1 indexed groups without losing raw lexemes or occurrence order.

    ``fact_index`` addresses the ``facts`` map, ``evidence_pairs`` holds exact
    content/occurrence IDs, and each claim row is ``[fact,pair]`` or
    ``[fact,pair,{r:raw_override,t:time_observation,x:extensions}]``. Raw defaults
    to the semantic value only when its canonical JSON is exactly equal. The
    containing record's snapshot, record ID and XML path are stored once.
    """
    facts = context["facts"]
    fact_index = context["fact_index"]
    evidence_pairs = context["evidence_pairs"]
    expanded = []
    for group in context["memberships"]:
        for row in group["claims"]:
            fact = facts[fact_index[row[0]]]
            evidence_id, source_ref = evidence_pairs[row[1]]
            extras = row[2] if len(row) == 3 else {}
            claim = {key: fact[key] for key in (
                "field", "value", "status", "unit", "code", "display_label",
                "normalization_rule",
            )}
            claim.update({"raw_value": extras.get("r", fact["value"]),
                          "time_observation": extras.get("t"),
                          "evidence_id": evidence_id, "source_ref": source_ref})
            claim.update(extras.get("x", {}))
            expanded.append({"snapshot_id": group["snapshot_id"],
                             "record_id": group["record_id"],
                             "domain": fact["domain"], "claim": claim})
    return expanded


def _evidence_tokens(namespace: str, generation_id: str,
                     refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for ref in refs:
        evidence_id, source_ref = ref.get("evidence_id"), ref.get("source_ref")
        if not isinstance(evidence_id, str) or not isinstance(source_ref, str):
            continue
        pair = (evidence_id, source_ref)
        if pair in seen:
            continue
        seen.add(pair)
        result.append({"evidence_id": _token([namespace, generation_id, evidence_id, source_ref]),
                       "content_id": evidence_id, "source_ref": source_ref})
    return result


def _event_result(db: sqlite3.Connection, campaign_snapshots: list[dict[str, Any]],
                  pawn_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    def semantic_payload(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: semantic_payload(v) for k, v in value.items()
                    if k not in {"evidence_id", "source_ref", "evidence_refs", "record_id",
                                 "source_path", "snapshot_id", "interpretation_id"}}
        if isinstance(value, list):
            return [semantic_payload(v) for v in value]
        return value

    variants: dict[str, dict[str, Any]] = {}
    by_saved_id: dict[str, set[str]] = defaultdict(set)
    unknown = []
    for snapshot in campaign_snapshots:
        sid = snapshot["snapshot_id"]
        for row in db.execute(
            "SELECT e.event_index,e.payload FROM events e JOIN event_links l ON "
            "e.snapshot_id=l.snapshot_id AND e.event_index=l.event_index "
            "WHERE l.snapshot_id=? AND l.pawn_id=?", (sid, pawn_id)):
            event = catalog.unpack(row["payload"])
            occurrence = event.get("occurrence") or {}
            semantic = semantic_payload(event)
            saved_id = event.get("saved_event_id")
            payload_hash = hashlib.sha256(_json(semantic)).hexdigest()
            key = (saved_id + ":" + payload_hash if saved_id else
                   "no-id:" + sid + ":" + str(row["event_index"]) + ":" + payload_hash)
            if key not in variants:
                variants[key] = {"event": event, "supporting_snapshots": [], "possible_duplicate": saved_id is None}
            variants[key]["supporting_snapshots"].append(sid)
            if saved_id:
                by_saved_id[saved_id].add(key)
    dated = []
    for key, value in variants.items():
        event = value["event"]
        occurrence = event.get("occurrence") or {}
        item = {"event_key": key, "kind": event.get("kind"),
                "saved_event_id": event.get("saved_event_id"),
                "participants": event.get("participants", []),
                "occurrence": occurrence, "claims": event.get("claims", []),
                "supporting_snapshots": sorted(set(value["supporting_snapshots"])),
                "evidence_refs": event.get("evidence_refs", []),
                "possible_duplicate": value["possible_duplicate"]}
        tick = occurrence.get("normalized_game_tick")
        if isinstance(tick, int) and occurrence.get("status") in {"known", "pre_arrival",
                                                                   "future_contradiction"}:
            dated.append(item)
        else:
            unknown.append(item)
    dated.sort(key=lambda e: (e["occurrence"]["normalized_game_tick"], e["event_key"]))
    unknown.sort(key=lambda e: e["event_key"])
    conflicts = [{"saved_event_id": key, "variant_keys": sorted(values)}
                 for key, values in sorted(by_saved_id.items()) if len(values) > 1]
    return dated, unknown, conflicts


def _event_participates(event: dict[str, Any], pawn_id: str) -> bool:
    return any(participant.get("pawn_id") == pawn_id
               for participant in event.get("participants", [])
               if isinstance(participant, dict))


def _dossier_events(db: sqlite3.Connection | None, snapshot: dict[str, Any],
                    pawn_id: str) -> list[dict[str, Any]]:
    if db is None:
        return [event for event in snapshot.get("events", [])
                if _event_participates(event, pawn_id)]
    sid = snapshot["snapshot_id"]
    return [catalog.unpack(row["payload"]) for row in db.execute(
        "SELECT e.payload FROM events e JOIN event_links l ON "
        "e.snapshot_id=l.snapshot_id AND e.event_index=l.event_index "
        "WHERE l.snapshot_id=? AND l.pawn_id=? ORDER BY e.event_index", (sid, pawn_id))]


def _timeline(db: sqlite3.Connection, snapshots: list[dict[str, Any]],
              pawn_rows: list[dict[str, Any]], pawn_id: str,
              domains: set[str] | None, detail: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    by_snapshot = {row["snapshot_id"]: row for row in snapshots}
    facts: dict[str, dict[str, Any]] = {}
    fact_index: list[str] = []
    fact_ordinals: dict[str, int] = {}
    evidence_pairs: list[list[str]] = []
    evidence_ordinals: dict[tuple[str, str], int] = {}
    memberships: list[dict[str, Any]] = []
    unknown_inventory_index: list[list[int]] = []
    refs = []
    claims_by_snapshot: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list))
    observations = []
    for row in sorted(pawn_rows, key=lambda r: (by_snapshot[r["snapshot_id"]]["tick"] is None,
                                                by_snapshot[r["snapshot_id"]]["tick"] or 0,
                                                r["snapshot_id"], r["record_id"])):
        sid, pawn = row["snapshot_id"], row["pawn"]
        snap = by_snapshot[sid]
        observations.append({"snapshot_id": sid, "tick": snap["tick"], "ticks_abs": snap["ticks_abs"],
                             "record_id": row["record_id"], "scope": pawn.get("scope"),
                             "status": pawn.get("status"), "holder_chain": pawn.get("holder_chain", []),
                             "evidence_refs": pawn.get("evidence_refs", [])})
        group = {"snapshot_id": sid, "record_id": row["record_id"],
                 "source_path": pawn.get("source_path"), "claims": []}
        group_index = len(memberships)
        memberships.append(group)
        refs.extend(pawn.get("evidence_refs", []))
        for domain, claim in _claims(pawn, domains=domains):
            semantic = _semantic(claim, domain)
            fact_id = _fact_key(semantic)
            if fact_id not in facts:
                facts[fact_id] = semantic
                fact_ordinals[fact_id] = len(fact_index)
                fact_index.append(fact_id)
            pair = (claim.get("evidence_id"), claim.get("source_ref"))
            if not all(isinstance(value, str) for value in pair):
                raise _fail("INVALID_SOURCE", "A retained claim has no evidence occurrence.")
            if pair not in evidence_ordinals:
                evidence_ordinals[pair] = len(evidence_pairs)
                evidence_pairs.append(list(pair))
            extras: dict[str, Any] = {}
            if _json(claim.get("raw_value")) != _json(semantic["value"]):
                extras["r"] = claim.get("raw_value")
            if claim.get("time_observation") is not None:
                extras["t"] = claim["time_observation"]
            extensions = {key: value for key, value in claim.items() if key not in {
                "field", "value", "raw_value", "status", "unit", "code",
                "display_label", "normalization_rule", "time_observation",
                "evidence_id", "source_ref",
            }}
            if extensions:
                extras["x"] = extensions
            encoded = [fact_ordinals[fact_id], evidence_ordinals[pair]]
            if extras:
                encoded.append(extras)
            if domain == "unknown_fields":
                unknown_inventory_index.append([group_index, len(group["claims"])])
            group["claims"].append(encoded)
            claims_by_snapshot[sid][_qualified_field(domain, claim.get("field"))].append(semantic)
            refs.append(claim)
    ordered = order_observations(observations)
    differences = []
    tick_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in ordered:
        if item["tick"] is not None:
            tick_groups[item["tick"]].append(item)
    ticks_ordered = sorted(tick_groups)
    for earlier, later in zip(ticks_ordered, ticks_ordered[1:]):
        before_group, after_group = tick_groups[earlier], tick_groups[later]
        branch_alternatives = len(before_group) > 1 or len(after_group) > 1
        for before in before_group:
            for after in after_group:
                a = claims_by_snapshot[before["snapshot_id"]]
                b = claims_by_snapshot[after["snapshot_id"]]
                for field in sorted(a.keys() & b.keys()):
                    if a[field] != b[field]:
                        differences.append({
                            "field": field,
                            "before_snapshot_id": before["snapshot_id"],
                            "after_snapshot_id": after["snapshot_id"],
                            "interval": {"after_tick": earlier, "before_tick": later},
                            "before": a[field][0] if len(a[field]) == 1 else a[field],
                            "after": b[field][0] if len(b[field]) == 1 else b[field],
                            "lineage_status": "unproven",
                            "branch_alternative": branch_alternatives,
                        })
    dated, unknown, conflicts = _event_result(db, snapshots, pawn_id)
    for event in dated + unknown:
        refs.extend(event.get("evidence_refs", []))
        for claim in event.get("claims", []):
            refs.append(claim)
    alternatives = []
    for item in ordered:
        if item["alternative_snapshot_ids"] and item["alternative_snapshot_ids"] not in alternatives:
            alternatives.append(item["alternative_snapshot_ids"])
    latest_tick = max((item["tick"] for item in ordered if item["tick"] is not None), default=None)
    latest = [row["pawn"] for row in pawn_rows if by_snapshot[row["snapshot_id"]]["tick"] == latest_tick]
    timeline = {"lineage_status": "unproven", "recorded_events": dated,
                "unknown_time_events": unknown, "state_observations": ordered,
                "observed_differences": differences, "inferences": [],
                "same_tick_alternatives": alternatives, "event_conflicts": conflicts}
    if detail == "full":
        by_field: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        expanded = expand_context_memberships({"facts": facts, "fact_index": fact_index,
                                               "evidence_pairs": evidence_pairs,
                                               "memberships": memberships})
        for member in expanded:
            field = _qualified_field(member["domain"], member["claim"]["field"])
            by_field[(member["snapshot_id"], field)].append(member["claim"])
        timeline["expanded_differences"] = [
            {**difference,
             "before_claims": by_field[(difference["before_snapshot_id"], difference["field"])],
             "after_claims": by_field[(difference["after_snapshot_id"], difference["field"])]}
            for difference in differences
        ]
    shared: dict[str, Any] = {}
    shared_memberships: dict[str, str] = {}
    for row in snapshots:
        value = row["payload"].get("context", {})
        context_id = hashlib.sha256(_json(value)).hexdigest()
        shared.setdefault(context_id, value)
        shared_memberships[row["snapshot_id"]] = context_id
    context = {"membership_encoding": "context/1", "facts": facts,
               "fact_index": fact_index, "evidence_pairs": evidence_pairs,
               "memberships": memberships,
               "unknown_inventory_index": unknown_inventory_index,
               "latest_dossiers": latest, "shared": shared,
               "shared_memberships": shared_memberships}
    if detail == "full":
        context["expanded_observations"] = expanded
    return timeline, context, refs


def _integer_claim(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _recorder_anchor(snapshot: dict[str, Any]) -> dict[str, str] | None:
    entities = snapshot["payload"].get("context", {}).get("related_entities", [])
    for entity in entities:
        if entity.get("kind") != "recorder_checkpoint":
            continue
        claims = {claim.get("field"): claim for claim in entity.get("claims", [])
                  if claim.get("status") == "observed"}
        names = ("campaign_token", "checkpoint_id", "producer_session_id")
        if all(isinstance(claims.get(name, {}).get("value"), str)
               and claims[name]["value"] for name in names):
            return {name: claims[name]["value"] for name in names}
    return None


def _journal_overlay(db: sqlite3.Connection, snapshots: list[dict[str, Any]],
                     pawn_id: str, journal_path: str | Path
                     ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Correlate verified recorder outputs without asserting snapshot ancestry."""
    from ..journal import read_journal

    try:
        journal = read_journal(journal_path, pawn_id=pawn_id)
    except InspectorError as exc:
        return ({"events": [], "conflicts": [], "sessions": []},
                {"status": "failed", "error_code": exc.code, "joins": [
                    {"snapshot_id": snap["snapshot_id"], "status": "journal_unavailable"}
                    for snap in snapshots if _recorder_anchor(snap)
                ]})
    sessions = {(row["campaign_id"], row["session_id"]): row
                for row in journal["sessions"]}
    conflict_keys = {(row["identity"]["campaign_id"],
                      row["identity"]["session_id"], row["identity"]["sequence"])
                     for row in journal["conflicts"]}
    joins = []
    for snapshot in snapshots:
        anchor = _recorder_anchor(snapshot)
        sid = snapshot["snapshot_id"]
        if anchor is None:
            continue
        campaign = anchor["campaign_token"]
        producer = anchor["producer_session_id"]
        checkpoint_id = anchor["checkpoint_id"]
        matching = [item for item in journal["events"]
                    if item["event"]["campaign_id"] == campaign
                    and item["event"]["session_id"] == producer
                    and item["event"]["event_type"] == "save_checkpoint"
                    and item["event"].get("payload", {}).get("checkpoint_id") == checkpoint_id
                    and item["event"].get("payload", {}).get("status") == "succeeded"]
        session = sessions.get((campaign, producer))
        metadata_row = db.execute("SELECT payload FROM snapshots WHERE snapshot_id=?",
                                  (sid,)).fetchone()
        clocks = (catalog.unpack(metadata_row["payload"]).get("metadata", {})
                  .get("clocks", {}) if metadata_row is not None else {})
        game_start = _integer_claim(clocks.get("gameStartAbsTick", {}).get("value"))
        starting_year = _integer_claim(clocks.get("startingYear", {}).get("value"))
        clock_matches = (session is not None and game_start is not None
                         and starting_year is not None
                         and session.get("game_start_abs_tick") == game_start
                         and session.get("starting_year") == starting_year)
        origin_sizes = set()
        for origin in db.execute("SELECT stat_json FROM origins WHERE snapshot_id=? "
                                 "AND status='parsed'", (sid,)):
            if origin["stat_json"]:
                origin_sizes.add(json.loads(origin["stat_json"]).get("size"))
        verified = []
        for item in matching:
            event = item["event"]
            payload = event["payload"]
            identity = (campaign, producer, event["sequence"])
            sha = payload.get("output_sha256")
            size = payload.get("output_bytes")
            if (identity not in conflict_keys and item.get("chronology") != "invalid"
                    and clock_matches and payload.get("output_association") == "verified"
                    and isinstance(sha, str) and sha == sid
                    and isinstance(size, int) and not isinstance(size, bool)
                    and origin_sizes == {size}):
                verified.append(item)
        status = "verified" if len(verified) == 1 and len(matching) == 1 else "unverified"
        joins.append({"snapshot_id": sid, "status": status,
                      "campaign_token": campaign, "producer_session_id": producer,
                      "checkpoint_id": checkpoint_id,
                      "matching_outcome_count": len(matching),
                      "journal_event_id": verified[0]["event"]["event_id"] if status == "verified" else None,
                      "source_sha256": sid, "time_basis_matches": bool(clock_matches)})
    return ({"events": journal["events"], "conflicts": journal["conflicts"],
             "sessions": journal["sessions"]},
            {"status": journal["coverage"]["status"],
             "error_code": None, "sources": journal["sources"],
             "coverage": journal["coverage"], "joins": joins})


def get_events(catalog_path: str | Path, *, anchor_snapshot_id: str | None = None,
               generation_id: str | None = None, snapshot_id: str | None = None,
               saved_event_id: str | None = None, cursor: str | None = None,
               limit: int = 50, freshness: str = "metadata",
               response_limit: int | None = None) -> dict[str, Any]:
    """Page every retained event occurrence, including records without participants.

    Pagination uses source identity and record index, never a guessed event clock.
    A cursor pins its immutable generation; changing selectors rejects that cursor.
    Exact saved-ID filters scan one event payload at a time, without loading pawns
    or raw evidence. Separate occurrences and conflicting payloads stay separate.
    """
    if type(limit) is not int or not 1 <= limit <= 500:
        raise _fail("INVALID_ARGUMENT", "Event page limit must be an integer from 1 to 500.")
    if response_limit is not None and (type(response_limit) is not int or
                                       not 0 < response_limit <= ABSOLUTE_LIMIT):
        raise _fail("INVALID_ARGUMENT", "The response limit is outside the supported range.")
    for name, value in (("snapshot_id", snapshot_id), ("saved_event_id", saved_event_id),
                        ("anchor_snapshot_id", anchor_snapshot_id)):
        if value is not None and (not isinstance(value, str) or not value):
            raise _fail("INVALID_ARGUMENT", f"{name} must be a nonempty string.")
    position = _untoken(cursor, 5) if cursor is not None else None
    if position is not None:
        if generation_id is not None and generation_id != position[1]:
            raise _fail("INVALID_CURSOR", "The event cursor belongs to another generation.")
        generation_id = position[1]
    report, namespace, selected_generation_id, db = _prepare(
        Path(catalog_path), freshness, generation_id)
    try:
        exact = (db.execute("SELECT campaign_id FROM snapshots WHERE snapshot_id=?",
                            (snapshot_id,)).fetchone() if snapshot_id is not None else None)
        if snapshot_id is not None and exact is None:
            raise _fail("SNAPSHOT_NOT_FOUND", "The selected snapshot is not in this generation.")
        if exact is not None and exact[0] is None:
            if anchor_snapshot_id is not None and anchor_snapshot_id != snapshot_id:
                raise _fail("INCOMPATIBLE_SNAPSHOT", "Unvalidated snapshots cannot establish a campaign join.")
            campaign_id, anchor = None, snapshot_id
        else:
            campaign_id, anchor = _campaign(db, anchor_snapshot_id if anchor_snapshot_id is not None
                                            else snapshot_id)
        selector = {"campaign_id": campaign_id, "anchor_snapshot_id": anchor,
                    "identity_basis": "validated_campaign" if campaign_id is not None else "snapshot_only",
                    "snapshot_id": snapshot_id, "saved_event_id": saved_event_id}
        selector_hash = hashlib.sha256(_json(selector)).hexdigest()
        last_key = None
        if position is not None:
            if position[:3] != [namespace, selected_generation_id, selector_hash]:
                raise _fail("INVALID_CURSOR", "The event cursor belongs to another selection.")
            try:
                event_index = int(position[4])
            except ValueError as exc:
                raise _fail("INVALID_CURSOR", "The event cursor index is invalid.") from exc
            if event_index < 0 or str(event_index) != position[4]:
                raise _fail("INVALID_CURSOR", "The event cursor index is invalid.")
            last_key = (position[3], event_index)
        groups = {row["campaign_id"]: row["n"] for row in db.execute(
            "SELECT campaign_id,count(*) AS n FROM snapshots GROUP BY campaign_id")}
        selected_count = groups.get(campaign_id, 0)
        where, params = "s.campaign_id IS ?", [campaign_id]
        if snapshot_id is not None:
            row = db.execute("SELECT campaign_id FROM snapshots WHERE snapshot_id=?",
                             (snapshot_id,)).fetchone()
            if row is None:
                raise _fail("SNAPSHOT_NOT_FOUND", "The selected snapshot is not in this generation.")
            if row[0] != campaign_id:
                raise _fail("INCOMPATIBLE_SNAPSHOT", "The snapshot belongs to another campaign.")
            where += " AND e.snapshot_id=?"
            params.append(snapshot_id)
            selected_count = 1
        rows = db.execute(
            "SELECT e.snapshot_id,e.event_index,e.payload,s.game_tick,s.ticks_abs "
            "FROM events e JOIN snapshots s ON s.snapshot_id=e.snapshot_id WHERE " +
            where + " ORDER BY e.snapshot_id,e.event_index", params)
        entries, matching, remaining, position_found = [], 0, 0, last_key is None
        for row in rows:
            event = catalog.unpack(row["payload"])
            if not isinstance(event, dict):
                raise _fail("INVALID_CATALOG", "A retained event payload is malformed.")
            if saved_event_id is not None and event.get("saved_event_id") != saved_event_id:
                continue
            matching += 1
            key = (row["snapshot_id"], row["event_index"])
            if key == last_key:
                position_found = True
            if last_key is not None and key <= last_key:
                continue
            remaining += 1
            if len(entries) < limit:
                entries.append({"snapshot_id": row["snapshot_id"],
                                "event_index": row["event_index"],
                                "game_tick": row["game_tick"], "ticks_abs": row["ticks_abs"],
                                "event": event,
                                "evidence": _evidence_tokens(namespace, selected_generation_id,
                                                             event.get("evidence_refs", []))})
        if not position_found:
            raise _fail("INVALID_CURSOR", "The event cursor record is not in this selection.")
        next_cursor = None
        if remaining > len(entries):
            end = entries[-1]
            next_cursor = _token([namespace, selected_generation_id, selector_hash,
                                  end["snapshot_id"], str(end["event_index"])])
        result = {
            "history_schema_version": HISTORY_VERSION,
            "catalog": {**report["catalog"], "campaign_id": campaign_id,
                        "anchor_snapshot_id": anchor},
            "selection": selector, "events": entries,
            "page": {"limit": limit, "returned_occurrences": len(entries),
                     "matching_occurrences": matching, "remaining_occurrences": remaining-len(entries),
                     "next_cursor": next_cursor,
                     "ordering": "snapshot_id,event_index; not chronology",
                     "complete_selection": cursor is None and next_cursor is None},
            "coverage": {**report["coverage"], "selected_snapshot_count": selected_count,
                         "compatible_snapshot_count": groups.get(campaign_id, 0) if campaign_id is not None else 0,
                         "incompatible_snapshot_count": sum(n for g, n in groups.items()
                                                            if g is not None and g != campaign_id),
                         "review_needed_snapshot_count": groups.get(None, 0)},
        }
        try:
            return _size_result(result, response_limit if response_limit is not None else CONTEXT_LIMIT,
                                semantic_complete=result["page"]["complete_selection"])
        except InspectorError as exc:
            if exc.code == "RESULT_TOO_LARGE":
                exc.details["selectors"] = {"limit": limit, "snapshot_id": snapshot_id,
                                            "saved_event_id": saved_event_id}
            raise
    finally:
        db.close()


def get_timeline(catalog_path: str | Path, *, pawn_id: str | None = None,
                 name: str | None = None, anchor_snapshot_id: str | None = None,
                 detail: str = "context", domains: list[str] | None = None,
                 freshness: str = "metadata", response_limit: int | None = None,
                 journal_path: str | Path | None = None,
                 generation_id: str | None = None) -> dict[str, Any]:
    """One call refreshes, selects, and returns all supported facts for one pawn."""
    _selector(pawn_id, name)
    if detail not in {"context", "full"}:
        raise _fail("INVALID_ARGUMENT", "Detail must be context or full.")
    if domains is not None and (not isinstance(domains, list) or
                                any(not isinstance(x, str) or not x for x in domains)):
        raise _fail("INVALID_ARGUMENT", "Domains must be a list of domain names.")
    path = Path(catalog_path)
    report, namespace, selected_generation_id, db = _prepare(path, freshness,
                                                             generation_id)
    try:
        campaign_id, anchor = _campaign(db, anchor_snapshot_id)
        snapshots = _snapshots(db, campaign_id)
        rows = _pawn_rows(db, [r["snapshot_id"] for r in snapshots],
                          normalize_pawn_id(pawn_id) if pawn_id is not None else None)
        selected, matches = _select_pawn(rows, pawn_id, name)
        timeline, context, refs = _timeline(db, snapshots, matches, selected,
                                           set(domains) if domains is not None else None, detail)
        journal_coverage = None
        if journal_path is not None:
            journal_data, journal_coverage = _journal_overlay(db, snapshots, selected,
                                                               journal_path)
            timeline["journal_events"] = journal_data["events"]
            timeline["journal_conflicts"] = journal_data["conflicts"]
            timeline["journal_sessions"] = journal_data["sessions"]
            context["recorder_joins"] = journal_coverage["joins"]
        aliases = []
        for row in matches:
            for alias in row["pawn"].get("aliases", []):
                if isinstance(alias, dict):
                    aliases.append({"value": alias.get("value") or alias.get("name"),
                                    "snapshot_id": row["snapshot_id"],
                                    "evidence_id": alias.get("evidence_id"),
                                    "source_ref": alias.get("source_ref")})
                else:
                    aliases.append({"value": alias, "snapshot_id": row["snapshot_id"]})
        ticks = [row["tick"] for row in snapshots if row["snapshot_id"] in
                 {match["snapshot_id"] for match in matches} and row["tick"] is not None]
        group_counts = {row["campaign_id"]: row["n"] for row in db.execute(
            "SELECT campaign_id,count(*) AS n FROM snapshots GROUP BY campaign_id")}
        domain_status_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for match in matches:
            for domain, body in match["pawn"].get("domains", {}).items():
                if isinstance(body, dict):
                    status = body.get("status")
                    if isinstance(status, str):
                        domain_status_counts[domain][status] += 1
        phases = []
        for observation in timeline["state_observations"]:
            phases.append({"kind": "state_observation", "snapshot_id": observation["snapshot_id"],
                           "tick": observation["tick"], "scope": observation["scope"],
                           "status": observation["status"],
                           "evidence_refs": observation["evidence_refs"]})
        for difference in timeline["observed_differences"]:
            phases.append({"kind": "observed_difference", **difference})
        result = {
            "history_schema_version": HISTORY_VERSION, "detail": detail,
            "catalog": {**report["catalog"], "campaign_id": campaign_id,
                        "anchor_snapshot_id": anchor},
            "subject": {"pawn_id": selected, "aliases": aliases,
                        "first_observed_tick": min(ticks) if ticks else None,
                        "last_observed_tick": max(ticks) if ticks else None,
                        "ambiguity": any(sum(m["snapshot_id"] == r["snapshot_id"] for m in matches) > 1
                                         for r in matches)},
            "overview": {"phases": phases, "basis": "observed facts and differences"},
            "timeline": timeline, "context": context,
            "coverage": {**report["coverage"], "selected_snapshot_count": len(snapshots),
                         "compatible_snapshot_count": group_counts.get(campaign_id, 0),
                         "incompatible_snapshot_count": sum(
                             count for group, count in group_counts.items()
                             if group is not None and group != campaign_id),
                         "review_needed_snapshot_count": group_counts.get(None, 0),
                         "materialized_observation_count": len(matches),
                         "domain_filter": domains,
                         "unknown_clock_count": sum(row["tick"] is None for row in snapshots),
                         "domain_status_counts": {domain: dict(counts)
                                                  for domain, counts in domain_status_counts.items()},
                         "earliest_observed_tick": min(ticks) if ticks else None,
                         "latest_observed_tick": max(ticks) if ticks else None,
                         "omitted_detail_count": 0,
                         **({"journal": {key: value for key, value in journal_coverage.items()
                                         if key != "joins"}} if journal_coverage else {})},
            "evidence": _evidence_tokens(namespace, selected_generation_id, refs),
        }
        return _size_result(result, response_limit or
                            (CONTEXT_LIMIT if detail == "context" else FULL_LIMIT))
    finally:
        db.close()


def get_dossier(source_or_catalog: str | Path, *, pawn_id: str | None = None,
                name: str | None = None, snapshot_id: str | None = None,
                detail: str = "context", response_limit: int | None = None,
                generation_id: str | None = None) -> dict[str, Any]:
    """Return a complete selected snapshot dossier with its direct context."""
    _selector(pawn_id, name)
    path = Path(source_or_catalog)
    if path.suffix.lower() == ".rws":
        if generation_id is not None:
            raise _fail("INVALID_ARGUMENT", "A generation ID requires a catalog source.")
        snapshot = catalog._extract_snapshot(path)
        metadata = snapshot["metadata"]
        available = [{"snapshot_id": snapshot["snapshot_id"], "campaign_id": None,
                      "tick": _metadata_clocks(snapshot)[0],
                      "ticks_abs": _metadata_clocks(snapshot)[1], "payload": snapshot}]
        rows = [{"snapshot_id": snapshot["snapshot_id"], "pawn_id": pawn.get("pawn_id"),
                 "record_id": pawn["record_id"], "pawn": pawn} for pawn in snapshot["pawns"]]
        namespace = "single-source"
        generation_id = snapshot["snapshot_id"]
        report = {"catalog": {"catalog_id": generation_id, "generation_id": generation_id,
                              "namespace_id": namespace, "freshness": "source"},
                  "coverage": {"candidate_count": 1, "parsed_count": 1, "failed_count": 0}}
        db = None
    else:
        report, namespace, selected_generation_id, db = _prepare(path, "metadata",
                                                                 generation_id)
        try:
            target = snapshot_id
            if target is None:
                count = db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
                if count != 1:
                    raise _fail("AMBIGUOUS_SNAPSHOT", "Select a snapshot_id for this catalog.")
                target = db.execute("SELECT snapshot_id FROM snapshots").fetchone()[0]
            row = db.execute("SELECT snapshot_id,campaign_id,game_tick,ticks_abs,payload "
                             "FROM snapshots WHERE snapshot_id=?", (target,)).fetchone()
            if row is None:
                raise _fail("SNAPSHOT_NOT_FOUND", "The requested snapshot is not in this generation.")
            available = [{"snapshot_id": row["snapshot_id"], "campaign_id": row["campaign_id"],
                          "tick": row["game_tick"], "ticks_abs": row["ticks_abs"],
                          "payload": catalog.unpack(row["payload"])}]
            rows = _pawn_rows(db, [target],
                              normalize_pawn_id(pawn_id) if pawn_id is not None else None)
        except Exception:
            db.close()
            raise
    try:
        selected, matches = _select_pawn(rows, pawn_id, name)
        if snapshot_id is not None and snapshot_id != available[0]["snapshot_id"]:
            raise _fail("SNAPSHOT_MISMATCH", "The requested snapshot does not match the source.")
        pawn_data = [row["pawn"] for row in matches]
        events = _dossier_events(db, available[0]["payload"], selected)
        refs = [ref for pawn in pawn_data for ref in pawn.get("evidence_refs", [])]
        refs.extend(claim for pawn in pawn_data for _, claim in _claims(pawn))
        refs.extend(ref for event in events for ref in event.get("evidence_refs", []))
        refs.extend(claim for event in events for claim in event.get("claims", []))
        result = {
            "history_schema_version": HISTORY_VERSION, "detail": detail,
            "catalog": report["catalog"],
            "subject": {"pawn_id": selected, "snapshot_id": available[0]["snapshot_id"],
                        "ambiguity": len(matches) > 1},
            "overview": {"materialized_records": len(pawn_data)},
            "dossier": {"pawns": pawn_data, "context": available[0]["payload"].get("context", {}),
                        "events": events},
            "coverage": report["coverage"],
            "evidence": _evidence_tokens(namespace,
                                         snapshot["snapshot_id"] if db is None else selected_generation_id,
                                         refs),
        }
        return _size_result(result, response_limit or
                            (CONTEXT_LIMIT if detail == "context" else FULL_LIMIT))
    finally:
        if db is not None:
            db.close()


def get_evidence(catalog_path: str | Path, evidence_id: str, *, cursor: str | None = None,
                 limit: int | None = None) -> dict[str, Any]:
    """Retrieve a lossless raw node, using generation-bound byte pages if needed."""
    namespace, generation_id, content_id, source_ref = _untoken(evidence_id, 4)
    source = Path(catalog_path)
    db = None
    if source.suffix.lower() == ".rws":
        if namespace != "single-source":
            raise _fail("INVALID_CURSOR", "The evidence belongs to another source.")
        snapshot = catalog._extract_snapshot(source)
        if snapshot["snapshot_id"] != generation_id:
            raise _fail("SOURCE_CHANGED", "The source changed since the evidence token was issued.")
        matching = [item for item in snapshot["evidence"]
                    if item["evidence_id"] == content_id and item["source_ref"] == source_ref]
        if len(matching) != 1:
            raise _fail("EVIDENCE_NOT_FOUND", "The evidence occurrence is unavailable.")
        row = matching[0]
        node = row["node"]
        current_namespace = namespace
    else:
        current_namespace, _, db = catalog.open_generation(source, generation_id)
    try:
        if namespace != current_namespace:
            raise _fail("INVALID_CURSOR", "The evidence belongs to another catalog namespace.")
        if db is not None:
            row = db.execute("SELECT o.path,o.snapshot_id,o.interpretation_id,n.node "
                             "FROM evidence_occurrences o JOIN evidence_nodes n "
                             "ON o.evidence_id=n.evidence_id WHERE o.source_ref=? AND o.evidence_id=?",
                             (source_ref, content_id)).fetchone()
            if row is None:
                raise _fail("EVIDENCE_NOT_FOUND", "The evidence occurrence is unavailable.")
            node = catalog.unpack(row["node"], limit=16 * 1024 * 1024)
        encoded = _json(node)
        page_limit = limit or EVIDENCE_PAGE_LIMIT
        if page_limit < 1 or page_limit > EVIDENCE_PAGE_LIMIT:
            raise _fail("INVALID_ARGUMENT", "The evidence page limit is invalid.")
        offset = 0
        if cursor is not None:
            binding = _untoken(cursor, 5)
            if binding[:4] != [namespace, generation_id, content_id, source_ref]:
                raise _fail("INVALID_CURSOR", "The evidence cursor does not match this occurrence.")
            try:
                offset = int(binding[4])
            except ValueError as exc:
                raise _fail("INVALID_CURSOR", "The evidence cursor offset is invalid.") from exc
            if offset < 0 or offset >= len(encoded):
                raise _fail("INVALID_CURSOR", "The evidence cursor offset is invalid.")
        end = min(offset + page_limit, len(encoded))
        result = {
            "history_schema_version": HISTORY_VERSION,
            "catalog": {"namespace_id": namespace, "generation_id": generation_id},
            "evidence_id": evidence_id, "content_id": content_id, "source_ref": source_ref,
            "snapshot_id": row["snapshot_id"], "interpretation_id": row["interpretation_id"],
            "path": row["path"], "total_bytes": len(encoded), "offset": offset,
            "next_cursor": (_token([namespace, generation_id, content_id, source_ref, str(end)])
                            if end < len(encoded) else None),
            "node": node if offset == 0 and end == len(encoded) else None,
            "fragment_base64": (None if offset == 0 and end == len(encoded) else
                                base64.b64encode(encoded[offset:end]).decode("ascii")),
            "encoding": "utf-8-json",
        }
        return _size_result(result, ABSOLUTE_LIMIT)
    finally:
        if db is not None:
            db.close()
