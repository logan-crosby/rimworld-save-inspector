"""Controlled concurrency and cleanup checks for the MCP snapshot lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
import threading
import time

import pytest

from rimworld_save_inspector.errors import InspectorError
from rimworld_save_inspector import mcp_server
from rimworld_save_inspector.source import load_snapshot as _real_load_snapshot


SYNTHETIC = b"<savegame><meta><gameVersion>1.6.4871 rev591</gameVersion></meta><game><tickManager><ticksGame>7</ticksGame></tickManager><maps><li><uniqueID>0</uniqueID><things><thing Class='Pawn'><id>P</id><def>Human</def></thing></things></li></maps></game></savegame>"


def _save(path: Path) -> Path:
    path.write_bytes(SYNTHETIC)
    return path


def test_concurrent_inspect_and_query_never_observe_partial_snapshot(monkeypatch, tmp_path):
    source = _save(tmp_path / "loading.rws")
    entered = threading.Event()
    release = threading.Event()

    def held_loader(path, *, cancel=None):
        entered.set()
        assert release.wait(3), "test barrier was not released"
        return _real_load_snapshot(path, cancel=cancel)

    monkeypatch.setattr(mcp_server, "load_snapshot", held_loader)
    lifecycle = mcp_server._Lifecycle(source)

    async def run():
        first_inspect = asyncio.create_task(lifecycle.inspect())
        assert await asyncio.to_thread(entered.wait, 2), "loader did not enter controlled barrier"
        assert lifecycle.state == lifecycle.LOADING
        assert lifecycle.service is None
        with pytest.raises(InspectorError) as simultaneous_inspect:
            await lifecycle.inspect()
        assert simultaneous_inspect.value.code == "SNAPSHOT_LOADING"
        with pytest.raises(InspectorError) as simultaneous_query:
            lifecycle.current_service()
        assert simultaneous_query.value.code == "SNAPSHOT_LOADING"
        assert lifecycle.service is None
        release.set()
        inspected = await asyncio.wait_for(first_inspect, timeout=4)
        assert lifecycle.state == lifecycle.READY
        assert inspected["data"]["snapshot_lifetime"] == "process"
        assert lifecycle.current_service().snapshot.snapshot_id == inspected["snapshot_id"]

    try:
        asyncio.run(run())
    finally:
        release.set()
        lifecycle.close()
    assert lifecycle.worker is not None and not lifecycle.worker.is_alive()


def test_failed_load_publishes_nothing_and_retry_recovers(monkeypatch, tmp_path):
    source = _save(tmp_path / "retry.rws")
    attempts = 0

    def fail_once(path, *, cancel=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise InspectorError("MALFORMED_XML", "Synthetic controlled failure.")
        return _real_load_snapshot(path, cancel=cancel)

    monkeypatch.setattr(mcp_server, "load_snapshot", fail_once)
    lifecycle = mcp_server._Lifecycle(source)

    async def run():
        with pytest.raises(InspectorError) as failure:
            await lifecycle.inspect()
        assert failure.value.code == "MALFORMED_XML"
        assert lifecycle.state == lifecycle.EMPTY
        assert lifecycle.service is None
        assert lifecycle.worker is not None and not lifecycle.worker.is_alive()
        with pytest.raises(InspectorError) as query_before_retry:
            lifecycle.current_service()
        assert query_before_retry.value.code == "SNAPSHOT_NOT_LOADED"
        recovered = await asyncio.wait_for(lifecycle.inspect(), timeout=4)
        assert lifecycle.state == lifecycle.READY
        assert recovered["snapshot_id"].startswith("sha256:")
        assert attempts == 2

    try:
        asyncio.run(run())
    finally:
        lifecycle.close()


def test_cancelled_inspect_joins_worker_and_releases_source_before_retry(monkeypatch, tmp_path):
    source = _save(tmp_path / "cancel.rws")
    entered = threading.Event()
    handle_closed = threading.Event()
    cancellation_seen = threading.Event()
    cancel_mode = True

    def cancellable_loader(path, *, cancel=None):
        if not cancel_mode:
            return _real_load_snapshot(path, cancel=cancel)
        try:
            with path.open("rb") as handle:
                assert handle.read(1) == b"<"
                entered.set()
                while not cancel.wait(0.005):
                    pass
                cancellation_seen.set()
                raise InspectorError("CANCELLED", "Controlled worker cancellation.")
        finally:
            handle_closed.set()

    monkeypatch.setattr(mcp_server, "load_snapshot", cancellable_loader)
    lifecycle = mcp_server._Lifecycle(source)

    async def run():
        task = asyncio.create_task(lifecycle.inspect())
        assert await asyncio.to_thread(entered.wait, 2), "worker did not reach cancellation barrier"
        worker = lifecycle.worker
        assert worker is not None and worker.is_alive()
        cancellation_started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - cancellation_started
        assert elapsed < 2, f"cancellation cleanup took {elapsed:.3f}s"
        assert cancellation_seen.is_set()
        assert handle_closed.is_set()
        assert worker is not None and not worker.is_alive(), "client stopped waiting while owned worker remained alive"
        assert lifecycle.state == lifecycle.EMPTY
        assert lifecycle.service is None
        with pytest.raises(InspectorError) as unloaded:
            lifecycle.current_service()
        assert unloaded.value.code == "CANCELLED"
        assert source.read_bytes().startswith(b"<"), "source remained unusable after the worker released it"
        nonlocal cancel_mode
        cancel_mode = False
        retried = await asyncio.wait_for(lifecycle.inspect(), timeout=4)
        assert lifecycle.state == lifecycle.READY
        assert retried["snapshot_id"].startswith("sha256:")

    try:
        asyncio.run(run())
    finally:
        lifecycle.close()


def test_shutdown_cancels_and_reaps_owned_worker_within_five_seconds(monkeypatch, tmp_path):
    source = _save(tmp_path / "shutdown.rws")
    entered = threading.Event()
    handle_closed = threading.Event()

    def cancellable_loader(path, *, cancel=None):
        try:
            with path.open("rb"):
                entered.set()
                while not cancel.wait(0.005):
                    pass
                raise InspectorError("CANCELLED", "Controlled shutdown cancellation.")
        finally:
            handle_closed.set()

    monkeypatch.setattr(mcp_server, "load_snapshot", cancellable_loader)
    lifecycle = mcp_server._Lifecycle(source)

    async def run():
        inspect_task = asyncio.create_task(lifecycle.inspect())
        assert await asyncio.to_thread(entered.wait, 2)
        worker = lifecycle.worker
        assert worker is not None and worker.is_alive()
        started = time.monotonic()
        await asyncio.to_thread(lifecycle.close)
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"shutdown exceeded five-second join bound: {elapsed:.3f}s"
        assert handle_closed.is_set()
        assert worker is not None and not worker.is_alive(), "close returned before the owned worker was reaped"
        assert lifecycle.state == lifecycle.CLOSING
        assert lifecycle.service is None
        with pytest.raises(InspectorError) as closed_query:
            lifecycle.current_service()
        assert closed_query.value.code == "CANCELLED"
        with pytest.raises(InspectorError) as cancelled_inspect:
            await asyncio.wait_for(inspect_task, timeout=1)
        assert cancelled_inspect.value.code == "CANCELLED"

    asyncio.run(run())
