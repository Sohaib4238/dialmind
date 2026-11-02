"""Follow-up emails after calls (Phase 7). Templates: agent_settings.yaml `emails`; account: .env (SMTP_*).

- The template comes from the call's outcome (choose_template). At most one email per call, and never to a
  Do-Not-Contact lead.
- EMAIL_TEST_MODE (default ON): every email goes to EMAIL_TEST_ADDRESS instead of the lead (like call test mode).
- Email not configured, no business address, daily limit reached, test address missing: saved as "not_sent" with
  the reason. Nothing else breaks.
- Every email is plain text with our footer (company, phone, address, "reply STOP") and a List-Unsubscribe header.
  STOP / unsubscribe replies are picked up by unsubscribe.py and mark the lead Do-Not-Contact.
- EMAIL_SENDING_DISABLED=1 is a hard kill switch (tests, demos), like CARTESIA_CALLS_DISABLED.

Plain SMTP: Gmail (smtp.gmail.com:587, an app password) or a company mail server (465 = SSL, else STARTTLS).
"""

import asyncio
import logging
import os
import re
import smtplib
import ssl
import threading
from datetime import timedelta
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, formatdate, make_msgid
from zoneinfo import ZoneInfo

from sqlmodel import func, select

from app import outcomes, standby
from app.agent_config import lead_variables
from app.bus import bus
from app.db import Call, EmailMessage, Lead, LeadEvent, as_utc, email_to_dict, get_session, utcnow
from app.timezones import lead_timezone
from settings import SettingsError, load_settings  # shared/settings.py

logger = logging.getLogger("dashboard.emails")

TEMPLATES = ("interested", "callback", "details", "voicemail_followup")
TEMPLATE_LABELS = {"interested": "Interested", "callback": "Not now / callback", "details": "Details",
                   "voicemail_followup": "After voicemail"}
# Empty lead details in an email read better as these.
FALLBACKS = {"lead_name": "there", "business_name": "your business", "service_required": "your project"}
SMTP_TIMEOUT_S = 20
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
_send_lock = threading.Lock()  # one send at a time, so the daily limit can't be overrun
_tasks: set[asyncio.Task] = set()


class EmailError(Exception):
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def test_mode() -> bool:
    """On unless EMAIL_TEST_MODE is explicitly 0/false/off (an empty or missing value means ON)."""
    return _env("EMAIL_TEST_MODE", "1").lower() not in ("0", "false", "off", "no")


def sending_disabled() -> bool:
    return _env("EMAIL_SENDING_DISABLED") == "1"


def smtp_settings() -> dict:
    port = _env("SMTP_PORT", "587")
    return {"host": _env("SMTP_HOST"), "port": int(port) if port.isdigit() else 587, "user": _env("SMTP_USER"),
            "password": os.getenv("SMTP_PASSWORD") or "", "security": _env("SMTP_SECURITY").lower(),
            "from_name": _env("EMAIL_FROM_NAME"), "from_address": _env("EMAIL_FROM_ADDRESS") or _env("SMTP_USER")}


def configured() -> tuple[bool, str]:
    from app.leads import normalize_email

    c = smtp_settings()
    if not c["host"]:
        return False, "email not configured: SMTP_HOST is empty in .env"
    if not normalize_email(c["from_address"])[0]:
        return False, "email not configured: EMAIL_FROM_ADDRESS is empty or not an email address"
    if c["user"] and not c["password"]:
        return False, "email not configured: SMTP_PASSWORD is empty"
    return True, ""


def sent_last_24h() -> int:
    since = utcnow() - timedelta(hours=24)
    with get_session() as s:
        return s.exec(select(func.count()).select_from(EmailMessage)
                      .where(EmailMessage.status == "sent", EmailMessage.sent_at >= since)).one()


def status() -> dict:
    """For the dashboard: can we send, where do emails go, how many today."""
    ok, why = configured()
    c = smtp_settings()
    try:
        e = load_settings().emails
        enabled, limit = e.enabled, e.daily_limit
    except SettingsError:
        enabled, limit = False, 0
    return {"configured": ok, "detail": why or f"Sending as {c['from_address']} via {c['host']}:{c['port']}",
            "enabled": enabled, "test_mode": test_mode(), "test_address": _env("EMAIL_TEST_ADDRESS"),
            "disabled": sending_disabled(), "sent_24h": sent_last_24h(), "daily_limit": limit,
            "from_address": c["from_address"]}


# ---- what to send ----------------------------------------------------------------------------

def choose_template(call: Call, s) -> str | None:
    """The template for a finished call, or None (no email)."""
    o = call.outcome
    if not o or o in ("do_not_contact", "wrong_number"):
        return None
    if o == "gatekeeper" and not call.email_collected:
        return None  # we never spoke to the lead themselves
    if o in outcomes.CALLBACK_OUTCOMES or (o == "interested" and call.callback_at):
        return "callback"
    if o in ("interested", "transferred"):
        return "interested"
    if o == "voicemail":
        return "voicemail_followup" if s.emails.after_voicemail else None
    if call.email_collected:  # they asked for details (or gave an email) on any other outcome
        return "details"
    return None


def callback_text(callback_at, tz_name: str | None) -> str:
    """'on Thursday, October 30 at 10 AM (your time)', or 'in the next few days'."""
    if not callback_at:
        return "in the next few days"
    t = as_utc(callback_at).astimezone(ZoneInfo(tz_name or "America/New_York"))
    clock = f"{t.hour % 12 or 12}" + (f":{t.minute:02d}" if t.minute else "") + (" AM" if t.hour < 12 else " PM")
    return f"on {t:%A, %B} {t.day} at {clock} (your time)"


def footer(s) -> str:
    lines = [f"{s.company_name} · {s.company_phone}", s.company_address]
    if s.company_website:
        lines.append(s.company_website)
    lines.append('Don\'t want these emails? Reply STOP or "unsubscribe" and we won\'t contact you again.')
    return "\n\n--\n" + "\n".join(x for x in lines if x.strip())


def build(s, template: str, lead: Lead, callback_at=None) -> tuple[str, str]:
    """(subject, body) with the lead's details, the callback time, and our footer."""
    tpl = getattr(s.emails, template)
    values = {k: v or FALLBACKS.get(k, "") for k, v in lead_variables(lead, s).items()}
    tz_name, _ = lead_timezone(lead.location, lead.phone)
    extra = {"callback_time": callback_text(callback_at, tz_name)}
    subject = " ".join(s.fill(tpl.subject, lead=values, extra=extra).split())[:200]
    body = s.fill(tpl.body, lead=values, extra=extra)
    body = re.sub(r"(?m)^[ \t]*[A-Za-z][A-Za-z ]{0,30}:[ \t]*\n", "", body + "\n")  # "Website:" with nothing after it
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return subject, body + footer(s)


# ---- sending ---------------------------------------------------------------------------------

def _smtp_send(msg: MimeMessage, c: dict) -> None:
    if standby.on():  # every email passes here
        raise EmailError("standby: email not sent (DIALER_STANDBY is set)")
    host, port = c["host"], c["port"]
    security = c["security"] or ("ssl" if port == 465 else "starttls")
    if security not in ("ssl", "starttls", "none"):
        raise EmailError("SMTP_SECURITY must be ssl, starttls or none")
    if security == "none" and host not in LOCAL_HOSTS:
        raise EmailError("SMTP_SECURITY=none is only allowed for a mail server on this computer")
    ctx = ssl.create_default_context()
    server = (smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=ctx) if security == "ssl"
              else smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S))
    with server:
        server.ehlo()
        if security == "starttls":
            if server.has_extn("starttls"):
                server.starttls(context=ctx)
                server.ehlo()
            elif host not in LOCAL_HOSTS:  # never send the password unencrypted
                raise EmailError("the mail server doesn't offer STARTTLS: try SMTP_PORT=465 (SSL)")
        if c["user"]:
            server.login(c["user"], c["password"])
        server.send_message(msg)


def _error_text(e: Exception) -> str:
    if isinstance(e, EmailError):
        return str(e)
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "the mail server refused the login (for Gmail use an app password, not your normal password)"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "the mail server refused the address"
    if isinstance(e, (TimeoutError, OSError)) and not isinstance(e, smtplib.SMTPException):
        return f"couldn't reach the mail server ({type(e).__name__}: {str(e)[:120]})"
    return f"{type(e).__name__}: {str(e)[:200]}"


def send(lead_id: int, template: str, call_id: int | None = None, callback_at=None) -> dict:
    """Build and send one email to a lead (or save why it wasn't sent). Returns the stored record."""
    if template not in TEMPLATES:
        raise EmailError(f"Unknown template {template!r}")
    s = load_settings()
    with _send_lock:
        with get_session() as db:
            lead = db.get(Lead, lead_id)
        if lead is None:
            raise EmailError("Lead not found")
        subject, body = build(s, template, lead, callback_at)
        c = smtp_settings()
        to = lead.email
        rec = EmailMessage(lead_id=lead_id, call_id=call_id, template=template, lead_email=to, to_email=to,
                           test_mode=test_mode(), subject=subject, body=body)
        ok, why = configured()
        if lead.dnc:
            rec.detail = "not sent: the lead is on Do not contact"
        elif not to:
            rec.detail = "not sent: no email address for this lead"
        elif not s.emails.enabled:
            rec.detail = "not sent: follow-up emails are switched off (Instructions → Follow-up emails)"
        elif sending_disabled():
            rec.detail = "not sent: email sending is turned off (EMAIL_SENDING_DISABLED=1)"
        elif not ok:
            rec.detail = f"not sent ({why})"
        elif not s.company_address.strip():
            rec.detail = "not sent: no business address yet (Instructions → Business address; the law requires it)"
        elif rec.test_mode and not _env("EMAIL_TEST_ADDRESS"):
            rec.detail = "not sent: test mode is on but EMAIL_TEST_ADDRESS isn't set in .env"
        elif sent_last_24h() >= s.emails.daily_limit:
            rec.detail = f"not sent: daily limit reached ({s.emails.daily_limit} emails in 24 hours)"
        else:
            if rec.test_mode:
                rec.to_email = _env("EMAIL_TEST_ADDRESS")
                rec.subject = f"[TEST for {to}] {subject}"
            domain = c["from_address"].rsplit("@", 1)[-1]
            msg = MimeMessage()
            msg["From"] = formataddr((c["from_name"] or s.company_name, c["from_address"]))
            msg["To"] = rec.to_email
            msg["Subject"] = rec.subject
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = rec.message_id = make_msgid(domain=domain)
            msg["Reply-To"] = c["from_address"]
            msg["List-Unsubscribe"] = f"<mailto:{c['from_address']}?subject=unsubscribe>"
            msg.set_content(body)
            try:
                _smtp_send(msg, c)
                rec.status, rec.sent_at = "sent", utcnow()
            except Exception as e:  # noqa: BLE001 - any SMTP/network problem: saved as failed, never raised
                rec.status, rec.detail = "failed", _error_text(e)
                logger.warning("email to lead %s failed: %s", lead_id, rec.detail)
        with get_session() as db:
            db.add(rec)
            db.flush()
            label = TEMPLATE_LABELS.get(template, template)
            text = (f"Email sent ({label}) to {rec.to_email}" + (f" (test mode; lead: {to})" if rec.test_mode else "")
                    if rec.status == "sent" else f"Email {'failed' if rec.status == 'failed' else 'not sent'} ({label}): "
                    + rec.detail.removeprefix("not sent: "))
            db.add(LeadEvent(lead_id=lead_id, kind="email", text=text, data={"email_id": rec.id, "status": rec.status,
                                                                             "call_id": call_id}))
            db.commit()
            db.refresh(rec)
    logger.info("email %s for lead %s: %s %s", rec.id, lead_id, rec.status, rec.detail)
    return email_to_dict(rec)


SAMPLE_LEAD = Lead(phone="+13055550100", name="Maria", business="Bloom Bakery", service_required="a new website",
                   location="Miami, FL 33130")


def send_test() -> dict:
    """One sample email (the "interested" template, a made-up lead) to EMAIL_TEST_ADDRESS only, to check the
    account without a call. Not linked to any lead, not counted against the daily limit."""
    to = _env("EMAIL_TEST_ADDRESS")
    ok, why = configured()
    if sending_disabled():
        return {"ok": False, "detail": "Email sending is turned off (EMAIL_SENDING_DISABLED=1)."}
    if not ok:
        return {"ok": False, "detail": why[:1].upper() + why[1:] + "."}
    if not to:
        return {"ok": False, "detail": "Set EMAIL_TEST_ADDRESS in .env first (the test email only goes there)."}
    s = load_settings()
    subject, body = build(s, "interested", SAMPLE_LEAD)
    c = smtp_settings()
    msg = MimeMessage()
    msg["From"] = formataddr((c["from_name"] or s.company_name, c["from_address"]))
    msg["To"] = to
    msg["Subject"] = f"[TEST email from the DialMind] {subject}"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=c["from_address"].rsplit("@", 1)[-1])
    msg.set_content(body)
    try:
        _smtp_send(msg, c)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": _error_text(e)}
    note = "" if s.company_address.strip() else " Note: real emails stay unsent until the business address is set."
    return {"ok": True, "detail": f"Sent to {to}. Check that inbox (and its spam folder).{note}"}


def send_for_call(call_id: int) -> dict | None:
    """The follow-up email for a finished call, if its outcome calls for one. At most one per call."""
    with get_session() as db:
        call = db.get(Call, call_id)
        lead = db.get(Lead, call.lead_id) if call and call.lead_id else None
        done = db.exec(select(EmailMessage).where(EmailMessage.call_id == call_id)).first()
    if call is None or lead is None or done is not None or call.status not in ("ended", "failed"):
        return email_to_dict(done) if done else None
    try:
        s = load_settings()
    except SettingsError:
        return None
    if not s.emails.enabled or lead.dnc or not lead.email:
        return None  # nothing to record: no email was due
    template = choose_template(call, s)
    if template is None:
        return None
    return send(lead.id, template, call_id=call_id, callback_at=call.callback_at)


def _publish(rec: dict | None) -> None:
    if rec:
        bus.publish({"type": "email", "email": rec})


async def _send_in_background(call_id: int) -> None:
    try:
        _publish(await asyncio.to_thread(send_for_call, call_id))
    except Exception:  # noqa: BLE001
        logger.exception("follow-up email for call %s crashed", call_id)


def after_call(call_id: int) -> None:
    """Called when a call has ended (and its lead is updated). Sends in the background: SMTP can take seconds."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _publish(send_for_call(call_id))
        return
    task = loop.create_task(_send_in_background(call_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def retry(email_id: int) -> dict:
    """Send a failed / not-sent email again (same template, fresh text)."""
    with get_session() as db:
        old = db.get(EmailMessage, email_id)
        call = db.get(Call, old.call_id) if old and old.call_id else None
    if old is None:
        raise EmailError("Email not found")
    if old.status == "sent":
        raise EmailError("This email was already sent.")
    rec = send(old.lead_id, old.template, call_id=old.call_id, callback_at=call.callback_at if call else None)
    _publish(rec)
    return rec


def list_emails(lead_id: int | None = None, call_id: int | None = None, limit: int = 200) -> list[dict]:
    with get_session() as db:
        stmt = select(EmailMessage)
        if lead_id is not None:
            stmt = stmt.where(EmailMessage.lead_id == lead_id)
        if call_id is not None:
            stmt = stmt.where(EmailMessage.call_id == call_id)
        rows = db.exec(stmt.order_by(EmailMessage.id.desc()).limit(min(limit, 1000))).all()
    return [email_to_dict(e) for e in rows]
