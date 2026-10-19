"""Bounded, read-only source access and immutable snapshot construction."""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from pathlib import Path
from typing import BinaryIO

from . import __version__
from .errors import InspectorError
from .models import ParseLimits, Snapshot
from .parser.core import parse_stream


class BoundedReader:
    def __init__(self, stream: BinaryIO, limits: ParseLimits, cancel: threading.Event | None = None) -> None:
        self._stream = stream
        self.limits = limits
        self.cancel = cancel
        self.started = time.monotonic()
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def _check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise InspectorError("CANCELLED", "Snapshot loading was cancelled.")
        if time.monotonic() - self.started > self.limits.deadline_seconds:
            raise InspectorError("LIMIT_EXCEEDED", "The parse wall deadline was exceeded.", {"limit": "deadline_seconds"})

    def read(self, size: int = -1) -> bytes:
        self._check()
        remaining = self.limits.input_bytes - self.bytes_read
        requested = self.limits.read_chunk_bytes if size is None or size < 0 else min(size, self.limits.read_chunk_bytes)
        if remaining <= 0:
            probe = self._stream.read(1)
            if probe:
                raise InspectorError("LIMIT_EXCEEDED", "The input byte limit was exceeded.", {"limit": "input_bytes"})
            return b""
        data = self._stream.read(min(requested, remaining))
        if data:
            self.bytes_read += len(data)
            self.digest.update(data)
        return data

    def close(self) -> None:
        # The loader owns the descriptor and must stat it after SAX reaches EOF.
        return None


def _file_identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def load_snapshot(source: Path, *, limits: ParseLimits | None = None, cancel: threading.Event | None = None) -> Snapshot:
    """Read one explicitly named regular file without writing or searching for saves."""
    limits = limits or ParseLimits()
    try:
        raw_source = os.fspath(source)
    except TypeError as exc:
        raise InspectorError("INVALID_ARGUMENT", "A local filesystem path is required.") from exc
    if isinstance(raw_source, str) and _has_url_scheme(raw_source):
        raise InspectorError("UNSUPPORTED_SOURCE", "Only local regular files are supported.")
    try:
        source_path = Path(raw_source)
    except TypeError as exc:
        raise InspectorError("INVALID_ARGUMENT", "A local filesystem path is required.") from exc
    try:
        resolved = source_path.resolve(strict=True)
        before_path = resolved.stat()
    except FileNotFoundError as exc:
        raise InspectorError("SOURCE_NOT_FOUND", "The configured save could not be opened.") from exc
    except OSError as exc:
        raise InspectorError("SOURCE_UNREADABLE", "The configured save could not be resolved.") from exc
    if not stat.S_ISREG(before_path.st_mode):
        raise InspectorError("UNSUPPORTED_SOURCE", "The source must be a regular file.")

    try:
        stream = resolved.open("rb")
    except FileNotFoundError as exc:
        raise InspectorError("SOURCE_NOT_FOUND", "The configured save could not be opened.") from exc
    except OSError as exc:
        raise InspectorError("SOURCE_UNREADABLE", "The configured save could not be read.") from exc

    try:
        with stream:
            fd_before = os.fstat(stream.fileno())
            if not stat.S_ISREG(fd_before.st_mode):
                raise InspectorError("UNSUPPORTED_SOURCE", "The source must be a regular file.")
            if _file_identity(before_path) != _file_identity(fd_before):
                raise InspectorError("SOURCE_CHANGED", "The source changed before parsing began.")
            reader = BoundedReader(stream, limits, cancel)
            try:
                parsed = parse_stream(reader, limits=limits, cancel=cancel)
            except InspectorError:
                raise
            except ImportError as exc:
                raise InspectorError("INTERNAL_ERROR", "The secure XML parser dependency is unavailable.", {"dependency": "defusedxml"}) from exc
            except Exception as exc:
                from xml.sax import SAXParseException

                try:
                    from defusedxml.common import DefusedXmlException
                except ImportError:
                    DefusedXmlException = ()  # type: ignore[assignment,misc]

                if isinstance(exc, DefusedXmlException):
                    raise InspectorError("UNSAFE_XML", "DTD, entity, or external-reference XML is forbidden.") from exc
                if isinstance(exc, SAXParseException):
                    raise InspectorError("MALFORMED_XML", "The XML document could not be parsed.") from exc
                raise
            fd_after = os.fstat(stream.fileno())
            if _file_identity(fd_before) != _file_identity(fd_after):
                raise InspectorError("SOURCE_CHANGED", "The source changed while it was being parsed.")
        try:
            path_after = source_path.resolve(strict=True)
            path_stat_after = path_after.stat()
        except OSError as exc:
            raise InspectorError("SOURCE_CHANGED", "The source path changed or disappeared during parsing.") from exc
        if path_after != resolved or _file_identity(before_path) != _file_identity(path_stat_after):
            raise InspectorError("SOURCE_CHANGED", "The source path changed while it was being parsed.")
        digest = reader.digest.hexdigest()
        snapshot_id = f"sha256:{digest}"
        parser_version, profile_version = __version__, parsed.profile_version
        interpretation = hashlib.sha256(f"{snapshot_id}\n{parser_version}\n{profile_version}".encode("utf-8")).hexdigest()
        return Snapshot(
            snapshot_id=snapshot_id,
            interpretation_id=f"interpretation:{interpretation}",
            source_bytes=reader.bytes_read,
            source_name=resolved.name,
            game_version=parsed.game_version,
            profile_version=profile_version,
            parser_version=parser_version,
            records=parsed.records,
            globals=parsed.globals,
            global_status=parsed.global_status,
            collection_states=parsed.collection_states,
            diagnostics=parsed.diagnostics,
            diagnostic_counts=parsed.diagnostic_counts,
            auxiliary_records=parsed.auxiliary_records,
            direct_map_candidate_count=parsed.direct_map_candidate_count,
            direct_map_candidate_map_paths=parsed.direct_map_candidate_map_paths,
        )
    except InspectorError:
        raise
    except PermissionError as exc:
        raise InspectorError("SOURCE_UNREADABLE", "The configured save could not be read.") from exc


def _has_url_scheme(value: str) -> bool:
    """Recognize a URL before pathlib normalizes repeated slashes."""
    scheme, separator, _ = value.partition("://")
    return bool(separator and scheme and scheme[0].isalpha()
                and all(character.isalnum() or character in "+.-" for character in scheme))
