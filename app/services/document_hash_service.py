"""Byte-level SHA-256 verification for staged closing PDF/TIFF documents."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings


ALLOWED_DOCUMENT_SUFFIXES = frozenset({".pdf", ".tif", ".tiff"})
CHUNK_BYTES = 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


class DocumentHashError(ValueError):
    """A safe, stable document-verification error code."""


@dataclass(frozen=True)
class DocumentHashResult:
    filename: str
    expected_sha256: str
    actual_sha256: str
    verified: bool


def staging_root() -> Path:
    return Path(get_settings().closing_staging_root).resolve()


def normalize_expected_digest(expected_sha256: str) -> str:
    candidate = expected_sha256.strip() if isinstance(expected_sha256, str) else ""
    if not _SHA256_HEX.fullmatch(candidate):
        raise DocumentHashError("invalid_expected_digest")
    return candidate.lower()


def validate_relative_document_name(relative_name: str) -> str:
    if not isinstance(relative_name, str) or not relative_name:
        raise DocumentHashError("invalid_filename")
    if relative_name.strip() != relative_name or "\x00" in relative_name:
        raise DocumentHashError("invalid_filename")

    path = Path(relative_name)
    if path.is_absolute() or ".." in path.parts:
        raise DocumentHashError("path_not_allowed")
    if path.suffix.lower() not in ALLOWED_DOCUMENT_SUFFIXES:
        raise DocumentHashError("unsupported_document_type")
    return relative_name


def resolve_staged_path(relative_name: str, *, root: Path | None = None) -> Path:
    validated_name = validate_relative_document_name(relative_name)
    base = (root or staging_root()).resolve()
    try:
        candidate = (base / validated_name).resolve(strict=True)
    except FileNotFoundError as exc:
        raise DocumentHashError("document_not_found") from exc
    except OSError as exc:
        raise DocumentHashError("document_unreadable") from exc

    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise DocumentHashError("path_not_allowed") from exc
    if not candidate.is_file():
        raise DocumentHashError("document_not_found")
    return candidate


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_DOCUMENT_BYTES:
                    raise DocumentHashError("document_too_large")
                digest.update(chunk)
    except DocumentHashError:
        raise
    except OSError as exc:
        raise DocumentHashError("document_unreadable") from exc
    return digest.hexdigest()


def verify_document_bytes_sync(
    relative_name: str,
    expected_sha256: str,
    *,
    root: Path | None = None,
) -> DocumentHashResult:
    """Hash file contents only; borrower metadata is never an input."""

    expected = normalize_expected_digest(expected_sha256)
    path = resolve_staged_path(relative_name, root=root)
    actual = _hash_file(path)
    return DocumentHashResult(
        filename=path.name,
        expected_sha256=expected,
        actual_sha256=actual,
        verified=hmac.compare_digest(actual, expected),
    )


async def verify_document_bytes(
    relative_name: str,
    expected_sha256: str,
    *,
    root: Path | None = None,
) -> DocumentHashResult:
    """Run streaming file I/O outside the asyncio event-loop thread."""

    return await asyncio.to_thread(
        verify_document_bytes_sync,
        relative_name,
        expected_sha256,
        root=root,
    )
