import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ClosingCreate(BaseModel):
    lender_org_id: str = Field(..., min_length=1, max_length=64)
    title_company_id: str | None = Field(default=None, max_length=64)
    external_ref: str | None = Field(default=None, max_length=128)
    borrower_display_name: str | None = None
    borrower_contact: dict[str, Any] | None = None
    los_callback_url: str | None = Field(
        default=None,
        description="CRM/LOS callback — fetched server-side (SSRF risk if untrusted).",
    )


class ClosingStatusResponse(BaseModel):
    id: uuid.UUID
    state: str
    external_ref: str | None
    lender_org_id: str
    scheduled_signing_at: datetime | None
    los_callback_url: str | None
    documents: list[dict[str, Any]]
    notary_assignments: list[dict[str, Any]]
    funding: dict[str, Any] | None
    recent_events: list[dict[str, Any]]

    model_config = {"from_attributes": False}


class AssignNotaryRequest(BaseModel):
    notary_id: str = Field(..., min_length=1, max_length=64)
    signing_agency_id: str | None = Field(default=None, max_length=64)
    trigger_los_sync: bool = Field(
        default=True,
        description="If true, enqueues a worker job that GETs los_callback_url.",
    )


class ReminderRequest(BaseModel):
    channel: Literal["email", "sms"] = "email"
    template: str = Field(default="closing_reminder_v1")
    schedule_in_seconds: int = Field(default=0, ge=0, le=86400)
