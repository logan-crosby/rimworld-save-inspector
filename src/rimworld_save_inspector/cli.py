"""Command line adapter for the shared immutable snapshot/query API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .errors import InspectorError
from .models import QueryRequest
from .service import QueryService
from .source import load_snapshot


class _ArgumentError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="rim-save", description="Read-only structured inspection of RimWorld save files")
    parser.add_argument("--version", action="version", version=f"rim-save {__version__}")
    commands = parser.add_subparsers(dest="command")

    inspect = commands.add_parser("inspect", help="inspect one local save snapshot")
    inspect.add_argument("save", help="path to a RimWorld save file")
    inspect.add_argument("query", choices=("metadata", "summary", "pawns", "pawn", "health", "genes", "equipment", "inventory", "research", "mods"))
    inspect.add_argument("query_argument", nargs="?", help="record ID for pawn, health, genes, equipment and inventory")
    inspect.add_argument("--limit", type=int)
    inspect.add_argument("--cursor")
    inspect.add_argument("--include-source-path", action="store_true")
    inspect.add_argument("--verbose", action="store_true")
    inspect.add_argument("--map-id")
    inspect.add_argument("--scope")
    inspect.add_argument("--faction")
    inspect.add_argument("--def", dest="pawn_def")
    inspect.add_argument("--name")
    inspect.add_argument("--guest-status")
    inspect.add_argument("--section")
    inspect.add_argument("--group")
    inspect.add_argument("--holder")
    inspect.add_argument("--holder-id")
    inspect.add_argument("--estimate-missing-as-one", action="store_true")

    mcp = commands.add_parser("mcp", help="run the optional read-only MCP stdio server")
    mcp.add_argument("--save", required=True, help="path to the configured RimWorld save file")
    mcp.add_argument("--verbose", action="store_true")
    return parser


def _filters(args: argparse.Namespace) -> dict[str, Any]:
    names = ("scope", "map_id", "faction", "pawn_def", "name", "guest_status", "section", "group", "holder", "holder_id")
    filters = {name: getattr(args, name) for name in names if getattr(args, name, None) is not None}
    if getattr(args, "estimate_missing_as_one", False):
        filters["estimate_missing_as_one"] = True
    if args.query == "pawn" or args.query in {"health", "genes", "equipment", "inventory"}:
        if not args.query_argument:
            raise _ArgumentError(f"{args.query} requires a record ID")
        filters["record_id"] = args.query_argument
    elif args.query_argument is not None:
        raise _ArgumentError(f"{args.query} does not accept a positional query argument")
    return filters


def _write_json(value: dict[str, Any]) -> bool:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()
        return True
    except BrokenPipeError:
        # Prevent the interpreter's shutdown flush from reporting the same pipe error.
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            pass
        return False


def _error(code: str, message: str, *, snapshot_id: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "snapshot_id": snapshot_id,
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "retryable": code in {"SOURCE_CHANGED", "SNAPSHOT_LOADING", "CANCELLED"},
        },
    }


def _dispatch_mcp(save: str, verbose: bool) -> int:
    """Forward to the separately packaged MCP server when it is available."""
    try:
        from . import mcp_server
    except ImportError:
        _write_json(_error("MCP_EXTRA_REQUIRED", "Install the optional MCP server component to use this command."))
        return 2
    try:
        return int(mcp_server.main(save_path=save, verbose=verbose) or 0)
    except InspectorError as exc:
        _write_json(exc.as_dict())
        return 2
    except KeyboardInterrupt:
        _write_json(_error("CANCELLED", "The MCP server was interrupted."))
        return 130


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command is None:
            raise _ArgumentError("a command is required")
        if args.command == "mcp":
            return _dispatch_mcp(args.save, args.verbose)

        query_name = "inspect" if args.query == "metadata" else args.query
        filters = _filters(args)
        if args.verbose:
            print(f"Loading {Path(args.save).name} and running {query_name}.", file=sys.stderr)
        snapshot = load_snapshot(args.save)
        service = QueryService(snapshot, snapshot_lifetime="invocation")
        response = service.execute(QueryRequest(query_name, filters, limit=args.limit, cursor=args.cursor))
        result = response.as_dict()
        if args.include_source_path:
            result["provenance"]["source_path"] = str(Path(args.save).expanduser().resolve())
        _write_json(result)
        return 0
    except _ArgumentError as exc:
        _write_json(_error("INVALID_ARGUMENT", str(exc)))
        return 2
    except InspectorError as exc:
        _write_json(exc.as_dict())
        if getattr(args if "args" in locals() else None, "verbose", False):
            print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        snapshot_id = locals().get("snapshot").snapshot_id if locals().get("snapshot") is not None else None
        _write_json(_error("CANCELLED", "The inspection was interrupted.", snapshot_id=snapshot_id))
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:
        if getattr(args if "args" in locals() else None, "verbose", False):
            traceback.print_exc(file=sys.stderr)
        elif isinstance(exc, Exception):
            print("Internal inspection error.", file=sys.stderr)
        _write_json(_error("INTERNAL_ERROR", "An internal inspection error occurred."))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
