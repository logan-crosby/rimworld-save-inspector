"""Read-only save discovery with atomic, immutable SQLite history generations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
import zlib
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from ..errors import InspectorError
from .identity import campaign_identity

SCHEMA_VERSION = "1.0"
STORAGE_LAYOUT_VERSION = 2
MAX_CANDIDATES = 10_000
MAX_SOURCE_BYTES = 512 * 1024 * 1024
DEFAULT_STORAGE_BUDGET = 20 * 1024 * 1024 * 1024
_STAT_KEYS = ("dev", "ino", "size", "mtime_ns", "ctime_ns")


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _fail(code: str, message: str, **details: Any) -> InspectorError:
    return InspectorError(code, message, details)


def _ro_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise _fail("GENERATION_UNAVAILABLE", "The requested catalog generation is unavailable.",
                    generation=str(path.name))
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db
    except sqlite3.DatabaseError as exc:
        raise _fail("INVALID_CATALOG", "The catalog database cannot be opened.") from exc


def _registry(path: Path, *, create: bool = False) -> sqlite3.Connection:
    if path.is_symlink():
        raise _fail("INVALID_CATALOG", "The catalog registry cannot be a symlink.")
    if not create and not path.is_file():
        raise _fail("INVALID_CATALOG", "The catalog registry does not exist.")
    try:
        db = sqlite3.connect(path, timeout=30)
        db.row_factory = sqlite3.Row
        if create:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS registry (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.commit()
        return db
    except sqlite3.DatabaseError as exc:
        raise _fail("INVALID_CATALOG", "The catalog registry cannot be opened.") from exc


def _get_registry(db: sqlite3.Connection, key: str) -> Any:
    try:
        row = db.execute("SELECT value FROM registry WHERE key=?", (key,)).fetchone()
    except sqlite3.DatabaseError as exc:
        raise _fail("INVALID_CATALOG", "The catalog registry is malformed.") from exc
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError) as exc:
        raise _fail("INVALID_CATALOG", "The catalog registry contains invalid data.") from exc


def _set_registry(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute("INSERT OR REPLACE INTO registry(key,value) VALUES(?,?)", (key, _json(value).decode()))


def _generation_dir(catalog: Path) -> Path:
    directory = Path(str(catalog) + ".generations")
    if directory.is_symlink():
        raise _fail("INVALID_CATALOG", "The generation directory cannot be a symlink.")
    return directory


def _generation_path(catalog: Path, generation_id: str) -> Path:
    if len(generation_id) != 64 or any(c not in "0123456789abcdef" for c in generation_id):
        raise _fail("INVALID_CATALOG", "The generation identifier is invalid.")
    return _generation_dir(catalog) / f"{generation_id}.sqlite"


def _validate_generation(path: Path, generation_id: str, *, full: bool = False) -> None:
    with closing(_ro_connection(path)) as db:
        try:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0] if full else "ok"
            row = db.execute("SELECT value FROM meta WHERE key='catalog_id'").fetchone()
            if integrity != "ok" or row is None or json.loads(row[0]) != generation_id:
                raise _fail("INVALID_CATALOG", "The catalog generation failed validation.")
            if not full:
                db.execute("SELECT 1 FROM origins LIMIT 1").fetchone()
                db.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone()
        except sqlite3.DatabaseError as exc:
            raise _fail("INVALID_CATALOG", "The catalog generation is malformed.") from exc


def _path(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if "://" in raw or raw.startswith("file:"):
        raise _fail("UNSUPPORTED_SOURCE", "Only local filesystem paths are supported.")
    return Path(raw).expanduser().absolute()


def _config(source: str | os.PathLike[str], recursive: bool,
            manifest: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None) -> dict[str, Any]:
    root = _path(source)
    if not root.is_dir() or root.is_symlink():
        raise _fail("UNSUPPORTED_SOURCE", "The collection root must be a local directory.")
    explicit: list[str] | None = None
    if manifest is not None:
        if isinstance(manifest, (str, os.PathLike)):
            manifest_path = _path(manifest)
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise _fail("INVALID_ARGUMENT", "The source manifest must be a readable JSON path array.") from exc
        else:
            loaded = list(manifest)
        if not isinstance(loaded, list) or any(not isinstance(x, str) for x in loaded):
            raise _fail("INVALID_ARGUMENT", "The source manifest must be a JSON path array.")
        explicit = sorted({str(_path(x)) for x in loaded})
        if len(explicit) > MAX_CANDIDATES:
            raise _fail("LIMIT_EXCEEDED", "The source manifest exceeds the candidate limit.")
    return {"root": str(root), "recursive": bool(recursive), "manifest": explicit,
            "include_hidden": False, "follow_symlinks": False}


def _discover(config: dict[str, Any]) -> list[Path]:
    if config["manifest"] is not None:
        return [Path(p) for p in config["manifest"]]
    root = Path(config["root"])
    iterator = root.rglob("*.rws") if config["recursive"] else root.glob("*.rws")
    paths = []
    for path in iterator:
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        paths.append(path.absolute())
        if len(paths) > MAX_CANDIDATES:
            raise _fail("LIMIT_EXCEEDED", "The collection exceeds the candidate limit.",
                        limit=MAX_CANDIDATES)
    return sorted(paths)


def _stat(path: Path) -> dict[str, int] | None:
    try:
        st = path.lstat()
    except (FileNotFoundError, PermissionError):
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns}


def _fingerprint(paths: list[Path]) -> list[dict[str, Any]]:
    return [{"path": str(path), "stat": _stat(path)} for path in paths]


def _hash_source(path: Path, expected: dict[str, int]) -> str:
    if expected["size"] > MAX_SOURCE_BYTES:
        raise _fail("LIMIT_EXCEEDED", "The source exceeds the byte limit.", source=path.name,
                    limit=MAX_SOURCE_BYTES)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            fd_before = os.fstat(stream.fileno())
            if tuple(getattr(fd_before, "st_" + k) if k != "mtime_ns" and k != "ctime_ns"
                     else getattr(fd_before, "st_" + k) for k in _STAT_KEYS) != tuple(expected[k] for k in _STAT_KEYS):
                raise _fail("SOURCE_CHANGED", "The source changed before hashing.", source=path.name)
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                if stream.tell() > MAX_SOURCE_BYTES:
                    raise _fail("LIMIT_EXCEEDED", "The source exceeds the byte limit.", source=path.name)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise _fail("SOURCE_UNREADABLE", "The source could not be read.", source=path.name) from exc
    if tuple(getattr(after, "st_" + k) for k in _STAT_KEYS) != tuple(expected[k] for k in _STAT_KEYS):
        raise _fail("SOURCE_CHANGED", "The source changed while hashing.", source=path.name)
    if _stat(path) != expected:
        raise _fail("SOURCE_CHANGED", "The source was replaced while hashing.", source=path.name)
    return digest.hexdigest()


def _extract_snapshot(path: Path, *, cancel: threading.Event | None = None) -> dict[str, Any]:
    from .source import extract_history_snapshot
    return extract_history_snapshot(path, cancel=cancel).as_dict()


def _schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        PRAGMA synchronous=FULL;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE origins (
            path TEXT PRIMARY KEY, stat_json TEXT, source_hash TEXT,
            status TEXT NOT NULL, error_code TEXT, snapshot_id TEXT,
            interpretation_id TEXT
        );
        CREATE INDEX origin_snapshot ON origins(snapshot_id);
        CREATE TABLE snapshots (
            snapshot_id TEXT PRIMARY KEY, campaign_id TEXT, game_tick INTEGER,
            ticks_abs INTEGER, interpretation_id TEXT NOT NULL, payload BLOB NOT NULL
        );
        CREATE TABLE snapshot_profiles (
            snapshot_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL
        );
        CREATE TABLE snapshot_context (
            snapshot_id TEXT PRIMARY KEY, payload BLOB NOT NULL
        );
        CREATE INDEX snapshot_campaign_tick ON snapshots(campaign_id, game_tick, snapshot_id);
        CREATE TABLE pawns (
            snapshot_id TEXT NOT NULL, pawn_id TEXT, record_id TEXT NOT NULL,
            payload BLOB NOT NULL, PRIMARY KEY(snapshot_id,record_id)
        );
        CREATE INDEX pawn_saved_id ON pawns(pawn_id,snapshot_id);
        CREATE TABLE events (
            snapshot_id TEXT NOT NULL, event_index INTEGER NOT NULL, payload BLOB NOT NULL,
            PRIMARY KEY(snapshot_id,event_index)
        );
        CREATE TABLE event_links (
            pawn_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, event_index INTEGER NOT NULL,
            PRIMARY KEY(pawn_id,snapshot_id,event_index)
        );
        CREATE TABLE evidence_nodes (
            evidence_id TEXT PRIMARY KEY, node BLOB NOT NULL
        );
        CREATE TABLE evidence_occurrences (
            source_ref TEXT PRIMARY KEY, evidence_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL, interpretation_id TEXT NOT NULL, path TEXT NOT NULL
        );
        CREATE INDEX evidence_content ON evidence_occurrences(evidence_id);
    """)


def _compress(value: Any) -> bytes:
    return zlib.compress(_json(value), level=6)


def _unpack(blob: bytes, *, limit: int = 128 * 1024 * 1024) -> Any:
    try:
        decoder = zlib.decompressobj()
        data = decoder.decompress(blob, limit + 1)
        if decoder.unconsumed_tail or not decoder.eof:
            raise _fail("INVALID_CATALOG", "The catalog record exceeds its limit or is incomplete.")
    except zlib.error as exc:
        raise _fail("INVALID_CATALOG", "The catalog contains corrupt compressed data.") from exc
    if len(data) > limit:
        raise _fail("INVALID_CATALOG", "The catalog record exceeds its limit.")
    try:
        return json.loads(data)
    except (UnicodeDecodeError, ValueError) as exc:
        raise _fail("INVALID_CATALOG", "The catalog contains invalid JSON.") from exc


def _insert_snapshot(db: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    snapshot_id = snapshot["snapshot_id"]
    metadata = snapshot["metadata"]
    clocks = metadata.get("clocks", {})
    tick = clocks.get("ticksGame", {}).get("value")
    absolute = clocks.get("ticksAbs", {}).get("value")
    payload = {k: v for k, v in snapshot.items() if k not in {"pawns", "events", "evidence"}}
    db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?)",
               (snapshot_id, campaign_identity(metadata), tick if isinstance(tick, int) else None,
                absolute if isinstance(absolute, int) else None, snapshot["interpretation_id"],
                _compress(payload)))
    db.execute("INSERT OR IGNORE INTO snapshot_context VALUES(?,?)",
               (snapshot_id, _compress(snapshot.get("context", {}))))
    profile_id = metadata.get("profile", {}).get("profile_id")
    if isinstance(profile_id, str) and profile_id:
        db.execute("INSERT OR IGNORE INTO snapshot_profiles VALUES(?,?)",
                   (snapshot_id, profile_id))
    for pawn in snapshot.get("pawns", []):
        db.execute("INSERT OR IGNORE INTO pawns VALUES(?,?,?,?)",
                   (snapshot_id, pawn.get("pawn_id"), pawn["record_id"], _compress(pawn)))
    for index, event in enumerate(snapshot.get("events", [])):
        db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?)",
                   (snapshot_id, index, _compress(event)))
        for participant in event.get("participants", []):
            pawn_id = participant.get("pawn_id")
            if pawn_id:
                db.execute("INSERT OR IGNORE INTO event_links VALUES(?,?,?)",
                           (pawn_id, snapshot_id, index))
    for item in snapshot.get("evidence", []):
        node = _compress(item["node"])
        existing = db.execute("SELECT node FROM evidence_nodes WHERE evidence_id=?",
                              (item["evidence_id"],)).fetchone()
        if existing is not None and existing[0] != node:
            raise _fail("INVALID_SOURCE", "Evidence content ID collision was detected.")
        db.execute("INSERT OR IGNORE INTO evidence_nodes VALUES(?,?)", (item["evidence_id"], node))
        db.execute("INSERT OR IGNORE INTO evidence_occurrences VALUES(?,?,?,?,?)",
                   (item["source_ref"], item["evidence_id"], snapshot_id,
                    snapshot["interpretation_id"], item["path"]))


def _copy_snapshot(source: sqlite3.Connection, target: sqlite3.Connection, snapshot_id: str) -> bool:
    row = source.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if row is None:
        return False
    target.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?)", tuple(row))
    try:
        context = source.execute("SELECT payload FROM snapshot_context WHERE snapshot_id=?",
                                 (snapshot_id,)).fetchone()
    except sqlite3.OperationalError:
        context = None
    context_payload = (context["payload"] if context is not None
                       else _compress(_unpack(row["payload"]).get("context", {})))
    target.execute("INSERT OR IGNORE INTO snapshot_context VALUES(?,?)",
                   (snapshot_id, context_payload))
    try:
        profile = source.execute("SELECT profile_id FROM snapshot_profiles WHERE snapshot_id=?",
                                 (snapshot_id,)).fetchone()
    except sqlite3.OperationalError:
        profile = None
    if profile is None:
        profile_id = _unpack(row["payload"]).get("metadata", {}).get("profile", {}).get("profile_id")
    else:
        profile_id = profile["profile_id"]
    if isinstance(profile_id, str) and profile_id:
        target.execute("INSERT OR IGNORE INTO snapshot_profiles VALUES(?,?)",
                       (snapshot_id, profile_id))
    for table, columns in (
        ("pawns", "snapshot_id,pawn_id,record_id,payload"),
        ("events", "snapshot_id,event_index,payload"),
        ("event_links", "pawn_id,snapshot_id,event_index"),
        ("evidence_occurrences", "source_ref,evidence_id,snapshot_id,interpretation_id,path"),
    ):
        rows = source.execute(f"SELECT {columns} FROM {table} WHERE snapshot_id=?", (snapshot_id,))
        placeholders = ",".join("?" for _ in columns.split(","))
        target.executemany(f"INSERT OR IGNORE INTO {table} VALUES({placeholders})", (tuple(r) for r in rows))
    ids = [r[0] for r in target.execute(
        "SELECT evidence_id FROM evidence_occurrences WHERE snapshot_id=?", (snapshot_id,))]
    for evidence_id in ids:
        node = source.execute("SELECT node FROM evidence_nodes WHERE evidence_id=?", (evidence_id,)).fetchone()
        if node:
            target.execute("INSERT OR IGNORE INTO evidence_nodes VALUES(?,?)", (evidence_id, node[0]))
    return True


def _previous(catalog: Path, db: sqlite3.Connection) -> tuple[str | None, sqlite3.Connection | None]:
    generation_id = _get_registry(db, "active")
    if generation_id is None:
        return None, None
    path = _generation_path(catalog, generation_id)
    _validate_generation(path, generation_id)
    return generation_id, _ro_connection(path)


def _origin_rows(db: sqlite3.Connection | None) -> dict[str, dict[str, Any]]:
    if db is None:
        return {}
    return {row["path"]: dict(row) for row in db.execute("SELECT * FROM origins")}


def _origin_digest(config: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    logical = {"config": config, "schema_version": SCHEMA_VERSION,
               "storage_layout_version": STORAGE_LAYOUT_VERSION,
               "source_contract": "history-source/1.0",
               "origins": [{k: row[k] for k in ("path", "source_hash", "status", "error_code",
                                                "snapshot_id", "interpretation_id")}
                           for row in sorted(rows, key=lambda r: r["path"])]}
    return hashlib.sha256(_json(logical)).hexdigest()


def _expected_interpretation(old_db: sqlite3.Connection | None,
                             old: dict[str, Any] | None) -> str | None:
    if old_db is None or old is None or not old.get("snapshot_id"):
        return None
    try:
        row = old_db.execute("SELECT profile_id FROM snapshot_profiles WHERE snapshot_id=?",
                             (old["snapshot_id"],)).fetchone()
    except sqlite3.OperationalError:
        row = None
    profile_id = row["profile_id"] if row is not None else None
    if profile_id is None:
        snapshot_row = old_db.execute("SELECT payload FROM snapshots WHERE snapshot_id=?",
                                      (old["snapshot_id"],)).fetchone()
        if snapshot_row is None:
            return None
        profile_id = (_unpack(snapshot_row["payload"]).get("metadata", {})
                      .get("profile", {}).get("profile_id"))
    if not isinstance(profile_id, str) or not profile_id:
        return None
    try:
        from .source import interpretation_id_for_profile
        return interpretation_id_for_profile(profile_id)
    except (ImportError, ValueError):
        return None


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _refresh(catalog: Path, registry: sqlite3.Connection, *, freshness: str,
             storage_budget: int, initial: bool = False,
             cancel: threading.Event | None = None) -> dict[str, Any]:
    if freshness not in {"metadata", "strict"}:
        raise _fail("INVALID_ARGUMENT", "Freshness must be metadata or strict for refresh.")
    registry.execute("BEGIN IMMEDIATE")
    config = _get_registry(registry, "config")
    namespace = _get_registry(registry, "namespace_id")
    if not isinstance(config, dict) or not isinstance(namespace, str):
        raise _fail("INVALID_CATALOG", "The catalog registry is incomplete.")
    old_id, old_db = _previous(catalog, registry)
    old_rows = _origin_rows(old_db)
    last_error = None
    spools: list[tempfile.TemporaryDirectory[str]] = []
    try:
        for attempt in range(2):
            if cancel is not None and cancel.is_set():
                raise _fail("CANCELLED", "The catalog refresh was cancelled.")
            started = time.time()
            paths = _discover(config)
            before = _fingerprint(paths)
            rows: list[dict[str, Any]] = []
            extracted: dict[str, dict[str, Any]] = {}
            spool = tempfile.TemporaryDirectory(prefix=".snapshot-spool-",
                                                dir=_generation_dir(catalog))
            spools.append(spool)
            reuse_ids: set[str] = set()
            hashed_count = 0
            reused_count = 0
            for item in before:
                if cancel is not None and cancel.is_set():
                    raise _fail("CANCELLED", "The catalog refresh was cancelled.")
                path = Path(item["path"])
                info = item["stat"]
                old = old_rows.get(str(path))
                row = {"path": str(path), "stat_json": _json(info).decode() if info else None,
                       "source_hash": None, "status": "failed", "error_code": None,
                       "snapshot_id": None, "interpretation_id": None}
                if info is None:
                    row["error_code"] = "UNSAFE_SOURCE" if path.is_symlink() else "SOURCE_UNREADABLE"
                    rows.append(row)
                    continue
                try:
                    if info["size"] > MAX_SOURCE_BYTES:
                        raise _fail("LIMIT_EXCEEDED", "The source exceeds the byte limit.")
                    expected_interpretation = _expected_interpretation(old_db, old)
                    valid_old_profile = (old is not None and expected_interpretation is not None
                                         and old["interpretation_id"] == expected_interpretation)
                    can_reuse = (freshness == "metadata" and valid_old_profile
                                 and old["status"] == "parsed" and old["stat_json"] == row["stat_json"])
                    if can_reuse:
                        source_hash = old["source_hash"]
                        reused_count += 1
                    else:
                        source_hash = _hash_source(path, info)
                        hashed_count += 1
                    row["source_hash"] = source_hash
                    same_snapshot = (valid_old_profile and old["status"] == "parsed"
                                     and old["source_hash"] == source_hash)
                    if same_snapshot:
                        row["snapshot_id"] = old["snapshot_id"]
                        row["interpretation_id"] = old["interpretation_id"]
                        reuse_ids.add(old["snapshot_id"])
                    elif source_hash in extracted:
                        row["snapshot_id"] = source_hash
                        row["interpretation_id"] = extracted[source_hash]["interpretation_id"]
                    else:
                        snapshot = (_extract_snapshot(path, cancel=cancel) if cancel is not None
                                    else _extract_snapshot(path))
                        if snapshot["snapshot_id"] != source_hash:
                            raise _fail("SOURCE_CHANGED", "The source hash changed during extraction.")
                        spooled = Path(spool.name) / f"{source_hash}.json.zlib"
                        spooled.write_bytes(_compress(snapshot))
                        extracted[source_hash] = {"interpretation_id": snapshot["interpretation_id"],
                                                  "spool": spooled}
                        row["snapshot_id"] = source_hash
                        row["interpretation_id"] = snapshot["interpretation_id"]
                    row["status"] = "parsed"
                except (InspectorError, OSError, ValueError, KeyError) as exc:
                    if isinstance(exc, InspectorError) and exc.code == "CANCELLED":
                        raise
                    row["error_code"] = exc.code if isinstance(exc, InspectorError) else "SOURCE_PARSE_FAILED"
                rows.append(row)
            current_paths = {str(path) for path in paths}
            for path, old in old_rows.items():
                if path not in current_paths and old["snapshot_id"]:
                    removed = dict(old)
                    removed["status"] = "unavailable"
                    removed["error_code"] = "SOURCE_REMOVED"
                    rows.append(removed)
                    reuse_ids.add(old["snapshot_id"])
            after = _fingerprint(_discover(config))
            if after != before:
                last_error = _fail("COLLECTION_CHANGED", "The collection changed during refresh.",
                                   attempt=attempt + 1)
                spool.cleanup()
                spools.remove(spool)
                continue
            generation_id = _origin_digest(config, rows)
            if generation_id == old_id:
                if cancel is not None and cancel.is_set():
                    raise _fail("CANCELLED", "The catalog refresh was cancelled.")
                registry.commit()
                return _report(catalog, namespace, generation_id, old_db, freshness,
                               started, time.time(), reused=reused_count, rehashed=hashed_count)
            final = _generation_path(catalog, generation_id)
            if not final.exists():
                fd, temporary_name = tempfile.mkstemp(prefix=".building-", suffix=".sqlite",
                                                       dir=_generation_dir(catalog))
                os.close(fd)
                temporary = Path(temporary_name)
                try:
                    target = sqlite3.connect(temporary)
                    try:
                        _schema(target)
                        target.execute("INSERT INTO meta VALUES(?,?)",
                                       ("catalog_id", _json(generation_id).decode()))
                        target.execute("INSERT INTO meta VALUES(?,?)",
                                       ("namespace_id", _json(namespace).decode()))
                        target.execute("INSERT INTO meta VALUES(?,?)",
                                       ("storage_layout_version", _json(STORAGE_LAYOUT_VERSION).decode()))
                        target.execute("INSERT INTO meta VALUES(?,?)",
                                       ("config", _json(config).decode()))
                        target.execute("INSERT INTO meta VALUES(?,?)",
                                       ("indexed_at", _json(time.time()).decode()))
                        for row in rows:
                            target.execute("INSERT INTO origins VALUES(?,?,?,?,?,?,?)",
                                           tuple(row[k] for k in
                                                 ("path", "stat_json", "source_hash", "status",
                                                  "error_code", "snapshot_id", "interpretation_id")))
                        for snapshot_id in reuse_ids:
                            if old_db is None or not _copy_snapshot(old_db, target, snapshot_id):
                                raise _fail("INVALID_CATALOG", "A reused snapshot is unavailable.")
                        for item in extracted.values():
                            _insert_snapshot(target, _unpack(item["spool"].read_bytes(),
                                                             limit=MAX_SOURCE_BYTES))
                        target.commit()
                    finally:
                        target.close()
                    with temporary.open("rb") as stream:
                        os.fsync(stream.fileno())
                    occupied = sum(p.stat().st_size for p in _generation_dir(catalog).glob("*.sqlite"))
                    if occupied + temporary.stat().st_size > storage_budget:
                        raise _fail("LIMIT_EXCEEDED", "The catalog storage budget is exhausted.",
                                    budget_bytes=storage_budget, occupied_bytes=occupied)
                    try:
                        os.link(temporary, final)
                    except FileExistsError:
                        # A concurrent or interrupted prior build may already have
                        # installed this immutable content-derived generation.
                        _validate_generation(final, generation_id, full=True)
                    _sync_dir(_generation_dir(catalog))
                finally:
                    temporary.unlink(missing_ok=True)
            _validate_generation(final, generation_id, full=True)
            if cancel is not None and cancel.is_set():
                raise _fail("CANCELLED", "The catalog refresh was cancelled.")
            try:
                _set_registry(registry, "active", generation_id)
                registry.commit()
            except Exception:
                registry.rollback()
                raise
            with closing(_ro_connection(final)) as active:
                return _report(catalog, namespace, generation_id, active, freshness,
                               started, time.time(), reused=reused_count,
                               rehashed=hashed_count)
        raise last_error or _fail("COLLECTION_CHANGED", "The collection changed during refresh.")
    finally:
        for spool in spools:
            spool.cleanup()
        if registry.in_transaction:
            registry.rollback()
        if old_db is not None:
            old_db.close()


def _report(catalog: Path, namespace: str, generation_id: str, db: sqlite3.Connection,
            freshness: str, started: float | None, ended: float | None,
            *, reused: int = 0, rehashed: int = 0) -> dict[str, Any]:
    if db is None:
        raise _fail("INVALID_CATALOG", "The active generation is unavailable.")
    rows = [dict(row) for row in db.execute("SELECT * FROM origins ORDER BY path")]
    groups: dict[str, list[str]] = {}
    for row in db.execute("SELECT snapshot_id,campaign_id FROM snapshots"):
        groups.setdefault(row["campaign_id"] or "review_needed", []).append(row["snapshot_id"])
    return {
        "history_schema_version": SCHEMA_VERSION,
        "catalog": {"namespace_id": namespace, "catalog_id": generation_id,
                    "generation_id": generation_id, "catalog_basename": catalog.name,
                    "freshness": freshness, "discovery_started": started,
                    "discovery_ended": ended, "files_reused": reused,
                    "files_rehashed": rehashed, "groups": {k: sorted(v) for k, v in groups.items()}},
        "coverage": {"candidate_count": sum(r["status"] != "unavailable" for r in rows),
                     "parsed_count": sum(r["status"] == "parsed" for r in rows),
                     "failed_count": sum(r["status"] == "failed" for r in rows),
                     "removed_count": sum(r["status"] == "unavailable" for r in rows),
                     "snapshot_count": db.execute("SELECT count(*) FROM snapshots").fetchone()[0],
                     "duplicate_origin_count": max(0, sum(r["status"] == "parsed" for r in rows)
                                                    - db.execute("SELECT count(DISTINCT snapshot_id) FROM origins WHERE status='parsed'").fetchone()[0])},
        "sources": [{"path": Path(r["path"]).name, "source_hash": r["source_hash"],
                     "status": r["status"], "error_code": r["error_code"],
                     "snapshot_id": r["snapshot_id"],
                     "interpretation_id": r["interpretation_id"]} for r in rows],
    }


def build_catalog(source: str | os.PathLike[str], output: str | os.PathLike[str], *,
                  recursive: bool = False,
                  manifest: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None = None,
                  storage_budget: int = DEFAULT_STORAGE_BUDGET,
                  cancel: threading.Event | None = None) -> dict[str, Any]:
    """Create a configured registry and its first immutable generation."""
    config = _config(source, recursive, manifest)
    catalog = _path(output)
    if catalog.exists() or catalog == Path(config["root"]):
        raise _fail("OUTPUT_COLLISION", "The output path already exists or collides with a source.")
    sources = _discover(config)
    if catalog in sources or any(catalog.samefile(p) for p in sources if p.exists() and catalog.exists()):
        raise _fail("OUTPUT_COLLISION", "The output path collides with a source.")
    catalog.parent.mkdir(parents=True, exist_ok=True)
    _generation_dir(catalog).mkdir(mode=0o700, exist_ok=False)
    try:
        fd = os.open(catalog, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        with _registry(catalog, create=True) as db:
            _set_registry(db, "namespace_id", uuid.uuid4().hex)
            _set_registry(db, "config", config)
            _set_registry(db, "storage_budget", storage_budget)
            db.commit()
            return _refresh(catalog, db, freshness="metadata",
                            storage_budget=storage_budget, initial=True, cancel=cancel)
    except Exception:
        # A failed initial build has no valid active generation. Remove only files
        # created inside this new application-owned catalog location.
        with _registry(catalog) as db:
            active = _get_registry(db, "active")
        if active is None:
            catalog.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(catalog) + suffix).unlink(missing_ok=True)
        raise


def refresh_catalog(catalog: str | os.PathLike[str], *, freshness: str = "metadata",
                    storage_budget: int | None = None,
                    cancel: threading.Event | None = None) -> dict[str, Any]:
    """Refresh sources then advance the pointer only after a validated generation exists."""
    path = _path(catalog)
    with _registry(path) as db:
        budget = storage_budget or _get_registry(db, "storage_budget") or DEFAULT_STORAGE_BUDGET
        return _refresh(path, db, freshness=freshness, storage_budget=budget, cancel=cancel)


def inspect_catalog(catalog: str | os.PathLike[str], *,
                    generation_id: str | None = None, freshness: str = "metadata") -> dict[str, Any]:
    """Inspect a refreshed catalog, or explicitly inspect a frozen generation."""
    path = _path(catalog)
    if freshness not in {"metadata", "strict", "frozen"}:
        raise _fail("INVALID_ARGUMENT", "Freshness must be metadata, strict, or frozen.")
    if freshness != "frozen" and generation_id is None:
        return refresh_catalog(path, freshness=freshness)
    with _registry(path) as registry:
        namespace = _get_registry(registry, "namespace_id")
        target_id = generation_id or _get_registry(registry, "active")
        if target_id is None:
            raise _fail("INVALID_CATALOG", "The catalog has no active generation.")
        target = _generation_path(path, target_id)
        _validate_generation(target, target_id)
        with closing(_ro_connection(target)) as db:
            return _report(path, namespace, target_id, db, "frozen", None, None)


def open_generation(catalog: str | os.PathLike[str], generation_id: str | None = None
                    ) -> tuple[str, str, sqlite3.Connection]:
    """Open one immutable generation read-only and return namespace, ID, connection."""
    path = _path(catalog)
    with _registry(path) as registry:
        namespace = _get_registry(registry, "namespace_id")
        target_id = generation_id or _get_registry(registry, "active")
    if not isinstance(namespace, str) or not isinstance(target_id, str):
        raise _fail("INVALID_CATALOG", "The catalog registry is incomplete.")
    target = _generation_path(path, target_id)
    _validate_generation(target, target_id)
    return namespace, target_id, _ro_connection(target)


def unpack(blob: bytes, *, limit: int = 128 * 1024 * 1024) -> Any:
    """Decode an application-owned compressed row with a hard uncompressed cap."""
    return _unpack(blob, limit=limit)
