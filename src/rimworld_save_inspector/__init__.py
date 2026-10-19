"""Read-only inspection of local RimWorld save snapshots."""

__version__ = "1.1.0b1"

from .errors import InspectorError
from .models import ParseLimits, QueryRequest, QueryResponse, Snapshot
from .service import QueryService
from .source import load_snapshot

__all__ = [
    "InspectorError",
    "ParseLimits",
    "QueryRequest",
    "QueryResponse",
    "QueryService",
    "Snapshot",
    "load_snapshot",
]
