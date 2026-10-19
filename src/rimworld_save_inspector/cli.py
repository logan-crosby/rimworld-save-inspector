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
    inspect.add_argument("--project-def")
    inspect.add_argument("--group")
    inspect.add_argument("--holder")
    inspect.add_argument("--holder-id")
    inspect.add_argument("--estimate-missing-as-one", action="store_true")

    mcp = commands.add_parser("mcp", help="run the optional read-only MCP stdio server")
    mcp.add_argument("--save", required=True, help="path to the configured RimWorld save file")
    mcp.add_argument("--verbose", action="store_true")

    journal = commands.add_parser("journal", help="read a local Life Recorder JSONL journal")
    journal.add_argument("path", help="journal segment or directory")
    journal.add_argument("--pawn-id")
    journal.add_argument("--session-id")
    journal.add_argument("--include-source-path", action="store_true")

    journal_mcp = commands.add_parser("journal-mcp", help="run the optional read-only journal MCP server")
    journal_mcp.add_argument("--journal", required=True, help="configured journal segment or directory")
    journal_mcp.add_argument("--verbose", action="store_true")

    catalog = commands.add_parser("catalog", help="build, refresh or inspect a history save catalog")
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_build = catalog_commands.add_parser("build")
    catalog_build.add_argument("source", help="explicit save directory")
    catalog_build.add_argument("--output", required=True, help="application-owned SQLite catalog")
    catalog_build.add_argument("--recursive", action="store_true")
    catalog_build.add_argument("--manifest", help="JSON array of explicitly selected source paths")
    catalog_refresh = catalog_commands.add_parser("refresh")
    catalog_refresh.add_argument("catalog")
    catalog_refresh.add_argument("--freshness", choices=("metadata", "strict"), default="metadata")
    catalog_inspect = catalog_commands.add_parser("inspect")
    catalog_inspect.add_argument("catalog")
    catalog_inspect.add_argument("--freshness", choices=("metadata", "strict", "frozen"), default="metadata")
    catalog_inspect.add_argument("--generation-id")

    dossier = commands.add_parser("dossier", help="one-save or catalog pawn dossier")
    dossier.add_argument("source")
    dossier.add_argument("--snapshot-id")
    dossier.add_argument("--generation-id", help="read a retained immutable generation")
    dossier.add_argument("--pawn-id")
    dossier.add_argument("--name")
    dossier.add_argument("--detail", choices=("context", "full"), default="context")
    timeline = commands.add_parser("timeline", help="one-call pawn history across a catalog")
    timeline.add_argument("catalog")
    timeline.add_argument("--anchor", dest="anchor_snapshot_id")
    timeline.add_argument("--generation-id", help="read a retained immutable generation")
    timeline.add_argument("--pawn-id")
    timeline.add_argument("--name")
    timeline.add_argument("--detail", choices=("context", "full"), default="context")
    timeline.add_argument("--domain", dest="domains", action="append")
    timeline.add_argument("--freshness", choices=("metadata", "strict", "frozen"), default="metadata")
    timeline.add_argument("--journal", help="optional configured Life Recorder journal")
    evidence = commands.add_parser("evidence", help="retrieve retained history source evidence")
    evidence.add_argument("catalog")
    evidence.add_argument("evidence_id")
    evidence.add_argument("--cursor")
    evidence.add_argument("--limit", type=int)
    events = commands.add_parser("events", help="page retained events including participantless records")
    events.add_argument("catalog")
    events.add_argument("--anchor", dest="anchor_snapshot_id")
    events.add_argument("--generation-id")
    events.add_argument("--snapshot-id")
    events.add_argument("--saved-event-id")
    events.add_argument("--cursor")
    events.add_argument("--limit", type=int, default=50)
    events.add_argument("--freshness", choices=("metadata", "strict", "frozen"), default="metadata")
    discover = commands.add_parser("discover", help="first-pass offline discovery without a pawn selector")
    discover.add_argument("catalog")
    discover.add_argument("--anchor", dest="anchor_snapshot_id")
    discover.add_argument("--generation-id")
    discover.add_argument("--freshness", choices=("metadata", "strict", "frozen"), default="metadata")
    discover.add_argument("--skeptic-limit", type=int, default=5)
    history_mcp = commands.add_parser("history-mcp", help="run configured read-only history MCP server")
    history_mcp.add_argument("--catalog", required=True)
    history_mcp.add_argument("--journal", help="optional configured Life Recorder journal")
    history_mcp.add_argument("--verbose", action="store_true")
    return parser


def _filters(args: argparse.Namespace) -> dict[str, Any]:
    names = ("scope", "map_id", "faction", "pawn_def", "name", "guest_status", "section", "project_def", "group", "holder", "holder_id")
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


def _dispatch_journal_mcp(journal: str, verbose: bool) -> int:
    try:
        from . import journal_mcp
    except ImportError:
        _write_json(_error("MCP_EXTRA_REQUIRED", "Install the optional MCP server component to use this command."))
        return 2
    try:
        return int(journal_mcp.main(journal_path=journal, verbose=verbose) or 0)
    except InspectorError as exc:
        _write_json(exc.as_dict())
        return 2
    except KeyboardInterrupt:
        _write_json(_error("CANCELLED", "The journal MCP server was interrupted."))
        return 130


def _dispatch_history(args: argparse.Namespace) -> int:
    from .history import catalog as history_catalog
    from .history.service import get_dossier, get_evidence, get_events, get_timeline

    if args.command == "catalog":
        if args.catalog_command == "build":
            result = history_catalog.build_catalog(args.source, args.output,
                                                   recursive=args.recursive,
                                                   manifest=args.manifest)
        elif args.catalog_command == "refresh":
            result = history_catalog.refresh_catalog(args.catalog, freshness=args.freshness)
        else:
            result = history_catalog.inspect_catalog(args.catalog,
                                                     generation_id=args.generation_id,
                                                     freshness=args.freshness)
    elif args.command == "dossier":
        result = get_dossier(args.source, pawn_id=args.pawn_id, name=args.name,
                             snapshot_id=args.snapshot_id, detail=args.detail,
                             generation_id=args.generation_id)
    elif args.command == "timeline":
        result = get_timeline(args.catalog, pawn_id=args.pawn_id, name=args.name,
                              anchor_snapshot_id=args.anchor_snapshot_id,
                              detail=args.detail, domains=args.domains,
                              freshness=args.freshness, journal_path=args.journal,
                              generation_id=args.generation_id)
    elif args.command == "evidence":
        result = get_evidence(args.catalog, args.evidence_id,
                              cursor=args.cursor, limit=args.limit)
    elif args.command == "events":
        result = get_events(args.catalog, anchor_snapshot_id=args.anchor_snapshot_id,
                            generation_id=args.generation_id, snapshot_id=args.snapshot_id,
                            saved_event_id=args.saved_event_id, cursor=args.cursor,
                            limit=args.limit, freshness=args.freshness)
    elif args.command == "discover":
        from .discovery import discover_catalog
        result = discover_catalog(args.catalog, anchor_snapshot_id=args.anchor_snapshot_id,
                                  generation_id=args.generation_id, freshness=args.freshness,
                                  skeptic_limit=args.skeptic_limit)
    else:
        from .history import mcp as history_mcp
        return history_mcp.main(catalog_path=args.catalog, verbose=args.verbose,
                                journal_path=args.journal)
    _write_json(result)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command is None:
            raise _ArgumentError("a command is required")
        if args.command == "mcp":
            return _dispatch_mcp(args.save, args.verbose)
        if args.command == "journal-mcp":
            return _dispatch_journal_mcp(args.journal, args.verbose)
        if args.command == "journal":
            from .journal import read_journal
            _write_json(read_journal(args.path, pawn_id=args.pawn_id, session_id=args.session_id,
                                     include_source_path=args.include_source_path))
            return 0
        if args.command in {"catalog", "dossier", "timeline", "evidence", "events", "discover", "history-mcp"}:
            return _dispatch_history(args)

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
        if getattr(args if "args" in locals() else None, "command", None) in {
            "catalog", "dossier", "timeline", "evidence", "events", "discover", "history-mcp"
        }:
            _write_json({"history_schema_version": "1.0", "error": {
                "code": exc.code, "message": exc.message, "details": exc.details,
                "retryable": exc.retryable,
            }})
        else:
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
