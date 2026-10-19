"""A parser interpretation change invalidates unchanged-source cache reuse."""

from pathlib import Path

from rimworld_save_inspector.history import catalog, source
from rimworld_save_inspector.history.service import get_dossier, get_evidence


def test_revision_change_refreshes_same_bytes_and_preserves_old_evidence(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    fixture = Path(__file__).parents[1] / "history_oracle/fixtures/alpha-early.xml"
    save = saves / "alpha.rws"
    save.write_bytes(fixture.read_bytes())
    before = save.stat()
    db = tmp_path / "catalog.sqlite"
    # Only the revision label changes in this synthetic cache test. Both parses
    # run the real extractor; no replacement snapshots stand in for source data.
    with monkeypatch.context() as old:
        old.setattr(source, "EXTRACTION_VERSION", "previous-test-revision")
        built = catalog.build_catalog(saves, db)
    old_generation = built["catalog"]["generation_id"]
    old_dossier = get_dossier(db, pawn_id="pawn-alpha-01", generation_id=old_generation)
    token = old_dossier["evidence"][0]["evidence_id"]
    retained_before = get_evidence(db, token)
    original = catalog._extract_snapshot
    calls = []
    def measured_extract(path, *, cancel=None):
        calls.append(path)
        return original(path, cancel=cancel)
    monkeypatch.setattr(catalog, "_extract_snapshot", measured_extract)
    refreshed = catalog.refresh_catalog(db, freshness="metadata")
    assert calls == [save.resolve()]
    assert refreshed["catalog"]["files_reused"] == 0
    assert refreshed["catalog"]["generation_id"] != old_generation
    assert refreshed["catalog"]["namespace_id"] == built["catalog"]["namespace_id"]
    assert get_evidence(db, token) == retained_before
    assert catalog.inspect_catalog(db, generation_id=old_generation, freshness="frozen")["catalog"]["generation_id"] == old_generation
    after = save.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
    assert save.read_bytes() == fixture.read_bytes()
