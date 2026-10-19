"""Conservative campaign and pawn identities for cross-save history."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

CAMPAIGN_FIELDS = (
    "seedString", "persistentRandomValue", "initialMapSize", "startingYear",
    "gameStartAbsTick", "startingTile",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def campaign_identity(metadata: Mapping[str, Any]) -> str | None:
    """Hash only the six validated, saved immutable campaign values."""
    fields = metadata.get("campaign_key")
    if not isinstance(fields, Mapping):
        return None
    stated_identity = fields.get("identity")
    if isinstance(stated_identity, Mapping) and stated_identity.get("status") != "compatible":
        return None
    fields = fields.get("fields", fields)
    if not isinstance(fields, Mapping):
        return None
    values = []
    for name in CAMPAIGN_FIELDS:
        claim = fields.get(name)
        if not isinstance(claim, Mapping) or claim.get("status") not in {"observed", "derived"}:
            return None
        value = claim.get("value")
        if isinstance(value, bool) or value is None or not isinstance(value, (str, int)):
            return None
        value = str(value).strip()
        if not value:
            return None
        if name == "initialMapSize":
            parts = value.strip("() ").split(",")
            if len(parts) != 3 or any(not p.strip().isdigit() for p in parts):
                return None
            value = "(" + ",".join(str(int(p.strip())) for p in parts) + ")"
        values.append(value)
    return hashlib.sha256(_canonical(values)).hexdigest()


def classify_compatibility(anchor: Mapping[str, Any], other: Mapping[str, Any]) -> str:
    """Return compatible, incompatible, or review_needed without a name fallback."""
    first, second = campaign_identity(anchor), campaign_identity(other)
    if first is None or second is None:
        return "review_needed"
    return "compatible" if first == second else "incompatible"


def normalize_pawn_id(value: Any) -> str | None:
    """Retain the saved ID spelling; discard only outer serialization whitespace."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
