from __future__ import annotations

import hashlib
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from app.models import ActorType, ClosingCase, DocumentVerificationStatus, WorkflowState
from app.services.document_hash_service import DocumentHashError
from app.services.document_service import (
    package_statuses_allow_advance,
    parse_document_manifest,
    register_document_manifest,
)
from app.services.workflow_engine import apply_transition
from app.services.notary_service import assign_notary


class _ScalarResult:
    def __init__(
        self,
        values: list[str] | None = None,
        *,
        scalar: uuid.UUID | None = None,
    ) -> None:
        self._values = values or []
        self._scalar = scalar

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[str]:
        return self._values

    def scalar_one_or_none(self) -> uuid.UUID | None:
        return self._scalar


class _AsyncSession:
    def __init__(
        self,
        statuses: list[str] | None = None,
        *,
        latest_event_id: uuid.UUID | None = None,
    ) -> None:
        self.statuses = statuses or []
        self.latest_event_id = latest_event_id
        self.added: list[object] = []
        self.flush_count = 0
        self.execute_count = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    def add_all(self, values: list[object]) -> None:
        self.added.extend(values)

    async def execute(self, _statement: object) -> _ScalarResult:
        self.execute_count += 1
        if self.execute_count == 1:
            return _ScalarResult(scalar=self.latest_event_id)
        return _ScalarResult(self.statuses)

    async def flush(self) -> None:
        self.flush_count += 1


class DocumentManifestTests(unittest.IsolatedAsyncioTestCase):
    def test_manifest_normalizes_digests(self) -> None:
        expected = hashlib.sha256(b"synthetic package").hexdigest().upper()

        documents = parse_document_manifest(
            {
                "documents": [
                    {"filename": "package.pdf", "expected_sha256": expected},
                    {"filename": "survey.tiff", "expected_sha256": "a" * 64},
                ]
            }
        )

        self.assertEqual(len(documents), 2)
        self.assertEqual(documents[0].expected_sha256, expected.lower())

    def test_manifest_must_be_non_empty(self) -> None:
        for payload in ({}, {"documents": []}, None):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    DocumentHashError, "document_manifest_required"
                ):
                    parse_document_manifest(payload)

    def test_manifest_rejects_duplicate_filenames(self) -> None:
        payload = {
            "documents": [
                {"filename": "package.pdf", "expected_sha256": "a" * 64},
                {"filename": "package.pdf", "expected_sha256": "b" * 64},
            ]
        }

        with self.assertRaisesRegex(DocumentHashError, "duplicate_document"):
            parse_document_manifest(payload)

    async def test_registration_persists_pending_results(self) -> None:
        db = _AsyncSession()
        closing_id = uuid.uuid4()
        event_id = uuid.uuid4()
        documents = parse_document_manifest(
            {
                "documents": [
                    {"filename": "package.pdf", "expected_sha256": "a" * 64},
                    {"filename": "survey.tif", "expected_sha256": "b" * 64},
                ]
            }
        )

        rows = await register_document_manifest(
            db,
            closing_id=closing_id,
            source_event_id=event_id,
            documents=documents,
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(db.flush_count, 1)
        self.assertTrue(
            all(row.status == DocumentVerificationStatus.PENDING.value for row in rows)
        )
        self.assertTrue(all(row.source_event_id == event_id for row in rows))


class DocumentGateTests(unittest.IsolatedAsyncioTestCase):
    def test_only_non_empty_all_verified_results_allow_advance(self) -> None:
        self.assertFalse(package_statuses_allow_advance([]))
        for blocked in (
            DocumentVerificationStatus.PENDING.value,
            DocumentVerificationStatus.MISMATCH.value,
            DocumentVerificationStatus.ERROR.value,
        ):
            with self.subTest(status=blocked):
                self.assertFalse(
                    package_statuses_allow_advance(
                        [DocumentVerificationStatus.VERIFIED.value, blocked]
                    )
                )
        self.assertTrue(
            package_statuses_allow_advance(
                [
                    DocumentVerificationStatus.VERIFIED.value,
                    DocumentVerificationStatus.VERIFIED.value,
                ]
            )
        )

    def test_manifest_rejects_unbounded_package(self) -> None:
        with self.assertRaisesRegex(DocumentHashError, "document_manifest_too_large"):
            parse_document_manifest(
                {
                    "documents": [
                        {
                            "filename": f"document-{index}.pdf",
                            "expected_sha256": "a" * 64,
                        }
                        for index in range(65)
                    ]
                }
            )
    async def test_transition_from_draft_requires_verification_event(self) -> None:
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )
        db = _AsyncSession()

        with self.assertRaisesRegex(
            ValueError, "document_hash_verification_required"
        ):
            await apply_transition(
                db,
                closing,
                WorkflowState.DOCS_READY.value,
                actor_type=ActorType.SYSTEM,
                actor_id="test",
            )

        self.assertEqual(closing.state, WorkflowState.DRAFT.value)

    async def test_notary_assignment_cannot_bypass_hash_gate(self) -> None:
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )
        db = _AsyncSession()

        with patch(
            "app.services.notary_service.get_closing_or_404",
            new=AsyncMock(return_value=closing),
        ):
            with self.assertRaisesRegex(
                ValueError, "document_hash_verification_required"
            ):
                await assign_notary(
                    db,
                    closing.id,
                    "synthetic-notary",
                    None,
                    "synthetic-actor",
                )

        self.assertEqual(closing.state, WorkflowState.DRAFT.value)

    async def test_pending_result_blocks_transition(self) -> None:
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )
        event_id = uuid.uuid4()
        db = _AsyncSession(
            [DocumentVerificationStatus.PENDING.value],
            latest_event_id=event_id,
        )

        with self.assertRaisesRegex(
            ValueError, "document_hash_verification_required"
        ):
            await apply_transition(
                db,
                closing,
                WorkflowState.DOCS_READY.value,
                actor_type=ActorType.SYSTEM,
                actor_id="test",
                document_verification_event_id=event_id,
            )

        self.assertEqual(closing.state, WorkflowState.DRAFT.value)

    async def test_later_transition_still_requires_verified_results(self) -> None:
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.DOCS_READY.value,
        )
        db = _AsyncSession()

        with self.assertRaisesRegex(
            ValueError, "document_hash_verification_required"
        ):
            await apply_transition(
                db,
                closing,
                WorkflowState.BORROWER_REVIEW.value,
                actor_type=ActorType.SYSTEM,
                actor_id="test",
            )

        self.assertEqual(closing.state, WorkflowState.DOCS_READY.value)

    async def test_stale_verification_event_blocks_transition(self) -> None:
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )
        db = _AsyncSession(
            [DocumentVerificationStatus.VERIFIED.value],
            latest_event_id=uuid.uuid4(),
        )

        with self.assertRaisesRegex(
            ValueError, "document_hash_verification_required"
        ):
            await apply_transition(
                db,
                closing,
                WorkflowState.DOCS_READY.value,
                actor_type=ActorType.SYSTEM,
                actor_id="test",
                document_verification_event_id=uuid.uuid4(),
            )

        self.assertEqual(closing.state, WorkflowState.DRAFT.value)

    async def test_verified_package_allows_transition(self) -> None:
        closing = ClosingCase(
            id=uuid.uuid4(),
            lender_org_id="synthetic-lender",
            state=WorkflowState.DRAFT.value,
        )
        event_id = uuid.uuid4()
        db = _AsyncSession(
            [DocumentVerificationStatus.VERIFIED.value],
            latest_event_id=event_id,
        )

        event = await apply_transition(
            db,
            closing,
            WorkflowState.DOCS_READY.value,
            actor_type=ActorType.SYSTEM,
            actor_id="test",
            document_verification_event_id=event_id,
        )

        self.assertEqual(closing.state, WorkflowState.DOCS_READY.value)
        self.assertEqual(event.to_state, WorkflowState.DOCS_READY.value)
        self.assertEqual(db.flush_count, 1)


if __name__ == "__main__":
    unittest.main()
