"""Independent synthetic contract checks. Uses only Python's standard library."""
from __future__ import annotations

import hashlib
import json
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPECTED = json.loads((ROOT / "expected.json").read_text())


def parse_complete_lines(name: str) -> list[dict]:
    raw = (ROOT / "fixtures" / name).read_bytes()
    assert raw.endswith(b"\n"), f"{name}: fixture must terminate complete records"
    rows = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{name}:{line_no}: invalid complete line: {exc}") from exc
    return rows


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def group_by_identity(rows: list[dict]) -> dict[tuple, list[dict]]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["campaign_id"], row["session_id"], row["sequence"])].append(row)
    return grouped


class FrozenOracle(unittest.TestCase):
    def test_valid_envelopes_and_sequence_identity(self):
        rows = parse_complete_lines("valid.jsonl")
        contract = EXPECTED["fixture_semantics"]["valid.jsonl"]
        self.assertEqual(contract["event_ids"], [r["event_id"] for r in rows])
        self.assertEqual(3, len({(r["campaign_id"], r["session_id"], r["sequence"]) for r in rows}))
        self.assertEqual("1.0", rows[0]["schema_version"])

    def test_forks_retain_distinct_sessions_and_verified_parent(self):
        rows = parse_complete_lines("fork.jsonl")
        contract = EXPECTED["fixture_semantics"]["fork.jsonl"]
        by_session = defaultdict(list)
        for row in rows:
            by_session[row["session_id"]].append(row)
        self.assertEqual(set(contract["sessions"]), set(by_session))
        root_success = [r for r in by_session["s-root"] if r["event_type"] == "save_checkpoint" and r["payload"]["status"] == "succeeded"]
        self.assertEqual([contract["verified_parent_checkpoint"]], [r["payload"]["checkpoint_id"] for r in root_success])
        for child in contract["children"]:
            start = by_session[child][0]
            self.assertEqual("cp-root", start["payload"]["loaded_checkpoint_id"])
            self.assertEqual("s-root", start["payload"]["parent_session_id"])
            self.assertEqual("verified", start["payload"]["parent_association"])
        self.assertEqual("s-left", next(r for r in by_session["s-left"] if r["event_type"] == "pawn_faction_changed")["session_id"])
        self.assertEqual("s-right", next(r for r in by_session["s-right"] if r["event_type"] == "pawn_faction_changed")["session_id"])

    def test_conflicts_compare_full_envelope_and_keep_all_origins(self):
        raw_lines = (ROOT / "fixtures" / "conflict.jsonl").read_bytes().splitlines()
        rows = [json.loads(line) for line in raw_lines]
        contract = EXPECTED["fixture_semantics"]["conflict.jsonl"]
        alternatives = group_by_identity(rows)[tuple(contract["identity"])]
        distinct = {canonical(row) for row in alternatives}
        self.assertEqual(contract["alternative_count"], len(distinct))
        self.assertEqual(contract["origin_count"], len(alternatives))
        self.assertEqual(contract["origin_count"] - 1, len({hashlib.sha256(line).hexdigest() for line in raw_lines}))
        self.assertTrue(all(row["payload"] == {"value": "first"} for row in alternatives))
        self.assertEqual({"alpha", "beta"}, {row["extensions"]["source_tag"] for row in alternatives})

    def test_gap_has_exact_noncontiguous_ranges_and_consumed_ids(self):
        rows = parse_complete_lines("gap.jsonl")
        contract = EXPECTED["fixture_semantics"]["gap.jsonl"]
        gap = next(row for row in rows if row["event_type"] == "gap")
        self.assertEqual(contract["event_id"], gap["event_id"])
        payload = gap["payload"]
        self.assertEqual(contract["ranges_exact"], payload["ranges_exact"])
        self.assertEqual(contract["dropped_count"], payload["dropped_count"])
        ranges = payload["missing_ranges"]
        self.assertEqual([tuple(r) for r in contract["missing_ranges"]],
                         [(r["first_sequence"], r["last_sequence"], r["cause"]) for r in ranges])
        self.assertEqual(contract["dropped_count"], payload["dropped_count"])
        self.assertEqual(payload["dropped_count"], sum(r["last_sequence"] - r["first_sequence"] + 1 for r in ranges))
        self.assertNotIn(3, {n for r in ranges for n in range(r["first_sequence"], r["last_sequence"] + 1)})

    def test_unknown_clocks_stay_null_and_unknown_type_is_preserved(self):
        rows = parse_complete_lines("unknown-clock.jsonl")
        contract = EXPECTED["fixture_semantics"]["unknown-clock.jsonl"]
        self.assertEqual(contract["sequences"], [row["sequence"] for row in rows])
        self.assertEqual([None, None, None], [rows[0][key] for key in ("ticks_game", "ticks_abs", "game_start_abs_tick")])
        event = next(row for row in rows if row["event_type"] == contract["unknown_type"])
        self.assertEqual(contract["unknown_type"], event["event_type"])
        self.assertIsNone(event["ticks_game"])

    def test_malformed_interior_line_is_source_failure_case(self):
        raw = (ROOT / "fixtures" / "malformed-interior.jsonl").read_bytes()
        contract = EXPECTED["fixture_semantics"]["malformed-interior.jsonl"]
        self.assertTrue(raw.endswith(b"\n"))
        lines = raw.splitlines()
        self.assertEqual(contract["line_count"], len(lines))
        with self.assertRaises(json.JSONDecodeError):
            json.loads(lines[contract["invalid_line"] - 1])
        self.assertEqual(contract["valid_suffix_event"], json.loads(lines[-1])["event_id"])

    def test_nonterminated_tail_is_excluded_incomplete_fragment(self):
        raw = (ROOT / "fixtures" / "truncated-tail.jsonl").read_bytes()
        contract = EXPECTED["fixture_semantics"]["truncated-tail.jsonl"]
        self.assertFalse(raw.endswith(b"\n"))
        complete, fragment = raw.rsplit(b"\n", 1)
        rows = [json.loads(line) for line in complete.splitlines()]
        self.assertEqual(contract["complete_event_ids"], [r["event_id"] for r in rows])
        with self.assertRaises(json.JSONDecodeError):
            json.loads(fragment)

    def test_contract_expectations_are_literal_and_fixture_hashes_match(self):
        self.assertEqual("synthetic-contract-v1", EXPECTED["oracle_id"])
        for name, digest in EXPECTED["sha256"].items():
            self.assertEqual(digest, hashlib.sha256((ROOT / "fixtures" / name).read_bytes()).hexdigest(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
