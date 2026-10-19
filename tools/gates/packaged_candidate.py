"""Build and exercise a clean installed G7 candidate outside its checkout.

This is an evidence producer, not the G7 decision maker. It emits T33/T36/T37
facts and the fresh required-suite results; the gate verifier validates those
facts together with G0-G6 evidence and the independent review.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.private_acceptance import load_private_inputs

SPEC_ID = "RWSI-1.0"
SPEC_REVISION = "2026-10-17.1"
REPORT_MARKER = "PACKAGED_CANDIDATE_REPORT="
PUBLIC_SAMPLE_PATH = "examples/minimal_colony.rws"
PUBLIC_SAMPLE_SHA256 = "f55d5e0741972f959b39029e26e6921c4d768bc5eb80c9726610b9ad85d3b7d7"
PUBLIC_SAMPLE_BYTES = 1498
PUBLIC_SEED_XML_SHA256 = "54340518cbca3e06fa399519922593fd6407f66000595052acd5553a18025b5a"
PUBLIC_SEED_XML_BYTES = 3895
SAVE_XML_PATTERN = re.compile(rb"(?is)<(?:savegame|game|world|factionManager|maps)(?:\s|>)")
FORBIDDEN_COPY_PARTS = {"src", ".venv", ".venv-3.11", ".venv-3.14", ".private", "private", "evidence", ".git"}
COPY_PATHS = (
    "tests",
    "tools",
    "docs/design.md",
    "docs/rimworld-save-inspector/gate-evidence.schema.json",
    "docs/rimworld-save-inspector/private-case-catalog.json",
    "docs/rimworld-save-inspector/responses.schema.json",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
)


class CandidateError(RuntimeError):
    def __init__(self, message: str, status: str = "FAIL") -> None:
        super().__init__(message)
        self.status = status


def _contains_unapproved_save_xml(content: bytes, clean_name: str, public_sample: bool) -> bool:
    if public_sample:
        return False
    if clean_name == "docs/design.md":
        # Remove exactly one authenticated Appendix A fence. Any changed or
        # duplicate fence remains visible to the save-XML detector.
        for match in re.finditer(rb"```xml\n(.*?)```", content, re.DOTALL):
            body = match.group(1)
            if len(body) == PUBLIC_SEED_XML_BYTES and hashlib.sha256(body).hexdigest() == PUBLIC_SEED_XML_SHA256:
                content = content[:match.start()] + content[match.end():]
                break
    return SAVE_XML_PATTERN.search(content) is not None


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_record(path: Path, base: Path) -> dict[str, Any]:
    return {"path": path.relative_to(base).as_posix(), "bytes": path.stat().st_size, "sha256": _sha256(path)}


def _private_candidate_payloads(root: Path, inputs: Sequence[dict[str, Any]]) -> list[str]:
    """Find private material by storage boundary or high-confidence payload identity/content."""
    findings: list[str] = []
    private = load_private_inputs(check_fixture=False)
    protected_hashes = ({private.anchor["oracle_sha256"]: "frozen-private-oracle-identity",
                         private.anchor["baseline_spec_sha256"]: "frozen-private-specification-identity",
                         _sha256(private.anchor_path): "frozen-private-anchor-identity"}
                        if private.ready and private.anchor is not None else {})
    for row in inputs:
        rel = PurePosixPath(str(row.get("path", "")))
        if ".private" in rel.parts:
            findings.append(f"{rel.as_posix()}:private-storage-tree")
            continue
        if isinstance(row.get("sha256"), str) and row["sha256"] in protected_hashes:
            findings.append(f"{rel.as_posix()}:{protected_hashes[row['sha256']]}")
            continue
        if (private.ready and row.get("bytes") == private.expected_bytes and
                row.get("sha256") == private.expected_sha256):
            findings.append(f"{rel.as_posix()}:frozen-private-fixture-identity")
            continue
        suffix = rel.name.lower()
        if not suffix.endswith((".rws", ".rws.gz", ".xml", ".json", ".jsonl", ".gz")):
            continue
        source = root / rel.as_posix()
        try:
            with source.open("rb") as stream:
                sample = stream.read(131_072)
        except OSError:
            findings.append(f"{rel.as_posix()}:unreadable-large-save-like-input")
            continue
        if (rel.suffix.lower() == ".json" and b'"oracle_sha256"' in sample and
                b'"baseline_spec_sha256"' in sample and b'"fixture_sha256"' in sample):
            findings.append(f"{rel.as_posix()}:private-anchor-shaped-json")
        elif (rel.suffix.lower() == ".json" and b'"assertions"' in sample and
              b'"expected"' in sample and b'"fixture"' in sample):
            findings.append(f"{rel.as_posix()}:private-oracle-shaped-json")
        elif re.search(rb"(?is)<\s*savegame\b", sample) and row.get("bytes", 0) >= 1_048_576:
            findings.append(f"{rel.as_posix()}:large-full-save-xml-payload")
        elif row.get("bytes", 0) >= 1_048_576 and rel.suffix.lower() in {".json", ".jsonl"} and (
                b'"raw_xml"' in sample or b'"full_save_xml"' in sample):
            findings.append(f"{rel.as_posix()}:large-raw-private-export-payload")
    return findings


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _candidate_state(root: Path) -> dict[str, Any]:
    commit = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    tracked = _git(root, "ls-files", "-z").split("\0")
    inputs = []
    for name in tracked:
        if not name:
            continue
        path = root / name
        if path.is_file() and not path.is_symlink():
            inputs.append(_file_record(path, root))
    inputs.sort(key=lambda item: item["path"])
    private_payloads = _private_candidate_payloads(root, inputs)
    lock = root / "uv.lock"
    pyproject = root / "pyproject.toml"
    if not lock.is_file() or not pyproject.is_file():
        raise CandidateError("candidate is missing pyproject.toml or uv.lock")
    input_manifest = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    source_rows = [{"path": row["path"], "sha256": row["sha256"]} for row in inputs]
    source_tree = json.dumps(source_rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {
        "commit": commit,
        "git_tree_oid": _git(root, "rev-parse", "HEAD^{tree}"),
        "tree_sha256": hashlib.sha256(source_tree).hexdigest(),
        "dirty": bool(status),
        "inputs": inputs,
        "private_payloads": private_payloads,
        "input_manifest_sha256": hashlib.sha256(input_manifest).hexdigest(),
        "lock_sha256": _sha256(lock),
        "pyproject_sha256": _sha256(pyproject),
    }


def validate_fixture(fixture: Path | None) -> dict[str, Any]:
    private = load_private_inputs(fixture, use_env_fixture=False)
    selected = private.fixture
    observed_bytes = selected.stat().st_size if selected and selected.is_file() else None
    before = _sha256(selected) if observed_bytes is not None else None
    return {"available": observed_bytes is not None, "identity_matches": private.ready,
            "private_input_hashes": private.input_hashes(),
            "expected_bytes": private.expected_bytes, "observed_bytes": observed_bytes,
            "expected_sha256": private.expected_sha256, "before_sha256": before,
            "after_sha256": None, "unavailable_reason": "; ".join(private.errors) or None,
            **({"_resolved": str(selected.resolve())} if private.ready and selected is not None else {})}


def _safe_copy_tree(src: Path, dst: Path, root: Path) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    for path in sorted(src.rglob("*")):
        root_rel = path.relative_to(root)
        if any(part in FORBIDDEN_COPY_PARTS or part.startswith(".venv") or part == "__pycache__" for part in root_rel.parts):
            continue
        if path.is_symlink():
            raise CandidateError(f"verification input contains a symlink: {root_rel.as_posix()}")
        if path.is_dir():
            continue
        copy_rel = path.relative_to(src)
        target = dst / copy_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied.append(_file_record(target, dst.parent))
    return copied


def _copy_verification_inputs(root: Path, workspace: Path) -> list[dict[str, Any]]:
    workspace.mkdir(parents=True, exist_ok=False)
    copied: list[dict[str, Any]] = []
    for item in COPY_PATHS:
        src = root / item
        if not src.exists():
            raise CandidateError(f"required verification input is missing: {item}")
        if src.is_symlink():
            raise CandidateError(f"required verification input is a symlink: {item}")
        if src.is_dir():
            copied.extend(_safe_copy_tree(src, workspace / item, root))
        else:
            dest = workspace / item
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            copied.append(_file_record(dest, workspace))
    copied.sort(key=lambda item: item["path"])
    validate_workspace_manifest(copied)
    return copied


def validate_workspace_manifest(files: Sequence[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for record in files:
        path = PurePosixPath(str(record.get("path", "")))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            errors.append(f"unsafe copied verification path: {path}")
        if any(part in FORBIDDEN_COPY_PARTS or part.startswith(".venv") for part in path.parts):
            errors.append(f"forbidden copied verification path: {path}")
        if str(path) in seen:
            errors.append(f"duplicate copied verification path: {path}")
        seen.add(str(path))
    if any(path == "src" or path.startswith("src/") for path in seen):
        errors.append("verification workspace contains product source tree")
    return errors


def validate_import_origin(origin: str | None, expected_site: Path, source_root: Path) -> list[str]:
    if not origin:
        return ["installed package module has no observed origin"]
    path = Path(origin).resolve()
    site = expected_site.resolve()
    source = source_root.resolve()
    errors = []
    try:
        path.relative_to(site)
    except ValueError:
        errors.append(f"installed module origin is outside the fresh environment: {origin}")
    try:
        path.relative_to(source)
    except ValueError:
        pass
    else:
        errors.append("installed module is shadowed by the product source checkout")
    return errors


def validate_import_records(records: Sequence[dict[str, Any]], expected_site: Path, source_root: Path) -> list[str]:
    errors: list[str] = []
    if not records:
        return ["no installed-package import provenance was observed in the required suite"]
    for row in records:
        errors.extend(validate_import_origin(row.get("origin"), expected_site, source_root))
    return sorted(set(errors))


def validate_candidate_state(candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if candidate.get("dirty") is not False:
        errors.append("candidate is not a clean committed tree")
    for key in ("commit", "tree_sha256", "input_manifest_sha256", "lock_sha256"):
        value = candidate.get(key)
        if not isinstance(value, str) or not value:
            errors.append(f"candidate proof is missing {key}")
    if not isinstance(candidate.get("inputs"), list) or not candidate["inputs"]:
        errors.append("candidate proof has no input manifest")
    forbidden_inputs = candidate.get("private_payloads", [])
    if forbidden_inputs:
        errors.append("candidate commit contains private save/export material: " + ", ".join(map(str, forbidden_inputs)))
    return errors


def audit_package_members(archive: Path, members: dict[str, bytes]) -> dict[str, list[str]]:
    """Audit exact archive contents and return paths/marker IDs without excerpts."""
    names = sorted(members)
    is_sdist = archive.name.endswith(".tar.gz")
    normalized = [name.split("/", 1)[1] if is_sdist and "/" in name else name for name in names]
    forbidden_names: list[str] = []
    ignored_parts = {".git", ".private", ".pytest_cache", "__pycache__", "build", "evidence", "tests", "tools"}
    for name, raw_name in zip(normalized, names, strict=True):
        posix = PurePosixPath(name)
        windows = PureWindowsPath(name)
        parts = name.replace("\\", "/").split("/")
        if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in parts:
            forbidden_names.append(f"unsafe-path:{name}")
        lowered = [part.lower() for part in parts]
        base = parts[-1].lower()
        public_sample = (name == PUBLIC_SAMPLE_PATH and len(members[raw_name]) == PUBLIC_SAMPLE_BYTES
                         and hashlib.sha256(members[raw_name]).hexdigest() == PUBLIC_SAMPLE_SHA256)
        if ((name.lower().endswith((".rws", ".pyc", ".pyo", ".xml", ".sqlite", ".db", ".log")) and not public_sample)
                or any(part in ignored_parts or part.startswith(".venv") or part.endswith(".egg-info") for part in lowered)
                or base in {".ds_store", ".coverage", "coverage.xml", ".env"}):
            forbidden_names.append(name)
    credential_patterns = {
        "github-token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
        "openai-style-token": re.compile(rb"sk-[A-Za-z0-9_-]{24,}"),
        "aws-access-key": re.compile(rb"AKIA[0-9A-Z]{16}"),
        "private-key-header": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
        "credential-assignment": re.compile(
            rb"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9/+_=.-]{12,}"
        ),
    }
    forbidden_content: list[str] = []
    for name, content in members.items():
        clean_name = name.split("/", 1)[1] if is_sdist and "/" in name else name
        public_sample = (clean_name == PUBLIC_SAMPLE_PATH and len(content) == PUBLIC_SAMPLE_BYTES
                         and hashlib.sha256(content).hexdigest() == PUBLIC_SAMPLE_SHA256)
        for marker in (b"/Users/", b"/private/tmp/", b"/home/"):
            if marker in content:
                forbidden_content.append(f"{name}:developer-path-marker")
        for marker, pattern in credential_patterns.items():
            if pattern.search(content):
                forbidden_content.append(f"{name}:credential-pattern:{marker}")
        if (b'"fixture"' in content and b'"assertions"' in content and b'"expected"' in content or
                b'"oracle_sha256"' in content and b'"baseline_spec_sha256"' in content and b'"fixture_sha256"' in content):
            forbidden_content.append(f"{name}:private-acceptance-json")
        if _contains_unapproved_save_xml(content, clean_name, public_sample):
            forbidden_content.append(f"{name}:embedded-save-xml")
        if (PurePosixPath(clean_name).suffix.lower() not in {".md", ".json"} and "PKG-INFO" not in clean_name
                and re.search(rb"(?is)<(?:name|label|worldName|filename|source_name)\b[^>]*>[^<]{0,160}private[-_ ]?colony\b[^<]{0,160}</(?:name|label|worldName|filename|source_name)\s*>", content)):
            forbidden_content.append(f"{name}:private-fixture-label-in-xml")
    return {"forbidden_names": forbidden_names, "forbidden_content": forbidden_content}


def audit_scanner_probe() -> dict[str, Any]:
    """Prove the archive audit catches representative prohibited content."""
    findings = audit_package_members(Path("probe.whl"), {
        "../escape.txt": b"",
        "/absolute.txt": b"",
        "C:\\absolute.txt": b"",
        "payload.txt": b"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",  # gitleaks:allow (synthetic test fixture, not a real token)
        "snippet.txt": b"<savegame><name>private-colony</name></savegame>",
        "docs/design.md": (ROOT / "docs/design.md").read_bytes(),
    })
    injected = audit_package_members(Path("probe.whl"), {
        "docs/design.md": (ROOT / "docs/design.md").read_bytes() + b"\n<savegame><world /></savegame>"
    })
    result = {
        "unsafe_path_detected": any(item.startswith("unsafe-path:") for item in findings["forbidden_names"]),
        "credential_detected": any("credential-pattern:github-token" in item for item in findings["forbidden_content"]),
        "private_xml_label_detected": any("private-fixture-label-in-xml" in item for item in findings["forbidden_content"]),
        "synthetic_design_example_exempt": not any(
            "docs/design.md" in item and "embedded-save-xml" in item for item in findings["forbidden_content"]
        ),
        "injected_design_xml_detected": any("embedded-save-xml" in item for item in injected["forbidden_content"]),
        "findings": findings,
    }
    result["passed"] = all(result[key] for key in (
        "unsafe_path_detected", "credential_detected", "private_xml_label_detected",
        "synthetic_design_example_exempt", "injected_design_xml_detected",
    ))
    return result


def validate_junit(path: Path, required_nodes: Sequence[str]) -> dict[str, Any]:
    if not path.is_file():
        return {"valid": False, "error": "JUnit proof is absent", "tests": 0, "passed": 0,
                "failed": 0, "errors": 0, "skipped": 0, "node_results": []}
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        return {"valid": False, "error": f"JUnit proof is unreadable: {exc}", "tests": 0, "passed": 0,
                "failed": 0, "errors": 0, "skipped": 0, "node_results": []}
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    cases: dict[str, list[ET.Element]] = {}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, "0"))
        for testcase in suite.iter("testcase"):
            name = testcase.get("name", "")
            classname = testcase.get("classname", "")
            cases.setdefault(f"{classname}::{name}", []).append(testcase)
    node_results = []
    for node in required_nodes:
        parts = node.split("::")
        module = parts[0].replace("/", ".").removesuffix(".py")
        if len(parts) == 1:
            matching = [(name, rows) for name, rows in cases.items()
                        if name.startswith(module + "::") or name.startswith(module + ".")]
            if not matching:
                node_results.append({"node": node, "status": "MISSING"})
            else:
                rows = [item for _, group in matching for item in group]
                status = ("FAIL" if any(any(item.find(tag) is not None for tag in ("failure", "error")) for item in rows)
                          else "SKIPPED" if any(item.find("skipped") is not None for item in rows)
                          else "PASS" if len(rows) == len({(item.get("classname"), item.get("name")) for item in rows})
                          else "DUPLICATE")
                node_results.append({"node": node, "status": status, "collected": len(rows)})
            continue
        test_name = parts[-1]
        expected_class = parts[-2] if len(parts) > 2 else None
        expected_classname = f"{module}.{expected_class}" if expected_class else module
        matches = []
        for key, rows in cases.items():
            classname, _, name = key.partition("::")
            if name != test_name or classname != expected_classname:
                continue
            matches.extend(rows)
        status = "MISSING" if not matches else "PASS"
        if len(matches) != 1:
            status = "MISSING" if not matches else "DUPLICATE"
        elif any(matches[0].find(tag) is not None for tag in ("failure", "error")):
            status = "FAIL"
        elif matches[0].find("skipped") is not None:
            status = "SKIPPED"
        node_results.append({"node": node, "status": status})
    passed = sum(row["status"] == "PASS" for row in node_results)
    return {"valid": passed == len(required_nodes), **totals, "passed": passed,
            "failed": sum(row["status"] == "FAIL" for row in node_results),
            "missing": sum(row["status"] in {"MISSING", "DUPLICATE"} for row in node_results),
            "skipped_required": sum(row["status"] == "SKIPPED" for row in node_results),
            "node_results": node_results}


def validate_suite_proof(proof: dict[str, Any], required_nodes: Sequence[str], exit_code: int | None) -> list[str]:
    errors: list[str] = []
    if exit_code != 0:
        errors.append(f"required suite exited {exit_code}")
    results = proof.get("node_results")
    if not isinstance(results, list):
        return errors + ["required suite node proof is absent"]
    observed = {row.get("node"): row.get("status") for row in results if isinstance(row, dict)}
    for node in required_nodes:
        status = observed.get(node)
        if status != "PASS":
            errors.append(f"required test node {node} has outcome {status or 'ABSENT'}")
    if proof.get("skipped_required", 0):
        errors.append("required suite contains skipped required cases")
    if proof.get("tests", 0) <= 0:
        errors.append("required suite has zero collected tests")
    return errors


def _pytest_import_audit_plugin_source() -> str:
    """Return syntactically complete pytest plugin source for installed-origin enforcement."""
    return """import importlib, json, os
from pathlib import Path

def pytest_sessionfinish(session, exitstatus):
    try:
        module = importlib.import_module('rimworld_save_inspector')
        origin = str(Path(module.__file__).resolve())
        site = Path(os.environ['RIMWORLD_SAVE_INSPECTOR_INSTALLED_SITE']).resolve()
        source = Path(os.environ['RIMWORLD_SAVE_INSPECTOR_SOURCE_ROOT']).resolve()
        source_shadow = origin.startswith(str(source) + '/') or origin == str(source)
        Path(os.environ['RIMWORLD_SAVE_INSPECTOR_IMPORT_AUDIT']).write_text(
            json.dumps({'origin': origin, 'site': str(site), 'source_shadow': source_shadow}),
            encoding='utf-8')
        if not (origin == str(site) or origin.startswith(str(site) + '/')) or source_shadow:
            session.exitstatus = 1
    except Exception as exc:
        Path(os.environ['RIMWORLD_SAVE_INSPECTOR_IMPORT_AUDIT']).write_text(
            json.dumps({'error': str(exc)}), encoding='utf-8')
        session.exitstatus = 1
"""


def _runtime_import_hook_source() -> str:
    """Return a site-packages bootstrap that observes direct/importlib child imports."""
    return '''"""Temporary installed-module provenance hook for this acceptance environment."""
import importlib._bootstrap
import json
import os
import sys
from pathlib import Path

_original_find_and_load = importlib._bootstrap._find_and_load

def _record_loaded_module(name, module):
    if name == "rimworld_save_inspector" or name.startswith("rimworld_save_inspector."):
        origin = getattr(module, "__file__", None)
        directory = os.environ.get("RIMWORLD_SAVE_INSPECTOR_IMPORT_LOG_DIR")
        if origin and directory:
            row = {"pid": os.getpid(), "module": name, "origin": str(Path(origin).resolve())}
            path = Path(directory) / (str(os.getpid()) + ".jsonl")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\\n")

def _recording_find_and_load(name, import_):
    module = _original_find_and_load(name, import_)
    _record_loaded_module(name, module)
    return module

importlib._bootstrap._find_and_load = _recording_find_and_load
'''


class Producer:
    def __init__(self, root: Path, out: Path, fixture: Path | None, interpreter: Path) -> None:
        self.root = root.resolve(strict=True)
        requested_out = out.expanduser()
        if not requested_out.is_absolute():
            requested_out = self.root / requested_out
        self.out = requested_out.resolve()
        try:
            self.out.relative_to(self.root)
        except ValueError:
            raise CandidateError("out must be a repo-relative evidence directory for the verifier marker")
        self.work_root: Path | None = None
        self.fixture_arg = fixture
        self.interpreter = interpreter.expanduser().resolve(strict=True)
        self.commands: list[dict[str, Any]] = []
        self._command_ids: set[str] = set()
        self._command_log_paths: set[Path] = set()
        self.artifacts: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self.blockers: list[str] = []
        self.report: dict[str, Any] = {}
        self._fixture_resolved: str | None = None

    def _record_artifact(self, path: Path, purpose: str) -> dict[str, Any]:
        record = {**_file_record(path, self.out), "purpose": purpose}
        self.artifacts.append(record)
        return record

    def _claim_command_logs(self, command_id: str, *paths: Path) -> None:
        if not command_id or not re.fullmatch(r"[A-Za-z0-9_-]+", command_id):
            raise CandidateError(f"invalid command ID for immutable logs: {command_id!r}")
        if command_id in self._command_ids:
            raise CandidateError(f"duplicate command ID would overwrite immutable logs: {command_id}")
        resolved = [path.resolve() for path in paths]
        if len(set(resolved)) != len(resolved) or any(path in self._command_log_paths for path in resolved):
            raise CandidateError(f"command log path collision would overwrite immutable logs: {command_id}")
        existing = [path for path in resolved if path.exists()]
        if existing:
            raise CandidateError(f"command log path already exists; refusing overwrite: {existing[0]}")
        self._command_ids.add(command_id)
        self._command_log_paths.update(resolved)

    def _run(self, command_id: str, argv: Sequence[str], cwd: Path, *, env: dict[str, str] | None = None,
             timeout: int = 600, stdout_file: Path | None = None) -> subprocess.CompletedProcess[str]:
        stdout_path = self.out / "commands" / f"{command_id}.stdout.txt"
        stderr_path = self.out / "commands" / f"{command_id}.stderr.txt"
        self._claim_command_logs(command_id, stdout_path, stderr_path)
        started_at = _utc()
        start = time.monotonic()
        timed_out = False
        try:
            result = subprocess.run(list(map(str, argv)), cwd=cwd, env=env, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout)
            stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            exit_code = None
        except OSError as exc:
            # Preserve a failed/missing executable as a command record instead of
            # letting one broken smoke cell erase the rest of the evidence.
            stdout, stderr, exit_code = "", f"{type(exc).__name__}: {exc}", 127
        duration = round(time.monotonic() - start, 6)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        cwd_label = ("source-checkout" if cwd == self.root else
                     "candidate-snapshot" if cwd.name == "candidate" else "external-verification-workspace")
        row = {"command_id": command_id, "argv": list(map(str, argv)),
               "cwd_label": cwd_label,
               "started_at": started_at, "duration_seconds": duration, "exit_code": exit_code,
               "timed_out": timed_out, "stdout_path": stdout_path.relative_to(self.out).as_posix(),
               "stderr_path": stderr_path.relative_to(self.out).as_posix(),
               "stdout_sha256": _sha256(stdout_path), "stderr_sha256": _sha256(stderr_path)}
        self.commands.append(row)
        self._record_artifact(stdout_path, f"{command_id} stdout")
        self._record_artifact(stderr_path, f"{command_id} stderr")
        return subprocess.CompletedProcess(list(argv), exit_code if exit_code is not None else 124, stdout, stderr)

    def _clean_environment(self, bin_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP", "VIRTUAL_ENV"):
            env.pop(key, None)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["PYTHONNOUSERSITE"] = "1"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        env["UV_NO_PROGRESS"] = "1"
        return env

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    def _archive_members(self, archive: Path) -> dict[str, bytes]:
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as zf:
                return {item: zf.read(item) for item in zf.namelist()}
        with tarfile.open(archive, "r:gz") as tf:
            result: dict[str, bytes] = {}
            for member in tf.getmembers():
                if member.isfile():
                    stream = tf.extractfile(member)
                    if stream:
                        result[member.name] = stream.read()
            return result

    def _duplicate_archive_members(self, archive: Path) -> list[str]:
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as zf:
                names = [item.filename for item in zf.infolist()]
        else:
            with tarfile.open(archive, "r:gz") as tf:
                names = [item.name for item in tf.getmembers() if item.isfile()]
        return sorted({name for name in names if names.count(name) > 1})

    def _required_nodes(self, workspace: Path) -> tuple[list[str], dict[str, list[str]]]:
        import importlib.util
        cases_path = workspace / "tools/gates/cases.py"
        spec = importlib.util.spec_from_file_location("_candidate_gate_cases", cases_path)
        if spec is None or spec.loader is None:
            raise CandidateError("candidate gate-case mapping could not be loaded")
        module = importlib.util.module_from_spec(spec)
        # dataclasses resolves the newly created class's module via sys.modules
        # while applying its decorator; register before executing the mapping.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        CASES = module.CASES
        excluded = {"T33", "T34", "T35", "T37"}
        case_nodes = {case_id: list(case.test_nodes) for case_id, case in CASES.items() if case_id not in excluded}
        node_to_cases: dict[str, list[str]] = {}
        for case_id, nodes in case_nodes.items():
            for node in nodes:
                node_to_cases.setdefault(node, []).append(case_id)
        companions = [*sorted((workspace / "tests/contract").glob("test_*.py")),
                      *sorted((workspace / "tests/v1").glob("test_*.py")),
                      workspace / "tests/packaging/test_v1_metadata.py"]
        for path in companions:
            if not path.is_file():
                raise CandidateError(f"required public companion is missing: {path.relative_to(workspace)}")
            tree = ast.parse(path.read_text())
            if not any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_")
                       or isinstance(item, ast.ClassDef) and any(isinstance(child, ast.FunctionDef) and child.name.startswith("test_") for child in item.body)
                       for item in tree.body):
                raise CandidateError(f"required public companion has no executable tests: {path.relative_to(workspace)}")
            node_to_cases.setdefault(path.relative_to(workspace).as_posix(), []).append("public-companion")
        nodes = sorted(node_to_cases)
        return nodes, case_nodes

    def run(self) -> Path:
        if self.out.exists():
            raise CandidateError("output directory already exists; failed attempts must remain immutable")
        self.out.mkdir(parents=True)
        started_at = _utc()
        status = "RUNNING"
        candidate: dict[str, Any] = {"commit": None, "tree_sha256": None, "input_manifest_sha256": None,
                                     "dirty": True, "inputs": [], "lock_sha256": None}
        fixture_info = validate_fixture(self.fixture_arg or load_private_inputs().fixture)
        if not fixture_info["available"]:
            self.blockers.append(str(fixture_info["unavailable_reason"]))
        elif not fixture_info.get("identity_matches"):
            self.blockers.append(str(fixture_info["unavailable_reason"]))
        else:
            self._fixture_resolved = str(fixture_info.pop("_resolved"))
        try:
            candidate = _candidate_state(self.root)
        except CandidateError as exc:
            self.failures.append(str(exc))
        self.failures.extend(validate_candidate_state(candidate))
        if self.failures:
            status = "FAIL"

        self.report = {"schema_version": "1.0", "producer": "tools/gates/packaged_candidate.py",
                       "run_id": f"packaged-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}",
                       "started_at": started_at, "finished_at": None, "case_ids": ["T33", "T36", "T37"],
                       "status": status, "spec": {"id": SPEC_ID, "revision": SPEC_REVISION},
                       "candidate": candidate, "fixture": fixture_info,
                       "environment": {"os": platform.platform(), "architecture": platform.machine(),
                                       "python_executable": str(self.interpreter), "python_version": None,
                                       "hardware": platform.processor()},
                       "archives": [], "workspace": {}, "install": {}, "required_suite": {},
                       "cases": [], "commands": self.commands, "artifacts": self.artifacts,
                       "blockers": self.blockers, "findings": self.failures}
        if status == "RUNNING":
            self._execute()
        elif status == "FAIL":
            self.report["cases"] = [{"case_id": case_id, "status": "FAIL", "expected": "candidate and private-fixture preconditions satisfied",
                                     "observed": {"reason": self.failures[0] if self.failures else "candidate precondition failed"}}
                                    for case_id in ("T33", "T36", "T37")]
        if self.report["environment"]["python_version"] is None:
            self.report["environment"]["python_version"] = self._python_version()
        self.report["spec"]["sha256"] = _sha256(self.root / "docs/design.md")
        self._finalize_fixture()
        self.report["status"] = self._aggregate_status()
        self.report["finished_at"] = _utc()
        self.report["artifacts"] = self._deduplicated_artifacts()
        self._write_json(self.out / "report.json", self.report)
        report_hash = _sha256(self.out / "report.json")
        (self.out / "report.sha256").write_text(f"{report_hash}  report.json\n", encoding="ascii")
        return self.out / "report.json"

    def _python_version(self) -> str:
        result = self._run("acceptance-interpreter-version", [str(self.interpreter), "--version"], self.root)
        return (result.stdout or result.stderr).strip()

    def _finalize_fixture(self) -> None:
        if not self._fixture_resolved:
            return
        fixture = Path(self._fixture_resolved)
        try:
            after = _sha256(fixture)
            size_after = fixture.stat().st_size
        except OSError:
            after, size_after = None, None
        self.report["fixture"]["after_sha256"] = after
        self.report["fixture"]["after_bytes"] = size_after
        if after != self.report["fixture"].get("before_sha256") or size_after != self.report["fixture"].get("expected_bytes"):
            self.failures.append("private fixture changed or became unavailable during acceptance run")
            self.report["findings"] = self.failures

    def _aggregate_status(self) -> str:
        if self.failures:
            return "FAIL"
        if self.blockers:
            return "BLOCKED"
        return "PASS"

    def _deduplicated_artifacts(self) -> list[dict[str, Any]]:
        by_path = {item["path"]: item for item in self.artifacts}
        return [by_path[key] for key in sorted(by_path)]

    def _execute(self) -> None:
        try:
            self._execute_verified()
        except CandidateError as exc:
            (self.blockers if exc.status == "BLOCKED" else self.failures).append(str(exc))
            self.report["findings"] = self.failures
            self.report["blockers"] = self.blockers
        except Exception as exc:
            self.failures.append(f"unexpected producer error: {type(exc).__name__}: {exc}")
            self.report["findings"] = self.failures
            self.report["blockers"] = self.blockers
        finally:
            existing = {row.get("case_id") for row in self.report.get("cases", []) if isinstance(row, dict)}
            fallback_status = "FAIL" if self.failures else "BLOCKED"
            for case_id in ("T33", "T36", "T37"):
                if case_id not in existing:
                    reason = (self.failures or self.blockers or ["producer did not emit case evidence"])[0]
                    self.report.setdefault("cases", []).append({
                        "case_id": case_id, "status": fallback_status,
                        "expected": "fresh packaged-candidate evidence",
                        "observed": {"reason": reason},
                    })

    def _execute_verified(self) -> None:
        uv = shutil.which("uv")
        if not uv:
            raise CandidateError("uv executable is unavailable for locked package build", "BLOCKED")
        self.work_root = Path(tempfile.mkdtemp(prefix="rwsi-g7-external-"))
        stage = self.work_root / "candidate-stage"
        stage.mkdir()
        build_dir = stage / "candidate"
        build_dir.mkdir()
        archive_path = self.out / "committed-candidate.tar"
        archive_stderr = self.out / "commands" / "git-archive.stderr.txt"
        self._claim_command_logs("git-archive", archive_path, archive_stderr)
        archive_argv = ["git", "-C", str(self.root), "archive", "--format=tar", candidate_commit(self.report)]
        archive_started = _utc()
        archive_clock = time.monotonic()
        with archive_path.open("wb") as target:
            completed = subprocess.run(archive_argv, cwd=self.root, stdout=target, stderr=subprocess.PIPE, check=False)
        archive_duration = round(time.monotonic() - archive_clock, 6)
        archive_stderr.parent.mkdir(parents=True, exist_ok=True)
        archive_stderr.write_bytes(completed.stderr)
        self.commands.append({"command_id": "git-archive", "argv": archive_argv,
                              "cwd_label": "candidate-checkout", "started_at": archive_started,
                              "duration_seconds": archive_duration, "exit_code": completed.returncode,
                              "timed_out": False, "stdout_path": archive_path.relative_to(self.out).as_posix(),
                              "stderr_path": archive_stderr.relative_to(self.out).as_posix(),
                              "stdout_sha256": _sha256(archive_path), "stderr_sha256": _sha256(archive_stderr)})
        self._record_artifact(archive_path, "git archive stdout tar stream for clean committed candidate")
        self._record_artifact(archive_stderr, "git archive stderr")
        if completed.returncode:
            raise CandidateError(f"git archive failed with exit {completed.returncode}")
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or ".." in pure.parts or not member.isfile() and not member.isdir():
                    raise CandidateError(f"unsafe candidate archive member: {member.name}")
            tf.extractall(build_dir)
        self.report["candidate"]["archive_sha256"] = _sha256(archive_path)
        self.report["candidate"]["archive_bytes"] = archive_path.stat().st_size
        dist = self.work_root / "dist"
        dist.mkdir()
        built = self._run("build-distributions", [uv, "build", "--offline", "--no-sources", "--no-create-gitignore",
                             "--out-dir", str(dist)], build_dir)
        if built.returncode:
            raise CandidateError(f"wheel/sdist build failed with exit {built.returncode}", "BLOCKED" if "not found" in built.stderr.lower() else "FAIL")
        archives = sorted([path for path in dist.iterdir() if path.suffix == ".whl" or path.name.endswith(".tar.gz")])
        wheel = next((path for path in archives if path.suffix == ".whl"), None)
        sdist = next((path for path in archives if path.name.endswith(".tar.gz")), None)
        if wheel is None or sdist is None:
            raise CandidateError("build did not produce exactly the required wheel and sdist")
        retained_dist = self.out / "dist"
        retained_dist.mkdir()
        archives = [shutil.copy2(path, retained_dist / path.name) for path in archives]
        wheel = next(path for path in archives if path.suffix == ".whl")
        sdist = next(path for path in archives if path.name.endswith(".tar.gz"))
        package_audit: list[dict[str, Any]] = []
        for archive in (wheel, sdist):
            members = self._archive_members(archive)
            member_rows = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                           for name, data in sorted(members.items())]
            findings = audit_package_members(archive, members)
            duplicates = self._duplicate_archive_members(archive)
            findings["duplicate_members"] = duplicates
            row = {**_file_record(archive, self.out), "kind": "wheel" if archive.suffix == ".whl" else "sdist",
                   "members": member_rows, "member_count": len(member_rows), "member_manifest_sha256": hashlib.sha256(
                       json.dumps(member_rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                   "audit_findings": findings}
            row["members_sha256"] = row["member_manifest_sha256"]
            package_audit.append(row)
            self._record_artifact(archive, f"built {row['kind']} candidate")
            if findings.get("forbidden_names") or findings.get("forbidden_content") or duplicates:
                self.failures.append(f"T37 archive audit found prohibited content in {archive.name}")
        self.report["archives"] = package_audit
        lock_external = self.work_root / "locked-requirements.txt"
        export = self._run("export-locked-dependencies", [uv, "export", "--frozen", "--all-extras",
                            "--no-emit-project", "--format", "requirements-txt", "--output-file", str(lock_external)], build_dir)
        if export.returncode or not lock_external.is_file():
            raise CandidateError("could not export exact locked dependencies for the acceptance environment", "BLOCKED")
        lock_export = shutil.copy2(lock_external, self.out / "locked-requirements.txt")
        self._record_artifact(lock_export, "uv lock export with all declared extras and dependency hashes")
        venv_path = self.work_root / "installed-env"
        venv_setup = self._run("create-empty-environment", [str(self.interpreter), "-m", "venv", "--without-pip", str(venv_path)],
                               self.work_root, env=self._clean_environment(self.work_root), timeout=300)
        if venv_setup.returncode:
            raise CandidateError("could not create the empty installed-candidate environment", "BLOCKED")
        python = venv_path / "bin" / "python"
        if not python.exists():
            python = venv_path / "Scripts" / "python.exe"
        env = self._clean_environment(python.parent)
        sync = self._run("install-locked-dependencies", [uv, "pip", "sync", "--python", str(python), str(lock_export)], self.work_root, env=env, timeout=1200)
        if sync.returncode:
            raise CandidateError("locked dependency installation failed; runtime tests were not run", "BLOCKED")
        install = self._run("install-wheel", [uv, "pip", "install", "--python", str(python), "--no-deps", "--no-index", str(wheel)], self.work_root, env=env, timeout=300)
        if install.returncode:
            raise CandidateError("wheel installation failed in the fresh external environment", "FAIL")
        site = self._site_packages(python, env, command_id="installed-required-site-packages")
        import_log_dir = self.work_root / "runtime-imports"
        import_log_dir.mkdir()
        import_hook = site / "_rwsi_g7_import_provenance.py"
        import_hook_registration = site / "rwsi_g7_import_provenance.pth"
        if import_hook.exists() or import_hook_registration.exists():
            raise CandidateError("fresh environment unexpectedly already has the runtime import hook", "FAIL")
        import_hook.write_text(_runtime_import_hook_source(), encoding="utf-8")
        import_hook_registration.write_text("import _rwsi_g7_import_provenance\n", encoding="utf-8")
        self.report["install"]["runtime_import_hook_sha256"] = _sha256(import_hook)
        self.report["install"]["runtime_import_hook_registration_sha256"] = _sha256(import_hook_registration)
        probe = self._run("installed-import-provenance", [str(python), "-c",
            "import importlib.metadata as m, rimworld_save_inspector as p, json; from rimworld_save_inspector.parser.core import PARSER_PROFILE; print(json.dumps({'origin':p.__file__,'version':m.version('rimworld-save-inspector'),'parser_version':p.__version__,'profile_version':PARSER_PROFILE}))"],
            self.work_root, env=env)
        if probe.returncode:
            raise CandidateError("installed package provenance probe failed", "FAIL")
        try:
            provenance = json.loads(probe.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise CandidateError("installed package provenance output is invalid", "FAIL")
        origin_errors = validate_import_origin(provenance.get("origin"), site, self.root)
        if origin_errors:
            self.failures.extend(origin_errors)
        self.report["candidate"].update({"package_version": provenance.get("version"),
                                         "parser_version": provenance.get("parser_version"),
                                         "profile_version": provenance.get("profile_version")})
        distributions = self._installed_inventory(python, env)
        self.report["install"] = {"python": str(python), "outside_checkout": True,
                                   "installed_module_origin": provenance.get("origin"), "installed_version": provenance.get("version"),
                                   "site_packages": str(site), "source_shadowing": bool(origin_errors),
                                   "locked_dependencies_sha256": _sha256(lock_export), "dependency_inventory": distributions,
                                   "wheel_path": wheel.relative_to(self.out).as_posix(),
                                   "wheel_sha256": _sha256(wheel),
                                   "runtime_import_hook_sha256": _sha256(import_hook),
                                   "runtime_import_hook_registration_sha256": _sha256(import_hook_registration)}
        workspace = self.work_root / "verification-workspace"
        copied = _copy_verification_inputs(build_dir, workspace)
        workspace_archive = self.out / "verification-workspace.tar"
        with tarfile.open(workspace_archive, "w") as tar:
            for record in copied:
                relative = record["path"]
                tar.add(workspace / relative, arcname=relative, recursive=False)
        self._record_artifact(workspace_archive, "exact external verification workspace snapshot; src/private/envs/evidence/.git excluded")
        self.report["workspace"] = {"path_label": "external-verification-workspace", "files": copied,
                                     "file_count": len(copied), "manifest_sha256": hashlib.sha256(
                                         json.dumps(copied, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                                     "archive_path": workspace_archive.relative_to(self.out).as_posix(),
                                     "archive_sha256": _sha256(workspace_archive),
                                     "archive_bytes": workspace_archive.stat().st_size,
                                     "forbidden_paths_present": [row["path"] for row in copied
                                                                  if any(part in FORBIDDEN_COPY_PARTS or part.startswith(".venv")
                                                                         for part in PurePosixPath(row["path"]).parts)],
                                     "source_tree_present": any(row["path"] == "src" or row["path"].startswith("src/")
                                                                for row in copied),
                                     "audit_errors": validate_workspace_manifest(copied)}
        if self.report["workspace"]["audit_errors"]:
            self.failures.extend(self.report["workspace"]["audit_errors"])
        nodes, case_nodes = self._required_nodes(workspace)
        audit_plugin = workspace / "packaged_import_audit.py"
        audit_result = self.work_root / "packaged-import-audit.json"
        audit_plugin.write_text(_pytest_import_audit_plugin_source(), encoding="utf-8")
        junit = self.out / "required-suite.junit.xml"
        stdout = self.out / "required-suite.stdout.txt"
        stderr = self.out / "required-suite.stderr.txt"
        file_nodes = {node for node in nodes if "::" not in node}
        execution_nodes = [node for node in nodes if "::" not in node or node.split("::", 1)[0] not in file_nodes]
        argv = [str(python), "-m", "pytest", "-q", "--tb=short", "-p", "packaged_import_audit",
                f"--junitxml={junit}", *execution_nodes]
        if self._fixture_resolved:
            env["RWSI_PRIVATE_FIXTURE"] = str(self._fixture_resolved)
        else:
            env.pop("RWSI_PRIVATE_FIXTURE", None)
        private = load_private_inputs(Path(self._fixture_resolved) if self._fixture_resolved else None)
        env["RWSI_PRIVATE_ORACLE"] = str(private.oracle_path)
        env["RWSI_PRIVATE_SPEC"] = str(private.spec_path)
        env["RWSI_PRIVATE_ANCHOR"] = str(private.anchor_path)
        if private.anchor_expected_sha256 is not None:
            env["RWSI_PRIVATE_ANCHOR_SHA256"] = private.anchor_expected_sha256
        env["RIMWORLD_SAVE_INSPECTOR_INSTALLED_SITE"] = str(site)
        env["RIMWORLD_SAVE_INSPECTOR_SOURCE_ROOT"] = str(self.root)
        env["RIMWORLD_SAVE_INSPECTOR_IMPORT_AUDIT"] = str(audit_result)
        env["RIMWORLD_SAVE_INSPECTOR_IMPORT_LOG_DIR"] = str(import_log_dir)
        run = self._run("required-suite", argv, workspace, env=env, timeout=2400)
        shutil.copyfile(self.out / "commands/required-suite.stdout.txt", stdout)
        shutil.copyfile(self.out / "commands/required-suite.stderr.txt", stderr)
        proof = validate_junit(junit, nodes)
        suite_errors = validate_suite_proof(proof, nodes, run.returncode)
        if audit_result.is_file():
            try:
                runtime_import_audit = json.loads(audit_result.read_text())
            except ValueError:
                runtime_import_audit = {"error": "invalid import audit JSON"}
        else:
            runtime_import_audit = {"error": "required suite import provenance is absent"}
        audit_copy = self.out / "required-suite-import-audit.json"
        if audit_result.is_file():
            shutil.copyfile(audit_result, audit_copy)
            self._record_artifact(audit_copy, "pytest-process installed import provenance")
        runtime_origin_errors = validate_import_origin(runtime_import_audit.get("origin"), site, self.root)
        import_records: list[dict[str, Any]] = []
        for log_path in sorted(import_log_dir.glob("*.jsonl")):
            for line in log_path.read_text(encoding="utf-8").splitlines():
                try:
                    import_records.append(json.loads(line))
                except ValueError:
                    suite_errors.append("runtime import provenance log contains invalid JSON")
        import_record_errors = validate_import_records(import_records, site, self.root)
        if import_record_errors:
            suite_errors.extend(import_record_errors)
        import_records_path = self.out / "required-suite-import-records.json"
        self._write_json(import_records_path, import_records)
        self._record_artifact(import_records_path, "all installed-package imports seen across pytest and child runtime processes")
        if runtime_origin_errors or runtime_import_audit.get("error") or runtime_import_audit.get("source_shadow"):
            suite_errors.extend(runtime_origin_errors or [str(runtime_import_audit.get("error") or "pytest process detected source shadowing")])
        self.report["required_suite"] = {"case_nodes": case_nodes, "required_nodes": nodes,
                                          "argv": argv, "exit_code": run.returncode,
                                          "junit_path": junit.relative_to(self.out).as_posix(), "junit_sha256": _sha256(junit) if junit.exists() else None,
                                          "junit_counts": {key: proof.get(key) for key in ("tests", "passed", "failures", "errors", "skipped", "skipped_required", "missing")},
                                          "node_results": proof.get("node_results", []),
                                          "runtime_import_audit": runtime_import_audit,
                                          "runtime_import_records": import_records,
                                          "runtime_import_record_count": len(import_records),
                                          "stdout_path": stdout.relative_to(self.out).as_posix(), "stdout_sha256": _sha256(stdout),
                                          "stderr_path": stderr.relative_to(self.out).as_posix(), "stderr_sha256": _sha256(stderr),
                                          "validation_errors": suite_errors}
        for artifact, purpose in ((junit, "required installed-wheel suite JUnit"), (stdout, "required installed-wheel suite stdout"),
                                  (stderr, "required installed-wheel suite stderr")):
            if artifact.exists():
                self._record_artifact(artifact, purpose)
        if suite_errors:
            if not self._fixture_resolved and all("SKIPPED" in error or "skipped required" in error.lower() for error in suite_errors):
                self.blockers.append("private fixture unavailable; required private case was skipped")
            else:
                self.failures.extend(suite_errors)
        scanner_probe = audit_scanner_probe()
        smoke_rows = self._package_smoke_matrix(uv, wheel, sdist, build_dir, lock_external.parent)
        t33_commands = [row for row in self.commands if row["command_id"].startswith("t33-")]
        self.report["package_smoke"] = smoke_rows
        self.report["cases"] = [
            {"case_id": "T33", "status": "PASS" if install.returncode == 0 and not origin_errors and
                     all(row["status"] == "PASS" for row in smoke_rows) else "FAIL",
             "expected": {"clean_committed_candidate": True, "wheel_installed_outside_checkout": True,
                          "sdist_installable": True, "wheel_and_sdist_cli_only_and_mcp_extra_smokes_pass": True,
                          "declared_locked_dependencies_only": True, "installed_origin_inside_fresh_environment": True},
             "observed": {"commit": self.report["candidate"]["commit"], "dirty": self.report["candidate"]["dirty"],
                          "install_exit_code": install.returncode, "origin": provenance.get("origin"),
                          "version": provenance.get("version"), "source_shadowing": bool(origin_errors),
                          "smoke_cells": smoke_rows,
                          "commands": [{"command_id": row["command_id"], "argv": row["argv"],
                                        "exit_code": row["exit_code"], "timed_out": row["timed_out"]}
                                       for row in t33_commands]}},
            {"case_id": "T36", "status": "PASS" if run.returncode == 0 and proof.get("tests", 0) > 0 and
                     proof.get("skipped_required", 0) == 0 and
                     all(row["status"] == "PASS" for row in proof.get("node_results", []) if "test_evidence_t36" in row["node"]) and any("test_evidence_t36" in row["node"] for row in proof.get("node_results", [])) else "FAIL",
             "expected": {"all_mapped_t36_nodes_pass": True, "zero_skips": True, "nonzero_collection": True},
             "observed": {"nodes": [row for row in proof.get("node_results", []) if "test_evidence_t36" in row["node"]],
                          "junit_counts": self.report["required_suite"]["junit_counts"], "validation_errors": suite_errors}},
            {"case_id": "T37", "status": "PASS" if scanner_probe["passed"] and all(
                 not row["audit_findings"]["forbidden_names"] and not row["audit_findings"]["forbidden_content"] and
                 not row["audit_findings"]["duplicate_members"] for row in package_audit) else "FAIL",
             "expected": {"wheel_and_sdist_audited": True, "forbidden_names": [],
                          "forbidden_content": [], "duplicate_members": []},
             "observed": {"archives": [{"kind": row["kind"], "sha256": row["sha256"], "member_manifest_sha256": row["member_manifest_sha256"],
                                         "audit_findings": row["audit_findings"]} for row in package_audit],
                          "scanner_probe": scanner_probe,
                          "audit_counts": {"archive_count": len(package_audit),
                                           "member_count": sum(row["member_count"] for row in package_audit),
                                           "scanner_probe_count": 5,
                                           "scanner_probe_passed": sum(bool(scanner_probe[key]) for key in (
                                               "unsafe_path_detected", "credential_detected",
                                               "private_xml_label_detected", "synthetic_design_example_exempt",
                                               "injected_design_xml_detected"))},
                          "commands": [{"command_id": "build-distributions", "argv": built.args,
                                        "exit_code": built.returncode, "timed_out": False}]}}
        ]
        self.report["commands"] = self.commands
        self.report["artifacts"] = self._deduplicated_artifacts()

    def _package_smoke_matrix(self, uv: str, wheel: Path, sdist: Path, candidate_root: Path,
                              work_root: Path) -> list[dict[str, Any]]:
        """Install both distributions with locked base/MCP deps in four clean envs."""
        lockfiles: dict[str, Path] = {}
        for label, extra in (("cli", None), ("mcp", "mcp")):
            destination = self.out / f"t33-{label}-locked-requirements.txt"
            argv = [uv, "export", "--frozen", "--no-emit-project", "--format", "requirements-txt",
                    "--output-file", str(destination)]
            if extra:
                argv.extend(["--extra", extra])
            exported = self._run(f"t33-export-{label}-lock", argv, candidate_root)
            if exported.returncode or not destination.is_file():
                raise CandidateError(f"T33 {label} dependency lock export failed", "BLOCKED")
            lockfiles[label] = destination
            self._record_artifact(destination, f"T33 {label}-only locked dependency export")

        matrix: list[dict[str, Any]] = []
        for artifact_label, artifact in (("wheel", wheel), ("sdist", sdist)):
            for extra_label in ("cli", "mcp"):
                cell = f"{artifact_label}-{extra_label}"
                env_root = work_root / f"t33-env-{cell}"
                made = self._run(f"t33-{cell}-create-venv",
                                 [str(self.interpreter), "-m", "venv", "--without-pip", str(env_root)], work_root,
                                 env=self._clean_environment(work_root), timeout=300)
                if made.returncode:
                    raise CandidateError(f"T33 could not create the {cell} empty environment", "BLOCKED")
                python = env_root / "bin" / "python"
                if not python.exists():
                    python = env_root / "Scripts" / "python.exe"
                env = self._clean_environment(python.parent)
                sync = self._run(f"t33-{cell}-sync-lock",
                                 [uv, "pip", "sync", "--python", str(python), str(lockfiles[extra_label])],
                                 work_root, env=env, timeout=1200)
                install_argv = [uv, "pip", "install", "--python", str(python), "--no-deps"]
                if artifact_label == "wheel":
                    install_argv.append("--no-index")
                else:
                    # sdist installation must resolve its declared build backend.
                    # Keep this offline while allowing uv's cached index metadata
                    # to select the cached hatchling build dependency.
                    install_argv.append("--offline")
                install_argv.append(str(artifact))
                install = self._run(f"t33-{cell}-install-{artifact_label}",
                                    install_argv, work_root, env=env, timeout=900)
                site = self._site_packages(python, env, command_id=f"t33-{cell}-site-packages")
                probe_code = (
                    "import importlib.metadata as m, importlib.util as u, json, rimworld_save_inspector as p; "
                    "print(json.dumps({'origin':str(p.__file__),'version':m.version('rimworld-save-inspector'),"
                    "'mcp_available':u.find_spec('mcp') is not None}))"
                )
                probe = self._run(f"t33-{cell}-import-probe", [str(python), "-c", probe_code], work_root, env=env)
                probe_result: dict[str, Any] = {"error": "probe did not produce valid JSON"}
                if probe.returncode == 0:
                    try:
                        probe_result = json.loads(probe.stdout.strip().splitlines()[-1])
                    except (ValueError, IndexError):
                        pass
                origin_errors = validate_import_origin(probe_result.get("origin"), site, self.root)
                version_ok = probe_result.get("version") == self.report["candidate"].get("package_version")
                mcp_ok = probe_result.get("mcp_available") is (extra_label == "mcp")
                script = env_root / ("Scripts" if os.name == "nt" else "bin") / ("rim-save.exe" if os.name == "nt" else "rim-save")
                cli = self._run(f"t33-{cell}-cli-version", [str(script), "--version"], work_root, env=env)
                cli_ok = cli.returncode == 0 and cli.stdout.strip() == f"rim-save {self.report['candidate'].get('package_version')}"
                errors: list[str] = []
                if sync.returncode:
                    errors.append("locked dependency sync failed")
                if install.returncode:
                    errors.append("distribution installation failed")
                if probe.returncode or probe_result.get("error"):
                    errors.append("installed import/version probe failed")
                errors.extend(origin_errors)
                if not version_ok:
                    errors.append("installed package version differs from the committed candidate metadata")
                if not mcp_ok:
                    errors.append(f"MCP extra availability does not match {extra_label} environment")
                if not cli_ok:
                    errors.append("installed rim-save --version command failed its output contract")
                cell_row = {"cell": cell, "artifact": artifact_label, "dependency_profile": extra_label,
                            "status": "PASS" if not errors else "FAIL",
                            "expected": {"origin_inside_fresh_environment": True, "candidate_version": self.report["candidate"].get("package_version"),
                                         "mcp_installed": extra_label == "mcp", "cli_version_exit_zero": True},
                            "observed": {"origin": probe_result.get("origin"), "version": probe_result.get("version"),
                                         "mcp_available": probe_result.get("mcp_available"), "cli_stdout": cli.stdout.strip(),
                                         "cli_exit_code": cli.returncode},
                            "command_ids": [f"t33-{cell}-create-venv", f"t33-{cell}-sync-lock",
                                            f"t33-{cell}-install-{artifact_label}", f"t33-{cell}-import-probe",
                                            f"t33-{cell}-cli-version"], "validation_errors": errors}
                matrix.append(cell_row)
                if errors:
                    self.failures.extend(f"T33 {cell}: {error}" for error in errors)
        return matrix

    def _site_packages(self, python: Path, env: dict[str, str], *, command_id: str) -> Path:
        result = self._run(command_id, [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                           self.work_root or self.out, env=env)
        if result.returncode:
            raise CandidateError("could not inspect installed environment site-packages", "FAIL")
        return Path(result.stdout.strip())

    def _installed_inventory(self, python: Path, env: dict[str, str]) -> list[dict[str, str]]:
        code = "import importlib.metadata as m,json; print(json.dumps(sorted([{'name':d.metadata['Name'],'version':d.version} for d in m.distributions()],key=lambda x:x['name'].lower())))"
        result = self._run("installed-dependency-inventory", [str(python), "-c", code], self.work_root or self.out, env=env)
        if result.returncode:
            raise CandidateError("could not inspect installed dependency versions", "FAIL")
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise CandidateError(f"installed dependency inventory is invalid: {exc}", "FAIL")

def candidate_commit(report: dict[str, Any]) -> str:
    commit = report.get("candidate", {}).get("commit")
    if not isinstance(commit, str) or not commit:
        raise CandidateError("candidate commit was not recorded")
    return commit


def run_packaged_candidate(root: Path, out: Path, fixture: Path | None, interpreter: Path) -> Path:
    """Produce fresh G7 package/runtime evidence and return the report path."""
    producer = Producer(root, out, fixture, interpreter)
    return producer.run()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--interpreter", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_packaged_candidate(args.root, args.out, args.fixture, args.interpreter)
    except CandidateError as exc:
        print(f"packaged candidate producer failed: {exc}", file=sys.stderr)
        return 1
    print(f"{REPORT_MARKER}{report.relative_to(args.root.resolve()).as_posix() if report.is_relative_to(args.root.resolve()) else report}")
    data = json.loads(report.read_text())
    return 0 if data.get("status") == "PASS" else 1 if data.get("status") == "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
