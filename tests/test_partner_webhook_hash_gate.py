from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.models import (
    ClosingCase,
    ClosingDocument,
    DocumentVerificationStatus,
    WorkflowState,
)
from app.routers.webhooks import partner_webhook
from app.schemas.partner import PartnerWebhookIn
from app.services.partner_webhook_service import (
    PartnerWebhookOutcome,
    ingest_partner_event,
)
from app.tasks.jobs import (
    reconcile_document_hash_dispatches_task,
    verify_document_package_task,
)


class _AsyncSession:
    def __init__(self, order: list[str] | None = None) -> None:
        self.added: list[object] = []
        self.order = order if order is not None else []

    def add(self, value: object) -> None:
        self.added.append(value)

    def add_all(self, values: list[object]) -> None:
        self.added.extend(values)

    async def flush(self) -> None:
        for value in self.added:
            if hasattr(value, "id") and getattr(value, "id") is None:
                setattr(value, "id", uuid.uuid4())

    async def commit(self) -> None:
        self.order.append("commit")

    async def execute(self, _statement: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: None)


class _SyncResult:
    def __init__(self, *, rows=None, scalar=None) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.scalar


class _SyncSession:
    def __init__(self, event) -> None:
        self.event = event
        self.execute_count = 0
        self.committed = False
        self.closed = False

    def execute(self, _statement):
        self.execute_count += 1
        if self.execute_count == 1:
            return _SyncResult(rows=[self.event])
        return _SyncResult(scalar=uuid.uuid4())

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class PartnerWebhookHashGateTests(unittest.IsolatedAsyncioTestCase):
    def test_reconciler_republishes_durable_pending_dispatch(self) -> None:
        event = SimpleNamespace(
            id=uuid.uuid4(),
            processed_ok=False,
            processing_error="document_hash_dispatch_pending",
        )
        session = _SyncSession(event)

        with (
            patch("app.tasks.jobs.sync_session", return_value=session),
            patch.object(verify_document_package_task, "delay") as delay,
        ):
            result = reconcile_document_hash_dispatches_task.run()

        delay.assert_called_once_with(str(event.id))
        self.assertEqual(result["dispatched"], 1)
        self.assertTrue(event.processed_ok)
        self.assertIsNone(event.processing_error)
        self.assertTrue(session.committed)
        self.assertTrue(session.closed)

    async def test_documents_packaged_registers_pending_without_advancing(self) -> None:
        db = _AsyncSession()
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )
        body = {
            "closing_id": str(closing.id),
            "payload": {
                "documents": [
                    {"filename": "package.pdf", "expected_sha256": "a" * 64},
                    {"filename": "survey.tiff", "expected_sha256": "b" * 64},
                ]
            },
        }

        resolve_closing = AsyncMock(return_value=closing)
        with patch(
            "app.services.partner_webhook_service._resolve_closing",
            new=resolve_closing,
        ):
            outcome = await ingest_partner_event(
                db,
                partner_id="synthetic-partner",
                event_type="documents_packaged",
                body=body,
                headers_snapshot={},
            )

        resolve_closing.assert_awaited_once_with(db, body, for_update=True)

        documents = [item for item in db.added if isinstance(item, ClosingDocument)]
        self.assertFalse(outcome.event.processed_ok)
        self.assertEqual(
            outcome.event.processing_error,
            "document_hash_dispatch_pending",
        )
        self.assertEqual(outcome.document_verification_event_id, outcome.event.id)
        self.assertEqual(closing.state, WorkflowState.DRAFT.value)
        self.assertEqual(len(documents), 2)
        self.assertTrue(
            all(
                item.status == DocumentVerificationStatus.PENDING.value
                for item in documents
            )
        )

    async def test_empty_manifest_stays_draft_and_is_not_dispatched(self) -> None:
        db = _AsyncSession()
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )

        with patch(
            "app.services.partner_webhook_service._resolve_closing",
            new=AsyncMock(return_value=closing),
        ):
            outcome = await ingest_partner_event(
                db,
                partner_id="synthetic-partner",
                event_type="documents_packaged",
                body={"payload": {"documents": []}},
                headers_snapshot={},
            )

        self.assertFalse(outcome.event.processed_ok)
        self.assertEqual(outcome.event.processing_error, "document_manifest_required")
        self.assertIsNone(outcome.document_verification_event_id)
        self.assertEqual(closing.state, WorkflowState.DRAFT.value)

    async def test_documents_event_cannot_request_later_target_state(self) -> None:
        db = _AsyncSession()
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )

        with patch(
            "app.services.partner_webhook_service._resolve_closing",
            new=AsyncMock(return_value=closing),
        ):
            outcome = await ingest_partner_event(
                db,
                partner_id="synthetic-partner",
                event_type="documents_packaged",
                body={
                    "target_state": WorkflowState.SIGNING_SCHEDULED.value,
                    "payload": {
                        "documents": [
                            {
                                "filename": "package.pdf",
                                "expected_sha256": "a" * 64,
                            }
                        ]
                    },
                },
                headers_snapshot={},
            )

        self.assertEqual(
            outcome.event.processing_error,
            "document_hash_target_state_not_allowed",
        )
        self.assertIsNone(outcome.document_verification_event_id)
        self.assertEqual(closing.state, WorkflowState.DRAFT.value)

    async def test_generic_target_state_cannot_bypass_hash_gate(self) -> None:
        db = _AsyncSession()
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )

        with patch(
            "app.services.partner_webhook_service._resolve_closing",
            new=AsyncMock(return_value=closing),
        ):
            outcome = await ingest_partner_event(
                db,
                partner_id="synthetic-partner",
                event_type="status_update",
                body={
                    "target_state": WorkflowState.DOCS_READY.value,
                    "payload": {},
                },
                headers_snapshot={},
            )

        self.assertEqual(
            outcome.event.processing_error,
            "document_hash_verification_required",
        )
        self.assertEqual(closing.state, WorkflowState.DRAFT.value)

    async def test_router_commits_pending_results_before_dispatch(self) -> None:
        order: list[str] = []
        db = _AsyncSession(order)
        event_id = uuid.uuid4()
        event = SimpleNamespace(
            id=event_id,
            processed_ok=True,
            processing_error=None,
        )
        outcome = PartnerWebhookOutcome(
            event=event,  # type: ignore[arg-type]
            document_verification_event_id=event_id,
        )
        body = PartnerWebhookIn(
            partner_id="synthetic-partner",
            event_type="documents_packaged",
            closing_id=str(uuid.uuid4()),
            payload={
                "documents": [
                    {"filename": "package.pdf", "expected_sha256": "a" * 64}
                ]
            },
        )

        with (
            patch(
                "app.routers.webhooks.partner_webhook_service.ingest_partner_event",
                new=AsyncMock(return_value=outcome),
            ),
            patch.object(
                verify_document_package_task,
                "delay",
                side_effect=lambda *_args: order.append("delay"),
            ),
        ):
            response = await partner_webhook(
                body,
                SimpleNamespace(headers={}),  # type: ignore[arg-type]
                db,  # type: ignore[arg-type]
            )

        self.assertEqual(order, ["commit", "delay", "commit"])
        self.assertTrue(response["document_verification_queued"])

    async def test_dispatch_failure_is_persisted_and_fails_closed(self) -> None:
        order: list[str] = []
        db = _AsyncSession(order)
        event_id = uuid.uuid4()
        event = SimpleNamespace(
            id=event_id,
            processed_ok=True,
            processing_error=None,
        )
        outcome = PartnerWebhookOutcome(
            event=event,  # type: ignore[arg-type]
            document_verification_event_id=event_id,
        )
        body = PartnerWebhookIn(
            partner_id="synthetic-partner",
            event_type="documents_packaged",
            payload={
                "documents": [
                    {"filename": "package.pdf", "expected_sha256": "a" * 64}
                ]
            },
        )

        with (
            patch(
                "app.routers.webhooks.partner_webhook_service.ingest_partner_event",
                new=AsyncMock(return_value=outcome),
            ),
            patch.object(
                verify_document_package_task,
                "delay",
                side_effect=RuntimeError("synthetic broker failure"),
            ),
            patch("app.routers.webhooks.logger.exception"),
        ):
            with self.assertRaises(HTTPException) as raised:
                await partner_webhook(
                    body,
                    SimpleNamespace(headers={}),  # type: ignore[arg-type]
                    db,  # type: ignore[arg-type]
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(order, ["commit", "commit"])
        self.assertFalse(event.processed_ok)
        self.assertEqual(event.processing_error, "document_hash_dispatch_failed")


if __name__ == "__main__":
    unittest.main()
