"""T33/T37: audit and exercise built distributions outside the checkout."""

from __future__ import annotations

import anyio
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
import venv
import zipfile
import pytest
from pathlib import PurePosixPath, PureWindowsPath
from datetime import datetime, timezone
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/synthetic/seed.rws"
PUBLIC_SAMPLE_PATH = "examples/minimal_colony.rws"
PUBLIC_SAMPLE_SHA256 = "f55d5e0741972f959b39029e26e6921c4d768bc5eb80c9726610b9ad85d3b7d7"
PUBLIC_SAMPLE_BYTES = 1498
PUBLIC_SEED_XML_SHA256 = "54340518cbca3e06fa399519922593fd6407f66000595052acd5553a18025b5a"
PUBLIC_SEED_XML_BYTES = 3895
SAVE_XML_PATTERN = re.compile(rb"(?is)<(?:savegame|game|world|factionManager|maps)(?:\s|>)")


def _contains_unapproved_save_xml(content: bytes, clean_name: str, public_sample: bool) -> bool:
    if public_sample:
        return False
    if clean_name == "docs/design.md":
        for match in re.finditer(rb"```xml\n(.*?)```", content, re.DOTALL):
            body = match.group(1)
            if len(body) == PUBLIC_SEED_XML_BYTES and hashlib.sha256(body).hexdigest() == PUBLIC_SEED_XML_SHA256:
                content = content[:match.start()] + content[match.end():]
                break
    return SAVE_XML_PATTERN.search(content) is not None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _archive_members(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name) for name in archive.namelist()}
    with tarfile.open(path, "r:gz") as archive:
        return {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile() and archive.extractfile(member) is not None
        }


def _audit_archive_members(artifact: Path, members: dict[str, bytes]) -> dict[str, list[str]]:
    """Return privacy/distribution findings without copying matched content."""
    names = sorted(members)
    normalized = [name.split("/", 1)[1] if artifact.name.endswith(".tar.gz") and "/" in name else name
                  for name in names]
    forbidden_names: list[str] = []
    for name, raw_name in zip(normalized, names, strict=True):
        posix = PurePosixPath(name)
        windows = PureWindowsPath(name)
        parts = name.replace("\\", "/").split("/")
        if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in parts:
            forbidden_names.append(f"unsafe-path:{name}")
        lowered_parts = [part.lower() for part in parts]
        ignored_parts = {".git", ".private", ".superpowers", ".codex", ".agents", ".pytest_cache", "__pycache__", "build", "evidence", "tests", "tools"}
        ignored_file = Path(parts[-1]).name.lower()
        public_sample = (name == PUBLIC_SAMPLE_PATH and len(members[raw_name]) == PUBLIC_SAMPLE_BYTES
                         and hashlib.sha256(members[raw_name]).hexdigest() == PUBLIC_SAMPLE_SHA256)
        if ((name.lower().endswith((".rws", ".pyc", ".pyo", ".xml", ".sqlite", ".db", ".log")) and not public_sample)
                or any(part in ignored_parts or part.startswith(".venv") or part.endswith(".egg-info")
                       for part in lowered_parts)
                or ignored_file in {".ds_store", ".coverage", "coverage.xml", ".env"}):
            forbidden_names.append(name)

    # These patterns catch high-confidence credential forms; matched values are
    # never included in the report. This is a precautionary scan, not a secret
    # scanner with complete coverage of every provider's token format.
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
        clean_name = name.split("/", 1)[1] if artifact.name.endswith(".tar.gz") and "/" in name else name
        public_sample = (clean_name == PUBLIC_SAMPLE_PATH and len(content) == PUBLIC_SAMPLE_BYTES
                         and hashlib.sha256(content).hexdigest() == PUBLIC_SAMPLE_SHA256)
        for marker, pattern in credential_patterns.items():
            if pattern.search(content):
                forbidden_content.append(f"{name}:credential-pattern:{marker}")
        if (b'"fixture"' in content and b'"assertions"' in content and b'"expected"' in content or
                b'"oracle_sha256"' in content and b'"baseline_spec_sha256"' in content and b'"fixture_sha256"' in content):
            forbidden_content.append(f"{name}:private-acceptance-json")
        # docs/design.md carries the public synthetic example specified by the
        # contract. Other embedded save-shaped XML and explicit private fixture
        # labels are rejected without recording a snippet or any matched bytes.
        if _contains_unapproved_save_xml(content, clean_name, public_sample):
            forbidden_content.append(f"{name}:embedded-save-xml")
        if (Path(name).suffix.lower() not in {".md", ".json"} and "PKG-INFO" not in name
                and re.search(rb"(?is)<(?:name|label|worldName|filename|source_name)\b[^>]*>[^<]{0,160}private[-_ ]?colony\b[^<]{0,160}</(?:name|label|worldName|filename|source_name)\s*>", content)):
            forbidden_content.append(f"{name}:private-fixture-label-in-xml")
    return {"forbidden_names": forbidden_names, "forbidden_content": forbidden_content}


@pytest.mark.parametrize("name", [
    ".superpowers/sdd/frozen/source/README.md",
    ".superpowers/evidence/attempt/output.txt",
    ".codex/config.toml",
    ".agents/worker.md",
])
def test_internal_agent_and_verification_trees_are_forbidden_even_without_private_markers(name):
    findings = _audit_archive_members(Path("probe.whl"), {name: b"ordinary text without secrets"})
    assert name in findings["forbidden_names"]


def test_design_archive_exception_rejects_added_save_xml():
    design = (ROOT / "docs/design.md").read_bytes()
    name = "candidate/docs/design.md"
    artifact = Path("candidate.tar.gz")
    assert _audit_archive_members(artifact, {name: design})["forbidden_content"] == []
    injected = design + b"\n<savegame><world /></savegame>"
    assert any("embedded-save-xml" in item for item in
               _audit_archive_members(artifact, {name: injected})["forbidden_content"])


def test_artifacts_install_outside_checkout_without_and_with_mcp_extra():
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
    out = ROOT / "evidence" / f"packaging-{run_id}"
    out.mkdir(parents=True, exist_ok=False)
    external_root = Path(tempfile.mkdtemp(prefix=f"rim-save-package-install-{run_id}-"))
    dist = out / "dist"
    dist.mkdir()
    work = external_root / "outside-checkout"
    work.mkdir()
    report: dict[str, object] = {
        "report_version": "1",
        "run_id": run_id,
        "started_at": _now(),
        "finished_at": None,
        "case_ids": ["T33", "T37"],
        "status": "RUNNING",
        "environment": {
            "os": platform.platform(),
            "architecture": platform.machine(),
            "python_executable": sys.executable,
            "python_version": platform.python_version(),
        },
        "cases": [],
        "commands": [],
        "artifacts": [],
    }
    cases: list[dict[str, object]] = report["cases"]  # type: ignore[assignment]
    commands: list[dict[str, object]] = report["commands"]  # type: ignore[assignment]
    artifacts: list[dict[str, object]] = report["artifacts"]  # type: ignore[assignment]
    failures: list[str] = []

    def run(command_id: str, argv: list[str], cwd: Path, timeout: int = 180,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        try:
            result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                    timeout=timeout, env=env)
        except subprocess.TimeoutExpired as error:
            elapsed = time.monotonic() - started
            stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else error.stdout or ""
            stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else error.stderr or ""
            stdout_path = out / "commands" / f"{command_id}.stdout"
            stderr_path = out / "commands" / f"{command_id}.stderr"
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            commands.append({
                "argv": argv,
                "cwd_label": "checkout" if cwd == ROOT else "outside-checkout",
                "started_at": _now(),
                "duration_seconds": round(elapsed, 6),
                "exit_code": None,
                "timed_out": True,
                "stdout_path": str(stdout_path.relative_to(out)),
                "stderr_path": str(stderr_path.relative_to(out)),
            })
            raise
        elapsed = time.monotonic() - started
        stdout_path = out / "commands" / f"{command_id}.stdout"
        stderr_path = out / "commands" / f"{command_id}.stderr"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        commands.append({
            "argv": argv,
            "cwd_label": "checkout" if cwd == ROOT else "outside-checkout",
            "started_at": _now(),
            "duration_seconds": round(elapsed, 6),
            "exit_code": result.returncode,
            "stdout_path": str(stdout_path.relative_to(out)),
            "stderr_path": str(stderr_path.relative_to(out)),
        })
        return result

    def check(case_id: str, check_id: str, expected: object, observed: object, passed: bool) -> None:
        cases.append({
            "case_id": case_id,
            "check_id": check_id,
            "status": "PASS" if passed else "FAIL",
            "expected": expected,
            "observed": observed,
        })
        if not passed:
            failures.append(f"{case_id}/{check_id}: expected {expected!r}, observed {observed!r}")

    try:
        scanner_probe = _audit_archive_members(Path("probe.whl"), {
            "../escape.txt": b"",
            "/absolute.txt": b"",
            "C:\\absolute.txt": b"",
            "payload.txt": b"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
            "snippet.txt": b"<savegame><name>private-colony</name></savegame>",
            "docs/design.md": (ROOT / "docs/design.md").read_bytes(),
        })
        injected_design_probe = _audit_archive_members(Path("probe.whl"), {
            "docs/design.md": (ROOT / "docs/design.md").read_bytes() + b"\n<savegame><world /></savegame>"
        })
        probe_ok = (
            any(item.startswith("unsafe-path:") for item in scanner_probe["forbidden_names"])
            and any("credential-pattern:github-token" in item for item in scanner_probe["forbidden_content"])
            and any("private-fixture-label-in-xml" in item for item in scanner_probe["forbidden_content"])
            and not any("docs/design.md" in item and "embedded-save-xml" in item
                        for item in scanner_probe["forbidden_content"])
            and any("embedded-save-xml" in item for item in injected_design_probe["forbidden_content"])
        )
        check("T37", "audit-scanner-probes", "scanner catches unsafe paths, token patterns and private XML markers without flagging the documented synthetic example",
              {"unsafe_path": True, "credential_marker": True, "private_xml_marker": True, "synthetic_design_exempt": True},
              probe_ok)

        uv = shutil.which("uv")
        if uv:
            build_argv = [uv, "build", "--offline", "--no-sources", "--no-create-gitignore",
                          "--wheel", "--sdist", "--out-dir", str(dist)]
            build_env = dict(os.environ)
            build_env["UV_OFFLINE"] = "1"
        else:
            build_argv = [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(dist)]
            build_env = None
        build = run("build", build_argv, ROOT, env=build_env)
        check("T33", "build", "wheel and sdist build successfully", {"exit_code": build.returncode}, build.returncode == 0)
        if build.returncode != 0:
            raise RuntimeError("distribution build failed; see commands/build.stderr")

        built = sorted(path for path in dist.iterdir() if path.is_file())
        wheel = next((path for path in built if path.suffix == ".whl"), None)
        sdist = next((path for path in built if path.name.endswith(".tar.gz")), None)
        check("T33", "artifact_count", "one wheel and one sdist", [path.name for path in built], wheel is not None and sdist is not None and len(built) == 2)
        if wheel is None or sdist is None or len(built) != 2:
            raise RuntimeError("expected exactly one wheel and one sdist")

        for artifact in (wheel, sdist):
            members = _archive_members(artifact)
            member_names = sorted(members)
            audit = _audit_archive_members(artifact, members)
            normalized = [name.split("/", 1)[1] if artifact == sdist and "/" in name else name for name in member_names]
            forbidden_content = audit["forbidden_content"]
            for name, content in members.items():
                for marker in (b"/Users/", b"/private/tmp/", b"/home/"):
                    if marker in content:
                        forbidden_content.append(f"{name}:developer-path-marker")
            forbidden_names = audit["forbidden_names"]
            check("T37", f"audit:{artifact.suffix}", "only the authenticated public sample save; no verification trees, ignored runtime files, credentials, or private XML markers",
                  {"forbidden_names": forbidden_names, "forbidden_content": forbidden_content},
                  not forbidden_names and not forbidden_content)
            artifacts.append({
                "path": str(artifact.relative_to(out)),
                "sha256": _sha256(artifact),
                "bytes": artifact.stat().st_size,
                "members": member_names,
            })

        for artifact_label, artifact in (("wheel", wheel), ("sdist", sdist)):
            for extra in (None, "mcp"):
                cell = f"{artifact_label}-{'cli-only' if extra is None else 'mcp-extra'}"
                env_dir = external_root / "envs" / cell
                venv.EnvBuilder(with_pip=True, clear=False).create(env_dir)
                python = env_dir / "bin" / "python"
                scripts = env_dir / "bin"
                env = dict(os.environ)
                env.pop("PYTHONPATH", None)
                env["PYTHONNOUSERSITE"] = "1"
                package_arg = str(artifact) if extra is None else f"{artifact}[{extra}]"
                install = run(f"install-{cell}", [str(python), "-m", "pip", "--isolated", "install", package_arg], work, 300, env=env)
                check("T33", f"install:{cell}", "fresh isolated install succeeds", {"exit_code": install.returncode}, install.returncode == 0)
                if install.returncode != 0:
                    continue

                save = work / f"seed-{cell}.rws"
                shutil.copyfile(FIXTURE, save)
                save_hash = _sha256(save)
                version = run(f"version-{cell}", [str(scripts / "rim-save"), "--version"], work, env=env)
                check("T33", f"entrypoint:{cell}", "rim-save starts outside checkout", {"exit_code": version.returncode, "stdout": version.stdout.strip()}, version.returncode == 0 and version.stdout.strip().startswith("rim-save "))

                origin_code = """import importlib.metadata as m
import importlib.util
import json
import rimworld_save_inspector as p

def ver(name):
    try:
        return m.version(name)
    except m.PackageNotFoundError:
        return None

names = ('rimworld-save-inspector', 'defusedxml', 'pydantic', 'mcp')
print(json.dumps({'version': p.__version__, 'origin': p.__file__,
                  'dependencies': {name: ver(name) for name in names},
                  'mcp_available': importlib.util.find_spec('mcp') is not None}, sort_keys=True))
"""
                origin = run(f"origin-{cell}", [str(python), "-c", origin_code], work, env=env)
                try:
                    provenance = json.loads(origin.stdout)
                except json.JSONDecodeError:
                    provenance = {}
                installed_path = provenance.get("origin", "")
                from_checkout = str(ROOT) in installed_path
                mcp_present = provenance.get("mcp_available") is True
                check("T33", f"provenance:{cell}", "record package/dependency versions and import from fresh environment outside checkout",
                      {"provenance": provenance, "from_checkout": from_checkout},
                      origin.returncode == 0 and not from_checkout and bool(provenance.get("version")))
                check("T33", f"optional-dependency:{cell}", "mcp absent without extra and present with extra", {"mcp_present": mcp_present}, mcp_present == (extra == "mcp"))

                if extra is None:
                    missing = run(f"missing-mcp-{cell}", [str(scripts / "rim-save"), "mcp", "--save", str(save)], work, env=env)
                    try:
                        error = json.loads(missing.stdout)
                        error_code = error["error"]["code"]
                    except (json.JSONDecodeError, KeyError, TypeError):
                        error_code = None
                    check("T33", f"mcp-guard:{cell}", {"exit_code": 2, "error_code": "MCP_EXTRA_REQUIRED"},
                          {"exit_code": missing.returncode, "error_code": error_code, "stderr": missing.stderr},
                          missing.returncode == 2 and error_code == "MCP_EXTRA_REQUIRED" and "Traceback" not in missing.stderr)

                    metadata = run(f"metadata-{cell}", [str(scripts / "rim-save"), "inspect", str(save), "metadata"], work, env=env)
                    try:
                        payload = json.loads(metadata.stdout)
                        version_metadata = payload["provenance"]["parser_version"]
                    except (json.JSONDecodeError, KeyError, TypeError):
                        version_metadata = None
                    check("T33", f"cli-metadata:{cell}", "metadata query succeeds with valid JSON", {"exit_code": metadata.returncode, "parser_version": version_metadata}, metadata.returncode == 0 and version_metadata is not None)
                    if metadata.returncode == 0:
                        pawns_result = run(f"pawns-{cell}", [str(scripts / "rim-save"), "inspect", str(save), "pawns", "--scope", "map"], work, env=env)
                        try:
                            pawn_id = json.loads(pawns_result.stdout)["data"]["entries"][0]["record_id"]
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                            pawn_id = None
                        check("T33", f"cli-pawns:{cell}", "public synthetic fixture returns a map pawn", {"exit_code": pawns_result.returncode, "pawn_record_id": pawn_id}, pawns_result.returncode == 0 and pawn_id is not None)
                        if pawn_id:
                            queries = [
                                ("summary",), ("pawns", "--scope", "map"), ("pawn", pawn_id),
                                ("pawn", pawn_id, "--section", "skills"), ("pawn", pawn_id, "--section", "traits"),
                                ("health", pawn_id), ("genes", pawn_id), ("equipment", pawn_id),
                                ("inventory", pawn_id), ("research",), ("mods",),
                            ]
                            for index, query in enumerate(queries):
                                result = run(f"cli-{cell}-{index:02d}", [str(scripts / "rim-save"), "inspect", str(save), *query], work, env=env)
                                try:
                                    json.loads(result.stdout)
                                    valid_json = True
                                except json.JSONDecodeError:
                                    valid_json = False
                                check("T33", f"cli-domain:{cell}:{query[0]}:{index}", "domain query succeeds with JSON", {"exit_code": result.returncode, "valid_json": valid_json}, result.returncode == 0 and valid_json)
                    check("T33", f"fixture-integrity:{cell}", "copied synthetic input hash equals repository fixture hash", {"copied_sha256": save_hash}, save_hash == _sha256(FIXTURE))
                else:
                    async def exercise_server(command: Path, args: list[str], suffix: str) -> None:
                        params = StdioServerParameters(command=str(command), args=args, cwd=str(work), env=env)
                        async with stdio_client(params) as (read_stream, write_stream):
                            async with ClientSession(read_stream, write_stream, read_timeout_seconds=12) as session:
                                await session.initialize()
                                listed = await session.list_tools()
                                names = {tool.name for tool in listed.tools}
                                loaded = await session.call_tool("inspect_save", {})
                                structured = loaded.structured_content or {}
                                sid = structured.get("snapshot_id")
                                check("T33", f"mcp-startup:{cell}:{suffix}", "real stdio handshake and ten tools", {"tool_count": len(names), "exit_context": "stdio child live"}, len(names) == 10)
                                check("T33", f"mcp-inspect:{cell}:{suffix}", "inspect_save returns a snapshot id", {"is_error": loaded.is_error, "snapshot_id": sid}, not loaded.is_error and isinstance(sid, str))
                                if not sid:
                                    return
                                pawns = await session.call_tool("list_pawns", {"snapshot_id": sid, "scope": "map"})
                                try:
                                    pawn_id = pawns.structured_content["data"]["entries"][0]["record_id"]
                                except (AttributeError, KeyError, IndexError, TypeError):
                                    pawn_id = None
                                calls = [
                                    ("get_colony_summary", {"snapshot_id": sid}),
                                    ("list_pawns", {"snapshot_id": sid, "scope": "map"}),
                                    ("get_pawn", {"snapshot_id": sid, "record_id": pawn_id}),
                                    ("get_health", {"snapshot_id": sid, "record_id": pawn_id}),
                                    ("get_genes", {"snapshot_id": sid, "record_id": pawn_id}),
                                    ("get_equipment", {"snapshot_id": sid, "record_id": pawn_id}),
                                    ("get_inventory", {"snapshot_id": sid, "record_id": pawn_id}),
                                    ("get_research", {"snapshot_id": sid}),
                                    ("get_mods", {"snapshot_id": sid}),
                                ] if pawn_id else []
                                for tool_name, arguments in calls:
                                    response = await session.call_tool(tool_name, arguments)
                                    check("T33", f"mcp-domain:{cell}:{suffix}:{tool_name}", "tool returns structured success", {"is_error": response.is_error, "has_structured_content": response.structured_content is not None}, not response.is_error and response.structured_content is not None)

                    try:
                        anyio.run(exercise_server, scripts / "rim-save", ["mcp", "--save", str(save)], "rim-save-mcp-subcommand")
                        anyio.run(exercise_server, scripts / "rim-save-mcp", ["--save", str(save)], "rim-save-mcp-console-script")
                    except Exception as error:
                        check("T33", f"mcp-transport:{cell}", "both real stdio entrypoints complete domain calls",
                              "no exception", {"error_type": type(error).__name__, "error": str(error)}, False)

        report["status"] = "FAIL" if failures or any(case["status"] == "FAIL" for case in cases) else "PASS"
    except Exception as error:
        failures.append(f"{type(error).__name__}: {error}")
        report["status"] = "FAIL"
        report["failure"] = failures[-1]
    finally:
        report["finished_at"] = _now()
        report["failures"] = failures
        report_path = out / "report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"PACKAGING_REPORT={report_path.relative_to(ROOT)}")
        shutil.rmtree(external_root, ignore_errors=True)

    assert report["status"] == "PASS", json.dumps(report, indent=2, sort_keys=True)
