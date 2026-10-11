"""Database tables. Times are timezone-aware UTC."""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


# Contact stages, in pipeline order.
STAGES = ["new", "ready", "contacted", "replied", "closed"]
STAGE_LABELS = {
    "new": "New",
    "ready": "Email found",
    "contacted": "Contacted",
    "replied": "Replied",
    "closed": "Closed",
}

# Email statuses returned by the verifier.
EMAIL_STATUSES = ["valid", "accept_all", "unknown", "invalid", "unchecked"]

ROLES = ["hiring_manager", "recruiter", "team_member", "executive", "other"]


class Company(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    domain: str | None = Field(default=None, index=True)
    mx_ok: bool | None = None
    website: str | None = None
    linkedin_url: str | None = None
    size: str | None = None
    funding_stage: str | None = None
    open_roles: str | None = None
    source: str | None = None
    notes: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Contact(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    company_id: int | None = Field(default=None, foreign_key="company.id", index=True)
    full_name: str
    first_name: str = ""
    last_name: str = ""
    headline: str | None = None
    title: str | None = None
    linkedin_url: str | None = Field(default=None, index=True)
    location: str | None = None
    about: str | None = None
    experience: str | None = None  # JSON list of {title, company, dates, current, company_url}
    role: str | None = None
    stage: str = "new"
    email: str | None = None
    email_status: str | None = None
    do_not_contact: bool = False
    source: str | None = None
    notes: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class EmailCandidate(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)
    address: str
    pattern: str
    rank: int
    score: float
    status: str = "unchecked"
    detail: str | None = None
    checked_at: datetime | None = None


class KnownEmail(SQLModel, table=True):
    """A real address seen at a domain, used as evidence for the naming pattern."""

    id: int | None = Field(default=None, primary_key=True)
    domain: str = Field(index=True)
    full_name: str
    address: str
    source: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Outreach(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)
    step: int  # 1 = first email, 2 and 3 = follow-ups
    to_address: str
    subject: str
    body: str
    draft_source: str = "template"  # "ollama" or "template"
    gmail_draft_id: str | None = None
    gmail_thread_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None
    outcome: str | None = None  # "replied" or "bounced"


class Capture(SQLModel, table=True):
    """One LinkedIn capture, kept to enforce the daily cap."""

    id: int | None = Field(default=None, primary_key=True)
    url: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
