from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.services.callback_client import fetch_callback_preview
from app.services.document_hash_service import (
    DocumentHashError,
    verify_document_bytes_sync,
)
from app.services.document_service import package_statuses_allow_advance
from app.tasks.celery_app import celery_app
from app.worker_db import sync_session

logger = logging.getLogger(__name__)


@celery_app.task(name="cos.send_reminder")
def send_reminder_task(closing_id: str, channel: str, template: str) -> dict:
    """
    Placeholder delivery — real stack would render templates and hit SNS / SendGrid.
    """
    logger.info(
        "reminder dispatched closing=%s channel=%s template=%s",
        closing_id,
        channel,
        template,
    )
    return {"ok": True, "closing_id": closing_id, "channel": channel, "template": template}


@celery_app.task(name="cos.sync_los_callback")
def sync_los_callback_task(closing_id: str, callback_url: str) -> dict:
    """
    SSRF-prone outbound GET issued from worker network context.
    """
    logger.info("los callback sync closing=%s url=%s", closing_id, callback_url)
    preview = fetch_callback_preview(callback_url)
    return {"closing_id": closing_id, "preview": preview}


@celery_app.task(name="cos.evaluate_funding")
def evaluate_funding_task(closing_id: str) -> dict:
    """
    Sync ORM path for worker-side funding evaluation.
    """
    try:
        cid = uuid.UUID(closing_id)
    except ValueError:
        return {"ok": False, "error": "invalid closing_id"}

    from app.models import ClosingCase, FundingChecklist, WorkflowState
    from app.models.enums import ActorType
    from app.services.workflow_engine import can_transition

    session = sync_session()
    try:
        closing = session.get(ClosingCase, cid)
        if closing is None:
            return {"ok": False, "error": "not_found"}

        chk = session.execute(
            select(FundingChecklist).where(FundingChecklist.closing_id == cid)
        ).scalar_one_or_none()
        if chk is None:
            from app.services.funding_service import DEFAULT_ITEMS

            chk = FundingChecklist(closing_id=cid, items=list(DEFAULT_ITEMS), all_cleared=False)
            session.add(chk)
            session.flush()

        items = list(chk.items or [])
        all_cleared = bool(items) and all(bool(i.get("done")) for i in items)
        chk.all_cleared = all_cleared

        moved = False
        if all_cleared and closing.state == WorkflowState.SIGNED.value:
            if can_transition(closing.state, WorkflowState.FUNDING_READY.value):
                _apply_transition_sync(
                    session,
                    closing,
                    WorkflowState.FUNDING_READY.value,
                    ActorType.SYSTEM,
                    "funding_worker",
                    {"checklist_id": str(chk.id)},
                )
                moved = True
        elif all_cleared and closing.state == WorkflowState.FUNDING_READY.value:
            if can_transition(closing.state, WorkflowState.CLOSED.value):
                _apply_transition_sync(
                    session,
                    closing,
                    WorkflowState.CLOSED.value,
                    ActorType.SYSTEM,
                    "funding_worker",
                    {"checklist_id": str(chk.id)},
                )
                moved = True

        session.commit()
        return {"ok": True, "closing_id": closing_id, "all_cleared": all_cleared, "moved": moved}
    except Exception as exc:  # noqa: BLE001
        logger.exception("funding evaluation failed")
        session.rollback()
        return {"ok": False, "error": str(exc)}
    finally:
        session.close()


@celery_app.task(name="cos.verify_document_package")
def verify_document_package_task(source_event_id: str) -> dict:
    """Verify one persisted manifest and advance only when every Result matches."""

    try:
        event_id = uuid.UUID(source_event_id)
    except ValueError:
        return {"ok": False, "error": "invalid_source_event_id"}

    from app.models import (
        ClosingCase,
        ClosingDocument,
        DocumentVerificationStatus,
        WorkflowState,
    )
    from app.models.enums import ActorType
    from app.services.workflow_engine import can_transition

    session = sync_session()
    try:
        rows = (
            session.execute(
                select(ClosingDocument)
                .where(ClosingDocument.source_event_id == event_id)
                .order_by(ClosingDocument.filename)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if not rows:
            return {"ok": False, "error": "document_manifest_not_found"}

        closing_id = rows[0].closing_id
        if any(row.closing_id != closing_id for row in rows):
            return {"ok": False, "error": "document_manifest_inconsistent"}

        closing = session.execute(
            select(ClosingCase)
            .where(ClosingCase.id == closing_id)
            .with_for_update()
        ).scalar_one_or_none()
        if closing is None:
            return {"ok": False, "error": "closing_not_found"}

        checked_at = datetime.now(timezone.utc)
        for row in rows:
            row.actual_sha256 = None
            row.error = None
            row.checked_at = checked_at
            try:
                result = verify_document_bytes_sync(
                    row.filename,
                    row.expected_sha256,
                )
            except DocumentHashError as exc:
                row.status = DocumentVerificationStatus.ERROR.value
                row.error = str(exc)
                continue

            row.actual_sha256 = result.actual_sha256
            if result.verified:
                row.status = DocumentVerificationStatus.VERIFIED.value
            else:
                row.status = DocumentVerificationStatus.MISMATCH.value
                row.error = "hash_mismatch"

        session.flush()
        # Package registration locks the same closing row. Re-read "latest" only
        # after hashing so a superseding manifest can never be released by this job.
        latest_event_id = session.execute(
            select(ClosingDocument.source_event_id)
            .where(ClosingDocument.closing_id == closing_id)
            .order_by(ClosingDocument.created_at.desc(), ClosingDocument.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        all_verified = package_statuses_allow_advance(row.status for row in rows)
        stale = latest_event_id != event_id
        moved = False
        if (
            all_verified
            and not stale
            and closing.state == WorkflowState.DRAFT.value
            and can_transition(closing.state, WorkflowState.DOCS_READY.value)
        ):
            _apply_transition_sync(
                session,
                closing,
                WorkflowState.DOCS_READY.value,
                ActorType.SYSTEM,
                "document_hash_worker",
                {"document_verification_event_id": source_event_id},
            )
            moved = True

        session.commit()
        return {
            "ok": all_verified,
            "closing_id": str(closing_id),
            "source_event_id": source_event_id,
            "verified": sum(
                row.status == DocumentVerificationStatus.VERIFIED.value for row in rows
            ),
            "total": len(rows),
            "moved": moved,
            "stale": stale,
        }
    except Exception as exc:  # noqa: BLE001 - task boundary must roll back safely.
        logger.exception("document package verification failed event=%s", source_event_id)
        session.rollback()
        return {"ok": False, "error": "document_verification_failed"}
    finally:
        session.close()


@celery_app.task(name="cos.reconcile_document_hash_dispatches")
def reconcile_document_hash_dispatches_task(limit: int = 100) -> dict:
    """Republish committed package Results left pending by an API interruption."""

    from app.models import ClosingDocument, PartnerWebhookEvent
    from app.services.document_service import (
        DOCUMENT_HASH_DISPATCH_FAILED,
        DOCUMENT_HASH_DISPATCH_PENDING,
    )

    batch_limit = max(1, min(limit, 500))
    session = sync_session()
    dispatched = 0
    failed = 0
    try:
        events = (
            session.execute(
                select(PartnerWebhookEvent)
                .where(
                    PartnerWebhookEvent.event_type == "documents_packaged",
                    PartnerWebhookEvent.processed_ok.is_(False),
                    PartnerWebhookEvent.processing_error.in_(
                        (
                            DOCUMENT_HASH_DISPATCH_PENDING,
                            DOCUMENT_HASH_DISPATCH_FAILED,
                        )
                    ),
                )
                .order_by(PartnerWebhookEvent.received_at)
                .limit(batch_limit)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for event in events:
            result_id = session.execute(
                select(ClosingDocument.id)
                .where(ClosingDocument.source_event_id == event.id)
                .limit(1)
            ).scalar_one_or_none()
            if result_id is None:
                event.processing_error = "document_manifest_not_found"
                failed += 1
                continue

            try:
                verify_document_package_task.delay(str(event.id))
            except Exception:  # noqa: BLE001 - leave durable marker for next pass.
                event.processing_error = DOCUMENT_HASH_DISPATCH_FAILED
                failed += 1
                continue

            event.processed_ok = True
            event.processing_error = None
            dispatched += 1

        session.commit()
        return {
            "ok": failed == 0,
            "examined": len(events),
            "dispatched": dispatched,
            "failed": failed,
        }
    except Exception:  # noqa: BLE001 - scheduler boundary must fail safely.
        logger.exception("document verification dispatch reconciliation failed")
        session.rollback()
        return {"ok": False, "error": "document_dispatch_reconciliation_failed"}
    finally:
        session.close()


def _apply_transition_sync(session, closing, to_state: str, actor_type, actor_id: str, payload: dict):
    from app.models import WorkflowEvent, WorkflowState
    from app.services.workflow_engine import can_transition

    prev = closing.state
    if not can_transition(prev, to_state):
        raise ValueError(f"Invalid transition {prev} -> {to_state}")
    if (
        to_state != WorkflowState.DRAFT.value
        and not _package_results_allow_advance_sync(session, closing.id)
    ):
        raise ValueError("document_hash_verification_required")

    closing.state = to_state
    session.add(
        WorkflowEvent(
            closing_id=closing.id,
            from_state=prev,
            to_state=to_state,
            actor_type=actor_type.value,
            actor_id=actor_id,
            payload=payload,
        )
    )


def _package_results_allow_advance_sync(session, closing_id: uuid.UUID) -> bool:
    from app.models import ClosingDocument

    latest_event_id = session.execute(
        select(ClosingDocument.source_event_id)
        .where(ClosingDocument.closing_id == closing_id)
        .order_by(ClosingDocument.created_at.desc(), ClosingDocument.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_event_id is None:
        return False

    statuses = (
        session.execute(
            select(ClosingDocument.status).where(
                ClosingDocument.closing_id == closing_id,
                ClosingDocument.source_event_id == latest_event_id,
            )
        )
        .scalars()
        .all()
    )
    return package_statuses_allow_advance(statuses)
