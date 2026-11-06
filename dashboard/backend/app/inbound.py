"""Inbound calls: someone calls our number (TELNYX_PHONE_NUMBER). Setup: README, Running it for real.

The switch is `inbound.enabled` in agent_settings.yaml (Instructions page), default OFF.
  ON   apply(): the lookup_caller + save_caller tools, the inbound agent (CARTESIA_INBOUND_AGENT_ID, synced like the
       outbound one), our call-events webhook on it, the SIP trunk's inbound side (only Telnyx's signaling IPs may send
       calls), and our number assigned to the inbound agent. Cartesia then answers calls to the number with it.
  OFF  our number is unassigned: Cartesia doesn't answer it. Nothing else is removed, so ON again is quick.
Outbound calls are untouched either way: they use the other agent, and any number can place calls assigned or not.

A call: the inbound agent says inbound.greeting (no lead details: Cartesia has no per-call variables for inbound
calls), then uses lookup_caller -> lookup_text() (a known lead: name, why we called, Do-Not-Contact; or a new
caller). A new caller's details come in through save_caller -> save_caller() (a new lead). The call itself is
recorded like an outbound one (calls.py, direction "inbound"), with the same tools and supervisor hand-off.
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime

from sqlmodel import select

from app import cartesia, cartesia_setup, leads, outcomes, standby, supervisor
from app.agent_config import time_variables
from app.db import Call, Lead, as_utc, get_session, utcnow
from app.timezones import lead_timezone
from settings import SettingsError, load_settings  # shared/settings.py

logger = logging.getLogger("dashboard.inbound")

# Who may send calls to our trunk: Telnyx's SIP signaling + UAC addresses, all regions (https://sip.telnyx.com/voice.json,
# version 2026-05-25). CARTESIA_INBOUND_ALLOWED_ADDRESSES (comma-separated) replaces the list.
TELNYX_SIP_IPS = (
    "192.76.120.10", "64.16.250.10",  # US (sip.telnyx.com)
    "192.76.120.31", "64.16.250.13",  # CA
    "185.246.41.140", "185.246.41.141",  # EU
    "103.115.244.145", "103.115.244.146",  # AU
    "103.115.244.158", "103.115.244.159",  # AP
    "185.246.42.128", "185.246.42.129",  # ME
    "192.76.120.14", "192.76.120.72", "192.76.120.73", "192.76.120.74", "192.76.120.75", "192.76.120.76",  # UAC
)
RETRY_AFTER_ERROR_S = 300
FIELD_MAX = {"name": 80, "business_name": 120, "service": 200, "note": 300, "location": 160}

state: dict = {"state": "unknown", "detail": "Not checked yet", "checked_at": None, "url": None}
_lock = asyncio.Lock()


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def allowed_addresses() -> list[str]:
    custom = [a.strip() for a in _env("CARTESIA_INBOUND_ALLOWED_ADDRESSES").split(",") if a.strip()]
    return sorted(custom or TELNYX_SIP_IPS)


def media_encryption() -> str:
    """SRTP on calls from Telnyx: "allowed" takes encrypted and plain audio, "required" only encrypted."""
    v = _env("CARTESIA_INBOUND_MEDIA_ENCRYPTION") or "allowed"
    return v if v in ("disabled", "allowed", "required") else "allowed"


def enabled() -> bool:
    try:
        return load_settings().inbound.enabled
    except SettingsError:
        return False


def status() -> dict:
    """For the Instructions page."""
    return {"enabled": enabled(), "number": _env("TELNYX_PHONE_NUMBER"), **state}


def _set(st: str, detail: str, url: str | None = None) -> dict:
    state.update(state=st, detail=detail, checked_at=time.time(), url=url)
    if st == "error":
        logger.warning("inbound calls: %s", detail)
    else:
        logger.info("inbound calls: %s", detail)
    return status()


def _public_url() -> str | None:
    from app import tunnel  # tunnel imports this module

    return tunnel.fixed_url() or tunnel.state.get("public_url")


async def apply(reason: str = "settings saved") -> dict:
    """Make Cartesia match the switch. Safe to call any time: changes only what differs."""
    async with _lock:
        try:
            return await _apply(reason)
        except (cartesia.CartesiaError, SettingsError) as e:
            return _set("error", f"{'Switching on' if enabled() else 'Switching off'} failed: {e}")
        except Exception as e:  # noqa: BLE001 - never break a settings save or the tunnel watcher
            logger.exception("inbound calls: apply failed")
            return _set("error", f"{'Switching on' if enabled() else 'Switching off'} failed: {type(e).__name__}: {e}")


async def _apply(reason: str) -> dict:
    if standby.on():
        return _set("standby", "Standby: this copy doesn't change Cartesia")
    number_id = _env("CARTESIA_FROM_NUMBER_ID")
    number = _env("TELNYX_PHONE_NUMBER") or "our number"
    if not number_id:
        return _set("error", "Cartesia isn't set up yet (no CARTESIA_FROM_NUMBER_ID)")
    inbound_agent = _env("CARTESIA_INBOUND_AGENT_ID")
    if not enabled():
        n = await cartesia.get_number(number_id)
        agent = n.get("agent") or {}
        if agent.get("id") and agent["id"] == inbound_agent:
            await cartesia.assign_number(number_id, None)
            return _set("off", f"Off: calls to {number} aren't answered (switched off just now)")
        if agent.get("id"):  # someone connected it by hand in the Playground: not ours to undo
            return _set("off", f"Off here, but Cartesia sends calls to {number} to the agent "
                               f"\"{agent.get('name') or agent['id']}\" (set in the Cartesia Playground)")
        return _set("off", f"Off: calls to {number} aren't answered")
    url = _public_url()
    if not url:
        return _set("pending", "Waiting for the public address (webhook) before switching on")
    await cartesia_setup.ensure_lookup_tool(url)
    await cartesia_setup.ensure_save_caller_tool(url)
    sync = await cartesia_setup.sync_inbound_agent(reason)
    agent_id = sync["agent_id"]
    if not await cartesia_setup.attach_webhook_to(agent_id):
        return _set("pending", "Waiting for the call-events webhook before switching on", url)
    await _ensure_trunk_inbound()
    n = await cartesia.get_number(number_id)
    if (n.get("agent") or {}).get("id") != agent_id:
        await cartesia.assign_number(number_id, agent_id)
    return _set("on", f"On: the AI answers calls to {number}", url)


async def _ensure_trunk_inbound() -> None:
    """The trunk accepts calls only from Telnyx's addresses. Merge patch: the outbound side isn't touched."""
    pid = _env("CARTESIA_SIP_PROVIDER_ID")
    if not pid:
        raise cartesia.CartesiaError("no CARTESIA_SIP_PROVIDER_ID")
    inb = (await cartesia.get_provider(pid)).get("inbound") or {}
    want, enc = allowed_addresses(), media_encryption()
    if sorted(inb.get("allowed_addresses") or []) != want or inb.get("media_encryption") != enc:
        await cartesia.update_provider_inbound(pid, {"allowed_addresses": want, "media_encryption": enc})
        logger.info("SIP trunk %s: inbound calls allowed from %d Telnyx addresses (media %s)", pid, len(want), enc)


async def maybe_apply(url: str | None) -> None:
    """From the tunnel watcher: once at start, when the public URL changes, and again a while after an error."""
    st, checked = state.get("state"), state.get("checked_at") or 0
    if st == "unknown" or (url and url != state.get("url") and enabled()) or \
            (st in ("error", "pending") and time.time() - checked >= (RETRY_AFTER_ERROR_S if st == "error" else 10)):
        await apply("backend start" if st == "unknown" else "retry")


# ---- the caller ----------------------------------------------------------------------------

def our_numbers() -> set[str]:
    return {n for n in (_env("TELNYX_PHONE_NUMBER"),) if n}


def caller_from_call(data: dict) -> str:
    """The caller's number from a Cartesia call object: whichever end isn't our own number."""
    tp = data.get("telephony_params") or {}
    ours = our_numbers()
    for k in ("from", "to"):
        v = str(tp.get(k) or "").strip()
        if v and v not in ours:
            return v
    return ""


def normalize(raw: str) -> str:
    """E.164 for a US number, else "" (hidden / anonymous / foreign callers can't become leads)."""
    phone, _ = leads.normalize_us_phone(str(raw or "").removeprefix("sip:").split("@")[0])
    return phone or ""


def _clean(v, n: int = 400) -> str:
    v = re.sub(r"[{}]", "", str(v or ""))
    return re.sub(r"\s+", " ", v).strip()[:n]


def _when(at: datetime, tz_name: str, now: datetime) -> str:
    from zoneinfo import ZoneInfo

    t, today = as_utc(at).astimezone(ZoneInfo(tz_name)), now.astimezone(ZoneInfo(tz_name))
    days = (today.date() - t.date()).days
    if days <= 0:
        return f"today at {t.hour % 12 or 12} {'AM' if t.hour < 12 else 'PM'}"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"on {t:%A}"
    return f"on {t:%B} {t.day}"


def why_we_called(lead: Lead, call_id: int | None, tz_name: str, now: datetime) -> str:
    with get_session() as s:
        prev = s.exec(select(Call).where(Call.lead_id == lead.id, Call.id != (call_id or 0))
                      .order_by(Call.id.desc())).all()
    out = next((c for c in prev if (c.direction or "outbound") == "outbound" and not c.simulated), None)
    if out is None:
        if prev:
            return f"We haven't called them; they called us before, {_when(prev[0].created_at, tz_name, now)}."
        return "We haven't called them yet (they're on our lead list)."
    what = outcomes.label(out.outcome or outcomes.from_result(out.result))
    about = f" about {_clean(lead.service_required, 120)}" if lead.service_required else ""
    return f"We called them {_when(out.created_at, tz_name, now)}{about}. How that call went: {what}."


def lookup_text(call: Call) -> str:
    """lookup_caller's answer: one 'name: value' line per detail, named like the [labels] in the instructions."""
    lead = leads.get_lead_row(call.lead_id) if call.lead_id else None
    try:
        s = load_settings()
        title_for = s.specialist_titles.title_for
    except SettingsError:
        title_for = lambda _: "one of our senior specialists"  # noqa: E731
    tz_name, _ = lead_timezone(lead.location if lead else "", lead.phone if lead else call.number)
    now = utcnow()
    tv = time_variables(tz_name, now)
    ok, _ = supervisor.availability()
    lines = {"known": "yes" if lead else "no", "do_not_contact": "yes" if lead and lead.dnc else "no"}
    if lead:
        lines.update({
            "their name": _clean(lead.name, 80), "their business": _clean(lead.business, 120),
            "the service they need": _clean(lead.service_required), "about their business": _clean(lead.business_description),
            "their project": _clean(lead.job_description), "their location": _clean(lead.location or lead.city, 160),
            "email on file": _clean(lead.email, 200), "why we called": why_we_called(lead, call.id, tz_name, now),
        })
    lines.update({
        "specialist title": title_for(lead.service_required if lead else ""),
        "specialist status": "available" if ok and not (lead and lead.dnc) else "unavailable",
        "their local time": tv["lead_local_time"], "their time zone": tv["lead_timezone"],
    })
    if not lead:
        lines["note"] = ("A new caller: not in our leads. Find out who they are (see A new caller)."
                         if normalize(call.number) else "A new caller with a hidden number: they can't be saved as a lead.")
    return "\n".join(f"{k}: {v}" for k, v in lines.items())


def save_caller(call: Call, args: dict) -> tuple[str, int | None]:
    """save_caller: a new caller's details. Returns (the AI's answer, the lead ID now linked to the call)."""
    f = {k: _clean(args.get(k), n) for k, n in FIELD_MAX.items()}
    try:
        s = load_settings()
        title = s.specialist_titles.title_for(f["service"])
    except SettingsError:
        title = "one of our senior specialists"
    ok, _ = supervisor.availability()
    fit, _why = leads.service_fit(f["service"])
    extra = (f" Note: {f['service']} isn't something we offer: say so politely, and don't offer our specialist."
             if fit == "not_fit" else "")
    reply = f"Saved. specialist title: {title}. specialist status: {'available' if ok else 'unavailable'}.{extra}"
    if not f["name"] and not f["service"]:
        return "Not saved: give at least their name or what they need.", call.lead_id
    phone = normalize(call.number)
    if call.lead_id:
        lead_id = leads.fill_from_caller(call.lead_id, f, call.id)
        return reply, lead_id
    if not phone:
        return reply.replace("Saved.", "Noted (their number is hidden, so they aren't saved as a lead)."), None
    if (existing := leads.find_by_phone(phone)) is not None:  # e.g. added by hand during the call
        return reply, leads.fill_from_caller(existing.id, f, call.id)
    lead = leads.create_inbound_lead(phone, f, call.id)
    return reply, lead.id
