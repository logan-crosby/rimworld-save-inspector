"""T36 adversarial checks for the evidence validator itself.

The synthetic PASS below is a schema-validator test vector, not gate evidence
for a product run. Every mutation re-hashes ordinary files where possible so
the assertion targets semantic checks as well as simple checksum detection.
"""

from __future__ import annotations

import json
import ast
import base64
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import tarfile
import unittest
import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.gates.cases import CASES  # noqa: E402
from tools.verify import (_artifact, _copy_performance_evidence, _evidence_shape_errors,
                          _g6_performance_report_binding_errors, _expected_packaging_check_ids,
                          _generic_manifest, _release_identity, _validate_generic_evidence,
                          _packaged_candidate_report_errors, _t20_report_errors,
                          _package_report_case_is_complete, _prerequisite_evidence_satisfied,
                          _copy_packaging_report, _gate_failure_findings, _junit_testcase_matches,
                          _package_archive_errors, _producer_command_log_errors,
                          _review_receipt_errors, now, run_gate, run_g7_gate,
                          validate_evidence, sha256, write_g0_evidence)  # noqa: E402


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
    def test_source_excluded_workspace_uses_hash_authenticated_installed_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            (workspace / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
            environment = Path(directory) / "installed-env"
            site = environment / "lib/python3.14/site-packages"
            module = site / "rimworld_save_inspector"
            core = module / "parser/core.py"
            core.parent.mkdir(parents=True)
            package = module / "__init__.py"
            package.write_text('__version__ = "1.0.0"\n')
            core.write_text('PARSER_PROFILE = "rimworld-1.6/v1"\n')

            class RecordedPath(str):
                def __new__(cls, value: str, path: Path):
                    instance = str.__new__(cls, value)
                    instance.hash = SimpleNamespace(mode="sha256", value=base64.urlsafe_b64encode(
                        hashlib.sha256(path.read_bytes()).digest()).rstrip(b"=").decode())
                    instance.size = path.stat().st_size
                    return instance

            def records() -> list[RecordedPath]:
                return [RecordedPath("rimworld_save_inspector/__init__.py", package),
                        RecordedPath("rimworld_save_inspector/parser/core.py", core)]

            distribution = SimpleNamespace(version="1.0.0", files=records(),
                                           locate_file=lambda item: site / str(item))
            with patch("tools.verify.ROOT", workspace), patch("tools.verify.sys.prefix", str(environment)), \
                    patch("importlib.metadata.distribution", return_value=distribution):
                self.assertFalse((workspace / "src").exists())
                self.assertEqual(_release_identity(), ("1.0.0", "1.0.0", "rimworld-1.6/v1"))
                with patch("importlib.metadata.distribution", side_effect=importlib.metadata.PackageNotFoundError):
                    self.assertEqual(_release_identity(), ("1.0.0", None, None))
                manifest = _generic_manifest(workspace / "G1-test", "G1", "BLOCKED", now(), [], [], [], [], [])
                self.assertFalse(any("candidate package/parser/profile version" in error for error in
                                     _evidence_shape_errors(manifest, [], [], [])))
                core.write_text('PARSER_PROFILE = "wrong-profile"\n')
                self.assertNotEqual(_release_identity(), ("1.0.0", "1.0.0", "rimworld-1.6/v1"),
                                    "changed installed bytes must fail their wheel RECORD hash")
                distribution.files = records()
                self.assertTrue(any("candidate package/parser/profile version" in error for error in
                                    _evidence_shape_errors(manifest, [], [], [])))
                core.write_text('PARSER_PROFILE = "rimworld-1.6/v1"\n')
                distribution.files = records()
                distribution.version = "0.1.0"
                self.assertTrue(any("candidate package/parser/profile version" in error for error in
                                    _evidence_shape_errors(manifest, [], [], [])))

    def test_g0_manifest_records_declared_v1_package_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "G0-public-only"
            unavailable = SimpleNamespace(fixture=None, ready=False, errors=["synthetic private input unavailable"],
                                          oracle=None, expected_bytes=None, expected_sha256=None,
                                          input_hashes=lambda: {})
            with patch("tools.verify.load_private_inputs", return_value=unavailable):
                write_g0_evidence(run, fixture=None, actor="test:version-identity")
            manifest = json.loads((run / "manifest.json").read_text())
            self.assertEqual(manifest["candidate"]["package_version"], "1.0.0")
            self.assertIsNone(manifest["candidate"]["parser_version"])
            self.assertIsNone(manifest["candidate"]["profile_version"])
            self.assertIn("pyproject.toml", {row["path"] for row in manifest["candidate"]["inputs"]})

    def test_generated_candidate_versions_match_v1_metadata_and_reject_stale_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            manifest = _generic_manifest(run, "G1", "BLOCKED", now(), [], [], [], [], [])
            candidate = manifest["candidate"]
            self.assertEqual((candidate["package_version"], candidate["parser_version"], candidate["profile_version"]),
                             ("1.0.0", "1.0.0", "rimworld-1.6/v1"))
            self.assertFalse(any("candidate package/parser/profile version" in error for error in
                                 _evidence_shape_errors(manifest, [], [], [])))
            candidate["package_version"] = "0.1.0"
            self.assertTrue(any("candidate package/parser/profile version" in error for error in
                                _evidence_shape_errors(manifest, [], [], [])))
            candidate["package_version"] = "1.0.0"
            candidate["parser_version"] = None
            self.assertTrue(any("candidate package/parser/profile version" in error for error in
                                _evidence_shape_errors(manifest, [], [], [])))

    def test_t20_runtime_package_identity_accepts_v1_and_rejects_stale_or_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            audit = run / "mcp-audit"
            audit.mkdir()
            observations = {}
            for mode in ("cli", "mcp"):
                observations[mode] = {"mode": mode, "error": None, "queries_completed": 1,
                                      "writes_under_save_root": [], "network_attempts": [],
                                      "source_hash_before": "a" * 64, "source_hash_after": "a" * 64,
                                      "mode_before": 0o444, "mode_after": 0o444,
                                      "runtime_package": {"version": "1.0.0",
                                                          "file": str(ROOT / "src/rimworld_save_inspector/__init__.py")}}
            observations["probe"] = {"writes_under_save_root": [{"kind": "write-open"}],
                                      "network_attempts": [{"event": "socket.getaddrinfo"}]}
            report = {"schema_version": 1, "case": "T20", "result": "pass",
                      "expected": {"network_attempts": [], "probe_observer_detects_write_and_network": True,
                                   "source_hash_preserved": True, "source_mode_preserved": True,
                                   "writes_under_save_root": []},
                      "observed": observations, "report_paths": ["cli.json", "mcp.json", "probe.json"]}
            for mode in ("cli", "mcp", "probe"):
                (audit / f"{mode}.json").write_text(json.dumps(observations[mode]))
            self.assertEqual(_t20_report_errors(run, report), [])
            observations["cli"]["runtime_package"]["version"] = "0.1.0"
            (audit / "cli.json").write_text(json.dumps(observations["cli"]))
            self.assertTrue(any("T20 cli runtime package identity" in error for error in
                                _t20_report_errors(run, report)))
            observations["cli"]["runtime_package"].pop("version")
            (audit / "cli.json").write_text(json.dumps(observations["cli"]))
            self.assertTrue(any("T20 cli runtime package identity" in error for error in
                                _t20_report_errors(run, report)))

    def test_g7_package_parser_and_installed_versions_accept_v1_not_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            report = {"schema_version": "1.0", "case_ids": ["T33", "T36", "T37"],
                      "status": "PASS", "candidate": {"package_version": "1.0.0",
                                                       "parser_version": "1.0.0",
                                                       "profile_version": "rimworld-1.6/v1"},
                      "install": {"installed_version": "1.0.0"}}
            def version_errors() -> list[str]:
                return [error for error in _packaged_candidate_report_errors(report, run)
                        if "versions differ" in error or "installed package version" in error]
            self.assertEqual(version_errors(), [])
            report["candidate"]["package_version"] = "0.1.0"
            report["install"]["installed_version"] = "0.1.0"
            self.assertTrue(any("versions differ" in error for error in version_errors()))
            self.assertTrue(any("installed package version" in error for error in version_errors()))
            report["candidate"]["package_version"] = "1.0.0"
            report["install"]["installed_version"] = None
            self.assertTrue(any("installed package version" in error for error in version_errors()))
            report["install"]["installed_version"] = "1.0.0"
            report["candidate"]["parser_version"] = "0.1.0"
            self.assertTrue(any("versions differ" in error for error in version_errors()))
            report["candidate"]["parser_version"] = "1.0.0"
            report["candidate"]["profile_version"] = None
            self.assertTrue(any("versions differ" in error for error in version_errors()))
            report["candidate"].update({"package_version": "2.0.0", "parser_version": "2.0.0",
                                         "profile_version": "rimworld-1.6/v1"})
            report["install"]["installed_version"] = "2.0.0"
            with patch("tools.verify._release_identity", return_value=("2.0.0", "2.0.0", "rimworld-1.6/v1")):
                self.assertTrue(any("versions differ" in error for error in version_errors()))
                self.assertTrue(any("installed package version" in error for error in version_errors()))

    def test_rich_package_members_authenticate_exact_archive_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            wheel = run / "sample.whl"
            sdist = run / "sample.tar.gz"
            payloads = {"sample/__init__.py": b"version = 1\n", "sample/module.py": b"value = 2\n"}
            with zipfile.ZipFile(wheel, "w") as archive:
                for name, payload in payloads.items():
                    archive.writestr(name, payload)
            with tarfile.open(sdist, "w:gz") as archive:
                for name, payload in payloads.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    archive.addfile(info, BytesIO(payload))
            members = [{"path": name, "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()}
                       for name, payload in sorted(payloads.items())]
            entries = [{"path": path.name, "bytes": path.stat().st_size, "sha256": sha256(path),
                        "members": json.loads(json.dumps(members))} for path in (wheel, sdist)]
            self.assertEqual(_package_archive_errors(run, {"artifacts": entries}), [])

            for mutation in ("tampered", "missing", "duplicate", "misclassified"):
                with self.subTest(mutation=mutation):
                    changed = json.loads(json.dumps(entries))
                    rows = changed[0]["members"]
                    if mutation == "tampered":
                        rows[0]["sha256"] = "0" * 64
                    elif mutation == "missing":
                        rows.pop()
                    elif mutation == "duplicate":
                        rows.append(dict(rows[0]))
                    else:
                        rows[0]["path"] = "sample/wrong.py"
                    self.assertTrue(_package_archive_errors(run, {"artifacts": changed}))

    def test_producer_command_logs_require_unique_paths_and_matching_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            commands_dir = run / "packaged-candidate" / "commands"
            commands_dir.mkdir(parents=True)
            outputs = {}
            for command_id in ("first", "second"):
                for stream in ("stdout", "stderr"):
                    relative = f"commands/{command_id}.{stream}.txt"
                    path = commands_dir / f"{command_id}.{stream}.txt"
                    path.write_text(f"{command_id} {stream}\n")
                    outputs[relative] = {"path": relative, "bytes": path.stat().st_size,
                                         "sha256": sha256(path)}
            commands = [{"command_id": command_id, "exit_code": 0, "timed_out": False,
                         **{f"{stream}_path": f"commands/{command_id}.{stream}.txt" for stream in ("stdout", "stderr")},
                         **{f"{stream}_sha256": outputs[f"commands/{command_id}.{stream}.txt"]["sha256"]
                            for stream in ("stdout", "stderr")}}
                        for command_id in ("first", "second")]
            self.assertEqual(_producer_command_log_errors(commands, outputs, run), [])

            for mutation in ("missing", "tampered", "duplicate-id", "reused-path", "misclassified-stream"):
                with self.subTest(mutation=mutation):
                    changed = json.loads(json.dumps(commands))
                    index = json.loads(json.dumps(outputs))
                    if mutation == "missing":
                        index.pop(changed[0]["stdout_path"])
                    elif mutation == "tampered":
                        changed[0]["stdout_sha256"] = "0" * 64
                    elif mutation == "duplicate-id":
                        changed[1]["command_id"] = changed[0]["command_id"]
                    elif mutation == "reused-path":
                        changed[1]["stdout_path"] = changed[0]["stdout_path"]
                        changed[1]["stdout_sha256"] = changed[0]["stdout_sha256"]
                    else:
                        changed[0]["stderr_path"] = changed[0]["stdout_path"]
                        changed[0]["stderr_sha256"] = changed[0]["stdout_sha256"]
                    self.assertTrue(_producer_command_log_errors(changed, index, run))

    def test_junit_mapping_uses_exact_module_class_and_function_identity(self) -> None:
        # This classname/name pair is retained verbatim from the failed G2 run's
        # JUnit record for the corrected T16 mapped class method.
        t16_node = ("tests/unit/test_core_contract.py::ReferenceAndPagingTests::"
                    "test_deadline_is_checked_at_eof_for_short_documents")
        actual_class_case = {
            "classname": "tests.unit.test_core_contract.ReferenceAndPagingTests",
            "name": "test_deadline_is_checked_at_eof_for_short_documents",
        }
        self.assertTrue(_junit_testcase_matches(t16_node, actual_class_case))
        self.assertFalse(_junit_testcase_matches(t16_node, {
            **actual_class_case, "classname": "tests.unit.test_core_contract.OtherTests"
        }))
        self.assertFalse(_junit_testcase_matches(t16_node, {
            **actual_class_case,
            "classname": "tests.unit.test_core_contract_shadow.ReferenceAndPagingTests",
        }))
        g7_node = ("tests/integration/test_private_equivalence.py::PrivateEquivalenceTests::"
                   "test_product_matches_frozen_f01_f28_and_dynamic_d01_d03")
        self.assertTrue(_junit_testcase_matches(g7_node, {
            "classname": "tests.integration.test_private_equivalence.PrivateEquivalenceTests",
            "name": "test_product_matches_frozen_f01_f28_and_dynamic_d01_d03",
        }))
        self.assertFalse(_junit_testcase_matches(g7_node, {
            "classname": "tests.integration.test_private_equivalence.OtherTests",
            "name": "test_product_matches_frozen_f01_f28_and_dynamic_d01_d03",
        }))

        function_node = ("tests/contract/test_cli_schema.py::"
                         "test_handwritten_contract_examples_validate_and_reject_unknown_fields")
        self.assertTrue(_junit_testcase_matches(function_node, {
            "classname": "tests.contract.test_cli_schema",
            "name": "test_handwritten_contract_examples_validate_and_reject_unknown_fields",
        }))
        self.assertFalse(_junit_testcase_matches(function_node, {
            "classname": "tests.contract.test_cli_schema.ShadowTests",
            "name": "test_handwritten_contract_examples_validate_and_reject_unknown_fields",
        }))

    def test_validator_rejects_junit_case_from_wrong_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _make_run(Path(directory))
            junit_path = run / "pytest.junit.xml"
            junit = junit_path.read_text()
            junit = junit.replace(
                'classname="tests.contract.test_cli_schema"',
                'classname="tests.contract.test_cli_schema_shadow"', 1,
            )
            junit_path.write_text(junit)
            _reseal(run, "BLOCKED")

            errors = validate_evidence(run)
            self.assertTrue(any("JUnit output omits mapped test" in error for error in errors), errors)

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
                  "fixture": {"private_input_hashes": {"anchor_expected": "3" * 64,
                                                        "anchor_observed": "3" * 64,
                                                        "oracle": "4" * 64,
                                                        "baseline_spec": "5" * 64,
                                                        "fixture": "6" * 64}},
                  "archives": [{"kind": kind, **row} for kind, row in archives.items()]}
        review = {
            "schema_version": "1.0", "case_id": "T37", "status": "PASS",
            "reviewer": {"identity": "independent-reviewer", "run_id": "review-001", "independent": True},
            "started_at": "2026-10-16T20:00:00Z", "finished_at": "2026-10-16T20:01:00Z",
            "candidate": dict(report["candidate"]), "archives": json.loads(json.dumps(archives)),
            "private_anchor": {"expected_sha256": "3" * 64, "observed_sha256": "3" * 64},
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

    def _review_errors(self, review: dict, report: dict, run: Path) -> list[str]:
        trusted = report["fixture"]["private_input_hashes"]
        private = SimpleNamespace(ready=True, expected_input_hashes=lambda: trusted)
        with patch("tools.verify.load_private_inputs", return_value=private):
            return _review_receipt_errors(review, report, run)

    def test_g7_review_receipt_binds_r01_r20_archives_and_real_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            review, report = self._review_fixture(run)
            self.assertEqual(self._review_errors(review, report, run), [])

    def test_g7_review_receipt_rejects_candidate_or_archive_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            review, report = self._review_fixture(run)
            review["candidate"]["tree_sha256"] = "0" * 64
            review["archives"]["sdist"]["members"][0]["sha256"] = "9" * 64
            errors = self._review_errors(review, report, run)
            self.assertTrue(any("exact candidate" in error for error in errors))
            self.assertTrue(any("sdist archive members" in error for error in errors))

    def test_g7_review_receipt_rejects_missing_requirement_or_reproduction_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            review, report = self._review_fixture(run)
            review["requirement_checks"].pop()
            review["reproductions"][0]["stdout"]["sha256"] = "0" * 64
            errors = self._review_errors(review, report, run)
            self.assertTrue(any("R01-R20" in error for error in errors))
            self.assertTrue(any("not bound to an artifact" in error for error in errors))

    def test_g7_review_receipt_rejects_anchor_substitution_or_missing_trust(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            review, report = self._review_fixture(run)
            review["private_anchor"]["observed_sha256"] = "7" * 64
            self.assertTrue(any("externally trusted private anchor" in error
                                for error in self._review_errors(review, report, run)))
            review["private_anchor"]["observed_sha256"] = "3" * 64
            missing = SimpleNamespace(ready=False, expected_input_hashes=lambda: None)
            with patch("tools.verify.load_private_inputs", return_value=missing):
                errors = _review_receipt_errors(review, report, run)
            self.assertTrue(any("externally trusted private anchor" in error for error in errors))

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

    def test_g6_selected_report_is_bound_to_retained_copy_without_private_brief(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run = root / "evidence" / "performance-formal-006"
            source_run.mkdir(parents=True)
            selected = source_run / "performance-report.md"
            selected.write_text("selected formal report\n")
            run = root / "G6" / "run-001"
            run.mkdir(parents=True)
            with patch("tools.verify.ROOT", root), patch.dict(os.environ, {
                    "RWSI_PERFORMANCE_RUN": str(source_run),
                    "RWSI_PERFORMANCE_REPORT": str(selected)}):
                evidence_dir, report_path = _copy_performance_evidence(run)
                self.assertEqual((evidence_dir, report_path), ("performance-formal", "performance-report.md"))
                selected.unlink()  # Later validation has only the retained evidence copy.
                manifest = _generic_manifest(run, "G6", "BLOCKED", now(), [], [], [], [], [])
                inputs = manifest["candidate"]["inputs"]
                retained = run / "performance-report.md"
                rows = [row for row in inputs if row["path"] == "performance-report.md"]
                self.assertEqual(rows, [{"path": "performance-report.md", "role": "config",
                                         "sha256": sha256(retained)}])
                self.assertFalse((root / "briefs/performance-report.md").exists())
                indexed = [_artifact(retained, "T35 selected report", run)]
                self.assertEqual(_g6_performance_report_binding_errors(run, inputs, indexed), [])
                self.assertFalse(any("performance-report.md" in error for error in
                                     _evidence_shape_errors(manifest, [], [], indexed)))
                self.assertFalse(any("G6 selected performance report" in error for error in
                                     _validate_generic_evidence(run, manifest, [], [], indexed)))

                self.assertTrue(any("input" in error for error in
                                    _g6_performance_report_binding_errors(run, [], indexed)))
                self.assertTrue(any("exactly one" in error for error in
                                    _g6_performance_report_binding_errors(run, inputs + rows, indexed)))
                self.assertTrue(any("config input" in error for error in
                                    _g6_performance_report_binding_errors(run, [{**rows[0], "role": "test"}], indexed)))
                manifest["candidate"]["inputs"] = [row for row in inputs if row["path"] != "performance-report.md"]
                self.assertTrue(any("G6 selected performance report" in error for error in
                                    _validate_generic_evidence(run, manifest, [], [], indexed)))
                manifest["candidate"]["inputs"] = inputs
                retained.write_text("tampered after copying\n")
                self.assertTrue(any("hash" in error for error in
                                    _g6_performance_report_binding_errors(run, inputs, indexed)))
                self.assertTrue(any("G6 selected performance report" in error for error in
                                    _validate_generic_evidence(run, manifest, [], [], indexed)))
                retained.write_text("selected formal report\n")
                wrong_hash = [{**rows[0], "sha256": "0" * 64}]
                self.assertTrue(any("hash" in error for error in
                                    _g6_performance_report_binding_errors(run, wrong_hash, indexed)))
                self.assertTrue(any("artifact" in error for error in
                                    _g6_performance_report_binding_errors(run, inputs, [])))
                retained.unlink()
                self.assertTrue(any("missing" in error for error in
                                    _g6_performance_report_binding_errors(run, inputs, indexed)))

    def test_t35_rejects_stale_provenance_before_private_fixture_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core_path = root / "src/rimworld_save_inspector/parser/core.py"
            service_path = root / "src/rimworld_save_inspector/service.py"
            benchmark_path = root / "tools/performance/benchmark_core.py"
            for path in (core_path, service_path, benchmark_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"synthetic source: {path.name}\n")
            report_path = root / "performance-report.md"
            report_path.write_text(
                "Provenance hashes:\n"
                "src/rimworld_save_inspector/parser/core.py `" + "0" * 64 + "`\n"
                f"src/rimworld_save_inspector/service.py `{sha256(service_path)}`\n"
                f"tools/performance/benchmark_core.py `{sha256(benchmark_path)}`\n"
            )
            evidence = root / "formal-workers"
            evidence.mkdir()
            self.assertFalse((root / ".private/private-fixture.rws").exists())

            test_file = ROOT / "tests/reliability/test_performance_report.py"
            spec = importlib.util.spec_from_file_location("t36_performance_report_test", test_file)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            test_case = module.PerformanceEvidenceTests()
            with patch.object(module, "ROOT", root), patch.dict(os.environ, {
                    "RWSI_PERFORMANCE_RUN": str(evidence),
                    "RWSI_PERFORMANCE_REPORT": str(report_path)}):
                with self.assertRaisesRegex(AssertionError, "formal timings are stale for changed input") as caught:
                    test_case.test_formal_three_worker_report_matches_current_core_and_thresholds()

            self.assertNotIn("private reference fixture is required", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
