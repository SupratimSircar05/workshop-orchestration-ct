from enum import Enum


class WorkflowState(str, Enum):
    DRAFT = "draft"
    DOCS_READY = "docs_ready"
    BORROWER_REVIEW = "borrower_review"
    SIGNING_SCHEDULED = "signing_scheduled"
    SIGNED = "signed"
    FUNDING_READY = "funding_ready"
    CLOSED = "closed"


class NotaryAssignmentStatus(str, Enum):
    INVITED = "invited"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    COMPLETED = "completed"


class ActorType(str, Enum):
    LENDER = "lender"
    TITLE = "title"
    BORROWER = "borrower"
    NOTARY = "notary"
    SYSTEM = "system"
    PARTNER_WEBHOOK = "partner_webhook"


class DocumentVerificationStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    ERROR = "error"
