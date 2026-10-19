"""Evidence-first, deterministic discovery over pawn-history snapshots."""

from .core import (
    DISCOVERY_VERSION,
    EVIDENCE_RUBRIC_VERSION,
    OfflineSemanticAdapter,
    build_evidence_packet,
    discover_collection,
    discover_snapshot,
    semantic_cache_key,
)
from .catalog import discover_catalog

__all__ = [
    "DISCOVERY_VERSION",
    "EVIDENCE_RUBRIC_VERSION",
    "OfflineSemanticAdapter",
    "build_evidence_packet",
    "discover_collection",
    "discover_catalog",
    "discover_snapshot",
    "semantic_cache_key",
]
