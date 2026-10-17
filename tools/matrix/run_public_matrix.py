#!/usr/bin/env python3
"""Run public synthetic correctness, CLI contracts and MCP subprocess tests.

Native macOS cells use the supplied locked interpreters. Optional Linux cells
use explicit ``docker run --platform linux/amd64`` and a sanitized public copy
of this checkout; the script never mounts the source checkout or private data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import shlex
import subprocess
import sys
import tempfile
import tarfile
import io
import re
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/synthetic/seed.rws"
LOCK = ROOT / "uv.lock"
TEST_NODE = "tests/packaging/test_public_matrix.py"
TEST_NODES = (
    "tests/integration/test_synthetic_domain_equivalence.py",
    "tests/contract/test_cli_schema.py",
    "tests/mcp/test_mcp_acceptance.py",
    TEST_NODE,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_manifest(root: Path) -> tuple[list[dict[str, object]], str]:
    entries: list[dict[str, object]] = []
    tree = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        relative = path.relative_to(root).as_posix()
        digest = sha256(path)
        size = path.stat().st_size
        entries.append({"path": relative, "bytes": size, "sha256": digest})
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")
    return entries, tree.hexdigest()


def json_marker(output: str, marker: str) -> dict[str, object] | None:
    match = re.search(re.escape(marker) + r"(\{[^\r\n]*\})", output)
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def sanitized_copy(destination: Path) -> Path:
    target = destination / "public-source"
    blocked = {".git", ".private", "evidence", "artifacts", "__pycache__", ".pytest_cache"}
    public_saves = {"tests/fixtures/synthetic/seed.rws": ROOT / "tests/fixtures/synthetic/seed.rws",
                    "examples/minimal_colony.rws": ROOT / "examples/minimal_colony.rws"}
    public_sample_sha256 = "f55d5e0741972f959b39029e26e6921c4d768bc5eb80c9726610b9ad85d3b7d7"
    for directory, dirnames, filenames in os.walk(ROOT, topdown=True, followlinks=False):
        source_dir = Path(directory)
        relative = source_dir.relative_to(ROOT)
        if any(part in blocked or part.startswith(".venv") for part in relative.parts):
            dirnames[:] = []
            continue
        destination_dir = target / relative
        destination_dir.mkdir(parents=True, exist_ok=True)
        dirnames[:] = [name for name in dirnames if name not in blocked
                       and not name.startswith(".venv")
                       and not (source_dir / name).is_symlink()]
        for name in filenames:
            source_file = source_dir / name
            if source_file.is_symlink() or name.startswith(".venv"):
                continue
            if name.lower().endswith(".rws"):
                key = source_file.relative_to(ROOT).as_posix()
                if key not in public_saves or (key == "examples/minimal_colony.rws" and
                                                sha256(source_file) != public_sample_sha256):
                    continue
            shutil.copy2(source_file, destination_dir / name)
    return target


def _unapproved_saves(root: Path) -> list[str]:
    allowed = {"tests/fixtures/synthetic/seed.rws": sha256(FIXTURE),
               "examples/minimal_colony.rws": "f55d5e0741972f959b39029e26e6921c4d768bc5eb80c9726610b9ad85d3b7d7"}
    return [path.relative_to(root).as_posix() for path in root.rglob("*.rws")
            if (path.relative_to(root).as_posix() not in allowed or
                sha256(path) != allowed[path.relative_to(root).as_posix()])]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="new ignored evidence/packaging-matrix-* directory")
    parser.add_argument("--python311", default=str(ROOT / ".venv-3.11/bin/python"))
    parser.add_argument("--python314", default=str(ROOT / ".venv/bin/python"))
    parser.add_argument("--docker", action="store_true", help="run Linux x86_64/amd64 cells; caller owns Docker readiness")
    parser.add_argument("--docker311-image", default="python:3.11-slim")
    parser.add_argument("--docker314-image", default="python:3.14-slim")
    parser.add_argument("--linux-ssh", action="store_true", help="run Linux cells over an explicitly configured SSH transport")
    parser.add_argument("--ssh-host", help="Linux guest address (required with --linux-ssh)")
    parser.add_argument("--ssh-user", help="Linux guest account (required with --linux-ssh)")
    parser.add_argument("--ssh-port", type=int, help="Linux guest SSH port (required with --linux-ssh)")
    parser.add_argument("--ssh-key", help="private key path (required with --linux-ssh)")
    parser.add_argument("--ssh-known-hosts", help="pinned known_hosts file (required with --linux-ssh)")
    parser.add_argument("--ssh-command", default="ssh", help="SSH client executable")
    args = parser.parse_args()

    if args.docker and args.linux_ssh:
        parser.error("--docker and --linux-ssh select different Linux transports")
    if args.linux_ssh:
        missing = [name for name in ("ssh_host", "ssh_user", "ssh_port", "ssh_key", "ssh_known_hosts")
                   if not getattr(args, name)]
        if missing:
            parser.error("--linux-ssh requires " + ", ".join("--" + name.replace("_", "-") for name in missing))
        if not Path(args.ssh_key).is_file() or not Path(args.ssh_known_hosts).is_file():
            parser.error("--ssh-key and --ssh-known-hosts must name existing files")
        if not (1 <= args.ssh_port <= 65535):
            parser.error("--ssh-port must be between 1 and 65535")

    out = (ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out).resolve()
    if out.exists():
        parser.error(f"output directory already exists: {out}")
    out.mkdir(parents=True)
    (out / "logs").mkdir()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    report: dict[str, object] = {
        "report_version": "1",
        "run_id": out.name,
        "case_ids": ["T34"],
        "related_cases": ["T25", "T27", "T28", "T32"],
        "status": "RUNNING",
        "started_at": now(),
        "finished_at": None,
        "host": {"os": platform.platform(), "architecture": platform.machine()},
        "source_git_head": revision.stdout.strip() if revision.returncode == 0 else None,
        "lock_sha256": sha256(LOCK),
        "public_fixture": {"path": "tests/fixtures/synthetic/seed.rws", "bytes": FIXTURE.stat().st_size, "sha256": sha256(FIXTURE)},
        "test_nodes": [{"path": node, "sha256": sha256(ROOT / node)} for node in TEST_NODES],
        "cells": [],
        "commands": [],
        "artifacts": [],
        "limits": ["No private fixture is copied or mounted in Linux cells.",
                   "Docker amd64 on arm64 and the Debian x86_64 guest use emulation."],
    }
    cells: list[dict[str, object]] = report["cells"]  # type: ignore[assignment]
    commands: list[dict[str, object]] = report["commands"]  # type: ignore[assignment]
    artifacts: list[dict[str, object]] = report["artifacts"]  # type: ignore[assignment]

    def capture(cell: str, argv: list[str], cwd: Path | None, timeout: int = 900,
                stdin_data: bytes | None = None) -> subprocess.CompletedProcess[str] | None:
        started_at = now()
        started = time.monotonic()
        try:
            result = subprocess.run(argv, cwd=cwd, input=stdin_data, capture_output=True,
                                   text=stdin_data is None, timeout=timeout, env=common_env)
            exit_code: int | None = result.returncode
            stdout = result.stdout if isinstance(result.stdout, str) else result.stdout.decode("utf-8", "replace")
            stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode("utf-8", "replace")
            timed_out = False
        except subprocess.TimeoutExpired as error:
            exit_code = None
            stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else error.stdout or ""
            stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else error.stderr or ""
            timed_out = True
            result = None
        except OSError as error:
            exit_code = None
            stdout = ""
            stderr = f"{type(error).__name__}: {error}\n"
            timed_out = False
            result = None
        duration = time.monotonic() - started
        stem = f"{cell}-{len(commands):02d}"
        stdout_path = out / "logs" / f"{stem}.stdout"
        stderr_path = out / "logs" / f"{stem}.stderr"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        commands.append({
            "argv": argv,
            "cwd_label": "public sanitized checkout" if cwd and cwd != ROOT else "product checkout",
            "started_at": started_at,
            "duration_seconds": round(duration, 6),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout_path": str(stdout_path.relative_to(out)),
            "stderr_path": str(stderr_path.relative_to(out)),
            "stdin_bytes": len(stdin_data) if stdin_data is not None else 0,
            "stdin_sha256": hashlib.sha256(stdin_data).hexdigest() if stdin_data is not None else None,
        })
        for path in (stdout_path, stderr_path):
            artifacts.append({"path": str(path.relative_to(out)), "bytes": path.stat().st_size, "sha256": sha256(path)})
        return result

    common_env = dict(os.environ)
    common_env.pop("PYTHONPATH", None)
    common_env["PYTHONNOUSERSITE"] = "1"

    def native_cell(version_label: str, python: str, expected_minor: str) -> None:
        executable = Path(python)
        if not executable.is_file():
            cells.append({"id": f"macos-arm64-python-{version_label}", "status": "BLOCKED", "reason": f"interpreter missing: {python}"})
            return
        probe = capture(f"probe-macos-{version_label}", [str(executable), "-c",
            "import importlib.metadata as m, importlib.util as iu, json, platform, rimworld_save_inspector as p, sys; "
            "names=('rimworld-save-inspector','defusedxml','pydantic','mcp'); "
            "print(json.dumps({'python':sys.version,'version':sys.version_info[:2], 'os':platform.system(),"
            "'machine':platform.machine(),'package_version':p.__version__,'package_origin':p.__file__,"
            "'dependencies':{n:(m.version(n) if n!='mcp' or iu.find_spec('mcp') else None) for n in names}}))"], ROOT)
        environment: dict[str, object] = {}
        if probe is not None and probe.returncode == 0:
            try:
                environment = json.loads(probe.stdout)
            except json.JSONDecodeError:
                pass
        cell_id = f"macos-arm64-python-{version_label}"
        if probe is None or probe.returncode != 0:
            cells.append({"id": cell_id, "status": "FAIL", "environment": environment,
                          "reason": "interpreter probe failed; see captured command stdout/stderr"})
            return
        if not environment:
            cells.append({"id": cell_id, "status": "FAIL", "environment": environment,
                          "reason": "interpreter probe returned invalid JSON; see captured command stdout/stderr"})
            return
        actual = environment.get("version")
        supported = actual == [int(expected_minor.split(".")[0]), int(expected_minor.split(".")[1])] and environment.get("os") == "Darwin" and environment.get("machine") == "arm64"
        if not supported:
            cells.append({"id": cell_id, "status": "BLOCKED", "environment": environment,
                          "reason": "interpreter does not match required macOS arm64/Python cell"})
            return
        result = capture(f"test-macos-{version_label}", [str(executable), "-m", "pytest", "-q", "-s", *TEST_NODES], ROOT, timeout=1800)
        profile = json_marker(result.stdout, "MATRIX_PROFILE=") if result is not None else None
        cells.append({"id": cell_id,
                      "status": "PASS" if result is not None and result.returncode == 0 else "FAIL",
                      "environment": environment, "test_node": TEST_NODE,
                      "profile": profile,
                      "observed": result.stdout.strip().splitlines()[-1] if result and result.stdout.strip() else "no pytest summary"})

    native_cell("3.11", args.python311, "3.11")
    native_cell("3.14", args.python314, "3.14")

    linux_cells = [("3.11", args.docker311_image), ("3.14", args.docker314_image)]
    if args.linux_ssh:
        # The only guest input is a tar stream from this deliberately sanitized
        # public checkout. No host path is mounted and no private directory is
        # included. Remote paths use a generated, conservative run-id token.
        safe_run_id = re.sub(r"[^A-Za-z0-9_-]", "-", out.name)[:64]
        ssh_base = [args.ssh_command, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                    "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={args.ssh_known_hosts}",
                    "-i", args.ssh_key, "-p", str(args.ssh_port), f"{args.ssh_user}@{args.ssh_host}"]
        remote_root = f"/tmp/rwsi-matrix-{safe_run_id}"
        with tempfile.TemporaryDirectory(prefix="rim-save-public-matrix-ssh-") as temp_name:
            public_root = sanitized_copy(Path(temp_name))
            public_fixture = public_root / "tests/fixtures/synthetic/seed.rws"
            if not public_fixture.is_file() or _unapproved_saves(public_root):
                raise RuntimeError("sanitized guest input failed public-fixture boundary checks")
            manifest, manifest_digest = source_manifest(public_root)
            report["public_source_manifest"] = manifest
            report["public_source_tree_sha256"] = manifest_digest
            tar_buffer = io.BytesIO()
            with tarfile.open(fileobj=tar_buffer, mode="w:gz") as archive:
                archive.add(public_root, arcname="public-source", recursive=True)
            tar_bytes = tar_buffer.getvalue()
            remote_setup = f"mkdir -p {shlex.quote(remote_root)} && tar -xzf - -C {shlex.quote(remote_root)}"
            transfer = capture("transfer-sanitized-source", [*ssh_base, remote_setup], None,
                               timeout=300, stdin_data=tar_bytes)
            report["linux_transport"] = {
                "type": "ssh-streamed-sanitized-tar", "host": args.ssh_host,
                "port": args.ssh_port, "user": args.ssh_user,
                "host_key_policy": "StrictHostKeyChecking=yes with caller-pinned known_hosts",
                "source_mounts": 0, "private_fixture_included": False,
                "tar_bytes": len(tar_bytes), "tar_sha256": hashlib.sha256(tar_bytes).hexdigest(),
            }
            transfer_ok = transfer is not None and transfer.returncode == 0
            base = f"{remote_root}/public-source"
            if transfer_ok:
                bootstrap = (
                    f"set -eu; cd {shlex.quote(base)}; "
                    f"mkdir -p {shlex.quote(remote_root + '/uv-bin')}; "
                    f"curl --fail --silent --show-error --location https://astral.sh/uv/0.11.6/install.sh "
                    f"--output {shlex.quote(remote_root + '/uv-installer.sh')}; "
                    f"printf 'UV_INSTALLER_SHA256='; sha256sum {shlex.quote(remote_root + '/uv-installer.sh')}; "
                    f"UV_INSTALL_DIR={shlex.quote(remote_root + '/uv-bin')} UV_NO_MODIFY_PATH=1 UV_VERSION=0.11.6 "
                    f"sh {shlex.quote(remote_root + '/uv-installer.sh')}; "
                    f"printf 'UV_VERSION='; {shlex.quote(remote_root + '/uv-bin/uv')} --version; "
                    f"printf 'UV_BINARY_SHA256='; sha256sum {shlex.quote(remote_root + '/uv-bin/uv')}; "
                    f"{shlex.quote(remote_root + '/uv-bin/uv')} python install 3.14"
                )
                boot = capture("bootstrap-linux-guest", [*ssh_base, f"sh -lc {shlex.quote(bootstrap)}"], None, timeout=900)
                transfer_ok = boot is not None and boot.returncode == 0
                if boot is not None:
                    installer_hash = re.search(r"UV_INSTALLER_SHA256=([0-9a-f]{64})", boot.stdout)
                    uv_version = re.search(r"UV_VERSION=(uv [^\r\n]+)", boot.stdout)
                    binary_hash = re.search(r"UV_BINARY_SHA256=([0-9a-f]{64})", boot.stdout)
                    report["linux_bootstrap"] = {
                        "installer_url": "https://astral.sh/uv/0.11.6/install.sh",
                        "pinned_uv_version": "0.11.6",
                        "observed_uv_version": uv_version.group(1) if uv_version else None,
                        "installer_sha256": installer_hash.group(1) if installer_hash else None,
                        "uv_binary_sha256": binary_hash.group(1) if binary_hash else None,
                        "python_314_installed": transfer_ok,
                    }
            else:
                boot = None
            if not transfer_ok:
                reason = "sanitized source transfer or pinned uv bootstrap failed; inspect captured command logs"
                for version, _image in linux_cells:
                    cells.append({"id": f"linux-x86_64-python-{version}", "status": "BLOCKED", "reason": reason})
            else:
                uv = shlex.quote(remote_root + "/uv-bin/uv")
                for version, _image in linux_cells:
                    env_dir = f".venv-{version}"
                    prep_and_probe = (
                        f"set -eu; cd {shlex.quote(base)}; "
                        f"UV_PROJECT_ENVIRONMENT={shlex.quote(env_dir)} {uv} sync --frozen --extra dev --extra mcp --python {version}; "
                        f"printf 'LOCKED_DEPENDENCY_TREE_SHA256='; {uv} tree --locked --depth 0 | sha256sum; "
                        f"{shlex.quote(env_dir + '/bin/python')} -c "
                        + shlex.quote(
                            "import importlib.metadata as m,importlib.util as iu,json,platform,sys,rimworld_save_inspector as p; "
                            "names=('defusedxml','pydantic','mcp','pytest'); "
                            "print('CELL_ENV='+json.dumps({'python':sys.version,'version':sys.version_info[:2],"
                            "'os':platform.system(),'machine':platform.machine(),'package_version':p.__version__,"
                            "'origin':p.__file__,'dependencies':{n:(m.version(n) if n!='mcp' or iu.find_spec('mcp') else None) for n in names}}))"
                        ) + "; "
                        f"RWSI_TCG_TIMEOUT_SCALE=10 {shlex.quote(env_dir + '/bin/python')} -m pytest -q -s "
                        + " ".join(shlex.quote(node) for node in TEST_NODES)
                    )
                    result = capture(f"test-linux-{version}", [*ssh_base, f"sh -lc {shlex.quote(prep_and_probe)}"], None, timeout=2400)
                    environment: dict[str, object] = {
                        "requested_platform": "linux/x86_64",
                        "execution_mode": "QEMU TCG emulation (Debian 12 x86_64 guest)",
                    }
                    profile: dict[str, object] = {}
                    if result is not None:
                        for line in result.stdout.splitlines():
                            if line.startswith("LOCKED_DEPENDENCY_TREE_SHA256="):
                                environment["locked_dependency_tree_sha256"] = line.split("=", 1)[1].split()[0]
                        cell_env = json_marker(result.stdout, "CELL_ENV=")
                        if cell_env is not None:
                            environment.update(cell_env)
                        profile = json_marker(result.stdout, "MATRIX_PROFILE=") or {}
                    expected_version = [int(version.split(".")[0]), int(version.split(".")[1])]
                    correct_environment = (environment.get("version") == expected_version
                                           and environment.get("os") == "Linux"
                                           and environment.get("machine") in {"x86_64", "amd64"})
                    cells.append({"id": f"linux-x86_64-python-{version}",
                                  "status": "FAIL" if result is not None and result.returncode != 0
                                  else "PASS" if correct_environment else "BLOCKED",
                                  "environment": environment, "profile": profile,
                                  "test_node": TEST_NODE,
                                  "reason": None if correct_environment else "guest did not report the required Linux x86_64/Python cell",
                                  "observed": result.stdout.strip().splitlines()[-1] if result and result.stdout.strip() else "no pytest summary"})
    elif not args.docker:
        for version, _image in linux_cells:
            cells.append({"id": f"linux-x86_64-python-{version}", "status": "BLOCKED", "reason": "Docker run not requested; daemon/readiness is lead-owned."})
    else:
        docker_info = capture("docker-info", ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"], None, timeout=30)
        docker_ready = docker_info is not None and docker_info.returncode == 0
        if not docker_ready:
            for version, _image in linux_cells:
                cells.append({"id": f"linux-x86_64-python-{version}", "status": "BLOCKED", "reason": "Docker daemon is not ready; no container was started."})
        else:
            with tempfile.TemporaryDirectory(prefix="rim-save-public-matrix-") as temp_name:
                public_root = sanitized_copy(Path(temp_name))
                public_fixture = public_root / "tests/fixtures/synthetic/seed.rws"
                assert public_fixture.is_file()
                assert not _unapproved_saves(public_root)
                manifest, manifest_digest = source_manifest(public_root)
                report["public_source_manifest"] = manifest
                report["public_source_tree_sha256"] = manifest_digest
                for version, image in linux_cells:
                    setup_and_test = (
                        "python -m pip --isolated install 'uv==0.11.6' && "
                        "uv sync --frozen --extra dev --extra mcp && "
                        "printf 'UV_VERSION='; uv --version; printf 'UV_BINARY_SHA256='; sha256sum \"$(command -v uv)\"; "
                        "printf 'LOCKED_DEPENDENCY_TREE_SHA256='; uv tree --locked --depth 0 | sha256sum; "
                        ".venv/bin/python -c \"import importlib.metadata as m,importlib.util as iu,json,platform,sys,rimworld_save_inspector as p; names=('defusedxml','pydantic','mcp','pytest'); print('CELL_ENV='+json.dumps({'python':sys.version,'version':sys.version_info[:2],'os':platform.system(),'machine':platform.machine(),'package_version':p.__version__,'origin':p.__file__,'dependencies':{n:(m.version(n) if n!='mcp' or iu.find_spec('mcp') else None) for n in names}}))\" && "
                        f"RWSI_TCG_TIMEOUT_SCALE=10 .venv/bin/python -m pytest -q -s {' '.join(shlex.quote(node) for node in TEST_NODES)}"
                    )
                    command = ["docker", "run", "--rm", "--platform=linux/amd64",
                              "--volume", f"{public_root}:/workspace", "--workdir", "/workspace",
                              image, "sh", "-c", setup_and_test]
                    result = capture(f"test-linux-{version}", command, None, timeout=1800)
                    environment: dict[str, object] = {
                        "image": image,
                        "requested_platform": "linux/amd64",
                        "execution_mode": "emulation (macOS arm64 host to linux/amd64)",
                    }
                    if result is not None:
                        cell_env = json_marker(result.stdout, "CELL_ENV=")
                        if cell_env is not None:
                            environment.update(cell_env)
                        dependency_tree = re.search(r"LOCKED_DEPENDENCY_TREE_SHA256=([0-9a-f]{64})", result.stdout)
                        if dependency_tree is not None:
                            environment["locked_dependency_tree_sha256"] = dependency_tree.group(1)
                        uv_version = re.search(r"UV_VERSION=(uv [^\r\n]+)", result.stdout)
                        if uv_version is not None:
                            environment["uv_version"] = uv_version.group(1)
                        uv_hash = re.search(r"UV_BINARY_SHA256=([0-9a-f]{64})", result.stdout)
                        if uv_hash is not None:
                            environment["uv_binary_sha256"] = uv_hash.group(1)
                        profile = json_marker(result.stdout, "MATRIX_PROFILE=")
                        if profile is not None:
                            environment["profile"] = profile
                    expected_version = [int(version.split(".")[0]), int(version.split(".")[1])]
                    correct_environment = (
                        environment.get("version") == expected_version
                        and environment.get("os") == "Linux"
                        and environment.get("machine") in {"x86_64", "amd64"}
                    )
                    cells.append({"id": f"linux-x86_64-python-{version}",
                                  "status": "FAIL" if result is not None and result.returncode != 0
                                  else "PASS" if correct_environment else "BLOCKED",
                                  "environment": environment,
                                  "test_node": TEST_NODE,
                                  "reason": None if correct_environment else "container did not report the required Linux x86_64/Python cell",
                                  "observed": result.stdout.strip().splitlines()[-1] if result and result.stdout.strip() else "no pytest summary"})

    statuses = [cell["status"] for cell in cells]
    report["status"] = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"
    report["finished_at"] = now()
    report_path = out / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"MATRIX_REPORT={report_path.relative_to(ROOT)}")
    return 0 if report["status"] == "PASS" else 1 if report["status"] == "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
