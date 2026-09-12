from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from app.models import (
    ActorType,
    ClosingCase,
    ClosingDocument,
    DocumentVerificationStatus,
    WorkflowEvent,
    WorkflowState,
)
from app.services.document_hash_service import DocumentHashError, DocumentHashResult
from app.tasks.jobs import _apply_transition_sync, verify_document_package_task


class _Result:
    def __init__(self, *, scalar: object = None, values: list[object] | None = None) -> None:
        self._scalar = scalar
        self._values = values or []

    def scalar_one_or_none(self) -> object:
        return self._scalar

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[object]:
        return self._values


class _SyncSession:
    def __init__(self, results: list[_Result]) -> None:
        self._results = iter(results)
        self.added: list[object] = []
        self.commits = 0
        self.flushes = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, _statement: object) -> _Result:
        return next(self._results)

    def add(self, value: object) -> None:
        self.added.append(value)

    def commit(self) -> None:
        self.commits += 1

    def flush(self) -> None:
        self.flushes += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _document(
    *,
    closing_id: uuid.UUID,
    event_id: uuid.UUID,
    filename: str = "package.pdf",
) -> ClosingDocument:
    return ClosingDocument(
        id=uuid.uuid4(),
        closing_id=closing_id,
        source_event_id=event_id,
        filename=filename,
        expected_sha256="a" * 64,
        status=DocumentVerificationStatus.PENDING.value,
        created_at=datetime.now(timezone.utc),
    )


class DocumentPackageTaskTests(unittest.TestCase):
    def test_sync_funding_transition_cannot_bypass_results(self) -> None:
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.SIGNED.value,
        )
        session = _SyncSession([_Result(scalar=None)])

        with self.assertRaisesRegex(
            ValueError, "document_hash_verification_required"
        ):
            _apply_transition_sync(
                session,
                closing,
                WorkflowState.FUNDING_READY.value,
                ActorType.SYSTEM,
                "test",
                {},
            )

        self.assertEqual(closing.state, WorkflowState.SIGNED.value)

    def test_all_verified_results_advance_exactly_once(self) -> None:
        closing_id = uuid.uuid4()
        event_id = uuid.uuid4()
        rows = [
            _document(closing_id=closing_id, event_id=event_id),
            _document(
                closing_id=closing_id,
                event_id=event_id,
                filename="survey.tiff",
            ),
        ]
        closing = ClosingCase(
            id=closing_id,
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )
        session = _SyncSession(
            [
                _Result(values=rows),
                _Result(scalar=closing),
                _Result(scalar=event_id),
                _Result(scalar=event_id),
                _Result(
                    values=[
                        DocumentVerificationStatus.VERIFIED.value,
                        DocumentVerificationStatus.VERIFIED.value,
                    ]
                ),
            ]
        )
        verified_result = DocumentHashResult(
            filename="synthetic-document",
            expected_sha256="a" * 64,
            actual_sha256="a" * 64,
            verified=True,
        )

        with (
            patch("app.tasks.jobs.sync_session", return_value=session),
            patch(
                "app.tasks.jobs.verify_document_bytes_sync",
                return_value=verified_result,
            ),
        ):
            result = verify_document_package_task.run(str(event_id))

        self.assertTrue(result["ok"])
        self.assertTrue(result["moved"])
        self.assertEqual(closing.state, WorkflowState.DOCS_READY.value)
        self.assertEqual(session.commits, 1)
        self.assertEqual(
            len([item for item in session.added if isinstance(item, WorkflowEvent)]),
            1,
        )
        self.assertTrue(
            all(row.status == DocumentVerificationStatus.VERIFIED.value for row in rows)
        )

    def test_mismatch_persists_actual_result_and_stays_draft(self) -> None:
        closing_id = uuid.uuid4()
        event_id = uuid.uuid4()
        row = _document(closing_id=closing_id, event_id=event_id)
        closing = ClosingCase(
            id=closing_id,
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )
        session = _SyncSession(
            [
                _Result(values=[row]),
                _Result(scalar=closing),
                _Result(scalar=event_id),
            ]
        )
        mismatch = DocumentHashResult(
            filename="package.pdf",
            expected_sha256="a" * 64,
            actual_sha256="b" * 64,
            verified=False,
        )

        with (
            patch("app.tasks.jobs.sync_session", return_value=session),
            patch("app.tasks.jobs.verify_document_bytes_sync", return_value=mismatch),
        ):
            result = verify_document_package_task.run(str(event_id))

        self.assertFalse(result["ok"])
        self.assertFalse(result["moved"])
        self.assertEqual(closing.state, WorkflowState.DRAFT.value)
        self.assertEqual(row.actual_sha256, "b" * 64)
        self.assertEqual(row.status, DocumentVerificationStatus.MISMATCH.value)
        self.assertEqual(row.error, "hash_mismatch")

    def test_hash_error_persists_error_and_stays_draft(self) -> None:
        closing_id = uuid.uuid4()
        event_id = uuid.uuid4()
        row = _document(closing_id=closing_id, event_id=event_id)
        closing = ClosingCase(
            id=closing_id,
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )
        session = _SyncSession(
            [
                _Result(values=[row]),
                _Result(scalar=closing),
                _Result(scalar=event_id),
            ]
        )

        with (
            patch("app.tasks.jobs.sync_session", return_value=session),
            patch(
                "app.tasks.jobs.verify_document_bytes_sync",
                side_effect=DocumentHashError("document_not_found"),
            ),
        ):
            result = verify_document_package_task.run(str(event_id))

        self.assertFalse(result["ok"])
        self.assertEqual(closing.state, WorkflowState.DRAFT.value)
        self.assertEqual(row.status, DocumentVerificationStatus.ERROR.value)
        self.assertEqual(row.error, "document_not_found")
        self.assertIsNone(row.actual_sha256)

    def test_stale_verified_package_does_not_advance(self) -> None:
        closing_id = uuid.uuid4()
        event_id = uuid.uuid4()
        row = _document(closing_id=closing_id, event_id=event_id)
        closing = ClosingCase(
            id=closing_id,
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )
        session = _SyncSession(
            [
                _Result(values=[row]),
                _Result(scalar=closing),
                _Result(scalar=uuid.uuid4()),
            ]
        )
        verified_result = DocumentHashResult(
            filename="package.pdf",
            expected_sha256="a" * 64,
            actual_sha256="a" * 64,
            verified=True,
        )

        with (
            patch("app.tasks.jobs.sync_session", return_value=session),
            patch(
                "app.tasks.jobs.verify_document_bytes_sync",
                return_value=verified_result,
            ),
        ):
            result = verify_document_package_task.run(str(event_id))

        self.assertTrue(result["ok"])
        self.assertTrue(result["stale"])
        self.assertFalse(result["moved"])
        self.assertEqual(closing.state, WorkflowState.DRAFT.value)
        self.assertFalse(
            any(isinstance(item, ClosingDocument) for item in session.added)
        )


if __name__ == "__main__":
    unittest.main()
