from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.partner import PartnerWebhookIn
from app.services import document_service, partner_webhook_service

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


@router.post("/partner")
async def partner_webhook(
    body: PartnerWebhookIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Accept unsigned partner callbacks.

    INTENTIONALLY INSECURE: no signature verification; replays are stored as new rows.
    """
    headers_snapshot = {k: v for k, v in request.headers.items()}
    envelope = body.model_dump()

    outcome = await partner_webhook_service.ingest_partner_event(
        db,
        partner_id=body.partner_id,
        event_type=body.event_type,
        body=envelope,
        headers_snapshot=headers_snapshot,
    )
    await db.commit()
    row = outcome.event

    verification_queued = False
    if outcome.document_verification_event_id is not None:
        from app.tasks.jobs import verify_document_package_task

        try:
            verify_document_package_task.delay(
                str(outcome.document_verification_event_id)
            )
        except Exception as exc:  # Celery transports expose multiple exception types.
            logger.exception(
                "document verification dispatch failed event=%s",
                outcome.document_verification_event_id,
            )
            row.processed_ok = False
            row.processing_error = document_service.DOCUMENT_HASH_DISPATCH_FAILED
            db.add(row)
            await db.commit()
            raise HTTPException(
                status_code=503,
                detail="Document verification could not be queued",
            ) from exc
        row.processed_ok = True
        row.processing_error = None
        db.add(row)
        await db.commit()
        verification_queued = True

    # Queue downstream funding evaluation when relevant LOS signals arrive.
    if body.closing_id and not verification_queued:
        try:
            uuid.UUID(str(body.closing_id))
        except ValueError:
            pass
        else:
            from app.tasks.jobs import evaluate_funding_task

            evaluate_funding_task.delay(str(body.closing_id))

    return {
        "received": True,
        "stored_event_id": str(row.id),
        "processed_ok": row.processed_ok,
        "error": row.processing_error,
        "document_verification_queued": verification_queued,
    }
