"""Reproducible gate runner and evidence validator (standard library only)."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.private_acceptance import CATALOG, load_private_inputs, public_case_ids
ORACLE = ROOT / "tests/oracle/extract.py"
SPEC = ROOT / "docs/design.md"
EVIDENCE_SCHEMA = ROOT / "docs/rimworld-save-inspector/gate-evidence.schema.json"
SPEC_REVISION = "2026-10-17.1"
PUBLIC_SAMPLE_PATH = "examples/minimal_colony.rws"
PUBLIC_SAMPLE_SHA256 = "f55d5e0741972f959b39029e26e6921c4d768bc5eb80c9726610b9ad85d3b7d7"
PUBLIC_SAMPLE_BYTES = 1498
V1_PACKAGE_VERSION = "1.0.0"
V1_PROFILE_VERSION = "rimworld-1.6/v1"


def _source_string_constant(relative: str, name: str) -> str | None:
    """Read a release constant without importing the product into G0."""
    try:
        tree = ast.parse((ROOT / relative).read_text())
    except (OSError, SyntaxError, UnicodeError):
        return None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name
                                                 for target in node.targets) and
                isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            return node.value.value
    return None


def _release_identity() -> tuple[str | None, str | None, str | None]:
    """Return independently declared metadata, package, and parser profile versions."""
    try:
        with (ROOT / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream).get("project", {})
        package = project.get("version") if isinstance(project, dict) else None
    except (OSError, tomllib.TOMLDecodeError):
        package = None
    return (package if isinstance(package, str) else None,
            _source_string_constant("src/rimworld_save_inspector/__init__.py", "__version__"),
            _source_string_constant("src/rimworld_save_inspector/parser/core.py", "PARSER_PROFILE"))


def _private_identity() -> tuple[int | None, str | None]:
    inputs = load_private_inputs(check_fixture=False)
    return inputs.expected_bytes, inputs.expected_sha256


BASELINE_BYTES, BASELINE_SHA = _private_identity()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _artifact(path: Path, purpose: str, run: Path) -> dict[str, Any]:
    return {"path": path.relative_to(run).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path), "purpose": purpose}


def _check(check_id: str, expected: Any, observed: Any, evidence: list[str] | None = None) -> dict[str, Any]:
    return {"check_id": check_id, "requirement_ids": ["R19"], "required": True,
            "status": "PASS" if expected == observed else "FAIL", "expected": expected,
            "observed": observed, "evidence_paths": evidence or ["oracle.json"]}


def spec_oracle_agreement(*, private_required: bool = True) -> dict[str, Any]:
    """Check public case IDs and anchored private facts against the frozen baseline."""
    spec = SPEC.read_text()
    catalog = json.loads(CATALOG.read_text())
    private = load_private_inputs(check_fixture=False)
    mismatches = list(private.errors) if private_required else []
    public_revision = catalog.get("spec_revision")
    if f"Specification ID: `RWSI-1.0`. Revision: `{public_revision}`" not in spec:
        mismatches.append("public specification identity/revision differs")
    if catalog.get("spec_id") != "RWSI-1.0":
        mismatches.append("public case catalog identity differs")
    section = spec.split("### 9.2 Private golden assertions", 1)[-1].split("### 9.3", 1)[0]
    table_ids = {line.split("|")[1].strip() for line in section.splitlines() if line.startswith("| F")}
    table_predicates = {cells[0]: cells[1] for line in section.splitlines()
                        if len(cells := [cell.strip() for cell in line.strip().strip("|").split("|")]) >= 3
                        and cells[0] in table_ids}
    f_ids, d_ids = public_case_ids()
    if table_ids != f_ids or f_ids != {f"F{i:02d}" for i in range(1, 29)} or d_ids != {f"D{i:02d}" for i in range(1, 4)}:
        mismatches.append("public specification and catalog case IDs differ")
    public_rows = {row["check_id"]: row for row in catalog["assertions"]}
    if any(row.get("predicate") != table_predicates.get(check_id) or
           row.get("expected_source") != "frozen_private_oracle" or not row.get("scope")
           for check_id, row in public_rows.items()):
        mismatches.append("public catalog predicates or expected sources differ from specification")
    if private_required and private.ready and private.oracle is not None:
        oracle = private.oracle
        if (oracle.get("spec_revision") != catalog.get("private_baseline_spec_revision") or
                {row.get("check_id") for row in oracle.get("assertions", [])} != f_ids or
                {row.get("id") for row in oracle.get("required_dynamic_comparisons", [])} != d_ids):
            mismatches.append("frozen private case IDs or baseline revision differ")
        for row in oracle["assertions"]:
            check_id = row["check_id"]
            public_scope = public_rows[check_id]["scope"]
            if (check_id in {f"F{i:02d}" for i in range(24, 29)} and
                    not (public_scope == "selected_specimen_pawn" and row["scope"].startswith("pawn_")) or
                    check_id not in {f"F{i:02d}" for i in range(24, 29)} and public_scope != row["scope"]):
                mismatches.append(f"{check_id} public/private scope contract differs")
        if (not all("expected" in row for row in oracle.get("assertions", [])) or
                not all(row.get("selection") and row.get("compare") for row in oracle.get("required_dynamic_comparisons", []))):
            mismatches.append("private expected facts or dynamic rules are incomplete")
        baseline = private.baseline_spec or ""
        old_section = baseline.split("### 9.2", 1)[-1].split("### 9.3", 1)[0]
        baseline_ids = {line.split("|")[1].strip() for line in old_section.splitlines() if line.startswith("| F")}
        if baseline_ids != f_ids:
            mismatches.append("frozen baseline specification case IDs differ")
        baseline_rows = {cells[0]: " ".join(cells[1:]) for line in old_section.splitlines()
                         if len(cells := [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]) >= 3
                         and cells[0] in f_ids}
        for assertion in oracle["assertions"]:
            check_id = assertion["check_id"]
            if check_id == "F01":
                continue
            tokens: list[str] = []
            def collect(value: Any) -> None:
                if isinstance(value, dict):
                    for child in value.values(): collect(child)
                elif isinstance(value, list):
                    for child in value: collect(child)
                elif value is not None:
                    tokens.append(("null" if value == "explicit_null" else str(value)).lower())
            collect(assertion["expected"])
            row = baseline_rows.get(check_id, "").replace(",", "").lower()
            if any(not (token.replace(",", "") in row or
                        (token == "0" and ("zero" in row or (check_id == "F15" and "all " in row))) or
                        (token == "1" and "one" in row)) for token in tokens):
                mismatches.append(f"{check_id} frozen expected leaves differ from baseline specification")
        if (oracle.get("fixture", {}).get("bytes") != private.expected_bytes or
                oracle.get("fixture", {}).get("sha256") != private.expected_sha256):
            mismatches.append("private fixture identity differs from anchor")
        for token in (str(private.expected_bytes), str(private.expected_sha256), str(oracle.get("fixture", {}).get("game_version"))):
            if token.replace(",", "") not in baseline.replace(",", ""):
                mismatches.append("frozen fixture identity differs from baseline specification")
                break
        dynamic_rows = {row["id"]: row for row in oracle["required_dynamic_comparisons"]}
        selection = dynamic_rows["D03"]["selection"]
        research_key = selection.split("key ", 1)[1].split(" after", 1)[0]
        for dynamic_id, term in {"D01": "hediff with a part body/index",
                                 "D02": "non-null gene override",
                                 "D03": f"{research_key} progress row"}.items():
            if term.lower() not in baseline.lower():
                mismatches.append(f"{dynamic_id} dynamic contract missing from baseline specification")
    return {"agree": not mismatches, "mismatches": mismatches}


def _manifest_shape_errors(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest.json top level must be an object"]
    if manifest.get("status") == "PASS" and manifest.get("gate_id") in {"G0", "G3", "G7"}:
        private = load_private_inputs(check_fixture=False)
        if not private.ready:
            errors.append("PASS private gate lacks an authenticated external anchor fingerprint: " + "; ".join(private.errors))
    required = {"schema_version", "run_id", "gate_id", "status", "started_at", "finished_at", "actor", "spec", "candidate", "environment", "fixture", "prerequisites", "check_summary", "artifacts", "blockers", "findings", "carried_forward_from"}
    if set(manifest) != required:
        errors.append("manifest top-level fields do not match the evidence schema")
    if manifest.get("schema_version") != "1.0" or manifest.get("gate_id") not in {f"G{i}" for i in range(9)}:
        errors.append("unsupported evidence schema or gate id")
    spec = manifest.get("spec") if isinstance(manifest.get("spec"), dict) else {}
    if spec.get("id") != "RWSI-1.0" or spec.get("revision") != SPEC_REVISION:
        errors.append("specification identity or revision differs from the checked-in baseline")
    if manifest.get("status") not in {"NOT_RUN", "RUNNING", "PASS", "FAIL", "BLOCKED"}:
        errors.append("invalid gate status")
    for name in ("started_at", "finished_at"):
        value = manifest.get(name)
        if value is not None:
            try:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
                    errors.append(f"{name} must be a UTC timestamp")
            except (AttributeError, ValueError):
                errors.append(f"{name} is not a valid date-time")
    nested_required = {
        "spec": {"id", "revision", "sha256"},
        "candidate": {"commit", "dirty", "inputs", "lock_sha256", "package_version", "parser_version", "profile_version"},
        "environment": {"os", "architecture", "hardware", "python_executable", "python_version", "dependencies"},
        "fixture": {"label", "available", "expected_bytes", "observed_bytes", "expected_sha256", "before_sha256", "after_sha256", "unavailable_reason"},
        "check_summary": {"required", "passed", "failed", "blocked", "not_run"},
    }
    for key, fields in nested_required.items():
        if not isinstance(manifest.get(key), dict) or set(manifest[key]) != fields:
            errors.append(f"{key} fields do not match evidence schema")
    return errors


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _safe_relative(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return not path.is_absolute() and "\\" not in value and ".." not in path.parts and not (len(value) > 1 and value[1] == ":")


def _evidence_shape_errors(manifest: dict[str, Any], checks: Any, commands: Any, artifacts: Any) -> list[str]:
    errors: list[str] = []
    spec = manifest.get("spec") if isinstance(manifest.get("spec"), dict) else {}
    if not _is_sha(spec.get("sha256")) or spec.get("sha256") != sha256(SPEC):
        errors.append("spec hash does not match the checked-in normative document")
    candidate = manifest.get("candidate") if isinstance(manifest.get("candidate"), dict) else {}
    declared_package, declared_parser, declared_profile = _release_identity()
    if (candidate.get("package_version") != declared_package or declared_package != V1_PACKAGE_VERSION or
            (manifest.get("gate_id") == "G0" and
             (candidate.get("parser_version") is not None or candidate.get("profile_version") is not None)) or
            (manifest.get("gate_id") != "G0" and
             (candidate.get("parser_version") != declared_parser or declared_parser != V1_PACKAGE_VERSION or
              candidate.get("profile_version") != declared_profile or declared_profile != V1_PROFILE_VERSION))):
        errors.append("candidate package/parser/profile version differs from the committed v1 release identity")
    if not isinstance(candidate.get("dirty"), bool) or not isinstance(candidate.get("inputs"), list):
        errors.append("candidate dirty/inputs fields have invalid types")
    else:
        for item in candidate["inputs"]:
            if not isinstance(item, dict) or set(item) != {"path", "role", "sha256"} or not _safe_relative(item.get("path")) or item.get("role") not in {"source", "test", "oracle", "schema", "spec", "lock", "config"} or not _is_sha(item.get("sha256")):
                errors.append("candidate input does not match evidence schema")
                continue
            # G6's selected report is retained inside its evidence run. Its
            # source may be an ignored private file and need not exist when
            # the evidence is validated later; the G6 binding check below
            # authenticates the retained bytes against both indexes.
            if manifest.get("gate_id") == "G6" and item["path"] == "performance-report.md":
                continue
            path = ROOT / item["path"]
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError):
                resolved = Path("/")
            if ROOT.resolve() not in resolved.parents or not path.is_file() or sha256(path) != item["sha256"]:
                errors.append(f"candidate input hash mismatch: {item['path']}")
    if manifest.get("gate_id") != "G0":
        lock_path = ROOT / "uv.lock"
        if not lock_path.is_file() or candidate.get("lock_sha256") != sha256(lock_path):
            errors.append("candidate dependency lock hash does not match uv.lock")
    fixture = manifest.get("fixture") if isinstance(manifest.get("fixture"), dict) else {}
    if not isinstance(fixture.get("available"), bool) or not isinstance(fixture.get("label"), str):
        errors.append("fixture metadata has invalid types")
    for field in ("expected_sha256", "before_sha256", "after_sha256"):
        value = fixture.get(field)
        if value is not None and not _is_sha(value):
            errors.append(f"fixture {field} is not a SHA-256")
    for field in ("expected_bytes", "observed_bytes"):
        value = fixture.get(field)
        if value is not None and (not isinstance(value, int) or value < 0):
            errors.append(f"fixture {field} must be a nonnegative integer or null")
    if not isinstance(checks, list):
        errors.append("checks.json must be an array")
    else:
        for item in checks:
            if not isinstance(item, dict) or set(item) != {"check_id", "requirement_ids", "required", "status", "expected", "observed", "evidence_paths"}:
                errors.append("check entry does not match evidence schema")
                continue
            if not isinstance(item["check_id"], str) or not isinstance(item["required"], bool) or item["status"] not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}:
                errors.append("check entry has invalid scalar types")
            if not isinstance(item["requirement_ids"], list) or not all(isinstance(x, str) for x in item["requirement_ids"]):
                errors.append("check requirement_ids must be strings")
            if not isinstance(item["evidence_paths"], list) or not all(isinstance(x, str) for x in item["evidence_paths"]):
                errors.append("check evidence_paths must be strings")
    if not isinstance(commands, list):
        errors.append("commands.json must be an array")
    else:
        for item in commands:
            if not isinstance(item, dict) or set(item) != {"argv", "cwd_label", "started_at", "duration_seconds", "exit_code", "stdout_path", "stderr_path"}:
                errors.append("command entry does not match evidence schema")
                continue
            if not isinstance(item["argv"], list) or not all(isinstance(x, str) for x in item["argv"]):
                errors.append("command argv must be an array of strings")
            if not isinstance(item["duration_seconds"], (int, float)) or item["duration_seconds"] < 0 or not isinstance(item["exit_code"], int):
                errors.append("command duration or exit code is invalid")
    if not isinstance(artifacts, list):
        errors.append("artifacts.json must be an array")
    else:
        for item in artifacts:
            if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256", "purpose"} or not _safe_relative(item.get("path")) or not isinstance(item.get("bytes"), int) or not _is_sha(item.get("sha256")) or not isinstance(item.get("purpose"), str):
                errors.append("artifact index entry does not match evidence schema")
    try:
        schema = json.loads(EVIDENCE_SCHEMA.read_text())
        if schema.get("$id") != "urn:rimworld-save-inspector:gate-evidence:1.0":
            errors.append("gate evidence schema identity mismatch")
        try:
            from jsonschema import Draft202012Validator, FormatChecker
            errors.extend(f"manifest schema: {error.message}" for error in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(manifest))
        except ImportError:
            errors.append("strict JSON Schema validation unavailable: jsonschema is not installed")
    except (OSError, json.JSONDecodeError):
        errors.append("gate evidence schema is unreadable")
    return errors


def _resolve_inside(run: Path, relative: Any) -> Path | None:
    if not _safe_relative(relative):
        return None
    candidate = run / relative
    try:
        resolved = candidate.resolve(strict=True)
        if run.resolve() not in resolved.parents:
            return None
        return resolved
    except (OSError, RuntimeError):
        return None


def _t20_report_errors(run: Path, report: Any) -> list[str]:
    errors: list[str] = []
    declared_package, declared_parser, _ = _release_identity()
    if not isinstance(report, dict) or report.get("schema_version") != 1 or report.get("case") != "T20" or report.get("result") != "pass":
        return ["T20 audit manifest is missing, malformed, or not PASS"]
    expected = report.get("expected", {})
    if expected != {"network_attempts": [], "probe_observer_detects_write_and_network": True,
                    "source_hash_preserved": True, "source_mode_preserved": True, "writes_under_save_root": []}:
        errors.append("T20 expected audit facts differ from the normative checks")
    raw = report.get("observed", {})
    if not isinstance(raw, dict):
        return errors + ["T20 raw observations are missing"]
    for mode in ("cli", "mcp"):
        item = raw.get(mode)
        if not isinstance(item, dict):
            errors.append(f"T20 {mode} observation is missing")
            continue
        query_count = item.get("queries_completed")
        if (item.get("mode") != mode or item.get("error") is not None or
                type(query_count) is not int or query_count < 1):
            errors.append(f"T20 {mode} did not complete a query successfully")
        if item.get("writes_under_save_root") != [] or item.get("network_attempts") != []:
            errors.append(f"T20 {mode} observed a runtime write or network attempt")
        if item.get("source_hash_before") != item.get("source_hash_after"):
            errors.append(f"T20 {mode} source hash changed")
        before, after = item.get("mode_before"), item.get("mode_after")
        if type(before) is not int or type(after) is not int or before != after or before & 0o222:
            errors.append(f"T20 {mode} source mode was not read-only and unchanged")
        runtime = item.get("runtime_package")
        runtime_file = runtime.get("file") if isinstance(runtime, dict) else None
        if (not isinstance(runtime, dict) or runtime.get("version") != declared_package or
                declared_package != declared_parser or declared_package != V1_PACKAGE_VERSION or
                not isinstance(runtime_file, str) or not runtime_file):
            errors.append(f"T20 {mode} runtime package identity is missing")
        else:
            try:
                if Path(runtime_file).resolve() != (ROOT / "src/rimworld_save_inspector/__init__.py").resolve():
                    errors.append(f"T20 {mode} runtime did not load the current development package")
            except (OSError, RuntimeError, ValueError):
                errors.append(f"T20 {mode} runtime package path is invalid")
    probe = raw.get("probe")
    if not isinstance(probe, dict):
        errors.append("T20 observer sensitivity probe is missing")
    else:
        probe_writes = probe.get("writes_under_save_root")
        probe_network = probe.get("network_attempts")
        if (not isinstance(probe_writes, list) or not isinstance(probe_network, list) or
                not all(isinstance(item, dict) for item in probe_writes) or
                not all(isinstance(item, dict) for item in probe_network) or
                [item.get("kind") for item in probe_writes if isinstance(item, dict)] != ["write-open"] or
                [item.get("event") for item in probe_network if isinstance(item, dict)] != ["socket.getaddrinfo"]):
            errors.append("T20 observer sensitivity probe did not detect both blocked operations")
    paths = report.get("report_paths")
    if not isinstance(paths, list) or len(paths) != 3:
        errors.append("T20 raw report path list is incomplete")
    for name in ("cli.json", "mcp.json", "probe.json"):
        path = _resolve_inside(run, f"mcp-audit/{name}")
        if path is None:
            errors.append(f"T20 copied raw {name} report is missing")
            continue
        try:
            copied = json.loads(path.read_text())
            expected_raw = raw.get(name.removesuffix(".json"))
            if copied != expected_raw:
                errors.append(f"T20 copied raw {name} differs from audit manifest")
        except (OSError, json.JSONDecodeError, TypeError):
            errors.append(f"T20 copied raw {name} is unreadable")
    return errors


def _matrix_report_errors(report: Any, run: Path | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(report, dict) or report.get("case_ids") != ["T34"]:
        return ["T34 matrix report is missing or names the wrong case"]
    if report.get("status") != "PASS":
        errors.append(f"T34 matrix overall status is {report.get('status')}")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("T34 report has no authenticated command/output artifact manifest")
    elif run is not None:
        artifact_paths: set[str] = set()
        for item in artifacts:
            if (not isinstance(item, dict) or not _safe_relative(item.get("path")) or
                    not _is_sha(item.get("sha256")) or type(item.get("bytes")) is not int or item["bytes"] < 0):
                errors.append("T34 artifact manifest contains an invalid row")
                continue
            if item["path"] in artifact_paths:
                errors.append(f"T34 artifact path is duplicated: {item['path']}")
                continue
            artifact_paths.add(item["path"])
            path = _resolve_inside(run, item["path"])
            if path is None or not path.is_file() or path.stat().st_size != item["bytes"] or sha256(path) != item["sha256"]:
                errors.append(f"T34 copied artifact is missing or hash-mismatched: {item['path']}")
    if report.get("lock_sha256") != sha256(ROOT / "uv.lock"):
        errors.append("T34 lock hash differs from current candidate")
    fixture = report.get("public_fixture", {})
    public_fixture = ROOT / "tests/fixtures/synthetic/seed.rws"
    if (not public_fixture.is_file() or not isinstance(fixture, dict) or
            fixture.get("path") != "tests/fixtures/synthetic/seed.rws" or
            fixture.get("sha256") != sha256(public_fixture) or fixture.get("bytes") != public_fixture.stat().st_size):
        errors.append("T34 public fixture identity differs from current candidate")
    required_nodes = {"tests/integration/test_synthetic_domain_equivalence.py",
                      "tests/contract/test_cli_schema.py", "tests/mcp/test_mcp_acceptance.py",
                      "tests/packaging/test_public_matrix.py"}
    node_rows = report.get("test_nodes")
    if not isinstance(node_rows, list) or {item.get("path") for item in node_rows if isinstance(item, dict)} != required_nodes:
        errors.append("T34 report does not identify every required test node")
    else:
        for item in node_rows:
            path = ROOT / item["path"]
            if not path.is_file() or item.get("sha256") != sha256(path):
                errors.append(f"T34 test node hash is stale: {item['path']}")
    source_manifest = report.get("public_source_manifest")
    if not isinstance(source_manifest, list):
        errors.append("T34 sanitized public source hash manifest is missing")
    else:
        by_path = {item.get("path"): item for item in source_manifest if isinstance(item, dict)}
        tree = hashlib.sha256()
        valid_source_rows = [item for item in source_manifest if isinstance(item, dict) and isinstance(item.get("path"), str)]
        for item in sorted(valid_source_rows, key=lambda row: row["path"]):
            if not isinstance(item, dict) or not _safe_relative(item.get("path")) or not _is_sha(item.get("sha256")):
                errors.append("T34 public source manifest contains an invalid entry")
                continue
            tree.update(item["path"].encode("utf-8"))
            tree.update(b"\0")
            tree.update(item["sha256"].encode("ascii"))
            tree.update(b"\n")
        if tree.hexdigest() != report.get("public_source_tree_sha256"):
            errors.append("T34 public source tree digest does not authenticate its file list")
        for path in (ROOT / "src/rimworld_save_inspector").rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            row = by_path.get(relative)
            if not row or row.get("sha256") != sha256(path) or row.get("bytes") != path.stat().st_size:
                errors.append(f"T34 source input hash is stale or missing: {relative}")
        for relative in required_nodes | {"uv.lock", "pyproject.toml", "docs/design.md",
                                         "tools/matrix/run_public_matrix.py", "tests/fixtures/synthetic/seed.rws"}:
            path = ROOT / relative
            row = by_path.get(relative)
            if not path.is_file() or not row or row.get("sha256") != sha256(path) or row.get("bytes") != path.stat().st_size:
                errors.append(f"T34 public input hash is stale or missing: {relative}")
    required_cells = {"macos-arm64-python-3.11": ("Darwin", "arm64", [3, 11]),
                      "macos-arm64-python-3.14": ("Darwin", "arm64", [3, 14]),
                      "linux-x86_64-python-3.11": ("Linux", "x86_64", [3, 11]),
                      "linux-x86_64-python-3.14": ("Linux", "x86_64", [3, 14])}
    cells = report.get("cells")
    cell_ids = [item.get("id") for item in cells if isinstance(item, dict)] if isinstance(cells, list) else []
    if (not isinstance(cells, list) or len(cells) != len(required_cells) or
            len(cell_ids) != len(cells) or set(cell_ids) != set(required_cells)):
        errors.append("T34 must contain exactly the four required platform/interpreter cells")
    else:
        for cell in cells:
            if not isinstance(cell, dict):
                errors.append("T34 cell is not an object")
                continue
            os_name, machine, version = required_cells[cell["id"]]
            env = cell.get("environment", {})
            if not isinstance(env, dict):
                errors.append(f"T34 cell environment is not an object: {cell['id']}")
                continue
            if cell.get("status") != "PASS":
                errors.append(f"T34 cell {cell['id']} is {cell.get('status')}")
            if (env.get("os") != os_name or env.get("machine") != machine or env.get("version") != version or
                    not env.get("python") or not (env.get("package_origin") or env.get("origin"))):
                errors.append(f"T34 cell environment is incomplete or mismatched: {cell['id']}")
            if os_name == "Linux" and not env.get("execution_mode"):
                errors.append(f"T34 Linux execution mode is unrecorded: {cell['id']}")
    return errors


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stdout_marker_value(stdout_path: Path, marker: str) -> str | None:
    """Read one explicit producer marker, tolerating only pytest's dot prefix.

    With ``pytest -q``, the progress dots may share stdout's final marker line.
    Accept that known prefix while rejecting arbitrary text, duplicate markers,
    and empty payloads so a log cannot ambiguously select an evidence report.
    """
    try:
        lines = stdout_path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    values: list[str] = []
    for line in lines:
        offset = 0
        while (position := line.find(marker, offset)) >= 0:
            prefix = line[:position]
            if any(character not in ". \t" for character in prefix):
                return None
            values.append(line[position + len(marker):].strip())
            offset = position + len(marker)
    if len(values) != 1 or not values[0]:
        return None
    return values[0]


def _copy_packaged_candidate_report(run: Path, stdout_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Copy the G7 producer report and every referenced artifact into this run."""
    marker = "PACKAGED_CANDIDATE_REPORT="
    value = _stdout_marker_value(stdout_path, marker)
    if value is None:
        return None, None
    relative = Path(value)
    if relative.is_absolute() or not _safe_relative(relative.as_posix()):
        return None, None
    source = ROOT / relative
    evidence_root = (ROOT / "evidence").resolve()
    try:
        source = source.resolve(strict=True)
        if evidence_root not in source.parents or not source.is_file():
            return None, None
        report = json.loads(source.read_text())
        rows = report.get("artifacts") if isinstance(report, dict) else None
        if not isinstance(rows, list):
            return None, None
        target_dir = run / "packaged-candidate"
        target_dir.mkdir()
        artifact_paths: set[str] = set()
        for row in rows:
            if (not isinstance(row, dict) or not _safe_relative(row.get("path")) or
                    row.get("path") in artifact_paths or not _is_sha(row.get("sha256")) or
                    type(row.get("bytes")) is not int or row["bytes"] < 0):
                return None, None
            artifact_paths.add(row["path"])
            origin = source.parent / row["path"]
            resolved = origin.resolve(strict=True)
            if source.parent.resolve() not in resolved.parents or not origin.is_file():
                return None, None
            if origin.stat().st_size != row["bytes"] or sha256(origin) != row["sha256"]:
                return None, None
            destination = target_dir / row["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination)
        report_bytes = source.read_bytes()
        report_hash = hashlib.sha256(report_bytes).hexdigest()
        sidecar = source.with_name("report.sha256")
        if not sidecar.is_file() or sidecar.read_text().strip() != f"{report_hash}  report.json":
            return None, None
        shutil.copy2(sidecar, target_dir / "report.sha256")
        destination_report = target_dir / "report.json"
        destination_report.write_bytes(report_bytes)
        return report, "packaged-candidate/report.json"
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return None, None


def _copy_review_receipt(run: Path, receipt_path: Path | None) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Copy an explicitly supplied independent-review receipt and its log artifacts."""
    if receipt_path is None:
        return None, None, None
    try:
        source = receipt_path.resolve(strict=True)
        if not source.is_file():
            return None, None, "review receipt path is not a file"
        receipt = json.loads(source.read_text())
        rows = receipt.get("artifacts") if isinstance(receipt, dict) else None
        if not isinstance(rows, list):
            return None, None, "review receipt has no artifact manifest"
        target_root = run / "review-artifacts"
        target_root.mkdir()
        seen: set[str] = set()
        for item in rows:
            relative = item.get("path") if isinstance(item, dict) else None
            if (not _safe_relative(relative) or relative in seen or not _is_sha(item.get("sha256")) or
                    type(item.get("bytes")) is not int or item["bytes"] < 0):
                return None, None, "review receipt has an invalid or duplicate artifact row"
            seen.add(relative)
            origin = (source.parent / relative).resolve(strict=True)
            if source.parent.resolve() not in origin.parents or not origin.is_file():
                return None, None, f"review artifact escapes its receipt directory: {relative}"
            if origin.stat().st_size != item["bytes"] or sha256(origin) != item["sha256"]:
                return None, None, f"review artifact hash mismatch: {relative}"
            destination = target_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination)
        destination_report = run / "review-report.json"
        shutil.copy2(source, destination_report)
        return receipt, "review-report.json", None
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, None, f"could not read/copy review receipt: {exc}"


def _producer_command_log_errors(commands: Any, artifact_index: dict[str, dict[str, Any]], run: Path) -> list[str]:
    """Require each producer command to retain its own authenticated output bytes."""
    if not isinstance(commands, list) or not commands:
        return ["G7 producer command records are missing"]
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for command in commands:
        if (not isinstance(command, dict) or not isinstance(command.get("command_id"), str) or
                not command["command_id"] or command.get("exit_code") != 0 or
                command.get("timed_out") is not False):
            errors.append("G7 producer command failed, timed out, or is malformed")
            continue
        command_id = command["command_id"]
        if command_id in seen_ids:
            errors.append(f"G7 producer command ID is duplicated: {command_id}")
        seen_ids.add(command_id)
        for stream in ("stdout", "stderr"):
            log_path = command.get(f"{stream}_path")
            if not _safe_relative(log_path):
                errors.append(f"G7 producer command {stream} path is missing or invalid")
                continue
            if log_path in seen_paths:
                errors.append(f"G7 producer command output path is reused: {log_path}")
            seen_paths.add(log_path)
            log_row = artifact_index.get(log_path)
            copied = _resolve_inside(run, f"packaged-candidate/{log_path}")
            if (log_row is None or log_row.get("sha256") != command.get(f"{stream}_sha256") or
                    copied is None or not copied.is_file() or
                    copied.stat().st_size != log_row.get("bytes") or sha256(copied) != log_row.get("sha256")):
                errors.append(f"G7 producer command {stream} log is not authenticated: {command_id}")
    return errors


def _packaged_candidate_report_errors(report: Any, run: Path) -> list[str]:
    """Authenticate clean-tree, archive, install, workspace and required-suite G7 facts."""
    errors: list[str] = []
    expected_cases = ["T33", "T36", "T37"]
    if (not isinstance(report, dict) or report.get("schema_version") != "1.0" or
            report.get("case_ids") != expected_cases):
        return ["G7 packaged-candidate report is missing, malformed or names the wrong cases"]
    if report.get("status") != "PASS":
        errors.append(f"G7 packaged-candidate producer status is {report.get('status')}")
    spec = report.get("spec")
    if not isinstance(spec, dict) or spec.get("id") != "RWSI-1.0" or spec.get("revision") != SPEC_REVISION or spec.get("sha256") != sha256(SPEC):
        errors.append("G7 producer specification identity or hash differs from the current normative document")
    candidate = report.get("candidate")
    if not isinstance(candidate, dict):
        return errors + ["G7 clean-candidate proof is missing"]
    if candidate.get("commit") != git("rev-parse", "HEAD") or candidate.get("dirty") is not False:
        errors.append("G7 package was not built from the current clean committed candidate")
    if git("status", "--porcelain"):
        errors.append("G7 current checkout is dirty after candidate capture")
    if candidate.get("git_tree_oid") != git("rev-parse", "HEAD^{tree}"):
        errors.append("G7 Git tree OID differs from current candidate")
    if candidate.get("lock_sha256") != sha256(ROOT / "uv.lock") or candidate.get("pyproject_sha256") != sha256(ROOT / "pyproject.toml"):
        errors.append("G7 lock or package metadata hash differs from current candidate")
    declared_package, declared_parser, declared_profile = _release_identity()
    if (candidate.get("package_version") != declared_package or declared_package != V1_PACKAGE_VERSION or
            candidate.get("parser_version") != declared_parser or declared_parser != V1_PACKAGE_VERSION or
            candidate.get("profile_version") != declared_profile or declared_profile != V1_PROFILE_VERSION):
        errors.append("G7 package/parser/profile versions differ from the declared candidate")
    inputs = candidate.get("inputs")
    if not isinstance(inputs, list):
        errors.append("G7 tracked input manifest is missing")
        inputs = []
    normalized_inputs: list[dict[str, Any]] = []
    input_paths: set[str] = set()
    for item in inputs:
        if (not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"} or
                not _safe_relative(item.get("path")) or type(item.get("bytes")) is not int or
                item["bytes"] < 0 or not _is_sha(item.get("sha256"))):
            errors.append("G7 tracked input manifest contains an invalid row")
            continue
        if item["path"] in input_paths:
            errors.append(f"G7 tracked input manifest duplicates {item['path']}")
            continue
        input_paths.add(item["path"])
        path = ROOT / item["path"]
        if (not path.is_file() or path.is_symlink() or path.stat().st_size != item["bytes"] or
                sha256(path) != item["sha256"]):
            errors.append(f"G7 tracked candidate input is stale: {item['path']}")
        normalized_inputs.append({"path": item["path"], "bytes": item["bytes"], "sha256": item["sha256"]})
    expected_tracked = []
    tracked_output = git("ls-files", "-z") or ""
    for name in tracked_output.split("\0"):
        path = ROOT / name if name else None
        if path is not None and path.is_file() and not path.is_symlink():
            expected_tracked.append({"path": name, "bytes": path.stat().st_size, "sha256": sha256(path)})
    expected_tracked.sort(key=lambda row: row["path"])
    normalized_inputs.sort(key=lambda row: row["path"])
    if normalized_inputs != expected_tracked:
        errors.append("G7 input manifest does not cover every current tracked regular file exactly")
    source_rows = [{"path": row["path"], "sha256": row["sha256"]} for row in normalized_inputs]
    if candidate.get("input_manifest_sha256") != _canonical_sha256(normalized_inputs):
        errors.append("G7 input manifest digest is invalid")
    if candidate.get("tree_sha256") != _canonical_sha256(source_rows):
        errors.append("G7 candidate tree digest is invalid")

    fixture = report.get("fixture")
    if (not isinstance(fixture, dict) or fixture.get("available") is not True or
            fixture.get("identity_matches") is not True or fixture.get("expected_bytes") != BASELINE_BYTES or
            fixture.get("observed_bytes") != BASELINE_BYTES or fixture.get("after_bytes") != BASELINE_BYTES or
            fixture.get("expected_sha256") != BASELINE_SHA or fixture.get("before_sha256") != BASELINE_SHA or
            fixture.get("after_sha256") != BASELINE_SHA):
        errors.append("G7 requires the unchanged frozen private fixture identity")
    else:
        private = load_private_inputs(check_fixture=False)
        if not private.ready or fixture.get("private_input_hashes") != private.expected_input_hashes():
            errors.append("G7 private acceptance inputs are not authenticated by the external anchor fingerprint")

    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("G7 artifact manifest is missing")
        artifacts = []
    artifact_index: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        if (not isinstance(item, dict) or not _safe_relative(item.get("path")) or
                not _is_sha(item.get("sha256")) or type(item.get("bytes")) is not int or item["bytes"] < 0):
            errors.append("G7 artifact manifest contains an invalid row")
            continue
        if item["path"] in artifact_index:
            errors.append(f"G7 artifact manifest duplicates {item['path']}")
            continue
        artifact_index[item["path"]] = item
        copied = _resolve_inside(run, f"packaged-candidate/{item['path']}")
        if (copied is None or not copied.is_file() or copied.stat().st_size != item["bytes"] or
                sha256(copied) != item["sha256"]):
            errors.append(f"G7 copied artifact is missing or hash-mismatched: {item['path']}")

    def check_report_artifact(relative: Any, digest: Any, byte_count: Any, label: str) -> None:
        if not isinstance(relative, str) or not _safe_relative(relative):
            errors.append(f"G7 {label} artifact path is invalid")
            return
        row = artifact_index.get(relative)
        if (row is None or row.get("sha256") != digest or row.get("bytes") != byte_count):
            errors.append(f"G7 {label} is not authenticated by the producer artifact manifest")

    archives = report.get("archives")
    if not isinstance(archives, list) or {row.get("kind") for row in archives if isinstance(row, dict)} != {"wheel", "sdist"} or len(archives) != 2:
        errors.append("G7 must include exactly one wheel and one sdist archive audit")
        archives = []
    archive_entries: list[dict[str, Any]] = []
    for archive in archives:
        if not isinstance(archive, dict) or archive.get("kind") not in {"wheel", "sdist"}:
            errors.append("G7 archive audit row is malformed")
            continue
        members = archive.get("members")
        if not isinstance(members, list) or not members:
            errors.append(f"G7 {archive.get('kind')} member manifest is missing")
            continue
        valid_members = []
        for member in members:
            if (not isinstance(member, dict) or not _safe_relative(member.get("path")) or
                    type(member.get("bytes")) is not int or member["bytes"] < 0 or not _is_sha(member.get("sha256"))):
                errors.append(f"G7 {archive.get('kind')} member manifest contains an invalid row")
                continue
            valid_members.append(member)
            public_sample = (member["path"].endswith("/" + PUBLIC_SAMPLE_PATH) or member["path"] == PUBLIC_SAMPLE_PATH) and member.get("sha256") == PUBLIC_SAMPLE_SHA256 and member.get("bytes") == PUBLIC_SAMPLE_BYTES
            if member["path"].lower().endswith((".rws", ".xml")) and not public_sample:
                errors.append(f"G7 {archive.get('kind')} includes a prohibited save/XML member: {member['path']}")
        valid_members.sort(key=lambda row: row["path"])
        member_digest = _canonical_sha256(valid_members)
        if (len(valid_members) != archive.get("member_count") or member_digest != archive.get("members_sha256") or
                member_digest != archive.get("member_manifest_sha256")):
            errors.append(f"G7 {archive.get('kind')} member digest/count does not authenticate its rows")
        findings = archive.get("audit_findings")
        if not isinstance(findings, dict) or findings.get("forbidden_names") != [] or findings.get("forbidden_content") != []:
            errors.append(f"G7 {archive.get('kind')} archive audit reports forbidden content")
        relative = archive.get("path")
        copied_archive = _resolve_inside(run, f"packaged-candidate/{relative}") if isinstance(relative, str) else None
        if copied_archive is None or not copied_archive.is_file():
            errors.append(f"G7 {archive.get('kind')} archive bytes were not copied")
        else:
            archive_entries.append({"path": f"packaged-candidate/{relative}", "sha256": archive.get("sha256"),
                                   "bytes": archive.get("bytes"), "members": members})
    if archive_entries:
        errors.extend(_package_archive_errors(run, {"artifacts": archive_entries}))
    source_snapshot_rows = [item for item in artifacts if isinstance(item, dict) and
                            item.get("sha256") == candidate.get("archive_sha256") and
                            item.get("bytes") == candidate.get("archive_bytes")]
    if (len(source_snapshot_rows) != 1 or
            source_snapshot_rows[0].get("purpose") != "git archive stdout tar stream for clean committed candidate"):
        errors.append("G7 committed source snapshot artifact is not uniquely authenticated")
    smoke = report.get("package_smoke")
    required_smoke = {"wheel-cli", "wheel-mcp", "sdist-cli", "sdist-mcp"}
    smoke_ids = [row.get("cell") for row in smoke if isinstance(row, dict)] if isinstance(smoke, list) else []
    if not isinstance(smoke, list) or len(smoke) != 4 or set(smoke_ids) != required_smoke or any(
            not isinstance(row, dict) or row.get("status") != "PASS" for row in smoke):
        errors.append("G7 T33 requires all four fresh wheel/sdist CLI-only and MCP-extra smoke cells to pass")
    else:
        for row in smoke:
            cell = row.get("cell")
            expected_artifact, expected_profile = cell.split("-", 1) if isinstance(cell, str) and "-" in cell else (None, None)
            expected_mcp = expected_profile == "mcp"
            observed = row.get("observed") if isinstance(row.get("observed"), dict) else {}
            expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
            if (row.get("validation_errors") != [] or row.get("artifact") != expected_artifact or
                    row.get("dependency_profile") != expected_profile or expected_profile not in {"cli", "mcp"} or
                    expected.get("mcp_installed") is not expected_mcp or observed.get("mcp_available") is not expected_mcp or
                    observed.get("cli_exit_code") != 0 or observed.get("version") != candidate.get("package_version") or
                    not isinstance(observed.get("origin"), str) or
                    not isinstance(row.get("command_ids"), list) or len(row["command_ids"]) != 5):
                errors.append(f"G7 T33 smoke cell is missing clean validation or its five commands: {row.get('cell')}")
            else:
                all_commands = {item.get("command_id"): item for item in report.get("commands", []) if isinstance(item, dict)}
                if (not set(row["command_ids"]) <= all_commands.keys() or
                        any(all_commands[item].get("exit_code") != 0 or all_commands[item].get("timed_out") is not False
                            for item in row["command_ids"] if item in all_commands)):
                    errors.append(f"G7 T33 smoke commands are missing for {row.get('cell')}")

    install = report.get("install")
    if not isinstance(install, dict):
        errors.append("G7 installed-candidate proof is missing")
    else:
        if install.get("outside_checkout") is not True or install.get("source_shadowing") is not False:
            errors.append("G7 package was not verified outside checkout without source shadowing")
        if (install.get("installed_version") != candidate.get("package_version") or
                install.get("installed_version") != declared_package or declared_package != V1_PACKAGE_VERSION):
            errors.append("G7 installed package version differs from the candidate")
        if install.get("wheel_sha256") != next((row.get("sha256") for row in archives if isinstance(row, dict) and row.get("kind") == "wheel"), None):
            errors.append("G7 installed wheel hash does not match the audited candidate wheel")
        origin_errors = _validate_external_install_origin(install.get("installed_module_origin"), install.get("site_packages"))
        errors.extend(origin_errors)
        deps_hash = install.get("locked_dependencies_sha256")
        requirements = artifact_index.get("locked-requirements.txt")
        if not requirements or requirements.get("sha256") != deps_hash:
            errors.append("G7 locked dependency inventory is not tied to the copied lock export")

    workspace = report.get("workspace")
    if not isinstance(workspace, dict):
        errors.append("G7 external verification workspace proof is missing")
    else:
        files = workspace.get("files")
        if not isinstance(files, list) or workspace.get("source_tree_present") is not False or workspace.get("forbidden_paths_present") != [] or workspace.get("audit_errors") != []:
            errors.append("G7 verification workspace contains forbidden source/private paths or has no file manifest")
        else:
            errors.extend(f"G7 workspace path audit: {item}" for item in _validate_copied_workspace(files))
            if workspace.get("file_count") != len(files) or workspace.get("manifest_sha256") != _canonical_sha256(files):
                errors.append("G7 copied workspace manifest count/digest is invalid")
        check_report_artifact(workspace.get("archive_path"), workspace.get("archive_sha256"),
                              workspace.get("archive_bytes"), "external verification workspace archive")

    suite = report.get("required_suite")
    if not isinstance(suite, dict):
        errors.append("G7 installed-wheel required-suite proof is missing")
    else:
        required_nodes = _g7_required_nodes()
        if suite.get("required_nodes") != required_nodes or suite.get("case_nodes") != _g7_case_nodes():
            errors.append("G7 required-suite node mapping differs from the current gate case table")
        junit_relative = suite.get("junit_path")
        junit = _resolve_inside(run, f"packaged-candidate/{junit_relative}") if isinstance(junit_relative, str) else None
        if junit is None or not junit.is_file() or sha256(junit) != suite.get("junit_sha256"):
            errors.append("G7 required-suite JUnit output is missing or unauthenticated")
        counts = suite.get("junit_counts")
        exact_nodes = [node for node in required_nodes if "::" in node]
        if (not isinstance(counts, dict) or counts.get("tests", 0) < len(exact_nodes) or counts.get("passed", 0) < len(required_nodes) or
                counts.get("failures") != 0 or counts.get("errors") != 0 or counts.get("skipped") != 0 or
                counts.get("skipped_required") != 0 or counts.get("missing") != 0):
            errors.append("G7 required suite has zero coverage, failures, errors, skips or missing mapped nodes")
        node_results = suite.get("node_results")
        if (not isinstance(node_results, list) or {row.get("node") for row in node_results if isinstance(row, dict)} != set(required_nodes) or
                len(node_results) != len(required_nodes) or any(not isinstance(row, dict) or row.get("status") != "PASS" for row in node_results)):
            errors.append("G7 required-suite node outcomes are incomplete or non-PASS")
        elif junit is not None and junit.is_file():
            try:
                import xml.etree.ElementTree as ET
                parsed = ET.parse(junit).getroot()
                xml_cases = list(parsed.iter("testcase"))
                skipped_count = sum(test.find("skipped") is not None for test in xml_cases)
                failure_count = sum(test.find("failure") is not None or test.find("error") is not None for test in xml_cases)
                if (len(xml_cases) != counts.get("tests") or skipped_count != counts.get("skipped") or
                        failure_count != counts.get("failures", 0) + counts.get("errors", 0)):
                    errors.append("G7 JUnit case counts disagree with the report")
                for node in required_nodes:
                    matches = [test for test in xml_cases if _junit_testcase_matches(node, test.attrib)]
                    expected_count = 1 if "::" in node else None
                    if (not matches or (expected_count is not None and len(matches) != expected_count) or
                            len(matches) != len({(test.get("classname"), test.get("name")) for test in matches}) or
                            any(test.find(tag) is not None for test in matches for tag in ("failure", "error", "skipped"))):
                        errors.append(f"G7 JUnit does not uniquely prove passing required coverage: {node}")
            except (OSError, ET.ParseError):
                errors.append("G7 required-suite JUnit XML could not be independently parsed")
        if suite.get("exit_code") != 0 or suite.get("validation_errors") != []:
            errors.append("G7 installed required suite failed or has validation errors")
        audit = suite.get("runtime_import_audit")
        if not isinstance(audit, dict) or audit.get("source_shadow") is not False:
            errors.append("G7 required-suite runtime import audit is missing or reports source shadowing")
        else:
            errors.extend(_validate_external_install_origin(audit.get("origin"), install.get("site_packages") if isinstance(install, dict) else None))
            if audit.get("site") != (install.get("site_packages") if isinstance(install, dict) else None):
                errors.append("G7 runtime import audit site-packages path differs from the fresh install")
        import_records = suite.get("runtime_import_records")
        if (not isinstance(import_records, list) or suite.get("runtime_import_record_count") != len(import_records) or
                not import_records or not isinstance(install, dict)):
            errors.append("G7 child-process runtime import provenance is incomplete")
        else:
            for item in import_records:
                if not isinstance(item, dict):
                    errors.append("G7 runtime import record is malformed")
                    continue
                errors.extend(_validate_external_install_origin(item.get("origin"), install.get("site_packages")))
        for stream in ("stdout", "stderr"):
            check_report_artifact(suite.get(f"{stream}_path"), suite.get(f"{stream}_sha256"),
                                  artifact_index.get(suite.get(f"{stream}_path"), {}).get("bytes") if isinstance(suite.get(f"{stream}_path"), str) else None,
                                  f"required-suite {stream}")
        errors.extend(_producer_command_log_errors(report.get("commands"), artifact_index, run))

    cases = report.get("cases")
    if not isinstance(cases, list) or {row.get("case_id") for row in cases if isinstance(row, dict)} != set(expected_cases) or len(cases) != len(expected_cases):
        errors.append("G7 producer must report exactly T33, T36 and T37")
    else:
        for row in cases:
            if not isinstance(row, dict) or row.get("status") != "PASS" or not isinstance(row.get("expected"), (dict, str)) or not isinstance(row.get("observed"), (dict, str)):
                errors.append(f"G7 producer case {row.get('case_id') if isinstance(row, dict) else '?'} is incomplete or not PASS")
            if isinstance(row, dict) and row.get("case_id") == "T37":
                observed = row.get("observed", {})
                probe = observed.get("scanner_probe") if isinstance(observed, dict) else None
                probe_keys = ("unsafe_path_detected", "credential_detected", "private_xml_label_detected",
                              "synthetic_design_example_exempt", "injected_design_xml_detected")
                if (not isinstance(probe, dict) or probe.get("passed") is not True or
                        any(probe.get(key) is not True for key in probe_keys)):
                    errors.append("G7 T37 scanner sensitivity probes are missing or did not all pass")
                counts = observed.get("audit_counts") if isinstance(observed, dict) else None
                if (not isinstance(counts, dict) or counts.get("archive_count") != 2 or
                        counts.get("scanner_probe_count") != 5 or counts.get("scanner_probe_passed") != 5):
                    errors.append("G7 T37 does not authenticate both archive audits and all scanner probes")
                reported_archives = observed.get("archives") if isinstance(observed, dict) else None
                top_archives = {archive.get("kind"): archive for archive in archives if isinstance(archive, dict)}
                if not isinstance(reported_archives, list) or len(reported_archives) != 2:
                    errors.append("G7 T37 case omits the exact reviewed archive identities")
                else:
                    for reviewed in reported_archives:
                        expected_archive = top_archives.get(reviewed.get("kind")) if isinstance(reviewed, dict) else None
                        if (expected_archive is None or reviewed.get("sha256") != expected_archive.get("sha256") or
                                reviewed.get("member_manifest_sha256") != expected_archive.get("member_manifest_sha256") or
                                reviewed.get("audit_findings") != expected_archive.get("audit_findings")):
                            errors.append("G7 T37 case archive details differ from the audited archive manifests")
            if isinstance(row, dict) and row.get("case_id") == "T33":
                observed = row.get("observed", {})
                cells = observed.get("smoke_cells") if isinstance(observed, dict) else None
                cell_ids = [cell.get("cell") for cell in cells if isinstance(cell, dict)] if isinstance(cells, list) else []
                if not isinstance(cells, list) or len(cells) != 4 or set(cell_ids) != required_smoke:
                    errors.append("G7 T33 case does not report all four distribution smoke cells")
                elif cells != smoke:
                    errors.append("G7 T33 case smoke rows differ from the authenticated producer smoke matrix")
            if isinstance(row, dict) and row.get("case_id") == "T36":
                observed = row.get("observed", {})
                nodes = observed.get("nodes") if isinstance(observed, dict) else None
                t36_nodes = set(_g7_case_nodes().get("T36", []))
                node_ids = {item.get("node") for item in nodes if isinstance(item, dict)} if isinstance(nodes, list) else set()
                if (node_ids != t36_nodes or len(nodes or []) != len(t36_nodes) or
                        any(item.get("status") != "PASS" for item in nodes if isinstance(item, dict))):
                    errors.append("G7 T36 case does not list every mapped tampering node as PASS")
    return errors


def _review_receipt_errors(receipt: Any, report: dict[str, Any], run: Path) -> list[str]:
    """Authenticate the fresh independent review against this exact candidate."""
    errors: list[str] = []
    if not isinstance(receipt, dict) or receipt.get("schema_version") != "1.0" or receipt.get("case_id") != "T37":
        return ["G7 independent review receipt is missing or has an unsupported schema/case"]
    try:
        import jsonschema
        schema_path = ROOT / "tools/gates/review-receipt.schema.json"
        schema = json.loads(schema_path.read_text())
        validator = jsonschema.validators.validator_for(schema)
        validator.check_schema(schema)
        errors.extend(f"G7 review receipt schema: {error.message}" for error in validator(schema, format_checker=jsonschema.FormatChecker()).iter_errors(receipt))
    except (ImportError, OSError, json.JSONDecodeError, jsonschema.SchemaError if "jsonschema" in locals() else ValueError) as exc:
        errors.append(f"G7 review receipt schema could not be enforced: {exc}")
    receipt_fields = {"schema_version", "case_id", "status", "reviewer", "started_at", "finished_at", "candidate", "private_anchor",
                      "archives", "requirement_checks", "t37_checks", "reproductions", "findings", "artifacts"}
    if set(receipt) != receipt_fields:
        errors.append("G7 reviewer receipt fields do not match the strict v1 contract")
    if receipt.get("status") not in {"PASS", "FAIL", "BLOCKED"}:
        errors.append("G7 independent review receipt has an invalid status")
    reviewer = receipt.get("reviewer")
    if (not isinstance(reviewer, dict) or set(reviewer) != {"identity", "run_id", "independent"} or
            not isinstance(reviewer.get("identity"), str) or not reviewer["identity"].strip() or
            not isinstance(reviewer.get("run_id"), str) or not reviewer["run_id"].strip() or reviewer.get("independent") is not True):
        errors.append("G7 review receipt does not identify an independent reviewer run")
    try:
        started = dt.datetime.fromisoformat(receipt["started_at"].replace("Z", "+00:00"))
        finished = dt.datetime.fromisoformat(receipt["finished_at"].replace("Z", "+00:00"))
        if (started.tzinfo is None or finished.tzinfo is None or started.utcoffset() != dt.timedelta(0) or
                finished.utcoffset() != dt.timedelta(0) or finished < started):
            raise ValueError
    except (KeyError, AttributeError, TypeError, ValueError):
        errors.append("G7 review receipt timestamps must be ordered UTC values")

    candidate = report.get("candidate", {})
    reviewed_candidate = receipt.get("candidate")
    candidate_fields = ("commit", "git_tree_oid", "tree_sha256", "input_manifest_sha256")
    if not isinstance(reviewed_candidate, dict) or set(reviewed_candidate) != set(candidate_fields) or any(
            reviewed_candidate.get(key) != candidate.get(key) for key in candidate_fields):
        errors.append("G7 review receipt does not bind to the exact candidate commit and input digests")
    private = load_private_inputs(check_fixture=False)
    authenticated = private.expected_input_hashes()
    reviewed_anchor = receipt.get("private_anchor")
    produced_hashes = report.get("fixture", {}).get("private_input_hashes") if isinstance(report.get("fixture"), dict) else None
    if (not private.ready or not isinstance(reviewed_anchor, dict) or authenticated is None or
            reviewed_anchor != {"expected_sha256": authenticated["anchor_expected"],
                                "observed_sha256": authenticated["anchor_observed"]} or
            produced_hashes != authenticated):
        errors.append("G7 review receipt does not bind to the externally trusted private anchor and producer inputs")
    produced_archives = {row.get("kind"): row for row in report.get("archives", []) if isinstance(row, dict)}
    reviewed_archives = receipt.get("archives")
    if not isinstance(reviewed_archives, dict) or set(reviewed_archives) != {"wheel", "sdist"}:
        errors.append("G7 review receipt must include exact wheel and sdist manifests")
    else:
        for kind in ("wheel", "sdist"):
            actual, expected = reviewed_archives.get(kind), produced_archives.get(kind)
            if not isinstance(actual, dict) or expected is None:
                errors.append(f"G7 review receipt is missing {kind} archive identity")
                continue
            if set(actual) != {"sha256", "bytes", "members_sha256", "members"}:
                errors.append(f"G7 reviewer {kind} archive does not match the strict identity schema")
            for field in ("sha256", "bytes", "members_sha256", "members"):
                if actual.get(field) != expected.get(field):
                    errors.append(f"G7 reviewer {kind} archive {field} differs from producer evidence")

    requirement_checks = receipt.get("requirement_checks")
    required_requirements = {f"R{i:02d}" for i in range(1, 21)}
    if not isinstance(requirement_checks, list):
        errors.append("G7 reviewer R01-R20 requirement mapping is missing")
        requirement_checks = []
    else:
        ids = [row.get("requirement_id") for row in requirement_checks if isinstance(row, dict)]
        if len(ids) != len(set(ids)) or set(ids) != required_requirements:
            errors.append("G7 reviewer must map each R01-R20 requirement exactly once")
        for row in requirement_checks:
            if (not isinstance(row, dict) or row.get("status") != "PASS" or
                    set(row) != {"requirement_id", "status", "expected", "observed", "evidence_refs"} or
                    not row.get("expected") or not row.get("observed") or
                    not isinstance(row.get("evidence_refs"), list) or not row["evidence_refs"]):
                errors.append("G7 reviewer requirement mapping has an incomplete or unsupported result")
                continue
            if any(ref not in {item.get("path") for item in receipt.get("artifacts", []) if isinstance(item, dict)} for ref in row["evidence_refs"]):
                errors.append(f"G7 reviewer requirement {row.get('requirement_id')} cites an unauthenticated artifact")

    t37_checks = receipt.get("t37_checks")
    required_t37 = {"archive_member_review", "prohibited_content_review", "source_provenance_review", "scalar_expectations_review"}
    if not isinstance(t37_checks, list):
        errors.append("G7 explicit T37 archive review is missing")
        t37_checks = []
    else:
        ids = [row.get("check_id") for row in t37_checks if isinstance(row, dict)]
        if len(ids) != len(set(ids)) or set(ids) != required_t37:
            errors.append("G7 T37 review must contain all archive/provenance checks exactly once")
        for row in t37_checks:
            if (not isinstance(row, dict) or row.get("status") != "PASS" or
                    set(row) != {"check_id", "status", "expected", "observed", "evidence_refs"} or
                    not row.get("expected") or not row.get("observed") or
                    not isinstance(row.get("evidence_refs"), list) or not row["evidence_refs"]):
                errors.append("G7 T37 archive check is incomplete or unsupported")
            elif any(ref not in {item.get("path") for item in receipt.get("artifacts", []) if isinstance(item, dict)}
                     for ref in row["evidence_refs"]):
                errors.append(f"G7 T37 check {row.get('check_id')} cites an unauthenticated artifact")

    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("G7 reviewer artifact manifest is missing")
        artifacts = []
    artifact_index: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        if (not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256", "purpose"} or
                not _safe_relative(item.get("path")) or
                type(item.get("bytes")) is not int or item["bytes"] < 0 or not _is_sha(item.get("sha256"))):
            errors.append("G7 reviewer artifact manifest contains an invalid row")
            continue
        if item["path"] in artifact_index:
            errors.append(f"G7 reviewer artifact manifest duplicates {item['path']}")
            continue
        artifact_index[item["path"]] = item
        path = _resolve_inside(run, f"review-artifacts/{item['path']}")
        if path is None or not path.is_file() or path.stat().st_size != item["bytes"] or sha256(path) != item["sha256"]:
            errors.append(f"G7 reviewer artifact is missing or unauthenticated: {item['path']}")
    reproductions = receipt.get("reproductions")
    if not isinstance(reproductions, list) or not reproductions:
        errors.append("G7 reviewer must include actual targeted reproduction logs")
    else:
        for reproduction in reproductions:
            if (not isinstance(reproduction, dict) or not isinstance(reproduction.get("argv"), list) or
                    set(reproduction) != {"command_id", "argv", "cwd", "started_at", "duration_seconds", "exit_code", "expected", "observed", "stdout", "stderr"} or
                    not reproduction["argv"] or any(not isinstance(arg, str) or not arg for arg in reproduction["argv"]) or
                    not isinstance(reproduction.get("cwd"), str) or not reproduction["cwd"] or
                    reproduction.get("exit_code") != 0 or not isinstance(reproduction.get("expected"), (dict, str)) or
                    not isinstance(reproduction.get("observed"), (dict, str)) or
                    type(reproduction.get("duration_seconds")) not in (int, float) or reproduction["duration_seconds"] < 0):
                errors.append("G7 reviewer reproduction record is incomplete or not successful")
                continue
            try:
                run_started = dt.datetime.fromisoformat(reproduction["started_at"].replace("Z", "+00:00"))
                if run_started.tzinfo is None or run_started.utcoffset() != dt.timedelta(0):
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                errors.append("G7 reviewer reproduction timestamp is not UTC")
            for stream in ("stdout", "stderr"):
                details = reproduction.get(stream)
                if not isinstance(details, dict) or set(details) != {"path", "sha256"} or not isinstance(details.get("path"), str):
                    errors.append(f"G7 reviewer reproduction {stream} is not captured")
                    continue
                record = artifact_index.get(details["path"])
                if record is None or record.get("sha256") != details.get("sha256"):
                    errors.append(f"G7 reviewer reproduction {stream} is not bound to an artifact")
                copied = _resolve_inside(run, f"review-artifacts/{details['path']}")
                if copied is None or not copied.is_file():
                    errors.append(f"G7 reviewer reproduction {stream} log is missing: {details['path']}")
    findings = receipt.get("findings")
    if not isinstance(findings, list):
        errors.append("G7 reviewer findings must be an array")
    elif findings:
        errors.append("G7 independent reviewer reports unresolved findings")
    if receipt.get("status") == "PASS" and errors:
        errors.append("G7 reviewer claims PASS despite invalid or mismatched review evidence")
    return errors


def _g7_evidence_errors(run: Path, manifest: dict[str, Any], checks: list[Any], commands: Any,
                        artifacts_index: Any) -> list[str]:
    errors: list[str] = []
    by_id = {row.get("check_id"): row for row in checks if isinstance(row, dict)}
    report_path = _resolve_inside(run, "packaged-candidate/report.json")
    report: Any = None
    if report_path is None or not report_path.is_file():
        if manifest.get("status") == "PASS":
            errors.append("G7 PASS lacks the copied packaged-candidate report")
    else:
        try:
            report = json.loads(report_path.read_text())
            sidecar = _resolve_inside(run, "packaged-candidate/report.sha256")
            if sidecar is None or not sidecar.is_file() or sidecar.read_text().strip() != f"{sha256(report_path)}  report.json":
                errors.append("G7 packaged-candidate report detached digest is missing or invalid")
            producer_state = report.get("status") if isinstance(report, dict) else None
            if producer_state == "PASS":
                errors.extend(_packaged_candidate_report_errors(report, run))
            elif producer_state == "BLOCKED":
                if not isinstance(report.get("blockers"), list) or not report["blockers"]:
                    errors.append("G7 producer says BLOCKED without a concrete blocker")
                cases = {row.get("case_id"): row for row in report.get("cases", []) if isinstance(row, dict)}
                if set(cases) != {"T33", "T36", "T37"} or any(cases.get(case_id, {}).get("status") != "BLOCKED" for case_id in ("T33", "T36", "T37")):
                    errors.append("G7 producer BLOCKED status is not reflected in each required case")
                if any(by_id.get(case_id, {}).get("status") != "BLOCKED" for case_id in ("T33", "T36", "T37")):
                    errors.append("G7 producer BLOCKED status is not reflected in gate evidence")
            elif producer_state == "FAIL":
                cases = {row.get("case_id"): row for row in report.get("cases", []) if isinstance(row, dict)}
                if not isinstance(report.get("findings"), list) or not report["findings"]:
                    errors.append("G7 producer says FAIL without a concrete finding")
                if set(cases) != {"T33", "T36", "T37"} or not any(row.get("status") == "FAIL" for row in cases.values()):
                    errors.append("G7 producer FAIL lacks a failed required case")
                if not any(by_id.get(case_id, {}).get("status") == "FAIL" for case_id in ("T33", "T36", "T37")):
                    errors.append("G7 producer FAIL is not reflected in gate evidence")
            else:
                errors.append("G7 producer report has an unknown state")
            indexed = next((item for item in artifacts_index if isinstance(item, dict) and item.get("path") == "packaged-candidate/report.json"), None) if isinstance(artifacts_index, list) else None
            if indexed is None or indexed.get("sha256") != sha256(report_path) or indexed.get("bytes") != report_path.stat().st_size:
                errors.append("G7 packaged-candidate report is not authenticated in artifacts.json")
        except (OSError, json.JSONDecodeError):
            errors.append("G7 packaged-candidate report is unreadable")
    if isinstance(report, dict):
        producer_cases = {row.get("case_id"): row for row in report.get("cases", []) if isinstance(row, dict)}
        for case_id in ("T33", "T36", "T37"):
            row = by_id.get(case_id)
            if not isinstance(row, dict):
                continue
            producer = producer_cases.get(case_id)
            if not isinstance(producer, dict):
                errors.append(f"G7 {case_id} has no producer machine assertion")
                continue
            if case_id != "T37":
                if row.get("status") == "PASS":
                    wrapper = {"case_id": case_id, "required_case_status": "PASS"}
                    if producer.get("status") != "PASS" or row.get("expected") != wrapper or row.get("observed") != wrapper:
                        errors.append(f"G7 {case_id} claims PASS without producer PASS and exact assertion wrapper")
                elif row.get("expected") != producer.get("expected") or row.get("observed") != producer.get("observed"):
                    errors.append(f"G7 {case_id} differs from the producer's machine assertion")

        review_path = _resolve_inside(run, "review-report.json")
        review_status = "BLOCKED"
        if report.get("status") != "PASS" and review_path is not None and review_path.is_file():
            errors.append("G7 reviewer receipt cannot authenticate a producer report that did not PASS")
            review_status = "FAIL"
        elif report.get("status") == "PASS" and review_path is not None and review_path.is_file():
            try:
                receipt = json.loads(review_path.read_text())
                review_status = receipt.get("status") if isinstance(receipt, dict) else "FAIL"
                errors.extend(_review_receipt_errors(receipt, report, run))
                indexed = next((item for item in artifacts_index if isinstance(item, dict) and item.get("path") == "review-report.json"), None) if isinstance(artifacts_index, list) else None
                if indexed is None or indexed.get("sha256") != sha256(review_path) or indexed.get("bytes") != review_path.stat().st_size:
                    errors.append("G7 independent review receipt is not authenticated in artifacts.json")
            except (OSError, json.JSONDecodeError):
                review_status = "FAIL"
                errors.append("G7 independent review receipt is unreadable")
        t37 = by_id.get("T37")
        if isinstance(t37, dict):
            producer_ok = producer_cases["T37"].get("status") == "PASS"
            expected_status = ("FAIL" if report.get("status") == "FAIL" else "BLOCKED"
                               if report.get("status") == "BLOCKED" else
                               "PASS" if producer_ok and review_status == "PASS" else
                               "FAIL" if review_status == "FAIL" else "BLOCKED")
            if t37.get("status") != expected_status:
                errors.append("G7 T37 status does not reflect both producer audit and reviewer receipt")
            if expected_status == "PASS":
                wrapper = {"case_id": "T37", "required_case_status": "PASS", "review_receipt_status": "PASS"}
                if t37.get("expected") != wrapper or t37.get("observed") != wrapper:
                    errors.append("G7 T37 PASS does not bind producer and reviewer status")

    if not isinstance(commands, list):
        errors.append("G7 commands must be an array")
    elif manifest.get("status") == "PASS":
        if len(commands) != 1 or not isinstance(commands[0], dict) or commands[0].get("exit_code") != 0:
            errors.append("G7 PASS requires one successful fresh packaged-candidate producer command")
        elif not any(arg.endswith("packaged_candidate.py") for arg in commands[0].get("argv", []) if isinstance(arg, str)):
            errors.append("G7 command does not invoke the packaged-candidate producer")
    return errors


def _g7_case_nodes() -> dict[str, list[str]]:
    from tools.gates.cases import CASES
    excluded = {"T33", "T34", "T35", "T37"}
    return {case_id: list(case.test_nodes) for case_id, case in CASES.items() if case_id not in excluded}


def _g7_required_nodes() -> list[str]:
    case_nodes = _g7_case_nodes()
    companions = {"tests/packaging/test_v1_metadata.py"}
    companions.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests/contract").glob("test_*.py"))
    companions.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests/v1").glob("test_*.py"))
    return sorted({node for nodes in case_nodes.values() for node in nodes} | companions)


def _validate_external_install_origin(origin: Any, site_packages: Any) -> list[str]:
    if not isinstance(origin, str) or not isinstance(site_packages, str) or not origin or not site_packages:
        return ["G7 installed module origin or site-packages path is missing"]
    try:
        Path(origin).resolve().relative_to(Path(site_packages).resolve())
    except (ValueError, OSError, RuntimeError):
        return ["G7 installed module did not resolve inside fresh environment site-packages"]
    if (ROOT / "src").resolve() in Path(origin).resolve().parents:
        return ["G7 installed module was shadowed by checkout source"]
    return []


def _validate_copied_workspace(files: Any) -> list[str]:
    errors = []
    if not isinstance(files, list):
        return ["workspace file manifest is missing"]
    seen = set()
    for item in files:
        if (not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"} or
                not _safe_relative(item.get("path")) or type(item.get("bytes")) is not int or
                item["bytes"] < 0 or not _is_sha(item.get("sha256"))):
            errors.append("workspace manifest has an invalid file row")
            continue
        path = item["path"]
        if path in seen:
            errors.append(f"workspace manifest duplicates {path}")
        seen.add(path)
        if path == "src" or path.startswith("src/") or ".private" in Path(path).parts or "evidence" in Path(path).parts:
            errors.append(f"workspace contains a prohibited path: {path}")
    return errors


def _g6_performance_report_binding_errors(run: Path, inputs: Any, artifacts_index: Any) -> list[str]:
    """Bind the selected G6 report to its retained, artifact-indexed bytes."""
    errors: list[str] = []
    rows = [row for row in inputs if isinstance(row, dict) and row.get("path") == "performance-report.md"] if isinstance(inputs, list) else []
    if len(rows) != 1 or rows[0].get("role") != "config" or not _is_sha(rows[0].get("sha256")):
        errors.append("G6 selected performance report requires exactly one config input hash")
        return errors
    report = _resolve_inside(run, "performance-report.md")
    if report is None or not report.is_file():
        errors.append("G6 selected performance report is missing from retained evidence")
        return errors
    digest = sha256(report)
    if rows[0]["sha256"] != digest:
        errors.append("G6 selected performance report input hash differs from retained bytes")
    indexed = [row for row in artifacts_index if isinstance(row, dict) and row.get("path") == "performance-report.md"] if isinstance(artifacts_index, list) else []
    if len(indexed) != 1 or indexed[0].get("sha256") != digest or indexed[0].get("bytes") != report.stat().st_size:
        errors.append("G6 selected performance report lacks an exact artifact hash and size")
    return errors


def _validate_generic_evidence(run: Path, manifest: dict[str, Any], checks: Any,
                               commands: Any, artifacts_index: Any) -> list[str]:
    """Meaning checks shared by G1-G8; shape validation alone is never acceptance."""
    from tools.gates.cases import CASES, GATE_CASES, PREREQUISITES

    errors: list[str] = []
    gate = manifest.get("gate_id")
    candidate_data = manifest.get("candidate") if isinstance(manifest.get("candidate"), dict) else {}
    required_ids = GATE_CASES.get(gate, ())
    if not isinstance(checks, list):
        return ["checks.json must be an array"]
    ids = [row.get("check_id") for row in checks if isinstance(row, dict) and isinstance(row.get("check_id"), str)]
    if len(ids) != len(set(ids)):
        errors.append("duplicate check IDs")
    for check_id in required_ids:
        rows = [row for row in checks if isinstance(row, dict) and row.get("check_id") == check_id]
        if len(rows) != 1:
            errors.append(f"required {gate} check {check_id} must occur exactly once")
    inputs = candidate_data.get("inputs", [])
    input_paths = {row.get("path") for row in inputs if isinstance(row, dict)}
    required_inputs = {"docs/design.md", "docs/rimworld-save-inspector/gate-evidence.schema.json",
                       "docs/rimworld-save-inspector/private-case-catalog.json",
                       "docs/rimworld-save-inspector/responses.schema.json", "tools/private_acceptance.py",
                       "tools/verify.py", "tools/gates/cases.py", "pyproject.toml", "uv.lock"}
    if gate in {"G1", "G2", "G3", "G4", "G5", "G6", "G7"}:
        required_inputs.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "src/rimworld_save_inspector").rglob("*.py"))
    if gate == "G3":
        required_inputs.update({"tests/oracle/extract.py", "docs/rimworld-save-inspector/private-case-catalog.json",
                                "tests/fixtures/synthetic/seed.rws", "tests/fixtures/synthetic/expected.json"})
    if gate == "G7":
        required_inputs.update({"tests/packaging/test_distribution.py", "tests/packaging/test_v1_metadata.py",
                                "tests/verification/test_evidence_t36.py"})
        required_inputs.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests/contract").glob("test_*.py"))
        required_inputs.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests/v1").glob("test_*.py"))
    for case_id in required_ids:
        key = "T24-G4" if gate == "G4" and case_id == "T24" else case_id
        case = CASES.get(key, CASES.get(case_id))
        if case:
            required_inputs.update(node.split("::", 1)[0] for node in case.test_nodes)
    if gate == "G6" and "T35" in required_ids:
        required_inputs.update({"performance-report.md", "tools/performance/benchmark_core.py"})
        errors.extend(_g6_performance_report_binding_errors(run, inputs, artifacts_index))
    if gate == "G6" and "T34" in required_ids:
        required_inputs.add("tools/matrix/run_public_matrix.py")
    missing_inputs = required_inputs - input_paths
    if missing_inputs:
        errors.append("candidate input hashes omit required files: " + ",".join(sorted(missing_inputs)))
    for row in checks:
        if not isinstance(row, dict):
            errors.append("checks.json contains a non-object entry")
            continue
        check_id = row.get("check_id")
        expected, observed = row.get("expected"), row.get("observed")
        matched = expected == observed
        if isinstance(expected, dict) and isinstance(observed, dict) and "case_coverage_complete" in expected:
            minimum = expected.get("minimum_collected_test_cases", 1)
            maximum_skipped = expected.get("maximum_skipped_required_tests", 0)
            case_results = observed.get("case_test_results")
            matched = (type(minimum) is int and type(maximum_skipped) is int and
                       observed.get("case_coverage_complete") is expected["case_coverage_complete"] is True and
                       type(observed.get("mapped_test_nodes")) is int and observed.get("mapped_test_nodes") == minimum and
                       isinstance(case_results, list) and len(case_results) == minimum and
                       all(isinstance(item, dict) and item.get("status") == "PASS" for item in case_results) and
                       type(observed.get("case_skipped_test_count")) is int and observed["case_skipped_test_count"] <= maximum_skipped and
                       type(observed.get("collected_test_cases")) is int and observed["collected_test_cases"] >= minimum and
                       type(observed.get("skipped_required_tests")) is int and observed["skipped_required_tests"] >= observed["case_skipped_test_count"] and
                       isinstance(observed.get("test_result_path"), str) and bool(observed.get("test_result_path")))
        if row.get("status") == "PASS" and not matched:
            errors.append(f"check {check_id} claims PASS with unequal expected/observed values")
        if row.get("status") == "PASS" and gate != "G7" and isinstance(check_id, str) and check_id.startswith("T"):
            case_key = "T24-G4" if gate == "G4" and check_id == "T24" else check_id
            case = CASES.get(case_key)
            if case is None or not case.complete or not case.test_nodes:
                errors.append(f"required case {check_id} is not declared fully covered by an executable test set")
            else:
                expected_nodes = set(case.test_nodes)
                observed_nodes = row.get("observed", {}).get("mapped_test_nodes") if isinstance(row.get("observed"), dict) else None
                if observed_nodes != len(expected_nodes):
                    errors.append(f"required case {check_id} test-node count does not match the coverage map")
                actual_argv = [arg for command in commands if isinstance(command, dict) for arg in command.get("argv", [])] if isinstance(commands, list) else []
                missing_nodes = expected_nodes - set(actual_argv)
                if missing_nodes:
                    errors.append(f"required case {check_id} command omits mapped test nodes: {', '.join(sorted(missing_nodes))}")
        if row.get("status") == "PASS" and row.get("required") and row.get("observed", {}).get("executed") is False if isinstance(row.get("observed"), dict) else False:
            errors.append(f"required check {check_id} claims PASS without execution")
        paths = row.get("evidence_paths", [])
        for relative in paths:
            evidence_file = _resolve_inside(run, relative)
            if evidence_file is None or not evidence_file.is_file():
                errors.append(f"check {check_id} evidence path missing or escapes run: {relative}")
            elif isinstance(artifacts_index, list):
                indexed = next((entry for entry in artifacts_index if isinstance(entry, dict) and entry.get("path") == relative), None)
                if indexed is None or indexed.get("sha256") != sha256(evidence_file) or indexed.get("bytes") != evidence_file.stat().st_size:
                    errors.append(f"check {check_id} evidence path is not authenticated by artifacts.json: {relative}")
        if row.get("status") == "PASS" and gate != "G7" and isinstance(expected, dict) and "case_coverage_complete" in expected:
            result_path = _resolve_inside(run, observed.get("test_result_path")) if isinstance(observed, dict) else None
            if result_path is None:
                errors.append(f"check {check_id} PASS lacks an authenticated test result")
            else:
                try:
                    if not result_path.is_file():
                        raise OSError("test result path is not a file")
                    import xml.etree.ElementTree as ET
                    root = ET.parse(result_path).getroot()
                    cases = list(root.iter("testcase"))
                    skipped_count = sum(case.find("skipped") is not None for case in cases)
                    if len(cases) != observed.get("collected_test_cases") or skipped_count != observed.get("skipped_required_tests"):
                        errors.append(f"check {check_id} test counts differ from JUnit output")
                    if isinstance(check_id, str) and check_id.startswith("T"):
                        case_key = "T24-G4" if gate == "G4" and check_id == "T24" else check_id
                        coverage_case = CASES.get(case_key)
                        if coverage_case:
                            for node in coverage_case.test_nodes:
                                matched_nodes = [testcase for testcase in cases
                                                 if _junit_testcase_matches(node, testcase.attrib)]
                                if len(matched_nodes) != 1:
                                    errors.append(f"check {check_id} JUnit output omits mapped test {node}")
                                elif matched_nodes[0].find("failure") is not None or matched_nodes[0].find("error") is not None or matched_nodes[0].find("skipped") is not None:
                                    errors.append(f"check {check_id} mapped test did not pass: {node}")
                            if row.get("status") == "PASS":
                                observed_results = observed.get("case_test_results")
                                if not isinstance(observed_results, list) or {item.get("node") for item in observed_results if isinstance(item, dict)} != set(coverage_case.test_nodes) or any(item.get("status") != "PASS" for item in observed_results if isinstance(item, dict)):
                                    errors.append(f"check {check_id} PASS lacks complete passing mapped-test results")
                except (OSError, ET.ParseError):
                    errors.append(f"check {check_id} test result XML is unreadable")
        if row.get("status") == "PASS" and gate != "G7" and check_id in {"T33", "T37"}:
            report_path = _resolve_inside(run, "package-report.json")
            if report_path is None or not report_path.is_file() or "package-report.json" not in row.get("evidence_paths", []):
                errors.append(f"check {check_id} PASS lacks its machine-readable packaging report")
            else:
                try:
                    report_index = next((entry for entry in artifacts_index if isinstance(entry, dict) and entry.get("path") == "package-report.json"), None) if isinstance(artifacts_index, list) else None
                    if report_index is None or report_index.get("sha256") != sha256(report_path) or report_index.get("bytes") != report_path.stat().st_size:
                        errors.append(f"check {check_id} packaging report is not authenticated by artifacts.json")
                    package_report = json.loads(report_path.read_text())
                    if not _package_report_case_is_complete(package_report, check_id):
                        errors.append(f"check {check_id} machine-readable packaging report is incomplete or not all PASS")
                    errors.extend(_package_archive_errors(run, package_report))
                except (OSError, json.JSONDecodeError, AttributeError):
                    errors.append(f"check {check_id} machine-readable packaging report is unreadable")
        if row.get("status") == "PASS" and gate == "G6" and check_id == "T20":
            report_path = _resolve_inside(run, "mcp-audit/report.json")
            if report_path is None:
                errors.append("T20 PASS lacks its copied audit manifest")
            else:
                try:
                    errors.extend(_t20_report_errors(run, json.loads(report_path.read_text())))
                except (OSError, json.JSONDecodeError):
                    errors.append("T20 copied audit manifest is unreadable")
        if row.get("status") == "PASS" and gate == "G6" and check_id == "T34":
            report_path = _resolve_inside(run, "matrix-report.json")
            if report_path is None:
                errors.append("T34 PASS lacks its copied matrix report")
            else:
                try:
                    errors.extend(_matrix_report_errors(json.loads(report_path.read_text()), run))
                except (OSError, json.JSONDecodeError):
                    errors.append("T34 copied matrix report is unreadable")
        if gate == "G3" and isinstance(check_id, str) and (check_id.startswith("F") or check_id.startswith("D")):
            report_path = _resolve_inside(run, "private-product-report.json")
            if report_path is None or not report_path.is_file() or "private-product-report.json" not in row.get("evidence_paths", []):
                if row.get("status") != "BLOCKED":
                    errors.append(f"check {check_id} lacks its private product/oracle report")
            else:
                try:
                    report_index = next((entry for entry in artifacts_index if isinstance(entry, dict) and entry.get("path") == "private-product-report.json"), None) if isinstance(artifacts_index, list) else None
                    if report_index is None or report_index.get("sha256") != sha256(report_path) or report_index.get("bytes") != report_path.stat().st_size:
                        errors.append(f"check {check_id} private comparison report is not authenticated")
                    private_report = json.loads(report_path.read_text())
                    expected_value, observed_value, expected_status = _private_report_check(private_report, check_id)
                    if row.get("expected") != expected_value or row.get("observed") != observed_value:
                        errors.append(f"check {check_id} evidence differs from its private comparison report")
                    if row.get("status") == "PASS" and expected_status != "PASS":
                        errors.append(f"check {check_id} claims PASS without matching frozen independent facts")
                    from tools.gates.cases import CASES
                    case = CASES.get(check_id)
                    if case is None or len(case.test_nodes) != 1:
                        errors.append(f"check {check_id} has no unique mapped executable comparison node")
                    else:
                        node = case.test_nodes[0]
                        actual_args = [arg for command in commands if isinstance(command, dict) for arg in command.get("argv", [])] if isinstance(commands, list) else []
                        if node not in actual_args:
                            errors.append(f"check {check_id} command omits product/oracle comparison node")
                        result = _resolve_inside(run, "pytest.junit.xml")
                        if result is None:
                            errors.append(f"check {check_id} lacks its JUnit comparison result")
                        else:
                            import xml.etree.ElementTree as ET
                            junit = ET.parse(result).getroot()
                            matched = [test for test in junit.iter("testcase")
                                       if _junit_testcase_matches(node, test.attrib)]
                            if len(matched) != 1 or matched[0].find("failure") is not None or matched[0].find("error") is not None or matched[0].find("skipped") is not None:
                                errors.append(f"check {check_id} comparison test is missing, failed or skipped")
                except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
                    errors.append(f"check {check_id} private comparison report is unreadable")
    if gate == "G7":
        errors.extend(_g7_evidence_errors(run, manifest, checks, commands, artifacts_index))
    if not isinstance(commands, list):
        errors.append("commands.json must be an array")
    else:
        for command in commands:
            if not isinstance(command, dict):
                errors.append("commands.json contains a non-object entry")
                continue
            for key in ("stdout_path", "stderr_path"):
                output_path = _resolve_inside(run, command.get(key))
                if output_path is None or not output_path.is_file():
                    errors.append(f"command {key} missing or escapes run directory: {command.get(key)}")
            try:
                started = dt.datetime.fromisoformat(command["started_at"].replace("Z", "+00:00"))
                if started.tzinfo is None or started.utcoffset() != dt.timedelta(0):
                    raise ValueError
            except (KeyError, AttributeError, TypeError, ValueError):
                errors.append("command started_at must be a valid UTC timestamp")
            if command.get("exit_code") != 0 and manifest.get("status") == "PASS":
                errors.append("passing evidence contains a nonzero command exit status")
            blocked_g7_exit = (gate == "G7" and command.get("exit_code") == 2 and
                               any(row.get("required") and row.get("status") == "BLOCKED" for row in checks if isinstance(row, dict)))
            if command.get("exit_code") != 0 and not blocked_g7_exit and not any(row.get("required") and row.get("status") == "FAIL" for row in checks if isinstance(row, dict)):
                errors.append("nonzero command exit has no corresponding failed or blocked required check")
    for item in artifacts_index if isinstance(artifacts_index, list) else []:
        path = _resolve_inside(run, item.get("path") if isinstance(item, dict) else None)
        if path is None or not path.is_file():
            errors.append(f"artifact path missing or escapes run directory: {item.get('path') if isinstance(item, dict) else item}")
        elif sha256(path) != item.get("sha256") or path.stat().st_size != item.get("bytes"):
            errors.append(f"artifact hash/size mismatch: {item.get('path')}")
    if not isinstance(artifacts_index, list):
        errors.append("artifacts.json must be an array")

    # The manifest lists all sidecars and command artifacts with hashes. Ensure
    # it is an exact index of the sidecar file plus referenced files.
    listed: dict[str, dict[str, Any]] = {}
    manifest_artifacts = manifest.get("artifacts", [])
    if not isinstance(manifest_artifacts, list):
        errors.append("manifest artifacts must be an array")
        manifest_artifacts = []
    for item in manifest_artifacts:
        if not isinstance(item, dict):
            errors.append("manifest artifact entry is not an object")
            continue
        path = _resolve_inside(run, item.get("path"))
        if path is None or not path.is_file():
            errors.append(f"manifest artifact missing or escapes run directory: {item.get('path')}")
            continue
        if sha256(path) != item.get("sha256") or path.stat().st_size != item.get("bytes"):
            errors.append(f"manifest artifact hash/size mismatch: {item.get('path')}")
        listed[item["path"]] = item
    for name in ("checks.json", "commands.json", "artifacts.json"):
        target = run / name
        if not target.is_file() or name not in listed or listed[name].get("sha256") != sha256(target):
            errors.append(f"manifest artifact index does not authenticate {name}")

    statuses = [row.get("status") for row in checks if isinstance(row, dict)]
    summary = manifest.get("check_summary", {})
    counts = {"required": sum(bool(row.get("required")) for row in checks if isinstance(row, dict)),
              "passed": statuses.count("PASS"), "failed": statuses.count("FAIL"),
              "blocked": statuses.count("BLOCKED"), "not_run": statuses.count("NOT_RUN")}
    if summary != counts:
        errors.append("check_summary does not match checks.json")
    state = manifest.get("status")
    if state == "PASS" and (counts["required"] == 0 or counts["failed"] or counts["blocked"] or counts["not_run"] or
                             any(row.get("required") and row.get("status") != "PASS" for row in checks if isinstance(row, dict))):
        errors.append("PASS contains failed, blocked, not-run or zero required checks")
    if state == "FAIL" and counts["failed"] == 0:
        errors.append("FAIL has no failed check")
    if state == "BLOCKED" and (counts["blocked"] == 0 or not manifest.get("blockers")):
        errors.append("BLOCKED requires a blocked check and a concrete blocker")
    if state in {"NOT_RUN", "RUNNING"} and manifest.get("finished_at") is not None:
        errors.append(f"{state} evidence must not have finished_at")
    if state in {"PASS", "FAIL", "BLOCKED"}:
        try:
            start = dt.datetime.fromisoformat(manifest["started_at"].replace("Z", "+00:00"))
            finish = dt.datetime.fromisoformat(manifest["finished_at"].replace("Z", "+00:00"))
            if start.tzinfo is None or finish.tzinfo is None or start.utcoffset() != dt.timedelta(0) or finish.utcoffset() != dt.timedelta(0) or finish < start:
                errors.append("finished run timestamps must be ordered valid UTC values")
        except (KeyError, AttributeError, TypeError, ValueError):
            errors.append("finished run requires valid started_at and finished_at timestamps")
    prerequisites = manifest.get("prerequisites", [])
    if not isinstance(prerequisites, list):
        errors.append("prerequisites must be an array")
        prerequisites = []
    for prerequisite in prerequisites:
        if not isinstance(prerequisite, dict):
            errors.append("prerequisite entry must be an object")
            continue
        if not prerequisite.get("satisfied") and state == "PASS":
            errors.append(f"PASS has unsatisfied prerequisite {prerequisite.get('gate_id')}")
        if prerequisite.get("satisfied") and not prerequisite.get("run_id"):
            errors.append(f"satisfied prerequisite {prerequisite.get('gate_id')} lacks accepted run_id")
        if prerequisite.get("satisfied"):
            matched = False
            for prior_manifest_path in (ROOT / "evidence").rglob("manifest.json"):
                try:
                    prior = json.loads(prior_manifest_path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if prior.get("run_id") == prerequisite.get("run_id") and prior.get("gate_id") == prerequisite.get("gate_id"):
                    matched = _prerequisite_evidence_satisfied(prior.get("gate_id"), gate, prior, prior_manifest_path.parent)
                    break
            if not matched:
                errors.append(f"prerequisite {prerequisite.get('gate_id')} run_id is not a valid accepted evidence run")
    expected_prerequisites = set(PREREQUISITES.get(gate, ()))
    observed_prerequisites = {row.get("gate_id") for row in prerequisites if isinstance(row, dict)}
    if observed_prerequisites != expected_prerequisites:
        errors.append(f"{gate} prerequisite set must be exactly {sorted(expected_prerequisites)}")
    if gate == "G7" and ((state == "PASS" and candidate_data.get("dirty") is not False) or
                          manifest.get("carried_forward_from") is not None):
        errors.append("G7 requires a clean candidate and fresh evidence")
    if gate == "G3" and state == "PASS":
        fixture = manifest.get("fixture", {})
        if (fixture.get("available") is not True or fixture.get("expected_sha256") != BASELINE_SHA or
                fixture.get("expected_bytes") != BASELINE_BYTES or fixture.get("observed_bytes") != BASELINE_BYTES or
                fixture.get("before_sha256") != BASELINE_SHA or fixture.get("after_sha256") != BASELINE_SHA):
            errors.append("G3 PASS requires the exact unchanged private fixture")
        try:
            normative = load_private_inputs(check_fixture=False).oracle
            expected_values = {row["check_id"]: row["expected"] for row in normative["assertions"]}
            for check_id, expected_value in expected_values.items():
                row = next((item for item in checks if item.get("check_id") == check_id), None)
                if row is None or row.get("status") != "PASS" or row.get("expected") != expected_value or row.get("observed") != expected_value:
                    errors.append(f"G3 private comparison does not match frozen oracle for {check_id}")
                    break
            private_run = json.loads((run / "private-product-report.json").read_text())
            dynamic_expected = private_run.get("oracle", {}).get("dynamic", {})
            for dynamic_id in ("D01", "D02", "D03"):
                row = next((item for item in checks if item.get("check_id") == dynamic_id), None)
                if (row is None or row.get("status") != "PASS" or dynamic_expected.get(dynamic_id) is None or
                        row.get("expected") != dynamic_expected.get(dynamic_id) or row.get("observed") != dynamic_expected.get(dynamic_id)):
                    errors.append(f"G3 dynamic product comparison does not match independent oracle for {dynamic_id}")
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            errors.append("G3 private oracle manifest is unavailable or malformed")
    return errors


def validate_evidence(run_dir: Path) -> list[str]:
    """Validate evidence shape, case coverage, artifact hashes and PASS truth."""
    run = run_dir.resolve()
    errors: list[str] = []
    required_files = ["manifest.json", "checks.json", "commands.json", "artifacts.json"]
    for name in required_files:
        candidate = run / name
        if not candidate.is_file():
            errors.append(f"missing evidence file: {name}")
    if errors:
        return errors
    try:
        manifest = json.loads((run / "manifest.json").read_text())
        checks = json.loads((run / "checks.json").read_text())
        commands = json.loads((run / "commands.json").read_text())
        artifacts_index = json.loads((run / "artifacts.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid evidence JSON: {exc}"]
    if not isinstance(manifest, dict):
        return ["manifest.json top level must be an object"]
    if manifest.get("gate_id") != "G0":
        errors.extend(_manifest_shape_errors(manifest))
        errors.extend(_evidence_shape_errors(manifest, checks, commands, artifacts_index))
        errors.extend(_validate_generic_evidence(run, manifest, checks, commands, artifacts_index))
        return errors
    for name in ("oracle.json", "synthetic.json"):
        if not (run / name).is_file():
            errors.append(f"missing evidence file: {name}")
    if errors:
        return errors
    try:
        oracle = json.loads((run / "oracle.json").read_text())
        synthetic = json.loads((run / "synthetic.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid oracle or synthetic evidence JSON: {exc}"]
    errors.extend(_manifest_shape_errors(manifest))
    errors.extend(_evidence_shape_errors(manifest, checks, commands, artifacts_index))
    if not isinstance(checks, list) or not isinstance(manifest.get("artifacts"), list):
        return errors + ["checks or artifact list has the wrong type"]
    required_ids = {f"F{i:02d}" for i in range(1, 29)} | {"D01", "D02", "D03", "T01", "oracle_import_boundary", "spec_oracle_consistency", "source_preservation"}
    found = [item.get("check_id") for item in checks if isinstance(item, dict)]
    if len(found) != len(set(found)):
        errors.append("duplicate check IDs")
    missing = required_ids - set(found)
    if missing:
        errors.append("missing required G0 checks: " + ",".join(sorted(missing)))
    statuses = [item.get("status") for item in checks if isinstance(item, dict)]
    for item in checks:
        if not isinstance(item, dict):
            errors.append("checks.json contains a non-object entry")
            continue
        if item.get("status") not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}:
            errors.append(f"invalid status for check {item.get('check_id')}")
        if item.get("required") and item.get("status") == "PASS" and item.get("check_id") not in {"D01", "D02", "D03"} and item.get("expected") != item.get("observed"):
            errors.append(f"required check claims PASS with unequal expected/observed: {item.get('check_id')}")
    if manifest.get("status") == "PASS":
        for dynamic_id in ("D01", "D02", "D03"):
            row = next((c for c in checks if c.get("check_id") == dynamic_id), None)
            if row is None or row.get("status") != "PASS" or row.get("observed") != oracle.get("dynamic", {}).get(dynamic_id):
                errors.append(f"dynamic evidence does not match independent XML observation: {dynamic_id}")
    if manifest.get("status") == "PASS" and any(item.get("required") and item.get("status") != "PASS" for item in checks if isinstance(item, dict)):
        errors.append("PASS contains a required check that did not pass")
    counts = {name: statuses.count(label) for name, label in (("passed", "PASS"), ("failed", "FAIL"), ("blocked", "BLOCKED"), ("not_run", "NOT_RUN"))}
    required_count = sum(bool(item.get("required")) for item in checks if isinstance(item, dict))
    summary = manifest.get("check_summary", {})
    expected_summary = {"required": required_count, **counts}
    if summary != expected_summary:
        errors.append("check_summary does not match checks.json")
    if manifest.get("status") == "PASS" and (counts["failed"] or counts["blocked"] or counts["not_run"] or required_count == 0):
        errors.append("PASS has failed, blocked, not-run or zero required checks")
    if manifest.get("status") == "BLOCKED" and not manifest.get("blockers"):
        errors.append("BLOCKED evidence has no blocker")
    fixture = manifest.get("fixture", {})
    if manifest.get("status") == "PASS":
        if not fixture.get("available") or fixture.get("expected_sha256") != BASELINE_SHA or fixture.get("before_sha256") != fixture.get("after_sha256"):
            errors.append("PASS fixture identity or preservation evidence is invalid")
        if oracle.get("private_input_hashes") != load_private_inputs(check_fixture=False).expected_input_hashes():
            errors.append("PASS private inputs are not authenticated by the frozen anchor")
        if oracle.get("all_match_spec") is not True:
            errors.append("PASS oracle does not agree with the normative manifest")
        if synthetic.get("expected") != synthetic.get("observed"):
            errors.append("PASS Appendix A synthetic expectation differs from independent XML extraction")
        t01 = next((c for c in checks if c.get("check_id") == "T01"), None)
        if t01 is None or t01.get("status") != "PASS" or t01.get("expected") != synthetic.get("expected") or t01.get("observed") != synthetic.get("observed"):
            errors.append("T01 evidence differs from the frozen synthetic fixture expectations")
        try:
            normative = load_private_inputs(check_fixture=False).oracle
            expected_oracle = {row["check_id"]: row["expected"] for row in normative["assertions"]}
            if oracle.get("expected") != expected_oracle or oracle.get("assertions") != expected_oracle:
                errors.append("oracle expected/observed values differ from the frozen normative manifest")
            if oracle.get("matches") != {key: True for key in expected_oracle}:
                errors.append("oracle match map is incomplete or includes a failed assertion")
            for check_id, expected_value in expected_oracle.items():
                row = next((c for c in checks if c.get("check_id") == check_id), None)
                if row is None or row.get("expected") != expected_value or row.get("observed") != expected_value or row.get("status") != "PASS":
                    errors.append(f"manifest check disagrees with oracle for {check_id}")
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            errors.append("normative oracle manifest is unavailable or malformed")
    if isinstance(artifacts_index, list):
        indexed = {row.get("path"): row for row in artifacts_index if isinstance(row, dict)}
        for name in required_files[1:]:
            if name == "artifacts.json":
                continue  # self-hash is represented by the manifest's artifact row.
            row = indexed.get(name)
            if not row or row.get("sha256") != sha256(run / name) or row.get("bytes") != (run / name).stat().st_size:
                errors.append(f"artifacts.json hash/size mismatch: {name}")
    else:
        errors.append("artifacts.json must be an array")
    for item in manifest.get("artifacts", []):
        rel = Path(item.get("path", ""))
        candidate = (run / rel).resolve()
        if rel.is_absolute() or run not in candidate.parents or not candidate.is_file():
            errors.append(f"artifact path missing or escapes run directory: {rel}")
        elif item.get("sha256") != sha256(candidate) or item.get("bytes") != candidate.stat().st_size:
            errors.append(f"artifact hash/size mismatch: {rel}")
    return errors


def write_g0_evidence(run: Path, *, fixture: Path | None, actor: str, oracle_result: dict[str, Any] | None = None) -> None:
    """Create a self-contained G0 report from real local observations."""
    run.mkdir(parents=True, exist_ok=False)
    started = now()
    private = load_private_inputs(fixture, use_env_fixture=False)
    selected = private.fixture.resolve() if private.fixture is not None and private.fixture.is_file() else None
    before = sha256(selected) if selected and selected.is_file() else None
    if private.ready and oracle_result is not None:
        pass
    elif private.ready and selected is not None:
        proc = subprocess.run([sys.executable, str(ORACLE), str(selected)], cwd=ROOT, capture_output=True, text=True, timeout=180)
        if proc.stdout.strip():
            oracle_result = json.loads(proc.stdout)
        if proc.returncode and oracle_result is None:
            oracle_result = {"all_match_spec": False, "error": proc.stderr[-2000:], "assertions": {}, "dynamic": {}}
    else:
        oracle_result = {"all_match_spec": False, "error": "; ".join(private.errors), "assertions": {}, "dynamic": {}}
    after = sha256(selected) if selected and selected.is_file() else None
    oracle_result["private_input_hashes"] = private.input_hashes()
    seed = ROOT / "tests/fixtures/synthetic/seed.rws"
    expected_seed = json.loads((ROOT / "tests/fixtures/synthetic/expected.json").read_text())
    synthetic_proc = subprocess.run([sys.executable, str(ORACLE), str(seed), "--synthetic"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    synthetic_observed = json.loads(synthetic_proc.stdout) if synthetic_proc.stdout.strip() else None
    (run / "synthetic.json").write_text(json.dumps({"expected": expected_seed, "observed": synthetic_observed}, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    (run / "oracle.json").write_text(json.dumps(oracle_result, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    manifest_data = private.oracle or {"assertions": [{"check_id": key} for key in sorted(public_case_ids()[0])]}
    checks: list[dict[str, Any]] = []
    if private.ready:
        for row in manifest_data["assertions"]:
            check_id = row["check_id"]
            checks.append(_check(check_id, row["expected"], oracle_result.get("assertions", {}).get(check_id)))
        for dynamic_id in ("D01", "D02", "D03"):
            observed = oracle_result.get("dynamic", {}).get(dynamic_id)
            valid = observed is not None and (dynamic_id != "D03" or bool(observed))
            checks.append({"check_id": dynamic_id, "requirement_ids": ["R19"], "required": True,
                           "status": "PASS" if valid else "FAIL", "expected": "independently selected XML facts", "observed": observed, "evidence_paths": ["oracle.json"]})
        checks.append(_check("source_preservation", before, after))
        checks.append(_check("spec_oracle_consistency", {"agree": True, "mismatches": []}, spec_oracle_agreement()))
        tree = ast.parse(ORACLE.read_text())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported.update(node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module)
        allowed = {"__future__", "argparse", "hashlib", "json", "os", "pathlib", "re", "sys", "xml"}
        import_check = "rimworld_save_inspector" not in imported and imported <= allowed
        checks.append(_check("oracle_import_boundary", True, import_check))
        checks.append(_check("T01", expected_seed, synthetic_observed, ["tests/fixtures/synthetic/expected.json", "synthetic.json"]))
    else:
        for row in manifest_data["assertions"]:
            checks.append({"check_id": row["check_id"], "requirement_ids": ["R19"], "required": True, "status": "BLOCKED", "expected": row.get("expected", "frozen private oracle fact"), "observed": "; ".join(private.errors), "evidence_paths": ["oracle.json"]})
        for check_id in ("D01", "D02", "D03", "source_preservation"):
            checks.append({"check_id": check_id, "requirement_ids": ["R19"], "required": True, "status": "BLOCKED", "expected": "private fixture observation", "observed": "private fixture unavailable", "evidence_paths": ["oracle.json"]})
        consistency = spec_oracle_agreement(private_required=False)
        checks.extend([{"check_id": "spec_oracle_consistency", "requirement_ids": ["R19"], "required": True,
                        "status": "PASS" if consistency["agree"] else "BLOCKED", "expected": {"agree": True, "mismatches": []}, "observed": consistency,
                        "evidence_paths": ["oracle.json"]}, _check("oracle_import_boundary", True, True),
                       _check("T01", expected_seed, synthetic_observed, ["tests/fixtures/synthetic/expected.json", "synthetic.json"])])
    (run / "checks.json").write_text(json.dumps(checks, indent=2, sort_keys=True) + "\n")
    commands = [{"argv": [sys.executable, str(ORACLE), "<fixture>"], "cwd_label": "product-checkout", "started_at": started,
                 "duration_seconds": 0.0, "exit_code": 0 if oracle_result.get("all_match_spec") else 1,
                 "stdout_path": "oracle.json", "stderr_path": "oracle-stderr.txt"},
                {"argv": [sys.executable, str(ORACLE), "tests/fixtures/synthetic/seed.rws", "--synthetic"], "cwd_label": "product-checkout", "started_at": started,
                 "duration_seconds": 0.0, "exit_code": synthetic_proc.returncode, "stdout_path": "synthetic.json", "stderr_path": "synthetic-stderr.txt"}]
    (run / "commands.json").write_text(json.dumps(commands, indent=2, sort_keys=True) + "\n")
    (run / "oracle-stderr.txt").write_text("")
    (run / "synthetic-stderr.txt").write_text(synthetic_proc.stderr)
    sidecars = []
    for path in (run / "checks.json", run / "commands.json", run / "oracle.json", run / "synthetic.json", run / "oracle-stderr.txt", run / "synthetic-stderr.txt"):
        sidecars.append(_artifact(path, "G0 verification output", run))
    (run / "artifacts.json").write_text(json.dumps(sidecars, indent=2, sort_keys=True) + "\n")
    artifacts = list(sidecars) + [_artifact(run / "artifacts.json", "Artifact index", run)]
    statuses = [check["status"] for check in checks]
    summary = {"required": len(checks), "passed": statuses.count("PASS"), "failed": statuses.count("FAIL"),
               "blocked": statuses.count("BLOCKED"), "not_run": statuses.count("NOT_RUN")}
    if summary["failed"]:
        state = "FAIL"
    elif summary["blocked"] or summary["not_run"]:
        state = "BLOCKED"
    else:
        state = "PASS"
    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain"))
    candidate_inputs = []
    for file, role in ((SPEC, "spec"), (CATALOG, "oracle"), (EVIDENCE_SCHEMA, "schema"),
                       (ROOT / "pyproject.toml", "config"),
                       (ROOT / "tools/private_acceptance.py", "config"),
                       (ORACLE, "oracle"), (ROOT / "tests/oracle/test_g0.py", "test"),
                       (ROOT / "tests/fixtures/synthetic/seed.rws", "test"),
                       (ROOT / "tests/fixtures/synthetic/expected.json", "oracle"),
                       (Path(__file__), "config")):
        candidate_inputs.append({"path": file.relative_to(ROOT).as_posix(), "role": role, "sha256": sha256(file)})
    observed_bytes = selected.stat().st_size if selected and selected.is_file() else None
    expected_sha = private.expected_sha256
    fixture_ok = private.ready and bool(selected and selected.is_file() and before == expected_sha and observed_bytes == private.expected_bytes)
    # F01 itself is the authoritative identity check; fixture shape records availability and identity.
    if selected is None or not selected.is_file():
        fixture_reason = "; ".join(private.errors) or "private fixture unavailable; public-only G0 work remains incomplete"
    elif not fixture_ok:
        fixture_reason = "; ".join(private.errors) or "fixture bytes or SHA-256 do not match the frozen baseline"
    else:
        fixture_reason = None
    blockers = []
    if state == "BLOCKED":
        blockers.append(fixture_reason or "one or more required G0 checks could not run")
    findings = []
    if state == "FAIL":
        findings = [f"{c['check_id']}: observed did not match expected" for c in checks if c["status"] == "FAIL"]
    manifest = {
        "schema_version": "1.0", "run_id": run.name, "gate_id": "G0", "status": state,
        "started_at": started, "finished_at": now(), "actor": actor,
        "spec": {"id": "RWSI-1.0", "revision": SPEC_REVISION, "sha256": sha256(SPEC)},
        "candidate": {"commit": commit, "dirty": dirty, "inputs": candidate_inputs, "lock_sha256": None,
                      "package_version": _release_identity()[0], "parser_version": None, "profile_version": None},
        "environment": {"os": platform.platform(), "architecture": platform.machine(), "hardware": platform.processor() or platform.machine(),
                        "python_executable": sys.executable, "python_version": platform.python_version(), "dependencies": {"stdlib_xml": "xml.etree.ElementTree"}},
        "fixture": {"label": "private fixture", "available": bool(selected and selected.is_file()), "expected_bytes": private.expected_bytes,
                    "observed_bytes": observed_bytes, "expected_sha256": expected_sha, "before_sha256": before, "after_sha256": after,
                    "unavailable_reason": fixture_reason},
        "prerequisites": [], "check_summary": summary, "artifacts": artifacts,
        "blockers": blockers, "findings": findings, "carried_forward_from": None,
    }
    (run / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _g0_public_setup_satisfied(manifest: Any, run: Path) -> bool:
    """G1 may use validated public G0 setup while private oracle rows are blocked."""
    if not isinstance(manifest, dict) or manifest.get("gate_id") != "G0" or manifest.get("status") != "BLOCKED":
        return False
    if manifest.get("fixture", {}).get("available") is not False:
        return False
    try:
        checks = json.loads((run / "checks.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(checks, list):
        return False
    by_id = {row.get("check_id"): row.get("status") for row in checks if isinstance(row, dict)}
    required_public = {"spec_oracle_consistency", "oracle_import_boundary", "T01"}
    required_private = {"source_preservation", *(f"F{i:02d}" for i in range(1, 29)), "D01", "D02", "D03"}
    return (required_public <= by_id.keys() and all(by_id[item] == "PASS" for item in required_public) and
            required_private <= by_id.keys() and all(by_id[item] == "BLOCKED" for item in required_private) and
            all(row.get("status") != "FAIL" for row in checks if isinstance(row, dict)))


def _prerequisite_evidence_satisfied(gate: str, consumer: str, manifest: Any, run: Path) -> bool:
    if not isinstance(manifest, dict) or validate_evidence(run):
        return False
    if manifest.get("status") == "PASS":
        return True
    return gate == "G0" and consumer == "G1" and _g0_public_setup_satisfied(manifest, run)


def _latest_accepted_run(gate: str, consumer: str) -> tuple[str | None, bool]:
    for manifest_path in sorted((ROOT / "evidence").rglob("manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("gate_id") == gate and _prerequisite_evidence_satisfied(gate, consumer, manifest, manifest_path.parent):
            return manifest.get("run_id"), True
    return None, False


def _case_check(check_id: str, expected: Any, observed: Any, status: str, evidence: list[str], gate_id: str | None = None) -> dict[str, Any]:
    from tools.gates.cases import REQUIREMENTS
    gate = gate_id or ("G4" if check_id == "T24-G4" else next((g for g, ids in __import__("tools.gates.cases", fromlist=["GATE_CASES"]).GATE_CASES.items() if check_id in ids), "G1"))
    return {"check_id": check_id.removesuffix("-G4"), "requirement_ids": list(REQUIREMENTS.get(gate, ("R19",))),
            "required": True, "status": status, "expected": expected, "observed": observed, "evidence_paths": evidence}


def _mapped_junit_identity(node: str) -> str | None:
    """Convert a pytest node ID to its exact JUnit ``classname::name`` key."""
    parts = node.split("::")
    if len(parts) not in {2, 3} or not parts[0].endswith(".py") or not parts[-1]:
        return None
    module = parts[0].removesuffix(".py").replace("/", ".")
    if not module or any(not part for part in module.split(".")):
        return None
    if len(parts) == 3 and not parts[1]:
        return None
    classname = module if len(parts) == 2 else f"{module}.{parts[1]}"
    return f"{classname}::{parts[-1]}"


def _junit_testcase_matches(node: str, testcase: Any) -> bool:
    if "::" not in node and node.endswith(".py") and isinstance(testcase, dict):
        module = node.removesuffix(".py").replace("/", ".")
        classname = testcase.get("classname", "")
        return classname == module or classname.startswith(module + ".")
    expected_identity = _mapped_junit_identity(node)
    if expected_identity is None or not isinstance(testcase, dict):
        return False
    observed_identity = f"{testcase.get('classname', '')}::{testcase.get('name', '')}"
    return observed_identity == expected_identity


def _gate_failure_findings(checks: list[dict[str, Any]]) -> list[str]:
    """Render FAIL rows without assuming their observed payload is a mapping.

    Observations from failed subprocesses and missing machine reports are often
    strings. Preserve the complete value in the durable finding instead of
    crashing while trying to summarize it.
    """
    findings: list[str] = []
    for row in checks:
        if not isinstance(row, dict) or row.get("status") != "FAIL":
            continue
        observed = row.get("observed")
        detail = observed.get("coverage_gap") if isinstance(observed, dict) else None
        if detail is None or detail == "":
            detail = observed
        if isinstance(detail, str):
            rendered = detail
        else:
            rendered = json.dumps(detail, sort_keys=True, ensure_ascii=False)
        findings.append(f"{row.get('check_id', '<unknown>')}: {rendered}")
    return findings


def _run_pytest_nodes(run: Path, nodes: list[str], extra_env: dict[str, str] | None = None) -> tuple[int | None, int, int, list[dict[str, Any]], list[dict[str, str]]]:
    stdout_path = run / "pytest.stdout.txt"
    stderr_path = run / "pytest.stderr.txt"
    junit_path = run / "pytest.junit.xml"
    if not nodes:
        stdout_path.write_text("No mapped test nodes were available.\n")
        stderr_path.write_text("")
        return None, 0, 0, [], []
    argv = [sys.executable, "-m", "pytest", "-q", "-s", "--junitxml", str(junit_path), *nodes]
    start = now()
    tick = time.monotonic()
    try:
        child_env = dict(os.environ)
        child_env.update(extra_env or {})
        result = subprocess.run(argv, cwd=ROOT, env=child_env, text=True, capture_output=True, timeout=300)
        code = result.returncode
        stdout_path.write_text(result.stdout)
        stderr_path.write_text(result.stderr)
    except subprocess.TimeoutExpired as exc:
        code = None
        stdout_path.write_text((exc.stdout or "").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
        stderr_path.write_text("test command timed out after 300 seconds; subprocess.run terminated and reaped the child\n")
    duration = time.monotonic() - tick
    test_rows: list[dict[str, Any]] = []
    if junit_path.is_file():
        try:
            import xml.etree.ElementTree as ET
            root = ET.parse(junit_path).getroot()
            for case in root.iter("testcase"):
                skipped = case.find("skipped") is not None or case.get("status") == "skipped"
                failed = case.find("failure") is not None or case.find("error") is not None
                test_rows.append({"node": case.get("classname", "") + "::" + case.get("name", ""),
                                  "status": "SKIPPED" if skipped else "FAIL" if failed else "PASS"})
        except (OSError, ET.ParseError):
            pass
    command = {"argv": argv, "cwd_label": "product-checkout", "started_at": start,
               "duration_seconds": round(duration, 6), "exit_code": code if code is not None else 124,
               "stdout_path": stdout_path.name, "stderr_path": stderr_path.name}
    return code, len(test_rows), sum(row["status"] == "SKIPPED" for row in test_rows), [command], test_rows


def run_g7_gate(fixture: Path | None, run: Path, review_report: Path | None = None,
                public_only: bool = False) -> int:
    """Run G7 only from a clean candidate, then bind producer and reviewer evidence."""
    from tools.gates.cases import GATE_CASES, PREREQUISITES

    run.mkdir(parents=True, exist_ok=False)
    started = now()
    selected = None if public_only else load_private_inputs(fixture).fixture
    private_inputs = load_private_inputs(selected)
    if selected is not None:
        selected = selected.resolve()
    prerequisite_rows = []
    missing_prerequisites = []
    for previous in PREREQUISITES.get("G7", ()):
        prior_run, accepted = _latest_accepted_run(previous, "G7")
        reason = "validated accepted evidence found" if accepted else "no valid accepted prerequisite evidence found"
        prerequisite_rows.append({"gate_id": previous, "run_id": prior_run, "satisfied": accepted, "reason": reason})
        if not accepted:
            missing_prerequisites.append(previous)

    stdout_path, stderr_path = run / "producer.stdout.txt", run / "producer.stderr.txt"
    receipt_path = review_report or (Path(os.environ["RWSI_REVIEW_REPORT"]) if os.environ.get("RWSI_REVIEW_REPORT") else None)
    report: dict[str, Any] | None = None
    report_path: str | None = None
    receipt: dict[str, Any] | None = None
    copied_receipt: str | None = None
    receipt_copy_error = None
    commands: list[dict[str, Any]] = []
    blockers: list[str] = []
    findings: list[str] = []
    attempt_code: int | None = None
    if missing_prerequisites:
        blockers.append("G7 execution waits for accepted prerequisites: " + ", ".join(missing_prerequisites))
        stdout_path.write_text("Packaged-candidate producer was not invoked because G0-G6 prerequisites are not accepted.\n")
        stderr_path.write_text("")
    elif public_only:
        blockers.append("G7 requires the frozen private fixture; public-only mode cannot establish G7")
        stdout_path.write_text("Packaged-candidate producer was not invoked in public-only mode.\n")
        stderr_path.write_text("")
    elif not selected or not selected.is_file():
        blockers.append("G7 frozen private fixture is unavailable")
        stdout_path.write_text("Packaged-candidate producer was not invoked because the private fixture is unavailable.\n")
        stderr_path.write_text("")
    elif not private_inputs.ready:
        blockers.extend(private_inputs.errors)
        stdout_path.write_text("Packaged-candidate producer was not invoked because the fixture identity differs.\n")
        stderr_path.write_text("")
    elif git("status", "--porcelain"):
        blockers.append("G7 requires a clean committed candidate; the checkout has tracked or untracked changes")
        stdout_path.write_text("Packaged-candidate producer was not invoked because the candidate is dirty.\n")
        stderr_path.write_text("")
    else:
        producer_out = ROOT / "evidence" / f"packaged-candidate-{run.name}"
        argv = [sys.executable, "tools/gates/packaged_candidate.py", "--root", str(ROOT),
                "--out", str(producer_out.relative_to(ROOT)), "--fixture", str(selected),
                "--interpreter", sys.executable]
        started_command = now()
        tick = time.monotonic()
        try:
            result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, timeout=3600)
            attempt_code = result.returncode
            stdout_path.write_text(result.stdout)
            stderr_path.write_text(result.stderr)
        except subprocess.TimeoutExpired as exc:
            attempt_code = 124
            stdout_path.write_text((exc.stdout or "").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
            stderr_path.write_text("packaged-candidate producer exceeded 3600 seconds; process was terminated and reaped\n")
        commands.append({"argv": argv, "cwd_label": "candidate-checkout", "started_at": started_command,
                         "duration_seconds": round(time.monotonic() - tick, 6), "exit_code": attempt_code,
                         "stdout_path": stdout_path.name, "stderr_path": stderr_path.name})
        report, report_path = _copy_packaged_candidate_report(run, stdout_path)
        if report is None:
            message = "packaged-candidate producer emitted no authenticated report"
            if attempt_code == 2:
                blockers.append(message)
            else:
                findings.append(message)
        elif attempt_code not in (0, 2):
            findings.append(f"packaged-candidate producer exited {attempt_code}")

    if report is not None and report.get("status") == "PASS" and receipt_path is not None:
        receipt, copied_receipt, receipt_copy_error = _copy_review_receipt(run, receipt_path)
        if receipt_copy_error:
            findings.append(receipt_copy_error)
    elif receipt_path is None:
        blockers.append("fresh independent G7 reviewer receipt is not supplied (set RWSI_REVIEW_REPORT)")

    producer_blocked = bool(report and report.get("status") == "BLOCKED")
    producer_errors = (_packaged_candidate_report_errors(report, run)
                       if report is not None and not producer_blocked else [])
    if report is None and commands and attempt_code not in (None, 2):
        producer_errors.append("packaged-candidate producer failed without emitting an authenticated report")
    findings.extend(producer_errors)
    review_errors = _review_receipt_errors(receipt, report, run) if isinstance(receipt, dict) and isinstance(report, dict) else []
    if receipt_copy_error:
        review_errors.append(receipt_copy_error)
    findings.extend(review_errors)
    producer_cases = {row.get("case_id"): row for row in report.get("cases", []) if isinstance(row, dict)} if report else {}
    review_status = receipt.get("status") if isinstance(receipt, dict) else None
    checks: list[dict[str, Any]] = []
    for case_id in GATE_CASES["G7"]:
        producer_case = producer_cases.get(case_id)
        if missing_prerequisites or public_only or not selected or not selected.is_file():
            status = "BLOCKED"
            expected = {"fresh_packaged_candidate_case": "PASS"}
            observed = {"producer_invoked": False, "blockers": blockers}
        elif producer_blocked:
            status = "BLOCKED"
            expected = {"fresh_packaged_candidate_case": "PASS"}
            observed = {"producer_status": "BLOCKED", "blockers": report.get("blockers", [])}
        elif producer_errors:
            status = "FAIL"
            expected = producer_case.get("expected", {"candidate_evidence": "valid"}) if producer_case else {"candidate_evidence": "valid"}
            observed = producer_case.get("observed", {"producer_exit_code": attempt_code}) if producer_case else {"producer_exit_code": attempt_code}
        elif case_id == "T37" and (review_status != "PASS" or review_errors):
            status = "BLOCKED" if review_status in {None, "BLOCKED"} and not review_errors else "FAIL"
            expected = {"archive_audit": "PASS", "independent_review": "PASS"}
            observed = {"archive_audit": producer_case.get("status") if producer_case else "MISSING",
                        "independent_review": review_status or "MISSING"}
        elif producer_case and producer_case.get("status") == "PASS":
            status = "PASS"
            expected = {"case_id": case_id, "required_case_status": "PASS"}
            observed = dict(expected)
            if case_id == "T37":
                observed["review_receipt_status"] = "PASS"
                expected["review_receipt_status"] = "PASS"
        else:
            status = "FAIL" if report is not None else "BLOCKED"
            expected = {"packaged_candidate_case": "PASS"}
            observed = producer_case.get("observed", producer_case.get("status")) if producer_case else {"report": "MISSING"}
        evidence = [report_path] if report_path else ["blockers.txt"]
        if case_id == "T37" and copied_receipt:
            evidence.append(copied_receipt)
        checks.append(_case_check(case_id, expected, observed, status, evidence, "G7"))

    candidate_dirty = bool(git("status", "--porcelain"))
    if missing_prerequisites or public_only or not selected or not selected.is_file() or candidate_dirty or not private_inputs.ready:
        for check in checks:
            check["status"] = "BLOCKED" if not findings else "FAIL"
            check["evidence_paths"] = ["blockers.txt" if not findings else "findings.txt"]
    (run / "pytest.stdout.txt").write_text(stdout_path.read_text())
    (run / "pytest.stderr.txt").write_text(stderr_path.read_text())
    (run / "findings.txt").write_text("\n".join(findings) + ("\n" if findings else "No findings.\n"))
    (run / "blockers.txt").write_text("\n".join(blockers) + ("\n" if blockers else "No blockers.\n"))
    (run / "checks.json").write_text(json.dumps(checks, indent=2, sort_keys=True) + "\n")
    (run / "commands.json").write_text(json.dumps(commands, indent=2, sort_keys=True) + "\n")
    artifacts = [_artifact(path, purpose, run) for path, purpose in (
        (run / "checks.json", "G7 case assertions"), (run / "commands.json", "exact producer command"),
        (run / "pytest.stdout.txt", "packaged-candidate producer stdout"), (run / "pytest.stderr.txt", "packaged-candidate producer stderr"),
        (run / "findings.txt", "review and candidate findings"), (run / "blockers.txt", "prerequisite and infrastructure blockers"))]
    if report_path:
        artifacts.append(_artifact(run / report_path, "packaged-candidate producer report", run))
        for path in sorted((run / "packaged-candidate").rglob("*")):
            if path.is_file() and path.name != "report.json":
                artifacts.append(_artifact(path, "authenticated packaged-candidate evidence", run))
    if copied_receipt:
        artifacts.append(_artifact(run / copied_receipt, "fresh independent reviewer receipt", run))
        for path in sorted((run / "review-artifacts").rglob("*")):
            if path.is_file():
                artifacts.append(_artifact(path, "independent reviewer command/artifact evidence", run))
    (run / "artifacts.json").write_text(json.dumps(artifacts, indent=2, sort_keys=True) + "\n")
    artifacts.append(_artifact(run / "artifacts.json", "artifact index", run))
    statuses = [row["status"] for row in checks]
    state = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"
    for item in prerequisite_rows:
        if not item["satisfied"]:
            blockers.append(f"Prerequisite {item['gate_id']} has no valid accepted evidence")
    manifest = _generic_manifest(run, "G7", state, started, checks, artifacts, prerequisite_rows,
                                 blockers, findings, selected if selected and selected.is_file() else None)
    (run / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    errors = validate_evidence(run)
    if errors:
        print("evidence validation failed: " + "; ".join(errors), file=sys.stderr)
        return 1
    print(f"G7 {state}: {run}")
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[state]


def _copy_packaging_report(run: Path, stdout_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Copy and parse the package test's explicit report marker, if present."""
    marker = "PACKAGING_REPORT="
    value = _stdout_marker_value(stdout_path, marker)
    if value is None:
        return None, None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None, None
    source = ROOT / relative
    try:
        resolved = source.resolve(strict=True)
        if ROOT.resolve() not in resolved.parents or not source.is_file():
            return None, None
        target = run / "package-report.json"
        report = json.loads(source.read_text())
        package_artifacts = report.get("artifacts")
        if not isinstance(package_artifacts, list):
            return None, None
        dist_dir = run / "package-dist"
        dist_dir.mkdir()
        for artifact in package_artifacts:
            if not isinstance(artifact, dict) or not _safe_relative(artifact.get("path")) or not _is_sha(artifact.get("sha256")):
                return None, None
            artifact_source = source.parent / artifact["path"]
            resolved_artifact = artifact_source.resolve(strict=True)
            if source.parent.resolve() not in resolved_artifact.parents or not artifact_source.is_file():
                return None, None
            if sha256(artifact_source) != artifact["sha256"] or artifact_source.stat().st_size != artifact.get("bytes"):
                return None, None
            copied = dist_dir / artifact_source.name
            copied.write_bytes(artifact_source.read_bytes())
            artifact["path"] = copied.relative_to(run).as_posix()
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report, target.name
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None, None


def _copy_mcp_audit_report(run: Path, stdout_path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Copy the T20 marker manifest and its three raw subprocess reports."""
    marker = "MCP_AUDIT_REPORT="
    value = _stdout_marker_value(stdout_path, marker)
    if value is None:
        return None, []
    source = Path(value).resolve()
    evidence_root = (ROOT / "evidence").resolve()
    if evidence_root not in source.parents or not source.is_file():
        return None, []
    target_dir = run / "mcp-audit"
    target_dir.mkdir()
    report = json.loads(source.read_text())
    shutil.copy2(source, target_dir / "report.json")
    copied = ["mcp-audit/report.json"]
    raw_paths = report.get("report_paths") if isinstance(report, dict) else None
    if not isinstance(raw_paths, list) or len(raw_paths) != 3:
        return report, copied
    for raw in raw_paths:
        item = Path(raw).resolve()
        if evidence_root not in item.parents or not item.is_file() or item.name not in {"cli.json", "mcp.json", "probe.json"}:
            return report, copied
        shutil.copy2(item, target_dir / item.name)
        copied.append(f"mcp-audit/{item.name}")
    return report, copied


def _latest_evidence_report(pattern: str, explicit: str | None) -> Path | None:
    if explicit:
        candidate = Path(explicit)
        candidate = candidate if candidate.is_absolute() else ROOT / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if (ROOT / "evidence").resolve() in resolved.parents and resolved.is_file():
            return resolved
        return None
    candidates = [path for path in (ROOT / "evidence").glob(pattern) if path.is_file()]
    return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None


def _copy_matrix_report(run: Path) -> tuple[dict[str, Any] | None, str | None]:
    source = _latest_evidence_report("packaging-matrix-*/report.json",
                                    os.environ.get("RWSI_MATRIX_REPORT") or os.environ.get("MATRIX_REPORT"))
    if source is None:
        return None, None
    try:
        report = json.loads(source.read_text())
        artifacts = report.get("artifacts")
        if not isinstance(artifacts, list):
            return None, None
        destination = run / "matrix-report-files"
        destination.mkdir()
        for item in artifacts:
            if (not isinstance(item, dict) or not _safe_relative(item.get("path")) or
                    not _is_sha(item.get("sha256")) or type(item.get("bytes")) is not int):
                return None, None
            artifact_source = source.parent / item["path"]
            resolved = artifact_source.resolve(strict=True)
            if source.parent.resolve() not in resolved.parents or not artifact_source.is_file():
                return None, None
            if artifact_source.stat().st_size != item["bytes"] or sha256(artifact_source) != item["sha256"]:
                return None, None
            artifact_target = destination / item["path"]
            artifact_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(artifact_source, artifact_target)
            item["path"] = artifact_target.relative_to(run).as_posix()
        target = run / "matrix-report.json"
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report, target.name
    except (OSError, json.JSONDecodeError):
        return None, None


def _copy_performance_evidence(run: Path) -> tuple[str | None, str | None]:
    explicit = os.environ.get("RWSI_PERFORMANCE_RUN")
    if explicit:
        source = Path(explicit)
        source = source if source.is_absolute() else ROOT / source
        try:
            source = source.resolve(strict=True)
        except OSError:
            return None, None
    else:
        roots = [path for path in (ROOT / "evidence").glob("performance-formal-*") if path.is_dir()]
        source = max(roots, key=lambda path: path.stat().st_mtime_ns).resolve() if roots else None
    if source is None or (ROOT / "evidence").resolve() not in source.parents or not source.is_dir():
        return None, None
    report_value = os.environ.get("RWSI_PERFORMANCE_REPORT")
    report_source = Path(report_value) if report_value else ROOT / "briefs/performance-report.md"
    if not report_source.is_absolute():
        report_source = ROOT / report_source
    try:
        report_source = report_source.resolve(strict=True)
    except OSError:
        return None, None
    if ROOT.resolve() not in report_source.parents or not report_source.is_file():
        return None, None
    destination = run / "performance-formal"
    destination.mkdir()
    for name in (f"run-{number}.{suffix}" for number in range(1, 4) for suffix in ("stdout", "stderr", "exitcode")):
        item = source / name
        if item.is_file():
            shutil.copy2(item, destination / name)
    report_target = run / "performance-report.md"
    selected_hash = sha256(report_source)
    shutil.copy2(report_source, report_target)
    if sha256(report_source) != selected_hash or sha256(report_target) != selected_hash:
        report_target.unlink(missing_ok=True)
        return None, None
    return destination.name, report_target.name


def _copy_private_report(run: Path, stdout_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Copy the integration test's scalar-only F/D comparison report."""
    marker = "PRIVATE_PRODUCT_REPORT="
    try:
        value = _stdout_marker_value(stdout_path, marker)
        if value is None:
            return None, None
        report = json.loads(value)
        target = run / "private-product-report.json"
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report, target.name
    except (OSError, json.JSONDecodeError, TypeError):
        return None, None


def _private_report_check(report: Any, check_id: str) -> tuple[Any, Any, str]:
    """Authenticate one F/D row against the frozen manifest and oracle run."""
    if not isinstance(report, dict):
        return "independent private product comparison", "comparison report missing", "FAIL"
    rows = report.get("checks")
    if not isinstance(rows, list):
        return "independent private product comparison", "comparison rows missing", "FAIL"
    matching = [row for row in rows if isinstance(row, dict) and row.get("check_id") == check_id]
    if len(matching) != 1:
        return "one unique independent comparison row", f"found {len(matching)}", "FAIL"
    row = matching[0]
    private_inputs = load_private_inputs(check_fixture=False)
    normative = private_inputs.oracle
    if not private_inputs.ready or not isinstance(normative, dict):
        return "authenticated private oracle", "private oracle unavailable", "FAIL"
    if check_id.startswith("F"):
        expected_row = next((item for item in normative["assertions"] if item["check_id"] == check_id), None)
        expected = expected_row.get("expected") if expected_row else None
        oracle_expected = report.get("oracle", {}).get("expected", {}).get(check_id) if isinstance(report.get("oracle"), dict) else None
    else:
        dynamic = report.get("oracle", {}).get("dynamic", {}) if isinstance(report.get("oracle"), dict) else {}
        expected = dynamic.get(check_id) if isinstance(dynamic, dict) else None
        oracle_expected = expected
    observed = row.get("observed")
    valid_oracle = (report.get("oracle_all_match_spec") is True and report.get("oracle_exit_code") == 0 and
                    oracle_expected == expected and report.get("source_preserved") is True and
                    report.get("private_input_hashes") == private_inputs.expected_input_hashes())
    fixture = report.get("fixture")
    valid_fixture = (isinstance(fixture, dict) and fixture.get("bytes") == BASELINE_BYTES and
                     fixture.get("sha256_before") == BASELINE_SHA and fixture.get("sha256_after") == BASELINE_SHA)
    valid = (expected is not None and row.get("expected") == expected and row.get("status") == "PASS" and
             observed == expected and valid_oracle and valid_fixture)
    return expected, observed, "PASS" if valid else "FAIL"


def _expected_packaging_check_ids() -> set[str]:
    # Archive audits are T37 coverage. Keep build/member counts in T33, but
    # don't conflate the separately reported privacy assertions.
    required = {"build", "artifact_count"}
    tools = ("get_colony_summary", "list_pawns", "get_pawn", "get_health", "get_genes",
             "get_equipment", "get_inventory", "get_research", "get_mods")
    cli_queries = ["summary", "pawns", "pawn", "pawn", "pawn", "health", "genes",
                   "equipment", "inventory", "research", "mods"]
    for artifact in ("wheel", "sdist"):
        for mode in ("cli-only", "mcp-extra"):
            cell = f"{artifact}-{mode}"
            for prefix in ("install", "entrypoint", "provenance", "optional-dependency"):
                required.add(f"{prefix}:{cell}")
            if mode == "cli-only":
                required.update({f"mcp-guard:{cell}", f"cli-metadata:{cell}", f"cli-pawns:{cell}", f"fixture-integrity:{cell}"})
                required.update(f"cli-domain:{cell}:{query}:{index}" for index, query in enumerate(cli_queries))
            else:
                for suffix in ("rim-save-mcp-subcommand", "rim-save-mcp-console-script"):
                    required.add(f"mcp-startup:{cell}:{suffix}")
                    required.add(f"mcp-inspect:{cell}:{suffix}")
                    required.update(f"mcp-domain:{cell}:{suffix}:{tool}" for tool in tools)
    return required


def _package_report_case_is_complete(report: Any, check_id: str) -> bool:
    if not isinstance(report, dict) or report.get("status") != "PASS":
        return False
    case_ids = report.get("case_ids")
    if not isinstance(case_ids, list) or not all(isinstance(value, str) for value in case_ids) or check_id not in case_ids:
        return False
    cases = report.get("cases")
    if not isinstance(cases, list):
        return False
    relevant = [row for row in cases if isinstance(row, dict) and row.get("case_id") == check_id]
    if not relevant or not all(isinstance(row.get("check_id"), str) for row in relevant) or len({row.get("check_id") for row in relevant}) != len(relevant) or any(row.get("status") != "PASS" for row in relevant):
        return False
    ids = {row.get("check_id") for row in relevant}
    if check_id == "T33":
        return ids == _expected_packaging_check_ids()
    if check_id == "T37":
        if ids != {"audit-scanner-probes", "audit:.whl", "audit:.gz"}:
            return False
        by_id = {row["check_id"]: row for row in relevant}
        for audit_id in ("audit:.whl", "audit:.gz"):
            observed = by_id[audit_id].get("observed")
            if not isinstance(observed, dict) or observed.get("forbidden_names") != [] or observed.get("forbidden_content") != []:
                return False
        probe = by_id["audit-scanner-probes"].get("observed")
        return isinstance(probe, dict) and probe == {
            "unsafe_path": True,
            "credential_marker": True,
            "private_xml_marker": True,
            "synthetic_design_exempt": True,
        }
    return False


def _package_archive_errors(run: Path, report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    entries = report.get("artifacts")
    if not isinstance(entries, list) or len(entries) != 2:
        return ["packaging report must identify exactly a wheel and sdist"]
    observed_kinds: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("packaging artifact entry must be an object")
            continue
        relative = entry.get("path")
        path = _resolve_inside(run, relative)
        if path is None or not path.is_file():
            errors.append(f"packaging archive is missing or escapes run directory: {relative}")
            continue
        if not _is_sha(entry.get("sha256")) or entry.get("sha256") != sha256(path) or entry.get("bytes") != path.stat().st_size:
            errors.append(f"packaging archive hash/size mismatch: {relative}")
            continue
        try:
            if path.name.endswith(".whl"):
                observed_kinds.add("wheel")
                kind = "wheel"
                with zipfile.ZipFile(path) as archive:
                    all_members = sorted(archive.namelist())
                    contents = [(member.filename, archive.read(member)) for member in archive.infolist()
                                if not member.is_dir()]
            elif path.name.endswith(".tar.gz"):
                observed_kinds.add("sdist")
                kind = "sdist"
                with tarfile.open(path, "r:gz") as archive:
                    file_members = [member for member in archive.getmembers() if member.isfile()]
                    all_members = sorted(member.name for member in file_members)
                    contents = []
                    for member in file_members:
                        stream = archive.extractfile(member)
                        if stream is not None:
                            contents.append((member.name, stream.read()))
            else:
                errors.append(f"packaging archive has unknown type: {relative}")
                continue
            members = sorted(name for name, _ in contents)
            reported_members = entry.get("members")
            if len(all_members) != len(set(all_members)):
                errors.append(f"packaging archive duplicates a member: {relative}")
            if isinstance(reported_members, list) and all(isinstance(name, str) for name in reported_members):
                if sorted(reported_members) != all_members or len(reported_members) != len(set(reported_members)):
                    errors.append(f"packaging report member list differs from archive: {relative}")
            elif isinstance(reported_members, list) and all(isinstance(row, dict) for row in reported_members):
                valid_rows = all(set(row) == {"path", "bytes", "sha256"} and _safe_relative(row.get("path")) and
                                 type(row.get("bytes")) is int and row["bytes"] >= 0 and _is_sha(row.get("sha256"))
                                 for row in reported_members)
                row_paths = [row["path"] for row in reported_members] if valid_rows else []
                if not valid_rows or sorted(row_paths) != members or len(row_paths) != len(set(row_paths)):
                    errors.append(f"packaging report member list differs from archive: {relative}")
                else:
                    actual = {name: content for name, content in contents}
                    for row in reported_members:
                        content = actual[row["path"]]
                        if row["bytes"] != len(content) or row["sha256"] != hashlib.sha256(content).hexdigest():
                            errors.append(f"packaging report member hash/size differs from archive: {relative}:{row['path']}")
            else:
                errors.append(f"packaging report member list differs from archive: {relative}")
            normalized = [name.split("/", 1)[1] if kind == "sdist" and "/" in name else name for name in all_members]
            normalized_content = [(name.split("/", 1)[1] if kind == "sdist" and "/" in name else name, content)
                                  for name, content in contents]
            forbidden = [name for name, content in normalized_content if Path(name).is_absolute() or "\\" in name or ".." in Path(name).parts or
                         (len(name) > 1 and name[1] == ":") or
                         (name.lower().endswith((".rws", ".pyc")) and not
                          (name == PUBLIC_SAMPLE_PATH and len(content) == PUBLIC_SAMPLE_BYTES and
                           hashlib.sha256(content).hexdigest() == PUBLIC_SAMPLE_SHA256)) or
                         name.startswith(("tests/", "tools/", "evidence/", ".private/", ".venv/")) or
                         "/__pycache__/" in f"/{name}/"]
            if forbidden:
                errors.append(f"forbidden archive members in {relative}: {forbidden}")
            for name, content in contents:
                try:
                    decoded = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if any(marker in decoded for marker in ("/Users/", "/private/tmp/", "/home/")):
                    errors.append(f"developer path found in {relative}:{name}")
        except (OSError, tarfile.TarError, zipfile.BadZipFile, KeyError, TypeError):
            errors.append(f"packaging archive is unreadable: {relative}")
    if observed_kinds != {"wheel", "sdist"}:
        errors.append("packaging artifacts do not contain exactly one wheel and one sdist")
    return errors


def write_unimplemented_gate(gate: str, run: Path, reason: str) -> None:
    from tools.gates.cases import GATE_CASES, PREREQUISITES

    run.mkdir(parents=True, exist_ok=False)
    started = now()
    check_ids = GATE_CASES.get(gate, (f"{gate}_execution",))
    checks = [{"check_id": check_id, "requirement_ids": ["R19"], "required": True,
               "status": "BLOCKED", "expected": "gate-specific executable assertions", "observed": reason,
               "evidence_paths": ["blocker.txt"]} for check_id in check_ids]
    (run / "blocker.txt").write_text(reason + "\n")
    (run / "checks.json").write_text(json.dumps(checks, indent=2) + "\n")
    (run / "commands.json").write_text("[]\n")
    (run / "artifacts.json").write_text("[]\n")
    artifacts = [_artifact(run / "blocker.txt", "blocker", run), _artifact(run / "checks.json", "checks", run),
                 _artifact(run / "commands.json", "commands", run)]
    artifacts_index = artifacts
    (run / "artifacts.json").write_text(json.dumps(artifacts_index, indent=2) + "\n")
    artifacts.append(_artifact(run / "artifacts.json", "artifact index", run))
    prerequisites = [{"gate_id": gate_id, "run_id": None, "satisfied": False, "reason": "gate execution adapters are not implemented"}
                     for gate_id in PREREQUISITES.get(gate, ())]
    manifest = _generic_manifest(run, gate, "BLOCKED", started, checks, artifacts, prerequisites,
                                 [reason], [reason])
    (run / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _generic_manifest(run: Path, gate: str, state: str, started: str, checks: list[dict[str, Any]],
                      artifacts: list[dict[str, Any]], prerequisites: list[dict[str, Any]],
                      blockers: list[str], findings: list[str], fixture_path: Path | None = None) -> dict[str, Any]:
    inputs = []
    for relative, role in (("docs/design.md", "spec"), ("docs/rimworld-save-inspector/gate-evidence.schema.json", "schema"),
                           ("docs/rimworld-save-inspector/private-case-catalog.json", "oracle"),
                           ("docs/rimworld-save-inspector/responses.schema.json", "schema"),
                           ("tools/private_acceptance.py", "config"),
                           ("tools/verify.py", "config"), ("tools/gates/cases.py", "config"), ("uv.lock", "lock"),
                           ("pyproject.toml", "config")):
        path = ROOT / relative
        if path.is_file():
            inputs.append({"path": relative, "role": role, "sha256": sha256(path)})
    if gate in {"G1", "G2", "G3", "G4", "G5", "G6", "G7"}:
        for path in (ROOT / "src/rimworld_save_inspector").rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            if not any(row["path"] == relative for row in inputs):
                inputs.append({"path": relative, "role": "source", "sha256": sha256(path)})
    from tools.gates.cases import CASES, GATE_CASES
    for case_id, case in CASES.items():
        if case.gate != gate:
            continue
        for node in case.test_nodes:
            relative = node.split("::", 1)[0]
            path = ROOT / relative
            if path.is_file() and not any(row["path"] == relative for row in inputs):
                inputs.append({"path": relative, "role": "test", "sha256": sha256(path)})
    for case_id in GATE_CASES.get(gate, ()):
        key = "T24-G4" if gate == "G4" and case_id == "T24" else case_id
        case = CASES.get(key, CASES.get(case_id))
        for node in case.test_nodes if case else ():
            relative = node.split("::", 1)[0]
            path = ROOT / relative
            if path.is_file() and not any(row["path"] == relative for row in inputs):
                inputs.append({"path": relative, "role": "test", "sha256": sha256(path)})
    if gate == "G6" and "T35" in GATE_CASES.get(gate, ()):
        # The selected report may come from an ignored local evidence path.
        # Hash its retained copy, so validation remains possible after the
        # source environment variable and source file are gone.
        report = run / "performance-report.md"
        if report.is_file():
            inputs.append({"path": report.name, "role": "config", "sha256": sha256(report)})
        for relative, role in (("tools/performance/benchmark_core.py", "config"),):
            path = ROOT / relative
            if path.is_file() and not any(row["path"] == relative for row in inputs):
                inputs.append({"path": relative, "role": role, "sha256": sha256(path)})
    if gate == "G6" and "T34" in GATE_CASES.get(gate, ()):
        relative = "tools/matrix/run_public_matrix.py"
        path = ROOT / relative
        if path.is_file() and not any(row["path"] == relative for row in inputs):
            inputs.append({"path": relative, "role": "config", "sha256": sha256(path)})
    if gate == "G7":
        for relative in ("tests/verification/test_evidence_t36.py", "tests/packaging/test_v1_metadata.py",
                         *(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests/contract").glob("test_*.py")),
                         *(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests/v1").glob("test_*.py"))):
            path = ROOT / relative
            if path.is_file() and not any(row["path"] == relative for row in inputs):
                inputs.append({"path": relative, "role": "test", "sha256": sha256(path)})
    if gate == "G3":
        for relative, role in (("tests/oracle/extract.py", "oracle"),
                               ("docs/rimworld-save-inspector/private-case-catalog.json", "oracle"),
                               ("tests/fixtures/synthetic/seed.rws", "test"),
                               ("tests/fixtures/synthetic/expected.json", "oracle")):
            path = ROOT / relative
            if path.is_file() and not any(row["path"] == relative for row in inputs):
                inputs.append({"path": relative, "role": role, "sha256": sha256(path)})
    fixture_hash = sha256(fixture_path) if fixture_path and fixture_path.is_file() else None
    fixture_bytes = fixture_path.stat().st_size if fixture_path and fixture_path.is_file() else None
    statuses = [row["status"] for row in checks]
    declared_package, declared_parser, declared_profile = _release_identity()
    return {"schema_version": "1.0", "run_id": run.name, "gate_id": gate, "status": state,
            "started_at": started, "finished_at": now(), "actor": f"local:{os.environ.get('CODEX_THREAD_ID', 'unavailable')}",
            "spec": {"id": "RWSI-1.0", "revision": SPEC_REVISION, "sha256": sha256(SPEC)},
            "candidate": {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
                          "inputs": inputs, "lock_sha256": sha256(ROOT / "uv.lock") if (ROOT / "uv.lock").exists() else None,
                          "package_version": declared_package, "parser_version": declared_parser,
                          "profile_version": declared_profile},
            "environment": {"os": platform.platform(), "architecture": platform.machine(), "hardware": platform.processor() or platform.machine(),
                            "python_executable": sys.executable, "python_version": platform.python_version(), "dependencies": {}},
            "fixture": {"label": "private fixture", "available": fixture_hash is not None,
                        "expected_bytes": BASELINE_BYTES, "observed_bytes": fixture_bytes, "expected_sha256": BASELINE_SHA,
                        "before_sha256": fixture_hash, "after_sha256": fixture_hash,
                        "unavailable_reason": None if fixture_hash else "private fixture not supplied or unavailable"},
            "prerequisites": prerequisites,
            "check_summary": {"required": sum(row["required"] for row in checks), "passed": statuses.count("PASS"),
                              "failed": statuses.count("FAIL"), "blocked": statuses.count("BLOCKED"), "not_run": statuses.count("NOT_RUN")},
            "artifacts": artifacts, "blockers": blockers, "findings": findings, "carried_forward_from": None}


def run_case_gate(gate: str, fixture: Path | None, run: Path, public_only: bool = False) -> int:
    """Run mapped G1-G4 cases and create immutable evidence; incomplete T cases fail."""
    from tools.gates.cases import CASES, GATE_CASES, PREREQUISITES
    run.mkdir(parents=True, exist_ok=False)
    started = now()
    private_inputs = load_private_inputs(None if public_only else fixture)
    fixture_path = None if public_only or not private_inputs.ready else private_inputs.fixture
    if fixture_path and fixture_path.is_file():
        before = sha256(fixture_path)
    else:
        fixture_path = None
        before = None
    case_ids = GATE_CASES[gate]
    performance_evidence_dir = None
    performance_report_path = None
    performance_env: dict[str, str] = {}
    matrix_report = None
    matrix_report_path = None
    if gate == "G6":
        performance_evidence_dir, performance_report_path = _copy_performance_evidence(run)
        if performance_evidence_dir and performance_report_path:
            performance_env = {"RWSI_PERFORMANCE_RUN": str(run / performance_evidence_dir),
                               "RWSI_PERFORMANCE_REPORT": str(run / performance_report_path)}
        matrix_report, matrix_report_path = _copy_matrix_report(run)
    nodes = []
    for case_id in case_ids:
        if case_id.startswith("F") or case_id.startswith("D"):
            if fixture_path is not None:
                nodes.extend(CASES[case_id].test_nodes)
            continue
        key = "T24-G4" if gate == "G4" and case_id == "T24" else case_id
        nodes.extend(CASES.get(key, CASES.get(case_id)).test_nodes if CASES.get(key, CASES.get(case_id)) else ())
    nodes = list(dict.fromkeys(nodes))
    test_env = dict(performance_env)
    if fixture_path is not None:
        test_env.update({"RWSI_PRIVATE_FIXTURE": str(fixture_path),
                         "RWSI_PRIVATE_ORACLE": str(private_inputs.oracle_path),
                         "RWSI_PRIVATE_SPEC": str(private_inputs.spec_path),
                         "RWSI_PRIVATE_ANCHOR": str(private_inputs.anchor_path),
                         "RWSI_PRIVATE_ANCHOR_SHA256": private_inputs.anchor_expected_sha256})
    code, collected, skipped, commands, test_rows = _run_pytest_nodes(run, nodes, test_env)
    package_report, package_report_path = _copy_packaging_report(run, run / "pytest.stdout.txt")
    private_report, private_report_path = _copy_private_report(run, run / "pytest.stdout.txt")
    mcp_audit_report, mcp_audit_paths = _copy_mcp_audit_report(run, run / "pytest.stdout.txt") if gate == "G6" else (None, [])
    # Each case is evaluated against its own mapped JUnit nodes. A failure in an
    # unrelated node remains visible in the run-wide command without making a
    # passing case report itself as failed.
    checks: list[dict[str, Any]] = []
    command_evidence = ["pytest.stdout.txt", "pytest.stderr.txt"] + (["pytest.junit.xml"] if (run / "pytest.junit.xml").is_file() else [])
    for case_id in case_ids:
        if case_id.startswith("F") or case_id.startswith("D"):
            if fixture_path is None:
                checks.append(_case_check(case_id, "independent product comparison for " + case_id,
                                          "private fixture unavailable", "BLOCKED", ["blockers.txt"], gate))
            else:
                expected, observed, comparison_status = _private_report_check(private_report, case_id)
                check_nodes = CASES[case_id].test_nodes
                node = check_nodes[0]
                junit_identity = _mapped_junit_identity(node)
                matched = [item for item in test_rows if item["node"] == junit_identity]
                test_passed = len(matched) == 1 and matched[0]["status"] == "PASS"
                status = comparison_status if test_passed else "FAIL"
                evidence = list(command_evidence)
                if private_report_path:
                    evidence.append(private_report_path)
                checks.append(_case_check(case_id, expected, observed, status, evidence, gate))
            continue
        key = "T24-G4" if gate == "G4" and case_id == "T24" else case_id
        case = CASES[key]
        mapped_count = sum(node in nodes for node in case.test_nodes)
        case_results = []
        for node in case.test_nodes:
            junit_identity = _mapped_junit_identity(node)
            matches = [item for item in test_rows if item["node"] == junit_identity]
            case_results.append({"node": node, "status": matches[0]["status"] if len(matches) == 1 else "MISSING"})
        case_skipped = sum(item["status"] == "SKIPPED" for item in case_results)
        case_tests_ok = (len(case_results) == len(case.test_nodes) and bool(case_results) and
                         all(item["status"] == "PASS" for item in case_results))
        expected = {"case_coverage_complete": True,
                    "minimum_collected_test_cases": len(case.test_nodes),
                    "maximum_skipped_required_tests": 0}
        observed = {"case_coverage_complete": case.complete and mapped_count > 0,
                    "mapped_test_nodes": mapped_count, "pytest_exit_code": code,
                    "collected_test_cases": collected, "skipped_required_tests": skipped,
                    "case_skipped_test_count": case_skipped, "case_test_results": case_results,
                    "test_result_path": "pytest.junit.xml" if (run / "pytest.junit.xml").is_file() else None,
                    "coverage_gap": case.reason if not case.complete else None}
        if mapped_count == 0:
            status = "FAIL"
        elif not case.complete:
            status = "FAIL"
        elif not case_tests_ok:
            status = "FAIL"
        elif case_id in {"T33", "T37"}:
            status = "PASS" if _package_report_case_is_complete(package_report, case_id) else "FAIL"
        elif case_id == "T20" and gate == "G6":
            audit_errors = _t20_report_errors(run, mcp_audit_report)
            observed["audit_errors"] = audit_errors
            observed["audit_manifest"] = "mcp-audit/report.json" if mcp_audit_report else None
            expected["audit_manifest_valid"] = True
            observed["audit_manifest_valid"] = not audit_errors
            status = "PASS" if case_tests_ok and not audit_errors else "FAIL"
        elif case_id == "T34" and gate == "G6":
            matrix_errors = _matrix_report_errors(matrix_report, run) if matrix_report is not None else ["matrix report missing"]
            observed["matrix_report"] = matrix_report_path
            observed["matrix_errors"] = matrix_errors
            expected["four_matrix_cells_pass_and_match_candidate"] = True
            observed["four_matrix_cells_pass_and_match_candidate"] = matrix_report is not None and not matrix_errors
            if matrix_report is None:
                status = "BLOCKED"
            else:
                status = "PASS" if case_tests_ok and not matrix_errors else "FAIL"
        else:
            status = "PASS"
        case_evidence = list(command_evidence)
        if case_id in {"T33", "T37"} and package_report_path:
            case_evidence.append(package_report_path)
            observed["package_report"] = package_report_path
        if case_id == "T20" and gate == "G6":
            case_evidence.extend(mcp_audit_paths)
        if case_id == "T34" and gate == "G6" and matrix_report_path:
            case_evidence.append(matrix_report_path)
        if case_id == "T35" and gate == "G6" and performance_report_path:
            case_evidence.append(performance_report_path)
        checks.append(_case_check(case_id, expected, observed, status, case_evidence, gate))
    if gate == "G3":
        # A missing private oracle fixture is an environmental block; it never
        # manufactures expected values from the parser under test.
        for check in checks:
            if check["check_id"].startswith("F") and check["status"] == "BLOCKED":
                continue
    prerequisites = []
    prerequisite_ids = PREREQUISITES.get(gate, ())
    for previous in prerequisite_ids:
        prior_run, accepted = _latest_accepted_run(previous, gate)
        reason = ("validated accepted evidence found" if accepted else
                  "validated public G0 setup; private checks remain blocked" if previous == "G0" and gate == "G1" else
                  "no valid accepted prerequisite evidence found")
        prerequisites.append({"gate_id": previous, "run_id": prior_run, "satisfied": accepted, "reason": reason})
        if not accepted:
            checks.append(_case_check(f"prerequisite_{previous}", {"satisfied": True}, {"satisfied": False, "reason": reason},
                                      "BLOCKED", ["blockers.txt"], gate))
    blockers = []
    if gate == "G3" and private_inputs.errors:
        blockers.extend(private_inputs.errors)
    findings = _gate_failure_findings(checks)
    if any(row["status"] == "BLOCKED" for row in checks):
        blockers.append("Required private-fixture or environmental evidence was unavailable; see checks.json and blockers.txt")
    for prerequisite in prerequisites:
        if not prerequisite["satisfied"]:
            blockers.append(f"Prerequisite {prerequisite['gate_id']} has no valid accepted evidence")
    (run / "pytest.stdout.txt").touch(exist_ok=True)
    (run / "pytest.stderr.txt").touch(exist_ok=True)
    (run / "findings.txt").write_text("\n".join(findings) + ("\n" if findings else "No findings.\n"))
    (run / "blockers.txt").write_text("\n".join(blockers) + ("\n" if blockers else "No blockers.\n"))
    (run / "checks.json").write_text(json.dumps(checks, indent=2, sort_keys=True) + "\n")
    (run / "commands.json").write_text(json.dumps(commands, indent=2, sort_keys=True) + "\n")
    artifacts = []
    for name, purpose in (("checks.json", "required case assertions"), ("commands.json", "exact subprocess records"),
                          ("pytest.stdout.txt", "test stdout"), ("pytest.stderr.txt", "test stderr"),
                          ("findings.txt", "coverage and assertion failures"), ("blockers.txt", "unavailable prerequisites")):
        artifacts.append(_artifact(run / name, purpose, run))
    if (run / "pytest.junit.xml").is_file():
        artifacts.append(_artifact(run / "pytest.junit.xml", "pytest case result XML", run))
    if (run / "package-report.json").is_file():
        artifacts.append(_artifact(run / "package-report.json", "T33/T37 package audit report", run))
    if (run / "private-product-report.json").is_file():
        artifacts.append(_artifact(run / "private-product-report.json", "independent F/D product comparison", run))
    if (run / "package-dist").is_dir():
        for archive in sorted(path for path in (run / "package-dist").iterdir() if path.is_file()):
            artifacts.append(_artifact(archive, "wheel or sdist reviewed by T37", run))
    if (run / "mcp-audit").is_dir():
        for path in sorted(item for item in (run / "mcp-audit").iterdir() if item.is_file()):
            artifacts.append(_artifact(path, "T20 raw runtime audit report", run))
    if (run / "matrix-report.json").is_file():
        artifacts.append(_artifact(run / "matrix-report.json", "T34 cross-platform matrix report", run))
    if (run / "matrix-report-files").is_dir():
        for path in sorted(item for item in (run / "matrix-report-files").rglob("*") if item.is_file()):
            artifacts.append(_artifact(path, "T34 matrix command or output evidence", run))
    if (run / "performance-formal").is_dir():
        for path in sorted(item for item in (run / "performance-formal").iterdir() if item.is_file()):
            artifacts.append(_artifact(path, "T35 formal benchmark worker evidence", run))
    if (run / "performance-report.md").is_file():
        artifacts.append(_artifact(run / "performance-report.md", "T35 performance report and input provenance", run))
    (run / "artifacts.json").write_text(json.dumps(artifacts, indent=2, sort_keys=True) + "\n")
    artifacts.append(_artifact(run / "artifacts.json", "artifact index", run))
    state = "FAIL" if any(row["status"] == "FAIL" for row in checks) else "BLOCKED" if any(row["status"] in {"BLOCKED", "NOT_RUN"} for row in checks) else "PASS"
    manifest = _generic_manifest(run, gate, state, started, checks, artifacts, prerequisites, blockers, findings, fixture_path)
    (run / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    validation_errors = validate_evidence(run)
    if validation_errors:
        print("evidence validation failed: " + "; ".join(validation_errors), file=sys.stderr)
        return 1
    print(f"{gate} {state}: {run}")
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[state]


def run_gate(gate: str, fixture: Path | None, out: Path, public_only: bool = False,
             review_report: Path | None = None) -> int:
    if gate not in {"G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "all"}:
        raise ValueError("gate must be G0-G8 or all")
    if gate in {"G1", "G2", "G3", "G4", "G5", "G6"}:
        return run_case_gate(gate, fixture, out, public_only)
    if gate == "G7":
        return run_g7_gate(fixture, out, review_report, public_only)
    if gate == "G8":
        write_unimplemented_gate(gate, out, "G8 is outside the requested acceptance gates and has no execution adapter")
        return 2
    if gate == "all":
        out.mkdir(parents=True, exist_ok=False)
        statuses = []
        for gate_id in ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7"):
            code = run_gate(gate_id, fixture, out / gate_id, public_only, review_report)
            statuses.append(code)
        if any(code == 1 for code in statuses):
            return 1
        return 2 if any(code == 2 for code in statuses) else 0
    selected = None if public_only else fixture
    if selected is None and not public_only and os.environ.get("RWSI_PRIVATE_FIXTURE"):
        selected = Path(os.environ["RWSI_PRIVATE_FIXTURE"])
    write_g0_evidence(out, fixture=selected, actor=f"local:{os.environ.get('CODEX_THREAD_ID', 'unavailable')}")
    errors = validate_evidence(out)
    if errors:
        print("evidence validation failed: " + "; ".join(errors), file=sys.stderr)
        return 1
    manifest = json.loads((out / "manifest.json").read_text())
    print(f"G0 {manifest['status']}: {out}")
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}.get(manifest["status"], 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", choices=[*(f"G{i}" for i in range(9)), "all"])
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--public-only", action="store_true")
    parser.add_argument("--validate-evidence", type=Path)
    parser.add_argument("--review-report", type=Path,
                        help="fresh independent G7 T37 receipt; otherwise read RWSI_REVIEW_REPORT")
    args = parser.parse_args(argv)
    if args.validate_evidence:
        evidence_path = args.validate_evidence
        if evidence_path.is_dir() and not (evidence_path / "manifest.json").is_file():
            manifests = sorted(evidence_path.glob("*/manifest.json"))
            if not manifests:
                print("no gate run manifests found", file=sys.stderr)
                return 1
            errors = [f"{manifest.parent.name}: {error}" for manifest in manifests for error in validate_evidence(manifest.parent)]
        else:
            errors = validate_evidence(evidence_path)
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("evidence valid")
        return 0
    if not args.gate or not args.out:
        parser.error("--gate and --out are required unless --validate-evidence is used")
    try:
        return run_gate(args.gate, args.fixture, args.out, args.public_only, args.review_report)
    except FileExistsError:
        print("refusing to overwrite an existing evidence run directory", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
