from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ClosingDocument, DocumentVerificationStatus
from app.services.document_hash_service import (
    DocumentHashError,
    normalize_expected_digest,
    validate_relative_document_name,
)

DOCUMENT_HASH_DISPATCH_PENDING = "document_hash_dispatch_pending"
DOCUMENT_HASH_DISPATCH_FAILED = "document_hash_dispatch_failed"
MAX_DOCUMENTS_PER_PACKAGE = 64

MAX_DOCUMENTS_PER_PACKAGE = 64
DOCUMENT_HASH_DISPATCH_PENDING = "document_hash_dispatch_pending"
DOCUMENT_HASH_DISPATCH_FAILED = "document_hash_dispatch_failed"


@dataclass(frozen=True)
class DocumentManifestItem:
    filename: str
    expected_sha256: str


def parse_document_manifest(payload: object) -> tuple[DocumentManifestItem, ...]:
    if not isinstance(payload, dict):
        raise DocumentHashError("document_manifest_required")
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise DocumentHashError("document_manifest_required")
    if len(raw_documents) > MAX_DOCUMENTS_PER_PACKAGE:
        raise DocumentHashError("document_manifest_too_large")
    if len(raw_documents) > MAX_DOCUMENTS_PER_PACKAGE:
        raise DocumentHashError("document_manifest_too_large")

    documents: list[DocumentManifestItem] = []
    filenames: set[str] = set()
    for raw in raw_documents:
        if not isinstance(raw, dict):
            raise DocumentHashError("invalid_document_manifest")
        filename = raw.get("filename")
        expected_sha256 = raw.get("expected_sha256")
        if not isinstance(filename, str) or len(filename) > 256:
            raise DocumentHashError("invalid_filename")
        validated_filename = validate_relative_document_name(filename)
        if validated_filename in filenames:
            raise DocumentHashError("duplicate_document")
        if not isinstance(expected_sha256, str):
            raise DocumentHashError("invalid_expected_digest")

        filenames.add(validated_filename)
        documents.append(
            DocumentManifestItem(
                filename=validated_filename,
                expected_sha256=normalize_expected_digest(expected_sha256),
            )
        )
    return tuple(documents)


async def register_document_manifest(
    db: AsyncSession,
    *,
    closing_id: uuid.UUID,
    source_event_id: uuid.UUID,
    documents: tuple[DocumentManifestItem, ...],
) -> list[ClosingDocument]:
    if not documents:
        raise DocumentHashError("document_manifest_required")

    rows = [
        ClosingDocument(
            closing_id=closing_id,
            source_event_id=source_event_id,
            filename=document.filename,
            expected_sha256=document.expected_sha256,
            status=DocumentVerificationStatus.PENDING.value,
        )
        for document in documents
    ]
    db.add_all(rows)
    await db.flush()
    return rows


def package_statuses_allow_advance(statuses: Iterable[str]) -> bool:
    values = tuple(statuses)
    return bool(values) and all(
        status == DocumentVerificationStatus.VERIFIED.value for status in values
    )


async def package_results_allow_advance(
    db: AsyncSession,
    *,
    closing_id: uuid.UUID,
    source_event_id: uuid.UUID | None = None,
) -> bool:
    latest_result = await db.execute(
        select(ClosingDocument.source_event_id)
        .where(ClosingDocument.closing_id == closing_id)
        .order_by(ClosingDocument.created_at.desc(), ClosingDocument.id.desc())
        .limit(1)
    )
    latest_event_id = latest_result.scalar_one_or_none()
    if latest_event_id is None:
        return False
    if source_event_id is not None and source_event_id != latest_event_id:
        return False

    result = await db.execute(
        select(ClosingDocument.status).where(
            ClosingDocument.closing_id == closing_id,
            ClosingDocument.source_event_id == latest_event_id,
        )
    )
    return package_statuses_allow_advance(result.scalars().all())


def serialize_document_result(row: ClosingDocument) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "source_event_id": str(row.source_event_id),
        "filename": row.filename,
        "expected_sha256": row.expected_sha256,
        "actual_sha256": row.actual_sha256,
        "status": row.status,
        "error": row.error,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
    }
