"""Diagnostic profiler for the unchanged private source; not a G6 benchmark."""

from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import time
from pathlib import Path

from rimworld_save_inspector.models import ParseLimits, QueryRequest
from rimworld_save_inspector.parser.core import parse_stream
from rimworld_save_inspector.service import QueryService
from rimworld_save_inspector.source import load_snapshot


class PrefixReader:
    def __init__(self, stream, limit: int):
        self.stream = stream
        self.remaining = limit

    def read(self, size=-1):
        if self.remaining <= 0:
            return b""
        chunk = self.stream.read(min(size if size >= 0 else self.remaining, self.remaining))
        self.remaining -= len(chunk)
        return chunk

    def close(self):
        return None


def save(profile: cProfile.Profile, path: Path):
    profile.dump_stats(str(path))
    with path.with_suffix(".txt").open("w") as output:
        stats = pstats.Stats(profile, stream=output).sort_stats("cumtime")
        stats.print_stats(50)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--prefix-mib", type=int, default=8)
    parser.add_argument("--prefix-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    profile = cProfile.Profile()
    with args.fixture.open("rb") as stream:
        try:
            profile.enable()
            parse_stream(PrefixReader(stream, args.prefix_mib * 1024 * 1024), limits=ParseLimits())
        except Exception as exc:
            # The sample ends within the XML document by design.
            print("prefix_error", type(exc).__name__, str(exc))
        finally:
            profile.disable()
    save(profile, args.output / "parse-prefix.pstats")
    if args.prefix_only:
        return

    started = time.perf_counter()
    snapshot = load_snapshot(args.fixture)
    print("load_seconds", time.perf_counter() - started, "records", len(snapshot.records), "globals", len(snapshot.globals))

    profile = cProfile.Profile()
    profile.enable()
    service = QueryService(snapshot)
    profile.disable()
    save(profile, args.output / "indexes.pstats")

    pawn = next(record for record in snapshot.records if record.type_name == "pawn" and record.scope == "map")
    profile = cProfile.Profile()
    profile.enable()
    for query in (
        QueryRequest("summary"),
        QueryRequest("pawn", {"record_id": pawn.record_id}),
        QueryRequest("equipment", {"record_id": pawn.record_id, "holder": "equipment"}),
        QueryRequest("inventory", {"record_id": pawn.record_id}),
    ):
        before = time.perf_counter()
        response = service.execute(query)
        print("query", query.name, "seconds", time.perf_counter() - before, "bytes", len(json.dumps(response.as_dict())))
    profile.disable()
    save(profile, args.output / "queries.pstats")


if __name__ == "__main__":
    main()
