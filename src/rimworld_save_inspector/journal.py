"""Strict, bounded, read-only ingestion for Life Recorder JSONL journals."""
from __future__ import annotations

from bisect import bisect_left, bisect_right

import hashlib
import json
import os
import stat
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import InspectorError

JOURNAL_SCHEMA_VERSION = "1.0"
JOURNAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://rimworld-save-inspector.local/schemas/journal-1.0.json",
    "title": "Life Recorder journal read result 1.0",
    "type": "object", "additionalProperties": False,
    "required": ["journal_schema_version", "sources", "subject", "sessions", "events", "conflicts", "coverage", "provenance"],
    "properties": {
        "journal_schema_version": {"const": "1.0"},
        "sources": {"type": "array", "items": {"type": "object"}},
        "subject": {"type": "object"}, "sessions": {"type": "array", "items": {"type": "object"}},
        "events": {"type": "array", "items": {"type": "object"}},
        "conflicts": {"type": "array", "items": {"type": "object"}},
        "coverage": {"type": "object"}, "provenance": {"type": "object"},
    },
}
_MAXIMA = {
    "segments": 10_000, "segment_bytes": 67_108_864, "total_bytes": 1_073_741_824,
    "line_bytes": 65_536, "depth": 32, "records": 1_000_000,
    "retained_bytes": 67_108_864, "response_bytes": 2_097_152,
}
_REQUIRED = {
    "schema_version", "recorder_version", "game_version", "campaign_id", "session_id", "sequence",
    "event_id", "event_type", "ticks_game", "ticks_abs", "game_start_abs_tick", "subject_id",
    "related_ids", "map_id", "checkpoint_id", "source", "payload",
}
_SOURCE_SEMANTICS = {"recorded_event", "state_observation", "observed_difference", "coverage"}
_GAP_CAUSES = {"queue_full", "serialization_error", "io_error", "storage_limit", "shutdown"}


@dataclass(frozen=True, slots=True)
class JournalLimits:
    segments: int = _MAXIMA["segments"]
    segment_bytes: int = _MAXIMA["segment_bytes"]
    total_bytes: int = _MAXIMA["total_bytes"]
    line_bytes: int = _MAXIMA["line_bytes"]
    depth: int = _MAXIMA["depth"]
    records: int = _MAXIMA["records"]
    retained_bytes: int = _MAXIMA["retained_bytes"]
    response_bytes: int = _MAXIMA["response_bytes"]

    def __post_init__(self) -> None:
        for name, maximum in _MAXIMA.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
                raise ValueError(f"{name} must be an integer from 1 through {maximum}")


def _fail(code: str, message: str, **details: Any) -> InspectorError:
    return InspectorError(code, message, details)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate object key: {key}")
        value[key] = item
    return value


def _bad_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _depth(value: Any, limit: int, current: int = 0) -> None:
    if current > limit:
        raise ValueError("JSON nesting depth exceeded")
    if isinstance(value, dict):
        for key, item in value.items():
            _depth(key, limit, current + 1)
            _depth(item, limit, current + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, limit, current + 1)


def _check_unicode(value: Any) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("JSON strings must not contain unpaired surrogate code points")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_unicode(key)
            _check_unicode(item)
    elif isinstance(value, list):
        for item in value:
            _check_unicode(item)


def _overlapping_observed_sequences(
    observed_sequences: list[int], missing_ranges: list[Mapping[str, Any]],
) -> list[int]:
    """Return observed IDs covered by missing ranges using indexed interval lookups."""
    overlapping: list[int] = []
    for missing in missing_ranges:
        first = bisect_left(observed_sequences, missing["first_sequence"])
        last = bisect_right(observed_sequences, missing["last_sequence"], lo=first)
        overlapping.extend(observed_sequences[first:last])
    return overlapping


def _validate_event(event: Any) -> None:
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    unknown = set(event) - _REQUIRED - {"extensions"}
    missing = _REQUIRED - set(event)
    if unknown or missing:
        raise ValueError(f"event keys invalid (missing={sorted(missing)}, unknown={sorted(unknown)})")
    if event["schema_version"] != "1.0":
        raise ValueError("unsupported schema_version")
    for key in ("recorder_version", "game_version", "campaign_id", "session_id", "event_type"):
        if not isinstance(event[key], str) or not event[key]:
            raise ValueError(f"{key} must be a nonempty string")
    sequence = event["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("sequence must be a positive integer")
    if event["event_id"] != f'{event["session_id"]}:{sequence}':
        raise ValueError("event_id does not match session_id and sequence")
    for key in ("ticks_game", "ticks_abs", "game_start_abs_tick"):
        if event[key] is not None and (isinstance(event[key], bool) or not isinstance(event[key], int)):
            raise ValueError(f"{key} must be an integer or null")
    for key in ("subject_id", "map_id", "checkpoint_id"):
        if event[key] is not None and (not isinstance(event[key], str) or not event[key]):
            raise ValueError(f"{key} must be a nonempty string or null")
    if not isinstance(event["related_ids"], list) or any(not isinstance(x, str) or not x for x in event["related_ids"]):
        raise ValueError("related_ids must contain nonempty strings")
    if len(set(event["related_ids"])) != len(event["related_ids"]):
        raise ValueError("related_ids must be distinct")
    source = event["source"]
    if (not isinstance(source, dict) or set(source) != {"collector", "semantics"}
            or not isinstance(source["collector"], str) or not source["collector"]
            or source["semantics"] not in _SOURCE_SEMANTICS):
        raise ValueError("source object is invalid")
    if not isinstance(event["payload"], dict):
        raise ValueError("payload must be an object")
    if "extensions" in event and not isinstance(event["extensions"], dict):
        raise ValueError("extensions must be an object")
    if event["event_type"] == "gap":
        payload = event["payload"]
        try:
            ranges = payload["missing_ranges"]
            dropped = payload["dropped_count"]
            exact = payload["ranges_exact"]
            if isinstance(dropped, bool) or not isinstance(dropped, int) or dropped <= 0 or not isinstance(exact, bool):
                raise ValueError
            prev = 0
            total = 0
            for item in ranges:
                first, last, cause = item["first_sequence"], item["last_sequence"], item["cause"]
                if (isinstance(first, bool) or not isinstance(first, int) or isinstance(last, bool)
                        or not isinstance(last, int) or first < 1 or last < first or last >= sequence
                        or first <= prev or cause not in _GAP_CAUSES):
                    raise ValueError
                prev = last
                total += last - first + 1
            if exact and total != dropped:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("gap payload is invalid") from None
    if event["event_type"] == "session_started":
        payload = event["payload"]
        if (payload.get("parent_association") not in {"verified", "unproven", "absent"}
                or not isinstance(payload.get("time_basis"), dict)
                or "starting_year" not in payload.get("time_basis", {})
                or (payload.get("time_basis", {}).get("starting_year") is not None
                    and (isinstance(payload["time_basis"]["starting_year"], bool)
                         or not isinstance(payload["time_basis"]["starting_year"], int)))
                or not isinstance(payload.get("collectors"), list)):
            raise ValueError("session_started payload is invalid")
    if event["event_type"] == "save_checkpoint":
        payload = event["payload"]
        try:
            if payload["status"] not in {"attempted", "succeeded", "failed"}:
                raise ValueError
            if any(not isinstance(payload[key], str) or not payload[key] for key in ("checkpoint_id", "save_attempt_id")):
                raise ValueError
            watermarks = [payload[key] for key in ("observed_sequence", "written_sequence", "durable_sequence")]
            if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in watermarks):
                raise ValueError
            if not watermarks[2] <= watermarks[1] <= watermarks[0] < sequence:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("save_checkpoint payload is invalid") from None


def _source_files(path: Path, limits: JournalLimits) -> list[Path]:
    if path.is_symlink():
        raise _fail("UNSAFE_SOURCE", "Journal source may not be a symlink.")
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise _fail("SOURCE_UNAVAILABLE", "Journal source could not be accessed.", reason=type(exc).__name__) from None
    if stat.S_ISREG(mode):
        return [path]
    if not stat.S_ISDIR(mode):
        raise _fail("UNSAFE_SOURCE", "Journal source must be a regular file or directory.")
    found: list[Path] = []
    stack = [path]
    while stack:
        folder = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError as exc:
            raise _fail("SOURCE_UNAVAILABLE", "A journal directory could not be read.", reason=type(exc).__name__) from None
        for entry in entries:
            p = Path(entry.path)
            if entry.is_symlink():
                # Symlinks are rejected rather than followed, including irrelevant ones.
                raise _fail("UNSAFE_SOURCE", "Journal directories may not contain symlinks.")
            if entry.is_dir(follow_symlinks=False):
                stack.append(p)
            elif entry.is_file(follow_symlinks=False) and p.suffix.lower() in {".jsonl", ".ndjson"}:
                found.append(p)
                if len(found) > limits.segments:
                    raise _fail("JOURNAL_LIMIT_EXCEEDED", "Journal contains more segments than the configured limit.")
    return sorted(found, key=lambda x: str(x))


def _read_one(path: Path, *, limits: JournalLimits, include_source_path: bool,
              retained_remaining: int) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]], int, int]:
    try:
        before_path = path.lstat()
        if not stat.S_ISREG(before_path.st_mode):
            raise _fail("UNSAFE_SOURCE", "Journal segment is not a regular file.")
        with path.open("rb") as stream:
            before_fd = os.fstat(stream.fileno())
            if not stat.S_ISREG(before_fd.st_mode):
                raise _fail("UNSAFE_SOURCE", "Journal segment is not a regular file.")
            if before_fd.st_size > limits.segment_bytes:
                raise _fail("JOURNAL_LIMIT_EXCEEDED", "Journal segment exceeds the configured byte limit.")
            raw = stream.read(limits.segment_bytes + 1)
            after_fd = os.fstat(stream.fileno())
        after_path = path.lstat()
    except InspectorError:
        raise
    except OSError as exc:
        raise _fail("SOURCE_UNAVAILABLE", "Journal segment could not be read.", reason=type(exc).__name__) from None
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if (identity(before_fd) != identity(after_fd) or identity(before_path) != identity(after_path)
            or before_fd.st_size != len(raw) or len(raw) > limits.segment_bytes):
        raise _fail("SOURCE_CHANGED", "Journal segment changed while it was being read.")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _fail("MALFORMED_SOURCE", "Journal segments must not contain a UTF-8 BOM.")
    segment_hash = hashlib.sha256(raw).hexdigest()
    source_id = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]
    source: dict[str, Any] = {"source_id": source_id, "segment_sha256": segment_hash, "bytes": len(raw),
                              "outcome": "complete", "complete_records": 0, "incomplete_tail": False,
                              "diagnostics": []}
    if include_source_path:
        source["path"] = str(path.resolve())
    if not raw:
        source["outcome"] = "empty"
        source["diagnostics"].append({"code": "EMPTY_SEGMENT", "message": "Journal segment contains no records."})
        return source, [], 0, 0
    complete = raw.endswith(b"\n")
    lines = raw.split(b"\n")
    fragment = b"" if complete else lines.pop()
    if complete:
        lines.pop()  # final empty split element
    events: list[tuple[dict[str, Any], dict[str, Any]]] = []
    retained = 0
    for line_no, line in enumerate(lines, 1):
        if not line:
            raise _fail("MALFORMED_SOURCE", "Journal contains an empty line.", line=line_no)
        if len(line) > limits.line_bytes:
            raise _fail("JOURNAL_LIMIT_EXCEEDED", "Journal line exceeds the configured byte limit.", line=line_no)
        try:
            decoded = line.decode("utf-8", errors="strict")
            event = json.loads(decoded, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
            _depth(event, limits.depth)
            _check_unicode(event)
            _validate_event(event)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError) as exc:
            raise _fail("MALFORMED_SOURCE", "Journal contains a malformed complete record.", line=line_no,
                        reason=str(exc)[:200]) from None
        origin = {"source_id": source_id, "line": line_no, "raw_line_sha256": hashlib.sha256(line).hexdigest()}
        retained += len(json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if retained > retained_remaining:
            raise _fail("JOURNAL_LIMIT_EXCEEDED", "Journal retained event data exceeds the configured limit.")
        events.append((event, origin))
    if fragment:
        if len(fragment) > limits.line_bytes:
            raise _fail("JOURNAL_LIMIT_EXCEEDED", "Journal final fragment exceeds the configured line byte limit.")
        source["outcome"] = "incomplete_tail"
        source["incomplete_tail"] = True
        source["diagnostics"].append({"code": "INCOMPLETE_TAIL", "line": len(lines) + 1,
                                      "message": "Non-newline-terminated final fragment was excluded."})
    source["complete_records"] = len(events)
    return source, events, len(raw), retained


def read_journal(path: str | os.PathLike[str], *, pawn_id: str | None = None,
                 session_id: str | None = None, limits: JournalLimits | Mapping[str, int] | None = None,
                 include_source_path: bool = False) -> dict[str, Any]:
    """Read a configured local JSONL source. No source content is ever modified."""
    try:
        bounded = limits if isinstance(limits, JournalLimits) else JournalLimits(**dict(limits or {}))
    except (TypeError, ValueError) as exc:
        raise _fail("INVALID_ARGUMENT", "Journal limits are invalid.", reason=str(exc)) from None
    for label, value in (("pawn_id", pawn_id), ("session_id", session_id)):
        if value is not None and (not isinstance(value, str) or not value):
            raise _fail("INVALID_ARGUMENT", f"{label} must be a nonempty string when supplied.")
    source_path = Path(path).expanduser()
    absolute = source_path.absolute()
    if any(component.is_symlink() for component in (absolute, *absolute.parents)):
        raise _fail("UNSAFE_SOURCE", "Journal source paths may not traverse symlinks.")
    files = _source_files(source_path, bounded)
    sources: list[dict[str, Any]] = []
    raw_events: list[tuple[dict[str, Any], dict[str, Any]]] = []
    total_bytes = 0
    retained_bytes = 0
    for file in files:
        try:
            total_bytes += file.lstat().st_size
        except OSError as exc:
            raise _fail("SOURCE_UNAVAILABLE", "Journal segment could not be inspected.", reason=type(exc).__name__) from None
        if total_bytes > bounded.total_bytes:
            raise _fail("JOURNAL_LIMIT_EXCEEDED", "Journal source set exceeds the configured total byte limit.")
        try:
            source, events, size, retained_size = _read_one(file, limits=bounded, include_source_path=include_source_path,
                                                             retained_remaining=bounded.retained_bytes - retained_bytes)
        except InspectorError as exc:
            source = {"source_id": hashlib.sha256(str(file.resolve()).encode()).hexdigest()[:24], "outcome": "failed",
                      "complete_records": 0, "incomplete_tail": False,
                      "diagnostics": [{"code": exc.code, "message": exc.message, "details": exc.details}]}
            if include_source_path:
                source["path"] = str(file.resolve())
            sources.append(source)
            if exc.code in {"JOURNAL_LIMIT_EXCEEDED", "SOURCE_CHANGED"}:
                raise
            continue
        sources.append(source)
        retained_bytes += retained_size
        raw_events.extend(events)
        if len(raw_events) > bounded.records:
            raise _fail("JOURNAL_LIMIT_EXCEEDED", "Journal source set exceeds the configured record limit.")
    if not files:
        sources.append({"source_id": hashlib.sha256(str(source_path).encode()).hexdigest(), "outcome": "empty",
                        "complete_records": 0, "incomplete_tail": False,
                        "diagnostics": [{"code": "NO_SEGMENTS", "message": "No JSONL journal segments were found."}]})

    # Detect inconsistent source/session clocks before grouping.
    session_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event, _origin in raw_events:
        session_rows[(event["campaign_id"], event["session_id"])].append(event)
    sessions: list[dict[str, Any]] = []
    session_basis: dict[tuple[str, str], str] = {}
    session_basis_parts: dict[tuple[str, str], tuple[str, int | None, int | None]] = {}
    event_diagnostics: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    diagnostics: list[dict[str, Any]] = []
    for key, rows in session_rows.items():
        starts = [e for e in rows if e["event_type"] == "session_started"]
        time_years = []
        for start in starts:
            value = start.get("payload", {}).get("time_basis", {}).get("starting_year")
            time_years.append(value if isinstance(value, int) and not isinstance(value, bool) else None)
        years = sorted(set(time_years), key=lambda x: (-1 if x is None else x))
        abs_values = sorted({e["game_start_abs_tick"] for e in rows if e["game_start_abs_tick"] is not None})
        session_start = starts[0] if starts else None
        if len(years) != 1 or len(abs_values) > 1:
            diagnostics.append({"code": "TIME_BASIS_MISMATCH", "severity": "warning",
                                "campaign_id": key[0], "session_id": key[1],
                                "message": "Session records disagree about their time basis."})
        year = years[0] if len(years) == 1 else None
        abs_tick = abs_values[0] if len(abs_values) == 1 else None
        if year is None or abs_tick is None or len(years) != 1 or len(abs_values) != 1:
            basis = "unknown:" + hashlib.sha256((key[0] + "\0" + key[1]).encode()).hexdigest()[:16]
            diagnostics.append({"code": "TIME_BASIS_INCOMPLETE", "severity": "warning",
                                "campaign_id": key[0], "session_id": key[1],
                                "message": "Missing or inconsistent game start tick or starting year creates a separate time-basis group."})
        else:
            basis = hashlib.sha256(json.dumps([key[0], abs_tick, year], separators=(",", ":")).encode()).hexdigest()[:16]
        session_basis[key] = basis
        session_basis_parts[key] = (key[0], abs_tick if len(abs_values) == 1 else None,
                                    year if len(years) == 1 else None)
        for e in rows:
            if (e["ticks_game"] is not None and e["ticks_game"] < 0) or (e["ticks_abs"] is not None and e["ticks_abs"] < 0):
                event_diagnostics[(key[0], key[1], e["sequence"])].append({"code": "CLOCK_NEGATIVE", "severity": "warning",
                    "message": "Negative game tick is retained with invalid chronology."})
            if all(e[k] is not None for k in ("ticks_game", "ticks_abs", "game_start_abs_tick")) and e["ticks_abs"] != e["ticks_game"] + e["game_start_abs_tick"]:
                event_diagnostics[(key[0], key[1], e["sequence"])].append({"code": "CLOCK_MISMATCH", "severity": "warning",
                    "message": "ticks_abs does not equal ticks_game plus game_start_abs_tick; event retained with invalid chronology."})
        ordered_ticks = [e["ticks_game"] for e in sorted(rows, key=lambda e: e["sequence"]) if e["ticks_game"] is not None]
        if any(later < earlier for earlier, later in zip(ordered_ticks, ordered_ticks[1:])):
            diagnostics.append({"code": "SESSION_CHRONOLOGY_MISMATCH", "severity": "warning",
                                "campaign_id": key[0], "session_id": key[1],
                                "message": "Known game ticks move backwards within this session; sequence is retained and no valid chronology is asserted."})
            for e in rows:
                event_diagnostics[(key[0], key[1], e["sequence"])].append({"code": "SESSION_CHRONOLOGY_MISMATCH", "severity": "warning",
                    "message": "Session tick order is inconsistent; this event is ordered by session sequence."})
        sequences = sorted({e["sequence"] for e in rows})
        exact_ranges: list[tuple[int, int]] = []
        approximate_ranges: list[tuple[int, int]] = []
        for e in rows:
            if e["event_type"] != "gap":
                continue
            overlapping = _overlapping_observed_sequences(sequences, e["payload"].get("missing_ranges", []))
            if overlapping:
                diagnostics.append({"code": "GAP_OVERLAPS_OBSERVED_SEQUENCE", "severity": "warning",
                                    "campaign_id": key[0], "session_id": key[1],
                                    "gap_sequence": e["sequence"], "observed_sequences": overlapping,
                                    "message": "A gap range claims sequence IDs that are present in the journal."})
                event_diagnostics[(key[0], key[1], e["sequence"])].append({
                    "code": "GAP_OVERLAPS_OBSERVED_SEQUENCE", "severity": "warning",
                    "message": "This gap range overlaps an observed sequence and cannot certify that event as missing."})
                continue
            bucket = exact_ranges if e["payload"].get("ranges_exact") else approximate_ranges
            bucket.extend((r["first_sequence"], r["last_sequence"]) for r in e["payload"].get("missing_ranges", []))
        for left, right in zip(sequences, sequences[1:]):
            missing = (left + 1, right - 1)
            if missing[0] > missing[1]:
                continue
            exact_ranges.sort()
            cursor = missing[0]
            for first, last in exact_ranges:
                if last < cursor or first > missing[1]:
                    continue
                if first > cursor:
                    break
                cursor = max(cursor, last + 1)
                if cursor > missing[1]:
                    break
            if cursor <= missing[1]:
                approximate = any(first <= missing[1] and last >= cursor for first, last in approximate_ranges)
                diagnostics.append({"code": "SEQUENCE_GAP_UNCERTAIN" if approximate else "SEQUENCE_GAP_UNEXPLAINED",
                                    "severity": "warning", "campaign_id": key[0], "session_id": key[1],
                                    "first_sequence": cursor, "last_sequence": missing[1],
                                    "message": "Sequence positions are not fully certified by an exact gap record."})
        sessions.append({"campaign_id": key[0], "session_id": key[1], "time_basis_id": basis,
                         "starting_year": year, "game_start_abs_tick": abs_tick,
                         "event_count": len(rows), "parent": None if session_start is None else {
                             "session_id": session_start.get("payload", {}).get("parent_session_id"),
                             "checkpoint_id": session_start.get("payload", {}).get("loaded_checkpoint_id"),
                             "checkpoint_metadata_id": session_start.get("checkpoint_id"),
                             "claimed_status": session_start.get("payload", {}).get("parent_association", "unproven"),
                             "verified": False}})
    by_campaign: dict[str, list[tuple[str, int | None, int | None]]] = defaultdict(list)
    for key, parts in session_basis_parts.items():
        by_campaign[key[0]].append(parts)
    for campaign, parts in by_campaign.items():
        if len(set(parts)) > 1:
            diagnostics.append({"code": "TIME_BASIS_SEPARATED", "severity": "warning",
                                "campaign_id": campaign,
                                "message": "Sessions do not share a verified time basis and remain in separate groups."})
    session_lookup = {(s["campaign_id"], s["session_id"]): s for s in sessions}

    grouped: dict[tuple[str, str, int], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for event, origin in raw_events:
        grouped[(event["campaign_id"], event["session_id"], event["sequence"])].append((event, origin))
    unique_events: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    duplicate_count = 0
    for identity, entries in grouped.items():
        alternatives: dict[str, dict[str, Any]] = {}
        all_origins: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event, origin in entries:
            canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            alternatives[digest] = event
            all_origins[digest].append(origin)
        for digest in all_origins:
            all_origins[digest].sort(key=lambda o: (o["source_id"], o["line"], o["raw_line_sha256"]))
        duplicate_count += sum(max(0, len(origins) - 1) for origins in all_origins.values())
        if len(alternatives) > 1:
            conflicts.append({"identity": {"campaign_id": identity[0], "session_id": identity[1], "sequence": identity[2]},
                              "alternatives": [{"canonical_sha256": digest, "event": alternatives[digest],
                                               "origins": all_origins[digest]} for digest in sorted(alternatives)]})
        else:
            digest = next(iter(alternatives))
            event_type = alternatives[digest]["event_type"]
            if event_type not in {"session_started", "session_ended", "coverage", "gap", "save_checkpoint", "pawn_baseline",
                                  "pawn_died", "pawn_resurrected", "pawn_born", "pawn_faction_changed", "pawn_guest_status_changed",
                                  "pawn_relation_changed", "pawn_health_changed", "pawn_genes_changed", "pawn_role_changed", "pawn_holder_changed"}:
                event_diagnostics[(identity[0], identity[1], identity[2])].append({"code": "UNSUPPORTED_EVENT_TYPE", "severity": "info",
                    "message": "Unknown event type is preserved without inferred semantics."})
            unique_events.append({"event": alternatives[digest], "origins": all_origins[digest],
                                  "canonical_sha256": digest, "time_basis_id": session_basis.get((identity[0], identity[1])),
                                  "chronology": "invalid" if any(d["code"] in {"CLOCK_MISMATCH", "CLOCK_NEGATIVE", "SESSION_CHRONOLOGY_MISMATCH"}
                                                                    for d in event_diagnostics.get(identity, [])) else "valid_or_unknown",
                                  "diagnostics": event_diagnostics.get(identity, [])})
    # Verify lineage only against conflict-free, deduplicated checkpoint outcomes.
    for session in sessions:
        parent = session["parent"]
        if not parent or parent["claimed_status"] == "absent":
            continue
        child_starts = [x for x in unique_events
                        if x["event"]["campaign_id"] == session["campaign_id"]
                        and x["event"]["session_id"] == session["session_id"]
                        and x["event"]["event_type"] == "session_started"]
        child_start_conflict = any(
            conflict["identity"]["campaign_id"] == session["campaign_id"]
            and conflict["identity"]["session_id"] == session["session_id"]
            and any(alternative["event"].get("event_type") == "session_started"
                    for alternative in conflict["alternatives"])
            for conflict in conflicts
        )
        candidates = [x for x in unique_events
                      if x["event"]["campaign_id"] == session["campaign_id"]
                      and x["event"]["session_id"] == parent["session_id"]
                      and x["event"]["event_type"] == "save_checkpoint"
                      and x["event"].get("payload", {}).get("status") == "succeeded"
                      and x["event"].get("payload", {}).get("checkpoint_id") == parent["checkpoint_id"]]
        parent_session = session_lookup.get((session["campaign_id"], parent["session_id"]))
        child_parts = session_basis_parts[(session["campaign_id"], session["session_id"])]
        parent_parts = session_basis_parts.get((session["campaign_id"], parent["session_id"]))
        parent_claim_bad = any(conflict["identity"]["campaign_id"] == session["campaign_id"]
                               and conflict["identity"]["session_id"] == parent["session_id"]
                               and any(a["event"].get("event_type") == "save_checkpoint"
                                       and a["event"].get("payload", {}).get("status") == "succeeded"
                                       and a["event"].get("payload", {}).get("checkpoint_id") == parent["checkpoint_id"]
                                       for a in conflict["alternatives"])
                               for conflict in conflicts)
        child_start = child_starts[0] if len(child_starts) == 1 and not child_start_conflict else None
        checkpoint = candidates[0] if len(candidates) == 1 else None
        clock_inconsistent = False
        if child_start is not None and checkpoint is not None:
            child_event = child_start["event"]
            checkpoint_event = checkpoint["event"]
            clock_inconsistent = child_start["chronology"] == "invalid" or checkpoint["chronology"] == "invalid"
            for clock in ("ticks_game", "ticks_abs"):
                child_tick = child_event.get(clock)
                checkpoint_tick = checkpoint_event.get(clock)
                if child_tick is not None and checkpoint_tick is not None and child_tick < checkpoint_tick:
                    clock_inconsistent = True
        if child_start_conflict:
            diagnostics.append({"code": "ANCESTRY_START_CONFLICT", "severity": "warning",
                                "campaign_id": session["campaign_id"], "session_id": session["session_id"],
                                "message": "Conflicting session_started records prevent verification of the parent association."})
        if clock_inconsistent:
            diagnostics.append({"code": "ANCESTRY_CLOCK_INCONSISTENT", "severity": "warning",
                                "campaign_id": session["campaign_id"], "session_id": session["session_id"],
                                "message": "Child session start does not follow the parent checkpoint in a valid clock chronology."})
        valid = (len(candidates) == 1 and parent_session is not None and child_parts == parent_parts
                 and child_parts[1] is not None and child_parts[2] is not None and not parent_claim_bad
                 and parent.get("checkpoint_id") == parent.get("checkpoint_metadata_id")
                 and child_start is not None and not clock_inconsistent)
        parent["verified"] = valid
        parent["status"] = "verified" if valid else "unproven"
        if not valid:
            diagnostics.append({"code": "ANCESTRY_UNPROVEN", "severity": "warning",
                                "campaign_id": session["campaign_id"], "session_id": session["session_id"],
                                "message": "Parent association could not be independently verified."})
    # Break cycles even when every individual checkpoint claim looks plausible.
    by_session_id = {(s["campaign_id"], s["session_id"]): s for s in sessions}
    for start in sessions:
        seen: list[tuple[str, str]] = []
        current = start
        while current.get("parent") and current["parent"].get("verified"):
            key = (current["campaign_id"], current["session_id"])
            if key in seen:
                for cycle_key in seen[seen.index(key):]:
                    cycle = by_session_id[cycle_key]
                    cycle["parent"]["verified"] = False
                    cycle["parent"]["status"] = "unproven"
                diagnostics.append({"code": "ANCESTRY_CYCLE", "severity": "warning",
                                    "campaign_id": start["campaign_id"], "session_id": start["session_id"],
                                    "message": "Parent associations contain a cycle and are not verified."})
                break
            seen.append(key)
            current = by_session_id.get((current["campaign_id"], current["parent"].get("session_id")))
            if current is None:
                break
    # Unknown types remain data and receive no inferred semantics; invalid clock claims stay attached to events.
    for item in unique_events:
        event = item["event"]
        if event["event_type"] == "session_started":
            parent_id = event.get("payload", {}).get("parent_session_id")
            claimed = event.get("payload", {}).get("parent_association")
            matching_session = next((s for s in sessions if s["campaign_id"] == event["campaign_id"] and s["session_id"] == event["session_id"]), None)
            if claimed == "verified" and matching_session and not matching_session["parent"]["verified"]:
                diagnostics.append({"code": "ANCESTRY_CLAIM_DOWNGRADED", "severity": "warning",
                                    "campaign_id": event["campaign_id"], "session_id": event["session_id"],
                                    "message": "Recorder ancestry claim was downgraded by reader verification."})
    # Pawn selection retains coverage/control records and records that refer to the selected pawn.
    if session_id is not None:
        selected_sessions = {e["event"]["session_id"] for e in unique_events if e["event"]["session_id"] == session_id}
        raw_events = [(e, o) for e, o in raw_events if e["session_id"] == session_id]
        unique_events = [e for e in unique_events if e["event"]["session_id"] == session_id]
        conflicts = [c for c in conflicts if c["identity"]["session_id"] == session_id]
        sessions = [s for s in sessions if s["session_id"] in selected_sessions]
    if pawn_id is not None:
        def mentions_selected(event: dict[str, Any]) -> bool:
            payload = event.get("payload", {})
            return (event.get("subject_id") == pawn_id
                    or pawn_id in event.get("related_ids", [])
                    or payload.get("subject_id") == pawn_id
                    or (isinstance(payload.get("related_ids"), list)
                        and pawn_id in payload["related_ids"])
                    or event.get("event_type") in {
                        "session_started", "session_ended", "coverage", "gap", "save_checkpoint"
                    })

        unique_events = [item for item in unique_events if mentions_selected(item["event"])]
        conflicts = [c for c in conflicts if any(mentions_selected(a["event"])
                                                   for a in c["alternatives"])]
        mentioned = {(x["event"]["campaign_id"], x["event"]["session_id"]) for x in unique_events}
        sessions = [s for s in sessions if (s["campaign_id"], s["session_id"]) in mentioned]
    unique_events.sort(key=lambda x: (x["event"]["campaign_id"], x["time_basis_id"] or "",
                                     x["chronology"] == "invalid",
                                     x["event"]["session_id"] if x["chronology"] == "invalid" else "",
                                     x["event"]["ticks_abs"] is None if x["chronology"] != "invalid" else False,
                                     (x["event"]["ticks_abs"] or 0) if x["chronology"] != "invalid" else x["event"]["sequence"],
                                     x["event"]["session_id"], x["event"]["sequence"]))
    outcomes = {source["outcome"] for source in sources}
    partial = bool(outcomes & {"failed", "incomplete_tail", "empty"}) or bool(conflicts) or bool(
        any(e["event"]["event_type"] == "gap" for e in unique_events)
    ) or any(d["code"].startswith("SEQUENCE_GAP_") for d in diagnostics)
    response = {
        "journal_schema_version": JOURNAL_SCHEMA_VERSION,
        "sources": sources,
        "subject": {"pawn_id": pawn_id, "session_id": session_id},
        "sessions": sorted(sessions, key=lambda x: (x["campaign_id"], x["session_id"])),
        "events": unique_events,
        "conflicts": sorted(conflicts, key=lambda x: (x["identity"]["campaign_id"], x["identity"]["session_id"], x["identity"]["sequence"])),
        "coverage": {"status": "partial" if partial else "complete", "partial": partial,
                     "source_count": len(sources), "complete_source_count": sum(s["outcome"] == "complete" for s in sources),
                     "failed_source_count": sum(s["outcome"] == "failed" for s in sources),
                     "incomplete_tail_count": sum(s["outcome"] == "incomplete_tail" for s in sources),
                     "record_count": len(unique_events), "duplicate_count": duplicate_count,
                     "conflict_count": len(conflicts), "gap_count": sum(e["event"]["event_type"] == "gap" for e in unique_events),
                     "diagnostics": diagnostics},
        "provenance": {"source_path_included": include_source_path,
                       "ordering": "game_tick_within_time_basis_then_session_sequence"},
    }
    size = len(json.dumps(response, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
    if size > bounded.response_bytes:
        raise _fail("JOURNAL_LIMIT_EXCEEDED", "Journal response exceeds the configured byte limit.", response_bytes=size)
    return response
