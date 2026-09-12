from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import DocumentVerificationStatus

if TYPE_CHECKING:
    from app.models.closing_case import ClosingCase


class ClosingDocument(Base):
    """Persisted integrity result for one document in a webhook package."""

    __tablename__ = "closing_documents"
    __table_args__ = (
        UniqueConstraint(
            "source_event_id",
            "filename",
            name="uq_closing_documents_source_event_filename",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    closing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("closing_cases.id", ondelete="CASCADE"),
        index=True,
    )
    source_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("partner_webhook_events.id", ondelete="CASCADE"),
        index=True,
    )

    filename: Mapped[str] = mapped_column(String(256))
    expected_sha256: Mapped[str] = mapped_column(String(64))
    actual_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32),
        default=DocumentVerificationStatus.PENDING.value,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    closing: Mapped["ClosingCase"] = relationship(back_populates="documents")
