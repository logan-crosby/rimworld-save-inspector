"""Negative checks for the G7 evidence producer's preflight and proof handling.

These tests validate only the orchestration/evidence boundary. They do not run
or simulate product acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools.gates.packaged_candidate import (
    CandidateError,
    Producer,
    _pytest_import_audit_plugin_source,
    _runtime_import_hook_source,
    _copy_verification_inputs,
    audit_scanner_probe,
    audit_package_members,
    _private_candidate_payloads,
    validate_candidate_state,
    validate_fixture,
    validate_import_origin,
    validate_import_records,
    validate_junit,
    validate_suite_proof,
    validate_workspace_manifest,
)
from tools.private_acceptance import load_private_inputs


class PackagedCandidateValidationTests(unittest.TestCase):
    def test_committed_snapshot_copy_keeps_test_package_and_excludes_product_private_state(self) -> None:
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="rwsi-packaged-workspace-unit-") as directory:
            base = Path(directory)
            archive_path = base / "committed.tar"
            with archive_path.open("wb") as stream:
                result = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", "HEAD"],
                                        stdout=stream, stderr=subprocess.PIPE, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            candidate = base / "candidate"
            candidate.mkdir()
            with tarfile.open(archive_path, "r:") as archive:
                members = archive.getmembers()
                self.assertTrue(all(not Path(member.name).is_absolute() and ".." not in Path(member.name).parts
                                    for member in members))
                archive.extractall(candidate)
            workspace = base / "verification-workspace"
            copied = _copy_verification_inputs(candidate, workspace)
            paths = {row["path"] for row in copied}
            self.assertIn("tests/__init__.py", paths)
            self.assertIn("tools/gates/cases.py", paths)
            self.assertIn("tests/fixtures/synthetic/seed.rws", paths)
            self.assertFalse((workspace / "src").exists())
            self.assertFalse((workspace / ".private").exists())
            self.assertFalse((workspace / ".git").exists())
            private = load_private_inputs(check_fixture=False)
            self.assertFalse(any(row["sha256"] == private.expected_sha256 for row in copied))
            self.assertEqual(validate_workspace_manifest(copied), [])
            producer = object.__new__(Producer)
            required_nodes, cases = producer._required_nodes(workspace)
            self.assertIn("tests/__init__.py", paths)
            self.assertTrue(any(node.startswith("tests/verification/test_evidence_t36.py::")
                                for node in required_nodes))
            self.assertTrue(any(case_id.startswith("F") for case_id in cases))

    def test_generated_installed_import_pytest_plugin_compiles(self) -> None:
        source = _pytest_import_audit_plugin_source()
        code = compile(source, "packaged_import_audit.py", "exec")
        self.assertIn("pytest_sessionfinish", code.co_names)

    def test_importlib_runtime_provenance_pth_records_child_import_origin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rwsi-import-hook-") as directory:
            root = Path(directory)
            venv = root / "venv"
            create = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                                    cwd=root, text=True, capture_output=True, check=False)
            self.assertEqual(create.returncode, 0, create.stderr)
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            site_result = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                                         cwd=root, text=True, capture_output=True, check=False)
            self.assertEqual(site_result.returncode, 0, site_result.stderr)
            site = Path(site_result.stdout.strip())
            package = site / "rimworld_save_inspector"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
            (site / "_rwsi_g7_import_provenance.py").write_text(_runtime_import_hook_source(), encoding="utf-8")
            (site / "rwsi_g7_import_provenance.pth").write_text(
                "import _rwsi_g7_import_provenance\n", encoding="utf-8")
            logs = root / "import-logs"
            logs.mkdir()
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            env["RIMWORLD_SAVE_INSPECTOR_IMPORT_LOG_DIR"] = str(logs)
            env["PYTHONNOUSERSITE"] = "1"
            completed = subprocess.run(
                [str(python), "-c", "import importlib; importlib.import_module('rimworld_save_inspector')"],
                cwd=root, env=env, text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            records = list(logs.glob("*.jsonl"))
            self.assertEqual(len(records), 1)
            row = json.loads(records[0].read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["module"], "rimworld_save_inspector")
            self.assertEqual(Path(row["origin"]).resolve(), (package / "__init__.py").resolve())

    def test_missing_smoke_executable_is_recorded_as_nonzero_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rwsi-missing-smoke-command-") as directory:
            producer = object.__new__(Producer)
            producer.out = Path(directory)
            producer.root = Path(directory) / "checkout"
            producer.commands = []
            producer._command_ids = set()
            producer._command_log_paths = set()
            producer.artifacts = []
            completed = producer._run("missing-cli", [str(Path(directory) / "absent-cli"), "--version"],
                                      Path(directory))
            self.assertEqual(completed.returncode, 127)
            self.assertIn("FileNotFoundError", completed.stderr)
            self.assertEqual(producer.commands[0]["exit_code"], 127)
            self.assertTrue((Path(directory) / "commands/missing-cli.stderr.txt").is_file())

    def test_required_and_four_smoke_site_probes_have_unique_authenticated_logs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rwsi-site-probes-") as directory:
            base = Path(directory)
            producer = object.__new__(Producer)
            producer.out = base
            producer.root = base / "checkout"
            producer.work_root = None
            producer.commands = []
            producer._command_ids = set()
            producer._command_log_paths = set()
            producer.artifacts = []
            env = dict(os.environ)
            command_ids = [
                "installed-required-site-packages",
                "t33-wheel-cli-site-packages",
                "t33-wheel-mcp-site-packages",
                "t33-sdist-cli-site-packages",
                "t33-sdist-mcp-site-packages",
            ]
            for command_id in command_ids:
                site = producer._site_packages(Path(sys.executable), env, command_id=command_id)
                self.assertTrue(site.is_absolute())
            self.assertEqual([row["command_id"] for row in producer.commands], command_ids)
            log_paths = [(row["stdout_path"], row["stderr_path"]) for row in producer.commands]
            flattened_paths = [path for pair in log_paths for path in pair]
            self.assertEqual(len(flattened_paths), len(set(flattened_paths)))
            artifacts = {row["path"]: row for row in producer.artifacts}
            self.assertEqual(set(flattened_paths), set(artifacts))
            for command in producer.commands:
                self.assertEqual(command["exit_code"], 0)
                for stream in ("stdout", "stderr"):
                    path = command[f"{stream}_path"]
                    artifact = artifacts[path]
                    payload = (base / path).read_bytes()
                    self.assertEqual(artifact["bytes"], len(payload))
                    self.assertEqual(artifact["sha256"], hashlib.sha256(payload).hexdigest())
                    self.assertEqual(command[f"{stream}_sha256"], artifact["sha256"])

    def test_duplicate_command_id_is_rejected_before_second_subprocess(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rwsi-command-id-guard-") as directory:
            base = Path(directory)
            producer = object.__new__(Producer)
            producer.out = base
            producer.root = base / "checkout"
            producer.commands = []
            producer._command_ids = set()
            producer._command_log_paths = set()
            producer.artifacts = []
            argv = [sys.executable, "-c", "print('captured')"]
            self.assertEqual(producer._run("unique-probe", argv, base).returncode, 0)
            with mock.patch("tools.gates.packaged_candidate.subprocess.run") as subprocess_run:
                with self.assertRaisesRegex(CandidateError, "duplicate command ID"):
                    producer._run("unique-probe", argv, base)
                subprocess_run.assert_not_called()

    def test_command_log_path_collision_is_rejected_before_subprocess(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rwsi-command-log-collision-") as directory:
            base = Path(directory)
            producer = object.__new__(Producer)
            producer.out = base
            producer.root = base / "checkout"
            producer.commands = []
            producer._command_ids = set()
            producer._command_log_paths = set()
            producer.artifacts = []
            commands = base / "commands"
            producer._claim_command_logs(
                "reserved-probe", commands / "new-probe.stdout.txt", commands / "new-probe.stderr.txt")
            with mock.patch("tools.gates.packaged_candidate.subprocess.run") as subprocess_run:
                with self.assertRaisesRegex(CandidateError, "command log path collision"):
                    producer._run("new-probe", [sys.executable, "-c", "print('must not run')"], base)
                subprocess_run.assert_not_called()

    def test_missing_private_fixture_is_explicitly_unavailable(self) -> None:
        fixture = validate_fixture(None)
        self.assertIs(fixture["available"], False)
        self.assertIn("unavailable", fixture["unavailable_reason"])
        self.assertIsNone(fixture["before_sha256"])

    def test_wrong_private_fixture_identity_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong-save.rws"
            path.write_bytes(b"synthetic wrong fixture")
            fixture = validate_fixture(path)
        self.assertIs(fixture["available"], True)
        self.assertIs(fixture["identity_matches"], False)
        self.assertNotEqual(fixture["before_sha256"], fixture["expected_sha256"])

    def test_dirty_candidate_cannot_enter_packaging_acceptance(self) -> None:
        candidate = {"commit": "a" * 40, "tree_sha256": "b" * 64,
                     "input_manifest_sha256": "c" * 64, "lock_sha256": "d" * 64,
                     "dirty": True, "inputs": [{"path": "uv.lock"}]}
        errors = validate_candidate_state(candidate)
        self.assertTrue(any("not a clean committed tree" in error for error in errors))

    def test_candidate_with_missing_input_manifest_is_rejected(self) -> None:
        candidate = {"commit": "a" * 40, "tree_sha256": "b" * 64,
                     "input_manifest_sha256": None, "lock_sha256": "d" * 64,
                     "dirty": False, "inputs": []}
        errors = validate_candidate_state(candidate)
        self.assertIn("candidate proof is missing input_manifest_sha256", errors)
        self.assertIn("candidate proof has no input manifest", errors)

    def test_known_private_full_save_identity_is_rejected_before_archive(self) -> None:
        candidate = {"commit": "a" * 40, "tree_sha256": "b" * 64,
                     "input_manifest_sha256": "c" * 64, "lock_sha256": "d" * 64,
                     "dirty": False,
                     "inputs": [{"path": "tests/data/candidate.rws", "bytes": load_private_inputs(check_fixture=False).expected_bytes,
                                 "sha256": load_private_inputs(check_fixture=False).expected_sha256}],
                     "private_payloads": ["tests/data/candidate.rws:frozen-private-fixture-identity"]}
        errors = validate_candidate_state(candidate)
        self.assertTrue(any("private save/export material" in error for error in errors))

    def test_public_synthetic_save_and_scalar_evidence_paths_are_allowed(self) -> None:
        candidate = {"commit": "a" * 40, "tree_sha256": "b" * 64,
                     "input_manifest_sha256": "c" * 64, "lock_sha256": "d" * 64,
                     "dirty": False,
                     "inputs": [{"path": "tests/fixtures/syntheticseed.rws", "bytes": 2048, "sha256": "d" * 64},
                                {"path": "evidence/G0/scalar-receipt.json", "bytes": 512, "sha256": "e" * 64}],
                     "private_payloads": []}
        self.assertEqual(validate_candidate_state(candidate), [])

    def test_private_storage_tree_is_detected_but_public_seed_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / ".private" / "private-fixture.rws"
            private.parent.mkdir()
            private.write_bytes(b"private")
            public = root / "tests/fixtures/synthetic/seed.rws"
            public.parent.mkdir(parents=True)
            public.write_text("<savegame><synthetic>true</synthetic></savegame>", encoding="utf-8")
            rows = [
                {"path": ".private/private-fixture.rws", "bytes": private.stat().st_size, "sha256": "f" * 64},
                {"path": "tests/fixtures/synthetic/seed.rws", "bytes": public.stat().st_size,
                 "sha256": "e" * 64},
            ]
            findings = _private_candidate_payloads(root, rows)
        self.assertEqual(findings, [".private/private-fixture.rws:private-storage-tree"])

    def test_frozen_oracle_and_anchor_cannot_be_hidden_under_public_names(self) -> None:
        private = load_private_inputs(check_fixture=False)
        if not private.ready:
            self.skipTest("frozen private inputs unavailable")
        rows = [{"path": "docs/renamed-case-data.json", "bytes": private.oracle_path.stat().st_size,
                 "sha256": private.anchor["oracle_sha256"]},
                {"path": "docs/renamed-inputs.json", "bytes": private.anchor_path.stat().st_size,
                 "sha256": hashlib.sha256(private.anchor_path.read_bytes()).hexdigest()}]
        findings = _private_candidate_payloads(Path(__file__).resolve().parents[2], rows)
        self.assertEqual(len(findings), 2)
        self.assertTrue(any("frozen-private-oracle-identity" in item for item in findings))
        self.assertTrue(any("frozen-private-anchor-identity" in item for item in findings))

    def test_wrong_import_origin_fails_source_shadow_and_environment_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            site = base / "venv" / "site-packages"
            source = base / "checkout"
            wrong = source / "src/rimworld_save_inspector/__init__.py"
            errors = validate_import_origin(str(wrong), site, source)
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("outside the fresh environment" in item for item in errors))
        self.assertTrue(any("shadowed" in item for item in errors))

    def test_absent_or_wrong_child_import_provenance_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            site = base / "venv" / "site-packages"
            source = base / "checkout"
            self.assertTrue(validate_import_records([], site, source))
            wrong = source / "src/rimworld_save_inspector/cli.py"
            self.assertTrue(validate_import_records([{"origin": str(wrong)}], site, source))

    def test_forbidden_copy_paths_are_rejected(self) -> None:
        errors = validate_workspace_manifest([
            {"path": "tests/test_ok.py"},
            {"path": "src/rimworld_save_inspector/__init__.py"},
            {"path": "tests/../.private/private-fixture.rws"},
        ])
        self.assertGreaterEqual(len(errors), 3)

    def test_archive_scanner_probe_detects_adversarial_markers(self) -> None:
        probe = audit_scanner_probe()
        self.assertIs(probe["passed"], True)
        self.assertIs(probe["unsafe_path_detected"], True)
        self.assertIs(probe["credential_detected"], True)
        self.assertIs(probe["private_xml_label_detected"], True)

    def test_only_authenticated_public_sample_save_is_allowed_in_archives(self) -> None:
        root = Path(__file__).resolve().parents[2]
        public = (root / "examples/minimal_colony.rws").read_bytes()
        clean = audit_package_members(Path("candidate.tar.gz"), {"candidate/examples/minimal_colony.rws": public})
        self.assertEqual(clean, {"forbidden_names": [], "forbidden_content": []})
        for path, content in (("candidate/examples/minimal_colony.rws", public + b" "),
                              ("candidate/examples/other.rws", public),
                              ("candidate/private.xml", public),
                              ("candidate/data.json", b'{"fixture":{},"assertions":[{"expected":1}]}')):
            report = audit_package_members(Path("candidate.tar.gz"), {path: content})
            self.assertTrue(report["forbidden_names"] or report["forbidden_content"])

    def test_absent_junit_proof_cannot_pass_required_nodes(self) -> None:
        required = ["tests/unit/test_sample.py::SampleTests::test_expected"]
        with tempfile.TemporaryDirectory() as directory:
            proof = validate_junit(Path(directory) / "absent.junit.xml", required)
        errors = validate_suite_proof(proof, required, 0)
        self.assertTrue(any("ABSENT" in error for error in errors))

    def test_file_level_required_suite_proof_rejects_a_skipped_parameterized_case(self) -> None:
        node = "tests/contract/test_quantity_filter_normalization.py"
        with tempfile.TemporaryDirectory() as directory:
            junit = Path(directory) / "required.junit.xml"
            junit.write_text('<testsuite tests="2" failures="0" errors="0" skipped="1">'
                             '<testcase classname="tests.contract.test_quantity_filter_normalization" name="test_case[a]"/>'
                             '<testcase classname="tests.contract.test_quantity_filter_normalization" name="test_case[b]">'
                             '<skipped/></testcase></testsuite>')
            proof = validate_junit(junit, [node])
            self.assertEqual(proof["node_results"][0]["status"], "SKIPPED")
            self.assertTrue(validate_suite_proof(proof, [node], 0))

    def test_junit_module_prefix_cannot_substitute_for_exact_required_node(self) -> None:
        required = ["tests/unit/test_sample.py::SampleTests::test_expected"]
        with tempfile.TemporaryDirectory() as directory:
            junit = Path(directory) / "prefix.junit.xml"
            junit.write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0">'
                '<testcase classname="tests.unit.test_sample_shadow.SampleTests" name="test_expected" />'
                '</testsuite>', encoding="utf-8")
            proof = validate_junit(junit, required)
        self.assertEqual(proof["node_results"][0]["status"], "MISSING")

    def test_nonzero_exit_cannot_pass_even_with_junit_pass_row(self) -> None:
        required = ["tests/unit/test_sample.py::SampleTests::test_expected"]
        with tempfile.TemporaryDirectory() as directory:
            junit = Path(directory) / "proof.xml"
            junit.write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0">'
                '<testcase classname="tests.unit.test_sample.SampleTests" name="test_expected" />'
                '</testsuite>', encoding="utf-8")
            proof = validate_junit(junit, required)
        errors = validate_suite_proof(proof, required, 7)
        self.assertIn("required suite exited 7", errors)
        self.assertEqual(proof["node_results"][0]["status"], "PASS")

    def test_skipped_required_case_cannot_pass(self) -> None:
        required = ["tests/unit/test_sample.py::SampleTests::test_expected"]
        with tempfile.TemporaryDirectory() as directory:
            junit = Path(directory) / "proof.xml"
            junit.write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="1">'
                '<testcase classname="tests.unit.test_sample.SampleTests" name="test_expected">'
                '<skipped message="fixture unavailable" />'
                '</testcase></testsuite>', encoding="utf-8")
            proof = validate_junit(junit, required)
        errors = validate_suite_proof(proof, required, 0)
        self.assertTrue(any("SKIPPED" in error for error in errors))
        self.assertTrue(any("skipped required cases" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
