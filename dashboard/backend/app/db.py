"""SQLite storage (data/dialer.db). Plain SQLModel tables so we can move to Postgres later
by changing DATABASE_URL only."""

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, Column, inspect, text
from sqlmodel import Field, Session, SQLModel, create_engine

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = Path(os.getenv("DIALER_DATA_DIR") or PROJECT_ROOT / "data")  # tests: a temp dir
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'dialer.db').as_posix()}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Call(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    room: str = Field(index=True, unique=True)
    number: str = ""
    # live status: dialing / ringing / answered / ended / failed
    status: str = "dialing"
    # final result for history: answered / no_answer / failed (None while in progress)
    result: str | None = None
    reason: str = ""
    source: str = "dashboard"  # dashboard / cli / simulated
    simulated: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: float | None = None  # talk time (answered -> ended)
    lead_id: int | None = Field(default=None, index=True)  # the lead this call was for, if any
    # What happened, in business terms (outcomes.py): interested / not_now / ... / needs_review
    outcome: str | None = None
    outcome_source: str = ""  # ai (record_outcome tool) / transcript / call (end reason) / manual
    outcome_note: str = ""
    callback_at: datetime | None = None  # UTC; for not_now / gatekeeper
    # Transfer to the supervisor (supervisor.py): "" / unavailable / waiting / accepted / declined / timeout /
    # transferring / transferred / failed
    transfer_status: str = ""
    transfer_summary: str = ""  # what the supervisor saw in the pop-up (written by the AI)
    transfer_note: str = ""  # why it ended this way, in plain words
    transfer_requested_at: datetime | None = None
    transfer_answered_at: datetime | None = None
    vm_message_left: bool = False  # reached voicemail and the AI said our voicemail message
    email_collected: str = ""  # the address the lead confirmed on this call (collect_email tool)
    email_reason: str = ""  # details / interested / not_now / other


class CallEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    call_id: int = Field(foreign_key="call.id", index=True)
    ts: datetime = Field(default_factory=utcnow)
    kind: str  # status / transcript / timing / state / log / agent
    role: str | None = None  # agent / callee / system
    text: str = ""
    data: dict = Field(default_factory=dict, sa_column=Column(JSON))


# Lead statuses (the Leads dashboard). "do_not_contact" always wins while Lead.dnc is set.
# "answered" is from before call outcomes existed; "texted" is for a later phase.
LEAD_STATUSES = (
    "new", "calling", "interested", "callback", "not_interested", "needs_review", "voicemail", "no_answer",
    "bad_line", "bad_number", "do_not_contact", "not_a_fit", "failed", "answered", "transferred", "texted",
)


class Lead(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    phone: str = Field(index=True, unique=True)  # E.164, US
    name: str = ""
    business: str = ""
    city: str = ""
    location: str = ""  # free text from the CSV, may include a state and zip ("Austin, TX 78701")
    service_required: str = ""
    business_description: str = ""
    job_description: str = ""
    email: str = ""  # from the CSV, or confirmed by the lead on a call (collect_email tool)
    fit_override: bool = False  # "Call anyway": ignore the service-fit check for this lead
    callback_at: datetime | None = None  # UTC; status "callback": the dialer calls again from then on
    callback_note: str = ""
    callback_count: int = 0  # callbacks in a row (reset by any other outcome)
    vm_count: int = 0  # calls that reached voicemail
    notes: str = ""
    extra: dict = Field(default_factory=dict, sa_column=Column(JSON))  # unknown CSV columns, as-is
    status: str = Field(default="new", index=True)
    dnc: bool = Field(default=False, index=True)
    dnc_reason: str = ""
    dnc_at: datetime | None = None
    attempts: int = 0
    last_call_at: datetime | None = None
    last_call_id: int | None = None
    source: str = ""  # e.g. the CSV file name, or "manual"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class LeadEvent(SQLModel, table=True):
    """A lead's activity log: imported, status changes, DNC marked/unmarked (texts later)."""

    id: int | None = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="lead.id", index=True)
    ts: datetime = Field(default_factory=utcnow)
    kind: str  # imported / status / dnc / call / note / email / sms
    text: str = ""
    data: dict = Field(default_factory=dict, sa_column=Column(JSON))


class EmailMessage(SQLModel, table=True):
    """A follow-up email (emails.py): sent, failed, or not sent and why. Kept for the lead page and History."""

    id: int | None = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="lead.id", index=True)
    call_id: int | None = Field(default=None, index=True)
    template: str  # interested / callback / details / voicemail_followup
    lead_email: str = ""  # the lead's address
    to_email: str = ""  # where it really went (the test address in test mode)
    test_mode: bool = False
    subject: str = ""
    body: str = ""
    status: str = "not_sent"  # sent / failed / not_sent
    detail: str = ""  # why it failed or wasn't sent
    message_id: str = ""  # our Message-ID (a STOP reply points back to it)
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None


class SmsMessage(SQLModel, table=True):
    """A follow-up text (sms.py): queued, sent, delivered, failed, or not sent and why. Lead page and History."""

    id: int | None = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="lead.id", index=True)
    call_id: int | None = Field(default=None, index=True)
    template: str  # not_answered / not_interested / not_now / interested
    lead_phone: str = ""  # the lead's number
    to_phone: str = ""  # where it really goes (the test number in test mode)
    test_mode: bool = False
    body: str = ""
    status: str = Field(default="not_sent", index=True)  # queued / sent / delivered / failed / not_sent
    detail: str = ""  # why it failed, wasn't sent, or is waiting
    telnyx_id: str = Field(default="", index=True)  # Telnyx message ID (delivery reports point back to it)
    send_after: datetime | None = None  # queued: the next time inside the lead's calling hours (UTC)
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None
    delivered_at: datetime | None = None


# Columns added after the first release: (table, column, SQL type + default)
NEW_COLUMNS = [
    ("lead", "location", "VARCHAR NOT NULL DEFAULT ''"),
    ("lead", "service_required", "VARCHAR NOT NULL DEFAULT ''"),
    ("lead", "business_description", "VARCHAR NOT NULL DEFAULT ''"),
    ("lead", "job_description", "VARCHAR NOT NULL DEFAULT ''"),
    ("lead", "fit_override", "BOOLEAN NOT NULL DEFAULT 0"),
    ("lead", "callback_at", "DATETIME"),
    ("lead", "callback_note", "VARCHAR NOT NULL DEFAULT ''"),
    ("lead", "callback_count", "INTEGER NOT NULL DEFAULT 0"),
    ("lead", "vm_count", "INTEGER NOT NULL DEFAULT 0"),
    ("lead", "email", "VARCHAR NOT NULL DEFAULT ''"),
    ("call", "outcome", "VARCHAR"),
    ("call", "outcome_source", "VARCHAR NOT NULL DEFAULT ''"),
    ("call", "outcome_note", "VARCHAR NOT NULL DEFAULT ''"),
    ("call", "callback_at", "DATETIME"),
    ("call", "transfer_status", "VARCHAR NOT NULL DEFAULT ''"),
    ("call", "transfer_summary", "VARCHAR NOT NULL DEFAULT ''"),
    ("call", "transfer_note", "VARCHAR NOT NULL DEFAULT ''"),
    ("call", "transfer_requested_at", "DATETIME"),
    ("call", "transfer_answered_at", "DATETIME"),
    ("call", "vm_message_left", "BOOLEAN NOT NULL DEFAULT 0"),
    ("call", "email_collected", "VARCHAR NOT NULL DEFAULT ''"),
    ("call", "email_reason", "VARCHAR NOT NULL DEFAULT ''"),
]


def _migrate() -> None:
    """Add columns that newer code expects to tables created by older versions (SQLite)."""
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("call")}
    lead_cols = {c["name"] for c in insp.get_columns("lead")}
    existing = {"call": cols, "lead": lead_cols}
    with engine.begin() as conn:
        if "lead_id" not in cols:
            conn.execute(text("ALTER TABLE call ADD COLUMN lead_id INTEGER"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_call_lead_id ON call (lead_id)"))
        for table, col, ddl in NEW_COLUMNS:
            if col not in existing[table]:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                if (table, col) == ("lead", "location"):  # older leads only had a city
                    conn.execute(text("UPDATE lead SET location = city WHERE location = ''"))
                if (table, col) == ("lead", "vm_count"):  # statuses renamed when call outcomes came in
                    conn.execute(text("UPDATE lead SET status = 'callback' WHERE status IN ('callback_scheduled', 'not_now')"))
                    conn.execute(text("UPDATE lead SET vm_count = 1 WHERE status = 'voicemail'"))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate()


def get_session() -> Session:
    return Session(engine, expire_on_commit=False)


def as_utc(dt: datetime | None) -> datetime | None:
    # SQLite drops tzinfo; everything we store is UTC.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def call_to_dict(c: Call) -> dict:
    return {
        "id": c.id,
        "room": c.room,
        "number": c.number,
        "status": c.status,
        "result": c.result,
        "reason": c.reason,
        "source": c.source,
        "simulated": c.simulated,
        "created_at": as_utc(c.created_at).isoformat() if c.created_at else None,
        "answered_at": as_utc(c.answered_at).isoformat() if c.answered_at else None,
        "ended_at": as_utc(c.ended_at).isoformat() if c.ended_at else None,
        "duration_s": c.duration_s,
        "lead_id": c.lead_id,
        "outcome": c.outcome,
        "outcome_source": c.outcome_source,
        "outcome_note": c.outcome_note,
        "callback_at": as_utc(c.callback_at).isoformat() if c.callback_at else None,
        "transfer_status": c.transfer_status,
        "transfer_summary": c.transfer_summary,
        "transfer_note": c.transfer_note,
        "transfer_requested_at": as_utc(c.transfer_requested_at).isoformat() if c.transfer_requested_at else None,
        "transfer_answered_at": as_utc(c.transfer_answered_at).isoformat() if c.transfer_answered_at else None,
        "vm_message_left": c.vm_message_left,
        "email_collected": c.email_collected,
        "email_reason": c.email_reason,
    }


def lead_to_dict(lead: Lead) -> dict:
    from app.timezones import lead_timezone

    tz, tz_source = lead_timezone(lead.location, lead.phone)
    return {
        "timezone": tz,
        "tz_source": tz_source,
        "id": lead.id,
        "phone": lead.phone,
        "name": lead.name,
        "business": lead.business,
        "city": lead.city,
        "location": lead.location,
        "service_required": lead.service_required,
        "business_description": lead.business_description,
        "job_description": lead.job_description,
        "email": lead.email,
        "fit_override": lead.fit_override,
        "callback_at": as_utc(lead.callback_at).isoformat() if lead.callback_at else None,
        "callback_note": lead.callback_note,
        "callback_count": lead.callback_count,
        "vm_count": lead.vm_count,
        "notes": lead.notes,
        "extra": lead.extra or {},
        "status": lead.status,
        "dnc": lead.dnc,
        "dnc_reason": lead.dnc_reason,
        "dnc_at": as_utc(lead.dnc_at).isoformat() if lead.dnc_at else None,
        "attempts": lead.attempts,
        "last_call_at": as_utc(lead.last_call_at).isoformat() if lead.last_call_at else None,
        "last_call_id": lead.last_call_id,
        "source": lead.source,
        "created_at": as_utc(lead.created_at).isoformat() if lead.created_at else None,
    }


def email_to_dict(e: EmailMessage) -> dict:
    return {
        "id": e.id, "lead_id": e.lead_id, "call_id": e.call_id, "template": e.template, "lead_email": e.lead_email,
        "to_email": e.to_email, "test_mode": e.test_mode, "subject": e.subject, "body": e.body, "status": e.status,
        "detail": e.detail, "created_at": as_utc(e.created_at).isoformat() if e.created_at else None,
        "sent_at": as_utc(e.sent_at).isoformat() if e.sent_at else None,
    }


def sms_to_dict(m: SmsMessage) -> dict:
    iso = lambda d: as_utc(d).isoformat() if d else None  # noqa: E731
    return {
        "id": m.id, "lead_id": m.lead_id, "call_id": m.call_id, "template": m.template, "lead_phone": m.lead_phone,
        "to_phone": m.to_phone, "test_mode": m.test_mode, "body": m.body, "status": m.status, "detail": m.detail,
        "send_after": iso(m.send_after), "created_at": iso(m.created_at), "sent_at": iso(m.sent_at),
        "delivered_at": iso(m.delivered_at),
    }


def lead_event_to_dict(e: LeadEvent) -> dict:
    return {"id": e.id, "lead_id": e.lead_id, "ts": as_utc(e.ts).isoformat(), "kind": e.kind, "text": e.text, "data": e.data or {}}


def event_to_dict(e: CallEvent) -> dict:
    return {
        "id": e.id,
        "call_id": e.call_id,
        "ts": as_utc(e.ts).isoformat(),
        "kind": e.kind,
        "role": e.role,
        "text": e.text,
        "data": e.data or {},
    }
