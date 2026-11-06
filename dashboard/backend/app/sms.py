"""Follow-up texts (SMS) after calls (Phase 7b), sent through the Telnyx Messaging API. Templates: agent_settings.yaml
`sms`; number: .env (TELNYX_SMS_FROM, default TELNYX_PHONE_NUMBER; optional TELNYX_MESSAGING_PROFILE_ID).

When (choose_template, from the call's outcome), at most one text per call:
  not reached: no_answer (also busy), voicemail, bad_line; or a gatekeeper answered   -> not_answered
  not_interested (said politely: rude / "stop calling" is do_not_contact)             -> not_interested
  not_now, supervisor busy, transfer failed                                          -> not_now ({callback_time})
  interested, or they asked for details (an email was collected)                     -> interested
  never: do_not_contact, wrong_number, transferred, failed (dial failed), needs_review, and never a DNC lead.
Limits: ONE text per lead, ever (whatever the outcome; a queued one counts) and sms.daily_limit in total (24 h).
Only inside the lead's calling hours (settings.calling_hours, their time zone): otherwise the text is queued until the
next allowed time (watch() checks the queue every 30 s; DNC and the limits are checked again right before sending).
Test mode (app/test_mode.py, the same switch as calls): every text goes to TEST_CALL_NUMBER; send_raw refuses any
other number while it's on (test_mode.guard). SMS_SENDING_DISABLED=1 is a hard kill switch (tests, demos).
Delivery reports and replies: POST /api/telnyx/messaging (Ed25519-signed, TELNYX_PUBLIC_KEY). STOP & co. -> DNC.
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlmodel import func, or_, select

from app import outcomes, standby, test_mode
from app.agent_config import lead_variables
from app.area_codes import zone
from app.bus import bus
from app.db import Call, Lead, LeadEvent, SmsMessage, as_utc, get_session, sms_to_dict, utcnow
from app.emails import FALLBACKS, callback_text
from app.timezones import lead_timezone
from settings import SettingsError, load_settings  # shared/settings.py

logger = logging.getLogger("dashboard.sms")

API_URL = "https://api.telnyx.com/v2"
WEBHOOK_PATH = "/api/telnyx/messaging"
# The server's addresses: the laptop must be able to take the webhook back from them (rollback), and the other way round.
OUR_WEBHOOK_HOSTS = ("dialer.example.com", "203-0-113-10.sslip.io")
TEMPLATES = ("not_answered", "not_interested", "not_now", "interested")
TEMPLATE_LABELS = {"not_answered": "Not answered / voicemail", "not_interested": "Not interested",
                   "not_now": "Not now / callback", "interested": "Interested"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
QUEUE_EVERY_S = 30
HTTP_TIMEOUT_S = 20
OPT_OUT = "Reply STOP to opt out."
# A whole reply that means "stop texting me" (carriers' standard opt-out words, and plain variants).
_STOP_RE = re.compile(r"^(?:please\s+)?(?:stop\s*all|stop|unsubscribe|cancel|end|quit|revoke|opt\s*out|optout|"
                      r"remove(?:\s+me)?|stop\s+(?:texting|messaging|contacting|sending)(?:\s+(?:me|us|this))?)"
                      r"(?:\s+please)?$", re.I)
_send_lock = threading.Lock()  # one decision/send at a time, so the limits can't be overrun
_tasks: set[asyncio.Task] = set()
_seen: dict[str, float] = {}  # webhook event IDs (Telnyx may resend)
state: dict = {"webhook": None}  # result of ensure_webhook (Health page)


class SmsError(Exception):
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def sending_disabled() -> bool:
    return _env("SMS_SENDING_DISABLED") == "1"


def from_number() -> str:
    return _env("TELNYX_SMS_FROM") or _env("TELNYX_PHONE_NUMBER")


def configured() -> tuple[bool, str]:
    if not _env("TELNYX_API_KEY"):
        return False, "texts not configured: TELNYX_API_KEY is empty in .env"
    if not re.fullmatch(r"\+1\d{10}", from_number()):
        return False, "texts not configured: TELNYX_SMS_FROM / TELNYX_PHONE_NUMBER isn't a +1 number"
    return True, ""


def _count(*where) -> int:
    with get_session() as s:
        return s.exec(select(func.count()).select_from(SmsMessage).where(*where)).one()


def sent_last_24h() -> int:
    return _count(SmsMessage.status.in_(("sent", "delivered")), SmsMessage.sent_at >= utcnow() - timedelta(hours=24))


def lead_already_texted(lead_id: int, test: bool) -> bool:
    """One text per lead, ever: any text sent (also one that failed later, it may have arrived) or still queued.
    Test-mode texts (they went to the test phone, never to the lead) only block other test-mode texts."""
    return _count(SmsMessage.lead_id == lead_id, SmsMessage.test_mode == test,
                  or_(SmsMessage.status == "queued", SmsMessage.sent_at.is_not(None))) > 0


def status() -> dict:
    """For the Health page: can we text, where texts go, how many today, the webhook."""
    ok, why = configured()
    try:
        c = load_settings().sms
        enabled, limit = c.enabled, c.daily_limit
    except SettingsError:
        enabled, limit = False, 0
    signed = bool(_env("TELNYX_PUBLIC_KEY"))
    return {"configured": ok, "detail": why or f"Sending from {from_number()} (Telnyx)", "enabled": enabled,
            "test_mode": test_mode.is_on(), "test_number": test_mode.number(), "disabled": sending_disabled(),
            "sent_24h": sent_last_24h(), "daily_limit": limit,
            "queued": _count(SmsMessage.status == "queued"), "from_number": from_number(),
            "profile_id": _env("TELNYX_MESSAGING_PROFILE_ID"),
            "webhook": (state["webhook"] or {}).get("detail") or "Not set yet (needs the tunnel and TELNYX_MESSAGING_PROFILE_ID)",
            "webhook_ok": bool((state["webhook"] or {}).get("ok")) and signed,
            "signature": "On (TELNYX_PUBLIC_KEY set)" if signed else "Off: replies and delivery reports are refused until TELNYX_PUBLIC_KEY is set"}


# ---- what to send ----------------------------------------------------------------------------

def choose_template(call: Call) -> str | None:
    """The template for a finished call, or None (no text)."""
    o = call.outcome
    if o in ("no_answer", "voicemail", "bad_line", "gatekeeper"):
        return "not_answered"
    if o == "not_interested":
        return "not_interested"
    if o in outcomes.CALLBACK_OUTCOMES:  # not_now, transfer_declined, transfer_failed (gatekeeper handled above)
        return "not_now"
    if o == "interested" or (o == "needs_review" and call.email_collected):
        return "interested"
    return None  # do_not_contact, wrong_number, transferred, failed, needs_review, unknown


def build(s, template: str, lead: Lead, callback_at=None) -> str:
    """The text with the lead's details, plus the company name and the opt-out line if the template lacks them."""
    values = {k: v or FALLBACKS.get(k, "") for k, v in lead_variables(lead, s).items()}
    if not lead.name:
        values["lead_name"] = "there"
    tz_name, _ = lead_timezone(lead.location, lead.phone)
    text = s.fill(getattr(s.sms, template), lead=values, extra={"callback_time": callback_text(callback_at, tz_name)})
    text = " ".join(text.split())
    if s.company_name.lower() not in text.lower():
        text = f"{s.company_name}: {text}"
    if "stop" not in text.lower():
        text = f"{text} {OPT_OUT}"
    return text


def next_send_time(tz_name: str, hours, now_utc: datetime) -> datetime | None:
    """now_utc if it's inside the calling hours in tz_name, else the next start of them (UTC)."""
    tz = zone(tz_name)
    local = now_utc.astimezone(tz)
    (sh, sm), (eh, em) = (map(int, hours.start.split(":")), map(int, hours.end.split(":")))
    for i in range(8):
        day = (local + timedelta(days=i)).date()
        if DAYS[day.weekday()] not in hours.days:
            continue
        start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
        end = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
        if start <= local < end:
            return now_utc
        if local < start:
            return start.astimezone(timezone.utc)
    return None


# ---- sending ---------------------------------------------------------------------------------

def _post(path: str, body: dict, method: str = "POST") -> dict:
    """Telnyx API call (JSON). Raises SmsError with Telnyx's own message on errors. The key is never logged."""
    if standby.on():  # every write to Telnyx passes here
        raise SmsError(f"standby: Telnyx {method} {path} not sent (DIALER_STANDBY is set)")
    req = urllib.request.Request(API_URL + path, data=json.dumps(body).encode(), method=method, headers={
        "Authorization": "Bearer " + _env("TELNYX_API_KEY"), "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            errs = json.loads(e.read() or b"{}").get("errors") or []
            msg = "; ".join(f"{x.get('title', '')}: {x.get('detail', '')}".strip(": ") for x in errs)[:300]
        except ValueError:
            msg = ""
        raise SmsError(f"Telnyx refused it (HTTP {e.code}{': ' + msg if msg else ''})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SmsError(f"couldn't reach Telnyx ({type(e).__name__})") from None


def _get(path: str) -> dict:
    req = urllib.request.Request(API_URL + path, headers={"Authorization": "Bearer " + _env("TELNYX_API_KEY"),
                                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SmsError(f"Telnyx refused it (HTTP {e.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SmsError(f"couldn't reach Telnyx ({type(e).__name__})") from None


def send_raw(to: str, text: str) -> str:
    """The ONLY place a text leaves: kill switch, then the test-mode guard, then Telnyx. Returns the message ID."""
    if sending_disabled():
        raise SmsError("text sending is turned off (SMS_SENDING_DISABLED=1)")
    try:
        test_mode.guard(to, "text")  # TEST MODE: only the test number, whatever path got here
    except test_mode.TestModeError as e:
        raise SmsError(str(e)) from None
    ok, why = configured()
    if not ok:
        raise SmsError(why)
    body = {"from": from_number(), "to": to, "text": text}
    if profile := _env("TELNYX_MESSAGING_PROFILE_ID"):
        body["messaging_profile_id"] = profile
    from app import tunnel  # (tunnel imports this module)

    if base := tunnel.state.get("public_url"):
        body["webhook_url"] = base + WEBHOOK_PATH  # delivery reports for this text
    data = _post("/messages", body).get("data") or {}
    if not data.get("id"):
        raise SmsError("Telnyx accepted the text but returned no message ID")
    return data["id"]


def _log_lead(db, rec: SmsMessage, text: str) -> None:
    db.add(LeadEvent(lead_id=rec.lead_id, kind="sms", text=text, data={"sms_id": rec.id, "status": rec.status,
                                                                         "call_id": rec.call_id}))


def _event_text(rec: SmsMessage) -> str:
    label = TEMPLATE_LABELS.get(rec.template, rec.template)
    if rec.status == "sent":
        return f"Text sent ({label}) to {rec.to_phone}" + (f" (test mode; lead: {rec.lead_phone})" if rec.test_mode else "")
    if rec.status == "queued":
        return f"Text queued ({label}): {rec.detail}"
    return f"Text {'failed' if rec.status == 'failed' else 'not sent'} ({label}): " + rec.detail.removeprefix("not sent: ")


def _try_send(rec: SmsMessage, lead: Lead, s) -> None:
    """Fill in rec.status/detail: sent, failed, or not_sent (rechecked right before sending)."""
    if lead.dnc:
        rec.status, rec.detail = "not_sent", "not sent: the lead is on Do not contact"
        return
    if not s.sms.enabled:
        rec.status, rec.detail = "not_sent", "not sent: follow-up texts are switched off (Instructions → Follow-up texts)"
        return
    if sent_last_24h() >= s.sms.daily_limit:
        rec.status, rec.detail = "not_sent", f"not sent: daily limit reached ({s.sms.daily_limit} texts in 24 hours)"
        return
    # A text made in test mode stays a test text (the lead was never really called), even if test mode is off now.
    rec.test_mode = rec.test_mode or test_mode.is_on()
    rec.to_phone = test_mode.number() if rec.test_mode else lead.phone
    if not rec.to_phone:
        rec.status, rec.detail = "not_sent", "not sent: test mode is on but TEST_CALL_NUMBER isn't set in .env"
        return
    try:
        rec.telnyx_id = send_raw(rec.to_phone, rec.body)
        rec.status, rec.sent_at, rec.detail, rec.send_after = "sent", utcnow(), "", None
    except SmsError as e:
        rec.status, rec.detail = ("not_sent", f"not sent: {e}") if sending_disabled() else ("failed", str(e))
        logger.warning("text to lead %s: %s", lead.id, rec.detail)


def create(lead_id: int, template: str, call_id: int | None = None, callback_at=None) -> dict:
    """Decide and send (or queue) one text to a lead, or save why not. Returns the stored record."""
    if template not in TEMPLATES:
        raise SmsError(f"Unknown template {template!r}")
    s = load_settings()
    with _send_lock:
        with get_session() as db:
            lead = db.get(Lead, lead_id)
        if lead is None:
            raise SmsError("Lead not found")
        rec = SmsMessage(lead_id=lead_id, call_id=call_id, template=template, lead_phone=lead.phone,
                         to_phone=lead.phone, test_mode=test_mode.is_on(), body=build(s, template, lead, callback_at))
        tz_name, _ = lead_timezone(lead.location, lead.phone)
        ok, why = configured()
        if lead.dnc:
            rec.detail = "not sent: the lead is on Do not contact"
        elif not s.sms.enabled:
            rec.detail = "not sent: follow-up texts are switched off (Instructions → Follow-up texts)"
        elif sending_disabled():
            rec.detail = "not sent: text sending is turned off (SMS_SENDING_DISABLED=1)"
        elif not ok:
            rec.detail = f"not sent ({why})"
        elif rec.test_mode and not test_mode.number():
            rec.detail = "not sent: test mode is on but TEST_CALL_NUMBER isn't set in .env"
        elif lead_already_texted(lead_id, rec.test_mode):
            rec.detail = "not sent: this lead already got a text (one per lead, ever)"
        elif tz_name is None:
            rec.detail = "not sent: the lead's time zone is unknown, so their calling hours are too (add a zip or state)"
        else:
            now = utcnow()
            at = next_send_time(tz_name, s.settings.calling_hours, now)
            if at is None:
                rec.detail = "not sent: no calling days set (Instructions → Call settings)"
            elif at > now:
                rec.status, rec.send_after = "queued", at
                local = at.astimezone(zone(tz_name))
                rec.detail = f"outside the lead's calling hours: sends {local:%a %b} {local.day}, {local:%H:%M} their time"
            else:
                _try_send(rec, lead, s)
        with get_session() as db:
            db.add(rec)
            db.flush()
            _log_lead(db, rec, _event_text(rec))
            db.commit()
            db.refresh(rec)
    logger.info("text %s for lead %s: %s %s", rec.id, lead_id, rec.status, rec.detail)
    return sms_to_dict(rec)


def process_queue(now: datetime | None = None) -> list[dict]:
    """Send queued texts that are due (DNC, the switch and the limits are checked again first)."""
    now = now or utcnow()
    with get_session() as db:
        due = db.exec(select(SmsMessage).where(SmsMessage.status == "queued", SmsMessage.send_after <= now)
                      .order_by(SmsMessage.send_after)).all()
    if not due:
        return []
    try:
        s = load_settings()
    except SettingsError:
        return []
    done = []
    for rec in due:
        with _send_lock:
            with get_session() as db:
                rec = db.get(SmsMessage, rec.id)
                lead = db.get(Lead, rec.lead_id)
                if rec is None or rec.status != "queued" or lead is None:
                    continue
            _try_send(rec, lead, s)
            with get_session() as db:
                db.add(rec)
                _log_lead(db, rec, _event_text(rec))
                db.commit()
                db.refresh(rec)
        done.append(sms_to_dict(rec))
        _publish(done[-1])
    return done


SAMPLE_LEAD = Lead(phone="+13055550100", name="Maria", business="Bloom Bakery", service_required="a new website",
                   location="Miami, FL 33130")


def send_test() -> dict:
    """ONE sample text (the "interested" template, a made-up lead) to TEST_CALL_NUMBER only (Health page button)."""
    to = test_mode.number()
    if not to:
        return {"ok": False, "detail": "Set TEST_CALL_NUMBER in .env first (the test text only goes there)."}
    s = load_settings()
    text = "[TEST] " + build(s, "interested", SAMPLE_LEAD)
    try:
        test_mode.guard(to, "text")
        msg_id = send_raw(to, text)
    except (SmsError, test_mode.TestModeError) as e:
        return {"ok": False, "detail": str(e)[:1].upper() + str(e)[1:]}
    logger.info("test text %s sent to %s", msg_id, to)
    return {"ok": True, "detail": f"Sent to {to} from {from_number()}. Check that phone."}


def for_call(call_id: int) -> dict | None:
    """The follow-up text for a finished call, if its outcome calls for one. At most one per call."""
    with get_session() as db:
        call = db.get(Call, call_id)
        lead = db.get(Lead, call.lead_id) if call and call.lead_id else None
        done = db.exec(select(SmsMessage).where(SmsMessage.call_id == call_id)).first()
    if call is None or lead is None or done is not None or call.status not in ("ended", "failed"):
        return sms_to_dict(done) if done else None
    if call.direction == "inbound":
        return None  # they called us (they have our number): no follow-up text
    try:
        s = load_settings()
    except SettingsError:
        return None
    if not s.sms.enabled or lead.dnc:
        return None  # nothing to record: no text was due
    template = choose_template(call)
    if template is None:
        return None
    return create(lead.id, template, call_id=call_id, callback_at=call.callback_at or lead.callback_at)


def _publish(rec: dict | None) -> None:
    if rec:
        bus.publish({"type": "sms", "sms": rec})


async def _in_background(call_id: int) -> None:
    try:
        _publish(await asyncio.to_thread(for_call, call_id))
    except Exception:  # noqa: BLE001
        logger.exception("follow-up text for call %s crashed", call_id)


def after_call(call_id: int) -> None:
    """Called when a call has ended (and its lead is updated). Telnyx can take a few seconds: in the background."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _publish(for_call(call_id))
        return
    task = loop.create_task(_in_background(call_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def watch() -> None:
    """Send queued texts when the lead's calling hours start."""
    while True:
        try:
            await asyncio.to_thread(process_queue)
        except Exception:  # noqa: BLE001
            logger.exception("text queue check failed")
        await asyncio.sleep(QUEUE_EVERY_S)


def retry(sms_id: int) -> dict:
    """Send a failed / not-sent text again (same template, fresh text, limits apply)."""
    with get_session() as db:
        old = db.get(SmsMessage, sms_id)
        call = db.get(Call, old.call_id) if old and old.call_id else None
    if old is None:
        raise SmsError("Text not found")
    if old.status in ("sent", "delivered", "queued"):
        raise SmsError("This text was already sent or is queued.")
    rec = create(old.lead_id, old.template, call_id=old.call_id, callback_at=call.callback_at if call else None)
    _publish(rec)
    return rec


def list_texts(lead_id: int | None = None, call_id: int | None = None, limit: int = 200) -> list[dict]:
    with get_session() as db:
        stmt = select(SmsMessage)
        if lead_id is not None:
            stmt = stmt.where(SmsMessage.lead_id == lead_id)
        if call_id is not None:
            stmt = stmt.where(SmsMessage.call_id == call_id)
        rows = db.exec(stmt.order_by(SmsMessage.id.desc()).limit(min(limit, 1000))).all()
    return [sms_to_dict(m) for m in rows]


# ---- from Telnyx: delivery reports + replies ---------------------------------------------------

def is_stop(text: str) -> bool:
    t = re.sub(r"[^a-z ]", " ", (text or "").lower())
    return bool(_STOP_RE.match(" ".join(t.split())))


def _stop_lead(sender: str) -> tuple[int | None, str]:
    """(lead to mark DNC, how we know). In test mode a STOP from the test phone means the lead of the last text sent
    there. The test phone itself is never put on DNC (it would block every test call)."""
    if sender and sender == test_mode.number():
        with get_session() as db:
            last = db.exec(select(SmsMessage).where(SmsMessage.to_phone == sender, SmsMessage.status.in_(("sent", "delivered")))
                           .order_by(SmsMessage.sent_at.desc())).first()
        return (last.lead_id, "via the test phone (last text sent there)") if last else (None, "test phone, no text sent there yet")
    from app.leads import find_by_phone

    lead = find_by_phone(sender)
    return (lead.id, "from the lead's number") if lead else (None, "not a lead")


def handle_webhook(raw: bytes) -> dict:
    """A verified Telnyx messaging event: delivery status of our texts, or an incoming reply."""
    from app import leads

    try:
        data = json.loads(raw).get("data") or {}
    except ValueError:
        return {"ok": True, "ignored": "not JSON"}
    event_id, now = data.get("id") or "", time.time()
    for k in [k for k, t in _seen.items() if now - t > 3600]:
        _seen.pop(k, None)
    if event_id and event_id in _seen:
        return {"ok": True, "duplicate": True}
    if event_id:
        _seen[event_id] = now
    kind = data.get("event_type") or ""
    p = data.get("payload") or {}
    if kind in ("message.sent", "message.finalized"):
        to = (p.get("to") or [{}])[0]
        st = to.get("status") or ""
        with get_session() as db:
            rec = db.exec(select(SmsMessage).where(SmsMessage.telnyx_id == (p.get("id") or "-"))).first()
            if rec is None:
                return {"ok": True, "ignored": "not one of our texts", "type": kind}
            if st == "delivered":
                rec.status, rec.delivered_at, rec.detail = "delivered", utcnow(), ""
            elif st in ("sending_failed", "delivery_failed"):
                errs = p.get("errors") or []
                rec.status = "failed"
                rec.detail = "; ".join(f"{e.get('title', '')} {e.get('detail', '')}".strip() for e in errs)[:300] or st
            elif st == "delivery_unconfirmed":
                rec.detail = "the carrier didn't confirm delivery"
            else:
                return {"ok": True, "type": kind, "status": st}
            db.add(rec)
            if rec.status in ("delivered", "failed"):
                _log_lead(db, rec, f"Text {rec.status} ({TEMPLATE_LABELS.get(rec.template, rec.template)})"
                          + (f": {rec.detail}" if rec.status == "failed" else ""))
            db.commit()
            db.refresh(rec)
        _publish(sms_to_dict(rec))
        return {"ok": True, "type": kind, "status": rec.status}
    if kind == "message.received":
        sender = str((p.get("from") or {}).get("phone_number") or "")
        text = str(p.get("text") or "")
        lead_id, how = _stop_lead(sender)
        logger.info("text reply from %s (%s): stop=%s", sender, how, is_stop(text))
        if lead_id is None:
            if is_stop(text) and sender != test_mode.number():
                try:
                    leads.add_dnc_number(sender, f"Replied {text.strip()[:20]!r} to a text")
                except leads.LeadError:
                    pass
                return {"ok": True, "type": kind, "stop": True, "lead_id": None}
            return {"ok": True, "type": kind, "ignored": how}
        if is_stop(text):
            leads.set_dnc(lead_id, True, f"Replied {text.strip()[:20]!r} to a text ({how})")
            return {"ok": True, "type": kind, "stop": True, "lead_id": lead_id}
        with get_session() as db:
            db.add(LeadEvent(lead_id=lead_id, kind="sms", text=f"Text reply ({how}): {text.strip()[:300]}",
                             data={"from": sender}))
            db.commit()
        return {"ok": True, "type": kind, "lead_id": lead_id}
    return {"ok": True, "type": kind}


def _our_webhook(url: str) -> bool:
    """Is this webhook one of ours? The host is compared, so a look-alike URL of another app doesn't pass."""
    host = (urlparse(url).hostname or "").lower()
    fixed = (urlparse(os.getenv("PUBLIC_BASE_URL", "")).hostname or "").lower()
    return host.endswith(".trycloudflare.com") or host in OUR_WEBHOOK_HOSTS or (bool(fixed) and host == fixed)


def ensure_webhook(public_base_url: str) -> dict:
    """Point our messaging profile's webhook (replies) at the public URL. Only a profile that has no webhook yet or one
    of our own URLs is changed: a profile another app uses (its own webhook) is never touched."""
    pid = _env("TELNYX_MESSAGING_PROFILE_ID")
    url = public_base_url.rstrip("/") + WEBHOOK_PATH
    if not pid:
        state["webhook"] = {"ok": False, "detail": "Replies (STOP) not set up: TELNYX_MESSAGING_PROFILE_ID is empty "
                                                   "(delivery reports still come back per text)"}
        return state["webhook"]
    if not _env("TELNYX_API_KEY"):
        state["webhook"] = {"ok": False, "detail": "TELNYX_API_KEY is empty"}
        return state["webhook"]
    try:
        prof = _get(f"/messaging_profiles/{pid}").get("data") or {}
        current = prof.get("webhook_url") or ""
        if current == url:
            action = "unchanged"
        elif current and not _our_webhook(current):
            host = re.sub(r"^https?://([^/]+).*$", r"\1", current)
            state["webhook"] = {"ok": False, "detail": f"Messaging profile {prof.get('name')!r} sends replies to another "
                                                       f"app ({host}): not changed. Use a profile of our own."}
            return state["webhook"]
        else:
            _post(f"/messaging_profiles/{pid}", {"webhook_url": url, "webhook_api_version": "2"}, method="PATCH")
            action = "updated"
        state["webhook"] = {"ok": True, "url": url, "action": action,
                            "detail": f"Replies from profile {prof.get('name')!r} → {url}"}
    except SmsError as e:
        state["webhook"] = {"ok": False, "detail": f"Couldn't set the messaging webhook: {e}"}
    return state["webhook"]
