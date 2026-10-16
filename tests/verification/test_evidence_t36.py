"""T36 adversarial checks for the evidence validator itself.

The synthetic PASS below is a schema-validator test vector, not gate evidence
for a product run. Every mutation re-hashes ordinary files where possible so
the assertion targets semantic checks as well as simple checksum detection.
"""

from __future__ import annotations

import json
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.gates.cases import CASES  # noqa: E402
from tools.verify import (_artifact, _copy_performance_evidence, _expected_packaging_check_ids,
                          _generic_manifest, _package_report_case_is_complete, _prerequisite_evidence_satisfied,
                          _copy_packaging_report, _gate_failure_findings, _review_receipt_errors, now, run_gate, run_g7_gate,
                          validate_evidence, sha256)  # noqa: E402


def _make_run(parent: Path) -> Path:
    run = parent / "G1-t36"
    run.mkdir()
    (run / "checks.json").write_text("[]\n")
    (run / "commands.json").write_text("[]\n")
    (run / "pytest.stdout.txt").write_text("3 passed\n")
    (run / "pytest.stderr.txt").write_text("")
    mapped_nodes = CASES["T24"].test_nodes
    testcases = "".join(
        f'<testcase classname="tests.contract.test_cli_schema" name="{node.split("::", 1)[1]}" />'
        for node in mapped_nodes
    )
    (run / "pytest.junit.xml").write_text(
        f'<testsuite tests="{len(mapped_nodes)}" failures="0" errors="0" skipped="0">{testcases}</testsuite>\n'
    )
    observed = {"case_coverage_complete": True, "mapped_test_nodes": len(mapped_nodes), "pytest_exit_code": 0,
                "collected_test_cases": len(mapped_nodes), "skipped_required_tests": 0,
                "case_skipped_test_count": 0,
                "case_test_results": [{"node": node, "status": "PASS"} for node in mapped_nodes],
                "test_result_path": "pytest.junit.xml"}
    expected = {"case_coverage_complete": True,
                "minimum_collected_test_cases": len(mapped_nodes), "maximum_skipped_required_tests": 0}
    checks = [{"check_id": "T24", "requirement_ids": ["R19"], "required": True,
               "status": "PASS", "expected": expected, "observed": observed,
               "evidence_paths": ["pytest.stdout.txt", "pytest.stderr.txt", "pytest.junit.xml"]},
              {"check_id": "T33", "requirement_ids": ["R19"], "required": True,
               "status": "BLOCKED", "expected": "package audit and external installs", "observed": "not run",
               "evidence_paths": ["blocker.txt"]}]
    (run / "blocker.txt").write_text("synthetic validator fixture: T33 intentionally blocked\n")
    (run / "checks.json").write_text(json.dumps(checks, indent=2, sort_keys=True) + "\n")
    command = {"argv": [sys.executable, "-m", "pytest", "-q", *mapped_nodes], "cwd_label": "synthetic-validator-fixture",
               "started_at": now(), "duration_seconds": 0.01, "exit_code": 0,
               "stdout_path": "pytest.stdout.txt", "stderr_path": "pytest.stderr.txt"}
    (run / "commands.json").write_text(json.dumps([command], indent=2, sort_keys=True) + "\n")
    _reseal(run, "BLOCKED")
    return run


def _reseal(run: Path, state: str | None = None) -> None:
    checks = json.loads((run / "checks.json").read_text())
    commands = json.loads((run / "commands.json").read_text())
    paths = ["checks.json", "commands.json", "pytest.stdout.txt", "pytest.stderr.txt", "pytest.junit.xml", "blocker.txt"]
    sidecars = [_artifact(run / name, "T36 synthetic validator input", run) for name in paths if (run / name).is_file()]
    (run / "artifacts.json").write_text(json.dumps(sidecars, indent=2, sort_keys=True) + "\n")
    artifacts = sidecars + [_artifact(run / "artifacts.json", "artifact index", run)]
    old = json.loads((run / "manifest.json").read_text()) if (run / "manifest.json").is_file() else None
    manifest = old or _generic_manifest(run, "G1", "BLOCKED", now(), checks, artifacts,
                                        [{"gate_id": "G0", "run_id": None, "satisfied": False,
                                          "reason": "synthetic prerequisite is intentionally unavailable"}],
                                        ["synthetic T36 fixture intentionally blocks package audit"],
                                        ["synthetic validator fixture only"])
    if state:
        manifest["status"] = state
    statuses = [row["status"] for row in checks]
    manifest["check_summary"] = {"required": sum(bool(row["required"]) for row in checks),
                                 "passed": statuses.count("PASS"), "failed": statuses.count("FAIL"),
                                 "blocked": statuses.count("BLOCKED"), "not_run": statuses.count("NOT_RUN")}
    if manifest["status"] == "FAIL" and not manifest["findings"]:
        manifest["findings"] = ["synthetic T36 failure fixture"]
    if manifest["status"] == "BLOCKED" and not manifest["blockers"]:
        manifest["blockers"] = ["synthetic T36 blocker"]
    manifest["artifacts"] = artifacts
    (run / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


class EvidenceTamperingTests(unittest.TestCase):
    def test_every_mapped_case_node_resolves_to_a_test_definition(self) -> None:
        missing: list[str] = []
        for case_id, case in CASES.items():
            for node in case.test_nodes:
                parts = node.split("::")
                source = ROOT / parts[0]
                if not source.is_file():
                    missing.append(f"{case_id}: missing file {parts[0]}")
                    continue
                tree = ast.parse(source.read_text(), filename=parts[0])
                body = tree.body
                resolved = True
                for name in parts[1:]:
                    definition = next((item for item in body
                                       if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                                       and item.name == name), None)
                    if definition is None:
                        missing.append(f"{case_id}: unresolved node {node} (missing {name})")
                        resolved = False
                        break
                    body = definition.body if isinstance(definition, ast.ClassDef) else []
                if resolved and not parts[-1].startswith("test_"):
                    missing.append(f"{case_id}: mapped node is not a test function: {node}")
        self.assertEqual(missing, [])

    def test_failed_gate_with_string_observation_keeps_valid_fail_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            checks = json.loads((run / "checks.json").read_text())
            failed = next(row for row in checks if row["check_id"] == "T24")
            failed["status"] = "FAIL"
            failed["observed"] = "pytest collection failed; independent report unavailable"
            findings = _gate_failure_findings(checks)
            self.assertEqual(findings, [
                "T24: pytest collection failed; independent report unavailable"
            ])
            (run / "checks.json").write_text(json.dumps(checks, indent=2, sort_keys=True) + "\n")
            _reseal(run, "FAIL")
            manifest = json.loads((run / "manifest.json").read_text())
            manifest["findings"] = findings
            (run / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

            self.assertEqual(validate_evidence(run), [])
            retained = json.loads((run / "checks.json").read_text())
            self.assertEqual(next(row for row in retained if row["check_id"] == "T24")["observed"],
                             "pytest collection failed; independent report unavailable")
            self.assertEqual(json.loads((run / "manifest.json").read_text())["status"], "FAIL")

    def _review_fixture(self, run: Path) -> tuple[dict, dict]:
        artifact_root = run / "review-artifacts" / "logs"
        artifact_root.mkdir(parents=True)
        for name, content in (("stdout.txt", "review reproduction stdout\n"), ("stderr.txt", "")):
            (artifact_root / name).write_text(content)
        artifacts = [
            {"path": f"logs/{name}", "bytes": (artifact_root / name).stat().st_size,
             "sha256": sha256(artifact_root / name), "purpose": "reviewer command log"}
            for name in ("stdout.txt", "stderr.txt")
        ]
        member = {"path": "rimworld_save_inspector/__init__.py", "bytes": 12, "sha256": "a" * 64}
        archives = {
            kind: {"sha256": ("b" if kind == "wheel" else "c") * 64, "bytes": 64,
                   "members_sha256": "d" * 64, "members": [member]}
            for kind in ("wheel", "sdist")
        }
        report = {"candidate": {"commit": "e" * 40, "git_tree_oid": "f" * 40,
                                "tree_sha256": "1" * 64, "input_manifest_sha256": "2" * 64},
                  "archives": [{"kind": kind, **row} for kind, row in archives.items()]}
        review = {
            "schema_version": "1.0", "case_id": "T37", "status": "PASS",
            "reviewer": {"identity": "independent-reviewer", "run_id": "review-001", "independent": True},
            "started_at": "2026-10-16T20:00:00Z", "finished_at": "2026-10-16T20:01:00Z",
            "candidate": dict(report["candidate"]), "archives": json.loads(json.dumps(archives)),
            "requirement_checks": [
                {"requirement_id": f"R{index:02d}", "status": "PASS", "expected": "reviewed",
                 "observed": "reviewed", "evidence_refs": ["logs/stdout.txt"]}
                for index in range(1, 21)
            ],
            "t37_checks": [
                {"check_id": check_id, "status": "PASS", "expected": "reviewed",
                 "observed": "reviewed", "evidence_refs": ["logs/stdout.txt"]}
                for check_id in ("archive_member_review", "prohibited_content_review",
                                 "source_provenance_review", "scalar_expectations_review")
            ],
            "reproductions": [{
                "command_id": "inspect-archive-manifests", "argv": ["python", "review_archives.py"],
                "cwd": "/private/tmp/reviewer", "started_at": "2026-10-16T20:00:10Z",
                "duration_seconds": 0.4, "exit_code": 0, "expected": "no prohibited content",
                "observed": "no prohibited content",
                "stdout": {"path": "logs/stdout.txt", "sha256": artifacts[0]["sha256"]},
                "stderr": {"path": "logs/stderr.txt", "sha256": artifacts[1]["sha256"]}
            }],
            "findings": [], "artifacts": artifacts
        }
        return review, report

    def test_g7_review_receipt_binds_r01_r20_archives_and_real_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            review, report = self._review_fixture(run)
            self.assertEqual(_review_receipt_errors(review, report, run), [])

    def test_g7_review_receipt_rejects_candidate_or_archive_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            review, report = self._review_fixture(run)
            review["candidate"]["tree_sha256"] = "0" * 64
            review["archives"]["sdist"]["members"][0]["sha256"] = "9" * 64
            errors = _review_receipt_errors(review, report, run)
            self.assertTrue(any("exact candidate" in error for error in errors))
            self.assertTrue(any("sdist archive members" in error for error in errors))

    def test_g7_review_receipt_rejects_missing_requirement_or_reproduction_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            review, report = self._review_fixture(run)
            review["requirement_checks"].pop()
            review["reproductions"][0]["stdout"]["sha256"] = "0" * 64
            errors = _review_receipt_errors(review, report, run)
            self.assertTrue(any("R01-R20" in error for error in errors))
            self.assertTrue(any("not bound to an artifact" in error for error in errors))

    def test_g7_missing_prerequisites_block_without_invoking_packager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "G7-blocked"
            with patch("tools.verify._latest_accepted_run", return_value=(None, False)):
                self.assertEqual(run_g7_gate(None, run), 2)
            self.assertEqual(validate_evidence(run), [])
            manifest = json.loads((run / "manifest.json").read_text())
            checks = json.loads((run / "checks.json").read_text())
            self.assertEqual(manifest["status"], "BLOCKED")
            self.assertEqual({row["check_id"] for row in checks}, {"T33", "T36", "T37"})
            self.assertTrue(all(row["status"] == "BLOCKED" for row in checks))
            self.assertEqual(json.loads((run / "commands.json").read_text()), [])
            self.assertIn("not invoked", (run / "producer.stdout.txt").read_text())

    def test_public_only_g0_blocks_private_rows_and_validate_evidence_cli_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "G0-public-only"
            code = run_gate("G0", None, run, public_only=True)
            self.assertEqual(code, 2)
            self.assertEqual(validate_evidence(run), [])
            manifest = json.loads((run / "manifest.json").read_text())
            self.assertTrue(_prerequisite_evidence_satisfied("G0", "G1", manifest, run))
            self.assertFalse(_prerequisite_evidence_satisfied("G0", "G2", manifest, run),
                             "public-only G0 is applicable only to the G1 public setup prerequisite")
            checks = json.loads((run / "checks.json").read_text())
            private_rows = [row for row in checks if row["check_id"].startswith(("F", "D")) or row["check_id"] == "source_preservation"]
            self.assertTrue(private_rows)
            self.assertTrue(all(row["status"] == "BLOCKED" for row in private_rows))
            result = subprocess.run([sys.executable, str(ROOT / "tools/verify.py"), "--validate-evidence", str(run)],
                                    cwd=ROOT, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("evidence valid", result.stdout)

    def test_complete_synthetic_manifest_is_accepted_as_validator_test_vector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(validate_evidence(_make_run(Path(directory))), [])

    def test_missing_required_case_is_rejected_even_when_status_says_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            checks = json.loads((run / "checks.json").read_text())
            checks.pop()
            (run / "checks.json").write_text(json.dumps(checks))
            _reseal(run)
            self.assertTrue(any("must occur exactly once" in error for error in validate_evidence(run)))

    def test_duplicate_required_case_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            checks = json.loads((run / "checks.json").read_text())
            checks.append(dict(checks[0]))
            (run / "checks.json").write_text(json.dumps(checks))
            _reseal(run)
            self.assertTrue(any("duplicate check IDs" in error for error in validate_evidence(run)))

    def test_skipped_required_test_cannot_claim_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            checks = json.loads((run / "checks.json").read_text())
            for row in checks:
                if isinstance(row["observed"], dict):
                    row["observed"]["skipped_required_tests"] = 1
                    if row["check_id"] == "T24":
                        row["observed"]["case_skipped_test_count"] = 1
                        row["observed"]["case_test_results"][0]["status"] = "SKIPPED"
            (run / "checks.json").write_text(json.dumps(checks))
            (run / "pytest.junit.xml").write_text('<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase classname="contract" name="test_contract"><skipped /></testcase></testsuite>')
            _reseal(run)
            self.assertTrue(any("claims PASS" in error for error in validate_evidence(run)))

    def test_zero_collected_test_count_cannot_claim_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            checks = json.loads((run / "checks.json").read_text())
            for row in checks:
                if isinstance(row["observed"], dict):
                    row["observed"]["collected_test_cases"] = 0
            (run / "checks.json").write_text(json.dumps(checks))
            (run / "pytest.junit.xml").write_text('<testsuite tests="0" failures="0" errors="0" skipped="0" />')
            _reseal(run)
            self.assertTrue(any("claims PASS" in error for error in validate_evidence(run)))

    def test_wrong_revision_hash_and_invalid_utc_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            manifest = json.loads((run / "manifest.json").read_text())
            manifest["spec"]["sha256"] = "0" * 64
            manifest["started_at"] = "2026-10-16T12:00:00"
            (run / "manifest.json").write_text(json.dumps(manifest))
            errors = validate_evidence(run)
            self.assertTrue(any("spec hash" in error for error in errors))
            self.assertTrue(any("UTC" in error or "date-time" in error for error in errors))

    def test_wrong_revision_is_rejected_even_with_the_current_spec_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            manifest = json.loads((run / "manifest.json").read_text())
            manifest["spec"]["revision"] = "2026-10-15.9"
            (run / "manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(any("identity or revision" in error for error in validate_evidence(run)))

    def test_changed_candidate_input_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            manifest = json.loads((run / "manifest.json").read_text())
            manifest["candidate"]["inputs"][0]["sha256"] = "0" * 64
            (run / "manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(any("candidate input hash mismatch" in error for error in validate_evidence(run)))

    def test_schema_rejects_unknown_manifest_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            manifest = json.loads((run / "manifest.json").read_text())
            manifest["invented_status"] = "PASS"
            (run / "manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(any("Additional properties" in error for error in validate_evidence(run)))

    def test_fail_state_without_failed_required_check_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            manifest = json.loads((run / "manifest.json").read_text())
            manifest["status"] = "FAIL"
            manifest["findings"] = ["synthetic failure"]
            (run / "manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(any("FAIL has no failed check" in error for error in validate_evidence(run)))

    def test_nonzero_command_exit_and_missing_output_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            commands = json.loads((run / "commands.json").read_text())
            commands[0]["exit_code"] = 1
            commands[0]["stdout_path"] = "missing.stdout.txt"
            (run / "commands.json").write_text(json.dumps(commands))
            _reseal(run)
            errors = validate_evidence(run)
            self.assertTrue(any("nonzero command exit" in error for error in errors))
            self.assertTrue(any("stdout_path missing" in error for error in errors))

    def test_artifact_hash_and_escaping_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _make_run(root)
            sidecars = json.loads((run / "artifacts.json").read_text())
            sidecars[0]["sha256"] = "0" * 64
            (run / "artifacts.json").write_text(json.dumps(sidecars))
            (run / "escape.txt").symlink_to(root / "outside.txt")
            (root / "outside.txt").write_text("outside")
            sidecars.append({"path": "escape.txt", "bytes": 7, "sha256": "0" * 64, "purpose": "escape"})
            (run / "artifacts.json").write_text(json.dumps(sidecars))
            errors = validate_evidence(run)
            self.assertTrue(any("artifact hash/size mismatch" in error for error in errors))
            self.assertTrue(any("escapes run directory" in error for error in errors))

    def test_pass_with_unsatisfied_prerequisite_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            manifest = json.loads((run / "manifest.json").read_text())
            manifest["prerequisites"] = [{"gate_id": "G0", "run_id": None, "satisfied": False, "reason": "not run"}]
            manifest["status"] = "PASS"
            (run / "manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(any("unsatisfied prerequisite" in error for error in validate_evidence(run)))

    def test_package_pass_without_authenticated_machine_report_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            checks = json.loads((run / "checks.json").read_text())
            t33 = next(row for row in checks if row["check_id"] == "T33")
            mapped = CASES["T33"].test_nodes
            t33["status"] = "PASS"
            t33["expected"] = {"case_coverage_complete": True, "pytest_exit_code": 0,
                               "minimum_collected_test_cases": 1, "maximum_skipped_required_tests": 0}
            t33["observed"] = {"case_coverage_complete": True, "mapped_test_nodes": len(mapped),
                               "pytest_exit_code": 0, "collected_test_cases": 3,
                               "skipped_required_tests": 0, "test_result_path": "pytest.junit.xml"}
            t33["evidence_paths"] = ["pytest.stdout.txt", "pytest.stderr.txt", "pytest.junit.xml"]
            commands = json.loads((run / "commands.json").read_text())
            commands[0]["argv"].extend(mapped)
            (run / "commands.json").write_text(json.dumps(commands))
            (run / "checks.json").write_text(json.dumps(checks))
            _reseal(run, "FAIL")
            errors = validate_evidence(run)
            self.assertTrue(any("machine-readable packaging report" in error for error in errors))

    def test_packaging_report_marker_accepts_quiet_pytest_progress_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "evidence" / "packaging-001"
            report_dir.mkdir(parents=True)
            payload = report_dir / "artifact.whl"
            payload.write_bytes(b"synthetic wheel evidence")
            report = {"artifacts": [{"path": payload.name, "bytes": payload.stat().st_size,
                                     "sha256": sha256(payload)}], "status": "PASS"}
            report_path = report_dir / "report.json"
            report_path.write_text(json.dumps(report))
            run = root / "G1" / "run-001"
            run.mkdir(parents=True)
            stdout = run / "pytest.stdout.txt"
            stdout.write_text("....PACKAGING_REPORT=evidence/packaging-001/report.json\n")

            with patch("tools.verify.ROOT", root):
                copied, relative = _copy_packaging_report(run, stdout)

            self.assertEqual(relative, "package-report.json")
            self.assertEqual(copied["status"], "PASS")
            copied_artifact = run / copied["artifacts"][0]["path"]
            self.assertEqual(copied_artifact.read_bytes(), b"synthetic wheel evidence")

    def test_packaging_report_marker_rejects_duplicate_ambiguous_and_escaping_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "G1" / "run-001"
            run.mkdir(parents=True)
            stdout = run / "pytest.stdout.txt"
            bad_logs = (
                "PACKAGING_REPORT=evidence/one/report.json\nPACKAGING_REPORT=evidence/two/report.json\n",
                "pytest output PACKAGING_REPORT=evidence/one/report.json\n",
                "PACKAGING_REPORT=../outside.json\n",
                "PACKAGING_REPORT=/tmp/outside.json\n",
            )
            with patch("tools.verify.ROOT", root):
                for contents in bad_logs:
                    with self.subTest(contents=contents):
                        stdout.write_text(contents)
                        copied, relative = _copy_packaging_report(run, stdout)
                        self.assertIsNone(copied)
                        self.assertIsNone(relative)
                        self.assertFalse((run / "package-dist").exists())

    def test_packaging_case_mapping_matches_t33_rows_and_keeps_t37_audit_separate(self) -> None:
        t33_ids = _expected_packaging_check_ids()
        report = {"status": "PASS", "case_ids": ["T33", "T37"], "cases": [
            *({"case_id": "T33", "check_id": check_id, "status": "PASS"} for check_id in sorted(t33_ids)),
            {"case_id": "T37", "check_id": "audit-scanner-probes", "status": "PASS",
             "observed": {"unsafe_path": True, "credential_marker": True, "private_xml_marker": True,
                          "synthetic_design_exempt": True}},
            {"case_id": "T37", "check_id": "audit:.whl", "status": "PASS",
             "observed": {"forbidden_names": [], "forbidden_content": []}},
            {"case_id": "T37", "check_id": "audit:.gz", "status": "PASS",
             "observed": {"forbidden_names": [], "forbidden_content": []}},
        ]}
        self.assertTrue(_package_report_case_is_complete(report, "T33"))
        self.assertTrue(_package_report_case_is_complete(report, "T37"))
        self.assertNotIn("audit:.whl", t33_ids)
        self.assertNotIn("audit:.gz", t33_ids)

        with self.subTest("missing true-required row"):
            missing = json.loads(json.dumps(report))
            missing["cases"] = [row for row in missing["cases"]
                                 if not (row.get("case_id") == "T33" and
                                         row.get("check_id") == "cli-domain:wheel-cli-only:summary:0")]
            self.assertFalse(_package_report_case_is_complete(missing, "T33"))
        with self.subTest("failed true-required row"):
            failed = json.loads(json.dumps(report))
            next(row for row in failed["cases"] if row.get("case_id") == "T33" and
                 row.get("check_id") == "build")["status"] = "FAIL"
            self.assertFalse(_package_report_case_is_complete(failed, "T33"))
        with self.subTest("duplicate true-required row"):
            duplicate = json.loads(json.dumps(report))
            duplicate["cases"].append({"case_id": "T33", "check_id": "build", "status": "PASS"})
            self.assertFalse(_package_report_case_is_complete(duplicate, "T33"))
        with self.subTest("audit rows cannot migrate into T33"):
            misplaced = json.loads(json.dumps(report))
            misplaced["cases"].append({"case_id": "T33", "check_id": "audit:.whl", "status": "PASS"})
            self.assertFalse(_package_report_case_is_complete(misplaced, "T33"))

    def test_performance_evidence_uses_scoped_explicit_report_and_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            source_run = root / "evidence" / "performance-formal-006"
            source_run.mkdir(parents=True)
            for number in range(1, 4):
                for suffix in ("stdout", "stderr", "exitcode"):
                    (source_run / f"run-{number}.{suffix}").write_text(f"006-{number}-{suffix}\n")
            default_report = root / "briefs" / "performance-report.md"
            explicit_report = root / "briefs" / "formal-006.md"
            default_report.parent.mkdir()
            default_report.write_text("historical formal-004 report\n")
            explicit_report.write_text("formal-006 strict result\n")
            run = root / "G6" / "run-001"
            run.mkdir(parents=True)

            with patch("tools.verify.ROOT", root), patch.dict(os.environ, {
                    "RWSI_PERFORMANCE_RUN": "evidence/performance-formal-006",
                    "RWSI_PERFORMANCE_REPORT": "briefs/formal-006.md"}):
                evidence_dir, report_path = _copy_performance_evidence(run)

            self.assertEqual(evidence_dir, "performance-formal")
            self.assertEqual(report_path, "performance-report.md")
            self.assertEqual((run / report_path).read_text(), "formal-006 strict result\n")
            self.assertEqual((run / evidence_dir / "run-1.stdout").read_text(), "006-1-stdout\n")

            escaped_run = root / "G6" / "run-002"
            escaped_run.mkdir()
            escaped_report = Path(outside) / "performance.md"
            escaped_report.write_text("outside root\n")
            with patch("tools.verify.ROOT", root), patch.dict(os.environ, {
                    "RWSI_PERFORMANCE_RUN": "evidence/performance-formal-006",
                    "RWSI_PERFORMANCE_REPORT": str(escaped_report)}):
                evidence_dir, report_path = _copy_performance_evidence(escaped_run)
            self.assertIsNone(evidence_dir)
            self.assertIsNone(report_path)
            self.assertFalse((escaped_run / "performance-formal").exists())

    def test_current_performance_report_authenticates_formal006_and_records_strict_failure(self) -> None:
        report = (ROOT / "briefs/performance-report.md").read_text()
        hashes = report.split("Provenance hashes:", 1)[-1]
        for relative in ("src/rimworld_save_inspector/parser/core.py",
                         "src/rimworld_save_inspector/service.py",
                         "tools/performance/benchmark_core.py"):
            with self.subTest(path=relative):
                self.assertIn(f"{relative} `{sha256(ROOT / relative)}`", hashes)
        self.assertIn("evidence/performance-formal-006", report)
        self.assertIn("Formal-006 strict result: FAIL", report)
        self.assertIn("527,627,375 ns", report)
        self.assertIn("250,000,000 ns", report)


if __name__ == "__main__":
    unittest.main()
