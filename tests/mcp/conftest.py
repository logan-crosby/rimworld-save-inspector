"""Retain synthetic MCP failure evidence outside pytest's temporary cleanup."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import uuid

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    evidence_root = Path(item.config.rootpath) / "evidence"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    slug = "".join(ch if ch.isalnum() else "-" for ch in item.nodeid)[-96:]
    destination = evidence_root / f"mcp-attempt-{stamp}-{slug}-{uuid.uuid4().hex[:8]}"
    destination.mkdir(parents=True, exist_ok=False)
    tmp_path = item.funcargs.get("tmp_path")
    if tmp_path is not None and Path(tmp_path).exists():
        shutil.copytree(tmp_path, destination / "synthetic-tmp", dirs_exist_ok=True)
    capture = "\n".join(f"===== {name} =====\n{content}" for name, content in report.sections)
    details = {
        "nodeid": item.nodeid,
        "phase": report.when,
        "command": [sys.executable, "-m", "pytest", item.nodeid],
        "duration_seconds": report.duration,
        "failure": str(report.longrepr),
        "captured_output": capture,
    }
    (destination / "failure.json").write_text(json.dumps(details, indent=2, sort_keys=True) + "\n", encoding="utf-8")
