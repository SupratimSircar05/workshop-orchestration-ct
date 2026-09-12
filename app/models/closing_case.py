import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import WorkflowState

if TYPE_CHECKING:
    from app.models.closing_document import ClosingDocument
    from app.models.funding_checklist import FundingChecklist
    from app.models.notary_assignment import NotaryAssignment
    from app.models.workflow_event import WorkflowEvent


class ClosingCase(Base):
    __tablename__ = "closing_cases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    lender_org_id: Mapped[str] = mapped_column(String(64), index=True)
    title_company_id: Mapped[str | None] = mapped_column(String(64), index=True)

    borrower_display_name: Mapped[str | None] = mapped_column(String(256))
    borrower_contact: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    state: Mapped[str] = mapped_column(
        String(32), default=WorkflowState.DRAFT.value, index=True
    )

    scheduled_signing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # INTENTIONALLY DANGEROUS: stored URL fetched server-side without SSRF controls.
    los_callback_url: Mapped[str | None] = mapped_column(Text)

    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    workflow_events: Mapped[list["WorkflowEvent"]] = relationship(
        back_populates="closing",
        cascade="all, delete-orphan",
    )
    notary_assignments: Mapped[list["NotaryAssignment"]] = relationship(
        back_populates="closing",
        cascade="all, delete-orphan",
    )
    funding_checklists: Mapped[list["FundingChecklist"]] = relationship(
        back_populates="closing",
        cascade="all, delete-orphan",
    )
    documents: Mapped[list["ClosingDocument"]] = relationship(
        back_populates="closing",
        cascade="all, delete-orphan",
    )
