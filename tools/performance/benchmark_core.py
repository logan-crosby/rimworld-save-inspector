"""Measure G6 core load/index and warm queries in fresh worker processes."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import resource
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from rimworld_save_inspector.models import QueryRequest
from rimworld_save_inspector.service import QueryService
from rimworld_save_inspector.source import load_snapshot


def _queries(pawn_id: str) -> list[QueryRequest]:
    return [
        QueryRequest("summary"),
        QueryRequest("pawn", {"record_id": pawn_id, "section": "core"}),
        QueryRequest("pawns", {"scope": "map"}, limit=50),
        QueryRequest("pawn", {"record_id": pawn_id, "section": "skills"}, limit=50),
        QueryRequest("pawn", {"record_id": pawn_id, "section": "traits"}, limit=50),
        QueryRequest("health", {"record_id": pawn_id}, limit=50),
        QueryRequest("genes", {"record_id": pawn_id, "group": "endogenes"}, limit=50),
        QueryRequest("genes", {"record_id": pawn_id, "group": "xenogenes"}, limit=50),
        QueryRequest("equipment", {"record_id": pawn_id, "holder": "equipment"}, limit=50),
        QueryRequest("equipment", {"record_id": pawn_id, "holder": "apparel"}, limit=50),
        QueryRequest("inventory", {"record_id": pawn_id}, limit=50),
        QueryRequest("research", {"section": "progress"}, limit=50),
        QueryRequest("research", {"section": "techprints"}, limit=50),
        QueryRequest("mods", limit=50),
    ]


def _worker(fixture: Path) -> dict[str, object]:
    started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    snapshot = load_snapshot(fixture)
    parsed = time.perf_counter_ns()
    service = QueryService(snapshot)
    indexed = time.perf_counter_ns()
    cpu_indexed = time.process_time_ns()
    pawn = next(record for record in snapshot.records if record.type_name == "pawn" and record.scope == "map")
    samples: list[dict[str, object]] = []
    for request in _queries(pawn.record_id):
        label = request.name + (":" + str(dict(request.filters).get("section") or dict(request.filters).get("group") or dict(request.filters).get("holder"))
                                if request.name in {"pawn", "genes", "equipment", "research"} else "")
        for repetition in range(10):
            before = time.perf_counter_ns()
            response = service.execute(request)
            encoded = json.dumps(response.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            elapsed = time.perf_counter_ns() - before
            samples.append({"query": label, "repetition": repetition + 1, "nanoseconds": elapsed, "response_bytes": len(encoded)})
    latencies = sorted(int(sample["nanoseconds"]) for sample in samples)
    p95 = latencies[math.ceil(0.95 * len(latencies)) - 1]
    latencies_by_query: dict[str, list[int]] = defaultdict(list)
    for sample in samples:
        latencies_by_query[str(sample["query"])].append(int(sample["nanoseconds"]))
    p95_by_query = {
        name: sorted(values)[math.ceil(0.95 * len(values)) - 1]
        for name, values in latencies_by_query.items()
    }
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss_native = usage.ru_maxrss
    rss_bytes = rss_native if sys.platform == "darwin" else rss_native * 1024
    return {
        "pid": os.getpid(), "platform": platform.platform(), "machine": platform.machine(),
        "python": sys.version, "executable": sys.executable, "rss_method": "resource.RUSAGE_SELF.ru_maxrss",
        "rss_native": rss_native, "rss_native_units": "bytes" if sys.platform == "darwin" else "KiB", "rss_bytes": rss_bytes,
        "fixture": str(fixture), "snapshot_id": snapshot.snapshot_id, "source_bytes": snapshot.source_bytes,
        "records": len(snapshot.records), "globals": len(snapshot.globals),
        "array_global_pairs": len(service._array_globals), "metadata_entries": len(service._metadata_globals),
        "child_records": sum(len(children) for children in service._children.values()),
        "parse_nanoseconds": parsed - started, "index_nanoseconds": indexed - parsed,
        "load_index_nanoseconds": indexed - started, "load_index_cpu_nanoseconds": cpu_indexed - cpu_started,
        "involuntary_context_switches": usage.ru_nivcsw, "voluntary_context_switches": usage.ru_nvcsw,
        "warm_query_p95_nanoseconds": p95,
        "warm_query_count": len(samples), "warm_query_p95_by_query_nanoseconds": p95_by_query,
        "warm_query_worst_per_query_p95_nanoseconds": max(p95_by_query.values()), "warm_queries": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(_worker(args.fixture.resolve()), sort_keys=True))
        return
    if args.runs < 1:
        parser.error("--runs must be positive")
    args.out.mkdir(parents=True, exist_ok=False)
    for number in range(1, args.runs + 1):
        command = [sys.executable, str(Path(__file__).resolve()), "--fixture", str(args.fixture.resolve()),
                   "--out", str(args.out.resolve()), "--worker"]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        (args.out / f"run-{number}.stdout").write_text(result.stdout)
        (args.out / f"run-{number}.stderr").write_text(result.stderr)
        (args.out / f"run-{number}.exitcode").write_text(str(result.returncode) + "\n")
        print(f"run {number}: exit={result.returncode}", flush=True)
        if result.returncode:
            break


if __name__ == "__main__":
    main()
