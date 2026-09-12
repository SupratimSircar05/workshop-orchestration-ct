from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    ClosingCase,
    ClosingDocument,
    FundingChecklist,
    NotaryAssignment,
    WorkflowEvent,
)
from app.schemas.closing import (
    AssignNotaryRequest,
    ClosingCreate,
    ClosingStatusResponse,
    ReminderRequest,
)
from app.services import document_service, notary_service, reminder_service

router = APIRouter(prefix="/closings", tags=["closings"])


@router.post("/create", status_code=201)
async def create_closing(body: ClosingCreate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = ClosingCase(
        lender_org_id=body.lender_org_id,
        title_company_id=body.title_company_id,
        external_ref=body.external_ref,
        borrower_display_name=body.borrower_display_name,
        borrower_contact=body.borrower_contact,
        los_callback_url=body.los_callback_url,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": str(row.id), "state": row.state}


@router.get("/{closing_id}/status", response_model=ClosingStatusResponse)
async def closing_status(closing_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> ClosingStatusResponse:
    result = await db.execute(select(ClosingCase).where(ClosingCase.id == closing_id))
    closing = result.scalar_one_or_none()
    if closing is None:
        raise HTTPException(status_code=404, detail="Closing not found")

    na_rows = (
        await db.execute(select(NotaryAssignment).where(NotaryAssignment.closing_id == closing.id))
    ).scalars().all()

    fc_row = (
        await db.execute(select(FundingChecklist).where(FundingChecklist.closing_id == closing.id))
    ).scalar_one_or_none()

    document_rows = (
        await db.execute(
            select(ClosingDocument)
            .where(ClosingDocument.closing_id == closing.id)
            .order_by(ClosingDocument.created_at.desc())
        )
    ).scalars().all()

    events = (
        await db.execute(
            select(WorkflowEvent)
            .where(WorkflowEvent.closing_id == closing.id)
            .order_by(WorkflowEvent.created_at.desc())
            .limit(15)
        )
    ).scalars().all()

    return ClosingStatusResponse(
        id=closing.id,
        state=closing.state,
        external_ref=closing.external_ref,
        lender_org_id=closing.lender_org_id,
        scheduled_signing_at=closing.scheduled_signing_at,
        los_callback_url=closing.los_callback_url,
        documents=[
            document_service.serialize_document_result(row) for row in document_rows
        ],
        notary_assignments=[
            {
                "id": str(r.id),
                "notary_id": r.notary_id,
                "status": r.status,
                "assigned_at": r.assigned_at.isoformat() if r.assigned_at else None,
            }
            for r in na_rows
        ],
        funding=(
            {
                "all_cleared": fc_row.all_cleared,
                "items": fc_row.items,
                "last_evaluated_at": fc_row.last_evaluated_at.isoformat()
                if fc_row.last_evaluated_at
                else None,
            }
            if fc_row
            else None
        ),
        recent_events=[
            {
                "to_state": e.to_state,
                "from_state": e.from_state,
                "actor_type": e.actor_type,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ],
    )


@router.post("/{closing_id}/assign-notary")
async def assign_notary(
    closing_id: uuid.UUID,
    body: AssignNotaryRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        na = await notary_service.assign_notary(
            db,
            closing_id,
            body.notary_id,
            body.signing_agency_id,
            actor_id=body.notary_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Closing not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    await db.commit()
    await db.refresh(na)

    if body.trigger_los_sync:
        result = await db.execute(select(ClosingCase).where(ClosingCase.id == closing_id))
        closing = result.scalar_one_or_none()
        if closing and closing.los_callback_url:
            from app.tasks.jobs import sync_los_callback_task

            sync_los_callback_task.delay(str(closing.id), closing.los_callback_url)

    return {"assignment_id": str(na.id), "status": na.status}


@router.post("/{closing_id}/reminders")
async def schedule_reminders(
    closing_id: uuid.UUID,
    body: ReminderRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await reminder_service.schedule_reminders(
            db,
            closing_id,
            channel=body.channel,
            template=body.template,
            schedule_in_seconds=body.schedule_in_seconds,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Closing not found") from None
