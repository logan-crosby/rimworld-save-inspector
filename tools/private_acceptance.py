"""Load frozen private acceptance inputs without embedding their contents in source.

The ignored anchor was established before product comparison.  A missing or
changed input is unavailable; it is never repaired from current observations.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "docs/rimworld-save-inspector/private-case-catalog.json"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def selected_path(variable: str, default: str) -> Path:
    return Path(os.environ.get(variable) or ROOT / default).expanduser()


@dataclass(frozen=True)
class PrivateInputs:
    fixture: Path | None
    oracle_path: Path
    spec_path: Path
    anchor_path: Path
    anchor_expected_sha256: str | None
    anchor_observed_sha256: str | None
    anchor: dict[str, Any] | None
    oracle: dict[str, Any] | None
    baseline_spec: str | None
    errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.errors

    @property
    def expected_bytes(self) -> int | None:
        return self.anchor.get("fixture_bytes") if self.anchor else None

    @property
    def expected_sha256(self) -> str | None:
        return self.anchor.get("fixture_sha256") if self.anchor else None

    def input_hashes(self) -> dict[str, str] | None:
        if not self.ready or self.fixture is None:
            return None
        try:
            return {"anchor_expected": self.anchor_expected_sha256,
                    "anchor_observed": digest(self.anchor_path),
                    "oracle": digest(self.oracle_path),
                    "baseline_spec": digest(self.spec_path), "fixture": digest(self.fixture)}
        except OSError:
            return None

    def expected_input_hashes(self) -> dict[str, str] | None:
        if not self.ready or self.anchor is None:
            return None
        return {"anchor_expected": self.anchor_expected_sha256,
                "anchor_observed": self.anchor_expected_sha256,
                "oracle": self.anchor["oracle_sha256"],
                "baseline_spec": self.anchor["baseline_spec_sha256"],
                "fixture": self.anchor["fixture_sha256"]}


def load_private_inputs(fixture: Path | None = None, *, check_fixture: bool = True,
                        use_env_fixture: bool = True) -> PrivateInputs:
    oracle_path = selected_path("RWSI_PRIVATE_ORACLE", ".private/acceptance-oracle.json")
    spec_path = selected_path("RWSI_PRIVATE_SPEC", ".private/baseline-design.md")
    anchor_path = selected_path("RWSI_PRIVATE_ANCHOR", ".private/acceptance-inputs.json")
    anchor_expected = os.environ.get("RWSI_PRIVATE_ANCHOR_SHA256")
    anchor_observed: str | None = None
    selected = fixture if fixture is not None else (Path(os.environ["RWSI_PRIVATE_FIXTURE"]).expanduser()
                                                  if use_env_fixture and os.environ.get("RWSI_PRIVATE_FIXTURE") else None)
    errors: list[str] = []
    anchor: dict[str, Any] | None = None
    oracle: dict[str, Any] | None = None
    baseline: str | None = None
    if not isinstance(anchor_expected, str) or len(anchor_expected) != 64 or any(
            character not in "0123456789abcdef" for character in anchor_expected):
        errors.append("external private anchor fingerprint unavailable or malformed")
        anchor_expected = None
    try:
        raw_anchor = anchor_path.read_bytes()
        anchor_observed = hashlib.sha256(raw_anchor).hexdigest()
        if anchor_expected is None or not hmac.compare_digest(anchor_observed, anchor_expected):
            errors.append("private anchor differs from external trusted fingerprint")
        else:
            anchor = json.loads(raw_anchor)
        if anchor is None:
            raise ValueError("private anchor is unauthenticated")
        if not isinstance(anchor, dict):
            raise ValueError("private anchor must be an object")
        if (anchor.get("schema_version") != "1.0" or
                not isinstance(anchor.get("source_commit"), str) or
                not isinstance(anchor.get("fixture_bytes"), int) or
                not isinstance(anchor.get("fixture_sha256"), str)):
            errors.append("private anchor has invalid shape")
    except (OSError, ValueError, AttributeError):
        anchor = None
        errors.append("private anchor unavailable or malformed")
    for path, key, label in ((oracle_path, "oracle_sha256", "oracle"),
                             (spec_path, "baseline_spec_sha256", "baseline specification")):
        try:
            if anchor is None or digest(path) != anchor.get(key):
                errors.append(f"private {label} differs from frozen anchor")
            else:
                if label == "oracle":
                    oracle = json.loads(path.read_text())
                    if not isinstance(oracle, dict):
                        raise ValueError("private oracle must be an object")
                else:
                    baseline = path.read_text()
        except (OSError, ValueError):
            if label == "oracle":
                oracle = None
            errors.append(f"private {label} unavailable or malformed")
    if oracle is not None and anchor is not None:
        identity = oracle.get("fixture", {})
        if (identity.get("bytes") != anchor.get("fixture_bytes") or
                identity.get("sha256") != anchor.get("fixture_sha256")):
            errors.append("private oracle fixture identity differs from frozen anchor")
        if (oracle.get("spec_id") != "RWSI-1.0" or
                f"Revision: `{oracle.get('spec_revision')}`" not in (baseline or "")):
            errors.append("private oracle and baseline specification differ")
    if check_fixture:
        if selected is None or not selected.is_file():
            errors.append("private fixture unavailable")
        else:
            try:
                if (anchor is None or selected.stat().st_size != anchor.get("fixture_bytes") or
                        digest(selected) != anchor.get("fixture_sha256")):
                    errors.append("private fixture differs from frozen anchor")
            except OSError:
                errors.append("private fixture unavailable")
    return PrivateInputs(selected, oracle_path, spec_path, anchor_path,
                         anchor_expected, anchor_observed,
                         anchor, oracle, baseline, tuple(errors))


def public_case_ids() -> tuple[set[str], set[str]]:
    catalog = json.loads(CATALOG.read_text())
    return ({row["check_id"] for row in catalog["assertions"]},
            set(catalog["required_dynamic_comparisons"]))
