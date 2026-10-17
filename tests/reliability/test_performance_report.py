"""Validate retained formal benchmark evidence against the current source."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unittest
from pathlib import Path

from tools.private_acceptance import load_private_inputs


ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class PerformanceEvidenceTests(unittest.TestCase):
    def test_formal_three_worker_report_matches_current_core_and_thresholds(self):
        evidence = Path(os.environ.get("RWSI_PERFORMANCE_RUN", ROOT / "evidence/performance-formal-004"))
        report_path = Path(os.environ.get("RWSI_PERFORMANCE_REPORT", ROOT / "briefs/performance-report.md"))
        self.assertTrue(report_path.is_file(), "formal benchmark report is required")
        report = report_path.read_text()
        provenance = report.split("Provenance hashes:", 1)[-1]
        for relative in ("src/rimworld_save_inspector/parser/core.py",
                         "src/rimworld_save_inspector/service.py",
                         "tools/performance/benchmark_core.py"):
            match = re.search(re.escape(relative) + r"\s+`([0-9a-f]{64})`", provenance)
            self.assertIsNotNone(match, f"report does not authenticate {relative}")
            self.assertEqual(match.group(1), _sha256(ROOT / relative),
                             f"formal timings are stale for changed input {relative}")

        private = load_private_inputs()
        self.assertTrue(private.ready and private.fixture is not None,
                        "T35 requires authenticated private acceptance inputs")
        fixture = private.fixture
        fixture_hash = _sha256(fixture)
        fixture_bytes = fixture.stat().st_size
        self.assertEqual(fixture_hash, private.expected_sha256)
        self.assertEqual(fixture_bytes, private.expected_bytes)
        expected_snapshot = "sha256:" + fixture_hash
        samples = []
        pids = set()
        for number in range(1, 4):
            output = evidence / f"run-{number}.stdout"
            error = evidence / f"run-{number}.stderr"
            exitcode = evidence / f"run-{number}.exitcode"
            self.assertTrue(output.is_file() and error.is_file() and exitcode.is_file(),
                            f"worker {number} evidence is incomplete")
            self.assertEqual(exitcode.read_text().strip(), "0", f"worker {number} exited unsuccessfully")
            sample = json.loads(output.read_text())
            self.assertEqual(sample["snapshot_id"], expected_snapshot)
            self.assertEqual(sample["source_bytes"], fixture_bytes)
            self.assertEqual(sample["machine"], "arm64", "reference timing host must be the declared native Mac")
            self.assertEqual(sample["rss_method"], "resource.RUSAGE_SELF.ru_maxrss")
            self.assertEqual(sample["warm_query_count"], 140)
            self.assertEqual(len(sample["warm_query_p95_by_query_nanoseconds"]), 14)
            self.assertLessEqual(sample["warm_query_worst_per_query_p95_nanoseconds"], 250_000_000)
            self.assertLess(sample["rss_bytes"], 536_870_912)
            pids.add(sample["pid"])
            samples.append(sample)
        self.assertEqual(len(pids), 3, "workers must be fresh independent processes")
        load_seconds = sorted(sample["load_index_nanoseconds"] / 1_000_000_000 for sample in samples)
        self.assertLessEqual(load_seconds[1], 30.0, "median load plus index must meet the reference threshold")
        self.assertTrue(all(value <= 60.0 for value in load_seconds), "every worker must meet the absolute load bound")


if __name__ == "__main__":
    unittest.main()
