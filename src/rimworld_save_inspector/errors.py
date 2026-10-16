"""Stable application errors shared by the loader and query service."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class InspectorError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        *,
        retryable: bool | None = None,
        snapshot_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = code in {"SOURCE_CHANGED", "SNAPSHOT_LOADING", "CANCELLED"} if retryable is None else retryable
        self.snapshot_id = snapshot_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "snapshot_id": self.snapshot_id,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "retryable": self.retryable,
            },
        }
