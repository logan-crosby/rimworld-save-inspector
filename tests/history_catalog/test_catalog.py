"""Catalog tests use independent small source dictionaries, not the extractor."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rimworld_save_inspector.history import catalog


def _claim(value, *, status="observed"):
    return {"value": value, "status": status}


def _snapshot(path: Path):
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    tick = int(data.decode().split(":")[1])
    return {
        "snapshot_id": digest,
        "interpretation_id": "synthetic-v1",
        "metadata": {
            "profile": {"schema_version": "1.0", "parser_version": "test",
                        "extraction_version": "test", "game_version": "1.6",
                        "support_status": "verified"},
            "campaign_key": {
                name: _claim(value) for name, value in {
                    "seedString": "coral", "persistentRandomValue": "81234567",
                    "initialMapSize": "(250,1,250)", "startingYear": "5500",
                    "gameStartAbsTick": "52500", "startingTile": "42,0",
                }.items()
            },
            "clocks": {"ticksGame": _claim(tick), "gameStartAbsTick": _claim(52500),
                       "startingYear": _claim(5500), "ticksAbs": _claim(tick + 52500, status="derived")},
        },
        "pawns": [{"pawn_id": "Human42", "record_id": "r1", "source_path": "/savegame[1]/pawn[1]",
                   "scope": "map", "holder_chain": [], "aliases": [],
                   "status": "materialized", "domains": {"identity": {"status": "complete", "claims": [],
                                                                    "links": []}},
                   "evidence_refs": []}],
        "events": [],
        "context": {"maps": [], "factions": [], "mods": [], "research": [],
                    "scenario": [], "art": [], "related_entities": []},
        "evidence": [],
        "coverage": {"source_status": "parsed", "domains": {}, "unknown_fields": [], "limits": {}},
        "diagnostics": [],
    }


def test_duplicate_origins_refresh_and_immutable_generation(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    first = saves / "one.rws"
    second = saves / "two.rws"
    first.write_bytes(b"alpha:100")
    second.write_bytes(first.read_bytes())
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"

    built = catalog.build_catalog(saves, db)
    assert built["coverage"]["candidate_count"] == 2
    assert built["coverage"]["snapshot_count"] == 1
    first_id = built["catalog"]["catalog_id"]
    assert (tmp_path / "catalog.sqlite.generations" / f"{first_id}.sqlite").exists()
    assert not list((tmp_path / "catalog.sqlite.generations").glob(".snapshot-spool-*"))

    unchanged = catalog.refresh_catalog(db)
    assert unchanged["catalog"]["catalog_id"] == first_id
    second.write_bytes(b"alpha:200")
    changed = catalog.refresh_catalog(db)
    assert changed["catalog"]["catalog_id"] != first_id
    assert changed["coverage"]["snapshot_count"] == 2
    old = catalog.inspect_catalog(db, generation_id=first_id, freshness="frozen")
    assert old["coverage"]["snapshot_count"] == 1
    assert catalog.inspect_catalog(db)["catalog"]["catalog_id"] == changed["catalog"]["catalog_id"]


def test_failed_input_is_coverage_not_empty_success(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "good.rws").write_bytes(b"alpha:100")
    (saves / "bad.rws").write_bytes(b"invalid")

    def extract(path):
        if path.name == "bad.rws":
            raise ValueError("synthetic parser failure")
        return _snapshot(path)

    monkeypatch.setattr(catalog, "_extract_snapshot", extract)
    result = catalog.build_catalog(saves, tmp_path / "catalog.sqlite")
    assert result["coverage"]["candidate_count"] == 2
    assert result["coverage"]["parsed_count"] == 1
    assert result["coverage"]["failed_count"] == 1
    assert any(row["status"] == "failed" for row in result["sources"])


def test_discovered_symlink_is_failed_coverage_without_following_target(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "good.rws").write_bytes(b"alpha:100")
    (saves / "link.rws").symlink_to("good.rws")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    report = catalog.build_catalog(saves, tmp_path / "catalog.sqlite")
    assert report["coverage"]["candidate_count"] == 2
    assert report["coverage"]["parsed_count"] == 1
    assert report["coverage"]["failed_count"] == 1
    assert any(row["path"] == "link.rws" and row["error_code"] == "UNSAFE_SOURCE"
               for row in report["sources"])


def test_output_input_collision_is_rejected(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    source = saves / "one.rws"
    source.write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    with pytest.raises(Exception, match="output|source|collision"):
        catalog.build_catalog(saves, source)
    assert source.read_bytes() == b"alpha:100"


def test_cancelled_refresh_keeps_previous_generation(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    source = saves / "one.rws"
    source.write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    original = catalog.build_catalog(saves, db)["catalog"]["catalog_id"]
    source.write_bytes(b"alpha:200")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Exception, match="cancelled"):
        catalog.refresh_catalog(db, cancel=cancel)
    assert catalog.inspect_catalog(db, freshness="frozen")["catalog"]["catalog_id"] == original


def test_missing_old_generation_never_rebinds_to_new_one(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    source = saves / "one.rws"
    source.write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    old_id = catalog.build_catalog(saves, db)["catalog"]["catalog_id"]
    source.write_bytes(b"alpha:200")
    catalog.refresh_catalog(db)
    (tmp_path / "catalog.sqlite.generations" / f"{old_id}.sqlite").unlink()
    with pytest.raises(Exception, match="unavailable"):
        catalog.inspect_catalog(db, generation_id=old_id, freshness="frozen")


def test_refresh_retries_one_source_replacement_and_keeps_new_hash(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    source = saves / "one.rws"
    source.write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    source.write_bytes(b"alpha:200")
    original_hash = catalog._hash_source
    calls = 0

    def replace_once(path, stat_info):
        nonlocal calls
        calls += 1
        if calls == 1:
            source.write_bytes(b"alpha:300")
        return original_hash(path, stat_info)

    monkeypatch.setattr(catalog, "_hash_source", replace_once)
    refreshed = catalog.refresh_catalog(db, freshness="strict")
    assert calls == 2
    assert refreshed["sources"][0]["source_hash"] == hashlib.sha256(b"alpha:300").hexdigest()


def test_repeated_collection_change_fails_without_advancing_pointer(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    source = saves / "one.rws"
    source.write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    old = catalog.build_catalog(saves, db)["catalog"]["generation_id"]
    source.write_bytes(b"alpha:200")
    original_hash = catalog._hash_source
    changed = 200

    def replace_each_time(path, stat_info):
        nonlocal changed
        changed += 100
        source.write_bytes(f"alpha:{changed}".encode())
        return original_hash(path, stat_info)

    monkeypatch.setattr(catalog, "_hash_source", replace_each_time)
    with pytest.raises(Exception, match="collection changed"):
        catalog.refresh_catalog(db, freshness="strict")
    assert catalog.inspect_catalog(db, freshness="frozen")["catalog"]["generation_id"] == old


def test_cancel_before_publication_preserves_active_generation(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    source = saves / "one.rws"
    source.write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "_extract_snapshot", lambda path, **_kwargs: _snapshot(path))
    db = tmp_path / "catalog.sqlite"
    original = catalog.build_catalog(saves, db)["catalog"]["generation_id"]
    source.write_bytes(b"alpha:200")
    cancel = threading.Event()
    validate = catalog._validate_generation

    def cancel_when_validated(path, generation_id, *, full=False):
        result = validate(path, generation_id, full=full)
        if full:
            cancel.set()
        return result

    monkeypatch.setattr(catalog, "_validate_generation", cancel_when_validated)
    with pytest.raises(Exception, match="cancelled"):
        catalog.refresh_catalog(db, cancel=cancel)
    assert catalog.inspect_catalog(db, freshness="frozen")["catalog"]["generation_id"] == original
    assert not list((tmp_path / "catalog.sqlite.generations").glob(".snapshot-spool-*"))


def test_malformed_generation_rejected_without_fallback(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "one.rws").write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    generation_id = catalog.build_catalog(saves, db)["catalog"]["generation_id"]
    generation = tmp_path / "catalog.sqlite.generations" / f"{generation_id}.sqlite"
    generation.write_bytes(b"not a sqlite database")
    with pytest.raises(Exception, match="catalog generation|malformed|database"):
        catalog.inspect_catalog(db, freshness="frozen")


@pytest.mark.parametrize("table,query", [
    ("snapshot_context", "timeline"),
    ("snapshots", "dossier"),
])
def test_corrupt_compressed_snapshot_is_structured_failure(tmp_path, monkeypatch,
                                                           table, query):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "one.rws").write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    generation_id = catalog.build_catalog(saves, db)["catalog"]["generation_id"]
    generation = tmp_path / "catalog.sqlite.generations" / f"{generation_id}.sqlite"
    with sqlite3.connect(generation) as writable:
        writable.execute(f"UPDATE {table} SET payload=?", (b"invalid-compression",))
    from rimworld_save_inspector.history.service import get_dossier, get_timeline
    from rimworld_save_inspector.errors import InspectorError
    with pytest.raises(InspectorError) as failed:
        if query == "timeline":
            get_timeline(db, pawn_id="Human42", freshness="frozen")
        else:
            get_dossier(db, pawn_id="Human42", generation_id=generation_id)
    assert failed.value.code == "INVALID_CATALOG"


def test_context_index_layout_detects_missing_current_table_and_reads_older_layout(
        tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "one.rws").write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    generation_id = catalog.build_catalog(saves, db)["catalog"]["generation_id"]
    generation = tmp_path / "catalog.sqlite.generations" / f"{generation_id}.sqlite"
    from rimworld_save_inspector.history.service import get_timeline
    from rimworld_save_inspector.errors import InspectorError

    with sqlite3.connect(generation) as writable:
        writable.execute("DROP TABLE snapshot_context")
    with pytest.raises(InspectorError) as failed:
        get_timeline(db, pawn_id="Human42", freshness="frozen")
    assert failed.value.code == "INVALID_CATALOG"

    with sqlite3.connect(generation) as writable:
        writable.execute("DELETE FROM meta WHERE key='storage_layout_version'")
    result = get_timeline(db, pawn_id="Human42", freshness="frozen")
    assert result["coverage"]["selected_snapshot_count"] == 1


def test_source_over_limit_remains_failed_coverage(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "one.rws").write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "MAX_SOURCE_BYTES", 4)
    report = catalog.build_catalog(saves, tmp_path / "catalog.sqlite")
    assert report["coverage"]["candidate_count"] == 1
    assert report["coverage"]["failed_count"] == 1
    assert report["sources"][0]["error_code"] == "LIMIT_EXCEEDED"


def test_two_refreshes_serialize_to_one_valid_active_generation(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    source = saves / "one.rws"
    source.write_bytes(b"alpha:100")
    monkeypatch.setattr(catalog, "_extract_snapshot", _snapshot)
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    source.write_bytes(b"alpha:200")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(catalog.refresh_catalog, db) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert results[0]["catalog"]["generation_id"] == results[1]["catalog"]["generation_id"]
    assert catalog.inspect_catalog(db, freshness="frozen")["catalog"]["generation_id"] == (
        results[0]["catalog"]["generation_id"])


def test_unchanged_real_source_refresh_uses_indexed_profile_without_payload_decode(tmp_path, monkeypatch):
    fixture = Path(__file__).parents[1] / "history_oracle" / "fixtures" / "alpha-early.xml"
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "alpha.rws").write_bytes(fixture.read_bytes())
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)

    def unexpected_decode(*_args, **_kwargs):
        raise AssertionError("warm metadata refresh decoded a snapshot payload")

    monkeypatch.setattr(catalog, "_unpack", unexpected_decode)
    refreshed = catalog.refresh_catalog(db, freshness="metadata")
    assert refreshed["catalog"]["files_reused"] == 1
    assert refreshed["catalog"]["files_rehashed"] == 0
