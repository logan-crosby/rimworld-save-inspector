#!/usr/bin/env python3
"""Audit the CLI and MCP adapters for writes into a configured save tree and runtime sockets.

Run in a fresh process so the audit hook is installed before application imports.
Reports contain event names and paths only; save contents are never copied to output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import sys
import time


ROOT = Path(__file__).resolve().parents[2]


def _inside(path: object, root: Path) -> bool:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    try:
        candidate = Path(os.fsdecode(path)).resolve(strict=False)
        candidate.relative_to(root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("cli", "mcp", "probe"), required=True)
    parser.add_argument("--save", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    save = args.save.resolve(strict=True)
    protected_root = save.parent
    writes: list[dict[str, str]] = []
    socket_operations: list[dict[str, str]] = []
    networks: list[dict[str, str]] = []
    counts = {"source_reads": 0, "queries": 0}

    def audit(event: str, event_args: tuple[object, ...]) -> None:
        if event == "open" and event_args:
            path = event_args[0]
            mode = event_args[1] if len(event_args) > 1 else None
            flags = event_args[2] if len(event_args) > 2 else 0
            if _inside(path, protected_root):
                if Path(os.fsdecode(path)).resolve(strict=False) == save and not (
                    isinstance(mode, str) and any(flag in mode for flag in "wax+")
                ) and not (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                    counts["source_reads"] += 1
                write_mode = isinstance(mode, str) and any(flag in mode for flag in "wax+")
                write_flags = isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
                if write_mode or write_flags:
                    writes.append({"kind": "write-open", "path": str(Path(os.fsdecode(path)).resolve(strict=False))})
                    raise PermissionError("runtime audit blocked a write-open under the save root")
        if event in {"os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.rmdir",
                     "os.chmod", "os.chown", "os.truncate", "os.utime", "os.symlink", "os.link"}:
            paths = [arg for arg in event_args[:2] if _inside(arg, protected_root)]
            if paths:
                writes.append({"kind": event, "path": str(paths[0])})
                raise PermissionError(f"runtime audit blocked {event} under the save root")
        if event.startswith("socket."):
            family = event_args[1] if event == "socket.__new__" and len(event_args) > 1 else None
            socket_operations.append({"event": event, "family": str(family) if family is not None else ""})
            # asyncio's Unix selector loop creates a local socketpair for wakeups;
            # it is a socket operation but not network egress. All network APIs
            # and IP-family socket creation are recorded and blocked.
            if event == "socket.__new__" and family == socket.AF_UNIX:
                return
            networks.append({"event": event})
            raise PermissionError("runtime audit blocked a socket operation")

    sys.addaudithook(audit)
    before_hash = _sha256(save)
    mode = save.stat().st_mode
    started = time.monotonic()
    exit_code = 0
    error = None
    runtime_package = None
    try:
        if args.mode != "probe":
            import rimworld_save_inspector
            runtime_package = {"file": str(Path(rimworld_save_inspector.__file__).resolve()),
                               "version": rimworld_save_inspector.__version__}
        if args.mode == "probe":
            try:
                with save.open("ab"):
                    pass
            except PermissionError:
                pass
            try:
                socket.getaddrinfo("example.invalid", 80)
            except PermissionError:
                pass
            counts["queries"] = 2
        elif args.mode == "cli":
            from rimworld_save_inspector.cli import main as cli_main
            exit_code = int(cli_main(["inspect", str(save), "summary"]))
            counts["queries"] = 1 if exit_code == 0 else 0
        else:
            import anyio
            from rimworld_save_inspector.mcp_server import create_server

            server = create_server(save)

            async def run_queries() -> None:
                try:
                    loaded = await server.call_tool("inspect_save", {})
                    if loaded.is_error:
                        raise RuntimeError(loaded.content)
                    sid = loaded.structured_content["snapshot_id"]
                    summary = await server.call_tool("get_colony_summary", {"snapshot_id": sid})
                    mods = await server.call_tool("get_mods", {"snapshot_id": sid, "limit": 1})
                    for result in (summary, mods):
                        if result.is_error:
                            raise RuntimeError(result.content)
                    counts["queries"] = 3
                finally:
                    server._rim_save_lifecycle.close()

            anyio.run(run_queries)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        exit_code = 1
    after_hash = _sha256(save)
    command = [sys.executable, str(Path(__file__).resolve()), "--mode", args.mode, "--save", str(save)]
    if args.report is not None:
        command.extend(["--report", str(args.report)])
    report = {
        "schema_version": 1,
        "mode": args.mode,
        "command": command,
        "python": sys.version,
        "platform": platform.platform(),
        "runtime_package": runtime_package,
        "report_path": str(args.report.resolve(strict=False)) if args.report is not None else None,
        "source_path": str(save),
        "source_hash_before": before_hash,
        "source_hash_after": after_hash,
        "mode_before": mode,
        "mode_after": save.stat().st_mode,
        "source_read_events": counts["source_reads"],
        "queries_completed": counts["queries"],
        "writes_under_save_root": writes,
        "socket_operations": socket_operations,
        "network_attempts": networks,
        "elapsed_seconds": time.monotonic() - started,
        "error": error,
    }
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
    os.write(2, ("MCP_RUNTIME_IO_AUDIT=" + encoded + "\n").encode("utf-8"))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
