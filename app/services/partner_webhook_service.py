"""
Partner webhook ingestion.

INTENTIONAL WEAKNESSES:
- No HMAC / signature verification on ingest.
- idempotency_key is persisted but duplicates are not rejected (replay).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ClosingCase, PartnerWebhookEvent, WorkflowState
from app.models.enums import ActorType
from app.services import document_service
from app.services.document_hash_service import DocumentHashError
from app.services.workflow_engine import apply_transition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PartnerWebhookOutcome:
    event: PartnerWebhookEvent
    document_verification_event_id: uuid.UUID | None = None


async def ingest_partner_event(
    db: AsyncSession,
    *,
    partner_id: str,
    event_type: str,
    body: dict[str, Any],
    headers_snapshot: dict[str, Any] | None,
) -> PartnerWebhookOutcome:
    # DEMO: always accept — no signature gate.
    row = PartnerWebhookEvent(
        partner_id=partner_id,
        event_type=event_type,
        idempotency_key=body.get("idempotency_key"),
        raw_body=body,
        headers_snapshot=headers_snapshot,
        processed_ok=False,
    )
    db.add(row)
    await db.flush()

    closing = await _resolve_closing(
        db,
        body,
        for_update=event_type == "documents_packaged",
    )
    if closing is None:
        row.processing_error = "closing_not_resolved"
        await db.flush()
        return PartnerWebhookOutcome(event=row)

    if event_type == "documents_packaged":
        target = body.get("target_state")
        if target not in (None, "", WorkflowState.DOCS_READY.value):
            row.processing_error = "document_hash_target_state_not_allowed"
            await db.flush()
            return PartnerWebhookOutcome(event=row)

        try:
            documents = document_service.parse_document_manifest(body.get("payload"))
            await document_service.register_document_manifest(
                db,
                closing_id=closing.id,
                source_event_id=row.id,
                documents=documents,
            )
        except DocumentHashError as exc:
            row.processing_error = str(exc)
            await db.flush()
            return PartnerWebhookOutcome(event=row)

        # The event doubles as a durable dispatch outbox. A periodic reconciler
        # retries this marker if the API process stops after commit but before publish.
        row.processed_ok = False
        row.processing_error = document_service.DOCUMENT_HASH_DISPATCH_PENDING
        await db.flush()
        return PartnerWebhookOutcome(
            event=row,
            document_verification_event_id=row.id,
        )

    target = body.get("target_state")
    if isinstance(target, str) and target:
        try:
            await apply_transition(
                db,
                closing,
                target,
                actor_type=ActorType.PARTNER_WEBHOOK,
                actor_id=partner_id,
                payload={"event_type": event_type, "envelope": body},
                correlation_id=body.get("idempotency_key"),
            )
        except ValueError as exc:
            row.processing_error = str(exc)
            await db.flush()
            return PartnerWebhookOutcome(event=row)

    # Convenience transitions for demo LOS events
    if event_type == "borrower_acknowledged":
        if closing.state == WorkflowState.DOCS_READY.value:
            await apply_transition(
                db,
                closing,
                WorkflowState.BORROWER_REVIEW.value,
                actor_type=ActorType.PARTNER_WEBHOOK,
                actor_id=partner_id,
                payload={"event_type": event_type},
            )

        # The webhook row doubles as a durable dispatch outbox. Celery beat
        # republishes pending/failed entries; verification itself is idempotent.
        row.processed_ok = False
        row.processing_error = document_service.DOCUMENT_HASH_DISPATCH_PENDING
        await db.flush()
    return PartnerWebhookOutcome(event=row)


async def _resolve_closing(
    db: AsyncSession,
    body: dict[str, Any],
    *,
    for_update: bool = False,
) -> ClosingCase | None:
    """Resolve and optionally lock a closing while a package Result is registered."""

    cid = body.get("closing_id")
    if cid:
        try:
            uid = uuid.UUID(str(cid))
        except ValueError:
            return None

        statement = select(ClosingCase).where(ClosingCase.id == uid)
        if for_update:
            statement = statement.with_for_update()
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    ext = body.get("external_ref")
    if not ext:
        return None

    statement = select(ClosingCase).where(ClosingCase.external_ref == ext)
    if for_update:
        statement = statement.with_for_update()
    q = await db.execute(statement)
    return q.scalar_one_or_none()
