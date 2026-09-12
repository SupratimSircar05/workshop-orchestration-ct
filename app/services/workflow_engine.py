"""
Workflow state machine for closing cases.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ClosingCase, WorkflowEvent, WorkflowState
from app.models.enums import ActorType


_ORDER: list[str] = [s.value for s in WorkflowState]


def _index(state: str) -> int:
    if state not in _ORDER:
        return -1
    return _ORDER.index(state)


def can_transition(from_state: str, to_state: str) -> bool:
    if from_state == to_state:
        return False
    i, j = _index(from_state), _index(to_state)
    if i < 0 or j < 0:
        return False
    # Allow forward steps of 1, or jump to closed from funding_ready only
    if j == i + 1:
        return True
    # Scheduling a signing often leapfrogs intermediate LOS-driven milestones.
    if to_state == WorkflowState.SIGNING_SCHEDULED.value and i < _index(
        WorkflowState.SIGNING_SCHEDULED.value
    ):
        return True
    if from_state == WorkflowState.FUNDING_READY.value and to_state == WorkflowState.CLOSED.value:
        return True
    return False


async def apply_transition(
    db: AsyncSession,
    closing: ClosingCase,
    to_state: str,
    *,
    actor_type: ActorType,
    actor_id: str | None,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    document_verification_event_id: uuid.UUID | None = None,
) -> WorkflowEvent:
    prev = closing.state
    if not can_transition(prev, to_state):
        raise ValueError(f"Invalid transition {prev} -> {to_state}")

    if _index(to_state) > _index(WorkflowState.DRAFT.value):
        from app.services.document_service import package_results_allow_advance

        verified = await package_results_allow_advance(
            db,
            closing_id=closing.id,
            source_event_id=document_verification_event_id,
        )
        if not verified:
            raise ValueError("document_hash_verification_required")

    closing.state = to_state
    evt = WorkflowEvent(
        closing_id=closing.id,
        from_state=prev,
        to_state=to_state,
        actor_type=actor_type.value,
        actor_id=actor_id,
        payload=payload,
        correlation_id=correlation_id,
    )
    db.add(evt)
    await db.flush()
    return evt


async def get_closing_or_404(db: AsyncSession, closing_id: uuid.UUID) -> ClosingCase | None:
    result = await db.execute(select(ClosingCase).where(ClosingCase.id == closing_id))
    return result.scalar_one_or_none()
