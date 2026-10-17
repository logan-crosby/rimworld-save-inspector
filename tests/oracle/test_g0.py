"""G0 tests for the independent XML oracle and evidence validator.

These tests deliberately import only tests/oracle/extract.py and tools/verify.py.
They must remain runnable without importing the product package.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.oracle.extract import extract_private, extract_synthetic  # noqa: E402
from tools.verify import spec_oracle_agreement, validate_evidence, write_g0_evidence  # noqa: E402
from tools.private_acceptance import load_private_inputs, public_case_ids  # noqa: E402


def _fixture_oracle_result() -> dict[str, object]:
    private = load_private_inputs(check_fixture=False)
    manifest = private.oracle
    expected = {row["check_id"]: row["expected"] for row in manifest["assertions"]}
    return {"assertions": expected, "expected": expected, "matches": {key: True for key in expected},
            "all_match_spec": True, "dynamic": {"D01": {"fixture": "test"}, "D02": {"fixture": "test"}, "D03": [{"fixture": "test"}]}}


class OracleTests(unittest.TestCase):
    def test_appendix_a_seed_matches_frozen_handwritten_expectations(self) -> None:
        fixture = ROOT / "tests/fixtures/synthetic/seed.rws"
        expected = json.loads((ROOT / "tests/fixtures/synthetic/expected.json").read_text())
        self.assertEqual(extract_synthetic(fixture), expected)

    def test_private_oracle_produces_every_f_and_d_case_without_product_imports(self) -> None:
        private = load_private_inputs()
        if not private.ready or private.fixture is None:
            self.skipTest("frozen private inputs unavailable")
        fixture = private.fixture
        before = hashlib.sha256(fixture.read_bytes()).hexdigest()
        observed = extract_private(fixture)
        after = hashlib.sha256(fixture.read_bytes()).hexdigest()

        self.assertEqual(before, private.expected_sha256)
        self.assertEqual(after, before)
        self.assertEqual(set(observed["assertions"]), {f"F{i:02d}" for i in range(1, 29)})
        self.assertEqual(set(observed["dynamic"]), {"D01", "D02", "D03"})
        self.assertTrue(observed["all_match_spec"])

        source = (ROOT / "tests/oracle/extract.py").read_text()
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertNotIn("rimworld_save_inspector", imported)
        self.assertLessEqual(imported, {"__future__", "argparse", "hashlib", "json", "os", "pathlib", "re", "sys", "xml"})

    def test_spec_and_oracle_companions_agree_on_frozen_assertions(self) -> None:
        spec = (ROOT / "docs/design.md").read_text()
        private = load_private_inputs(check_fixture=False)
        if not private.ready:
            self.skipTest("frozen private inputs unavailable")
        manifest = private.oracle
        self.assertIn("Specification ID: `RWSI-1.0`. Revision: `2026-10-17.1`", spec)
        self.assertEqual(manifest["spec_id"], "RWSI-1.0")
        self.assertEqual(manifest["spec_revision"], "2026-10-16.1")
        spec_ids = {f"F{i:02d}" for i in range(1, 29)}
        self.assertEqual({item["check_id"] for item in manifest["assertions"]}, spec_ids)
        self.assertEqual(public_case_ids(), (spec_ids, {"D01", "D02", "D03"}))
        self.assertEqual(spec_oracle_agreement(), {"agree": True, "mismatches": []})


class EvidenceValidatorTests(unittest.TestCase):
    def test_missing_or_tampered_private_inputs_block_g0_without_changing_frozen_files(self) -> None:
        private = load_private_inputs(check_fixture=False)
        if not private.ready:
            self.skipTest("frozen private baseline unavailable for fault injection")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_oracle = root / "altered-oracle.json"
            bad_oracle.write_bytes(private.oracle_path.read_bytes() + b" ")
            malformed_anchor = root / "malformed-anchor.json"
            malformed_anchor.write_text("[]")
            for label, overrides in (
                ("missing-oracle", {"RWSI_PRIVATE_ORACLE": str(root / "missing.json")}),
                ("tampered-oracle", {"RWSI_PRIVATE_ORACLE": str(bad_oracle)}),
                ("missing-anchor", {"RWSI_PRIVATE_ANCHOR": str(root / "missing-anchor.json")}),
                ("malformed-anchor", {"RWSI_PRIVATE_ANCHOR": str(malformed_anchor)}),
            ):
                with self.subTest(label=label), mock.patch.dict(os.environ, overrides):
                    run = root / label
                    write_g0_evidence(run, fixture=None, actor="fault-injection")
                    self.assertEqual(json.loads((run / "manifest.json").read_text())["status"], "BLOCKED")
                    self.assertEqual(validate_evidence(run), [])
            wrong_fixture = root / "wrong-fixture.rws"
            wrong_fixture.write_text("<savegame/>")
            wrong_run = root / "wrong-fixture"
            write_g0_evidence(wrong_run, fixture=wrong_fixture, actor="fault-injection")
            self.assertEqual(json.loads((wrong_run / "manifest.json").read_text())["status"], "BLOCKED")
            self.assertEqual(validate_evidence(wrong_run), [])
            self.assertEqual(load_private_inputs(check_fixture=False).expected_input_hashes(),
                             private.expected_input_hashes())

    def test_validator_accepts_a_complete_g0_run_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            # The verifier fixture builder emits a complete shape-valid report.
            from tools.verify import write_g0_evidence

            private = load_private_inputs()
            if not private.ready:
                self.skipTest("frozen private inputs unavailable")
            write_g0_evidence(run, fixture=private.fixture, actor="unit-test", oracle_result=_fixture_oracle_result())
            self.assertEqual(validate_evidence(run), [])
            checks_path = run / "checks.json"
            checks = json.loads(checks_path.read_text())
            checks[0]["status"] = "FAIL"
            checks_path.write_text(json.dumps(checks, indent=2) + "\n")
            self.assertTrue(validate_evidence(run))

    def test_public_only_is_blocked_and_never_passes_g0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            from tools.verify import write_g0_evidence

            write_g0_evidence(run, fixture=None, actor="unit-test")
            errors = validate_evidence(run)
            self.assertEqual(errors, [])
            manifest = json.loads((run / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "BLOCKED")
            self.assertGreater(manifest["check_summary"]["blocked"], 0)


if __name__ == "__main__":
    unittest.main()
