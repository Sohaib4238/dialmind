"""Phase 6: hand interested leads to the supervisor ("ask first, then transfer").

  1. The AI uses the request_supervisor webhook tool (a short summary of the call so far).
  2. Every open dashboard (also the supervisor's laptop, remotely) shows a pop-up with a sound: Accept / Busy.
  3. The tool answers "waiting" after a few seconds, so the AI says a filler line and asks again; the lead
     never sits in dead air. After wait_seconds without a click it counts as busy (timeout).
  4. Accept -> the AI uses Cartesia's transfer_to_number ({{transfer_number}} = the supervisor's phone).
     Busy / timeout / supervisor off -> the AI asks when he can call back (record_outcome).

Settings live in data/supervisor.json (not agent_settings.yaml: flipping "available" must not create a new
Cartesia agent version). Per call they go to Cartesia as dynamic variables.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.bus import bus
from app.db import DATA_DIR, as_utc, utcnow

logger = logging.getLogger("dashboard.supervisor")

SETTINGS_PATH = Path(os.getenv("SUPERVISOR_SETTINGS_PATH") or DATA_DIR / "supervisor.json")
E164 = re.compile(r"^\+[1-9]\d{1,14}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
HOURS_ZONES = {"America/New_York": "US Eastern time", "UTC": "UTC"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WAIT_MIN_S, WAIT_MAX_S = 10, 60
CHECK_WAIT_S = 5.0  # one request_supervisor call waits at most this long, then says "still waiting"

# Call.transfer_status values
WAITING, ACCEPTED, DECLINED, TIMEOUT, UNAVAILABLE = "waiting", "accepted", "declined", "timeout", "unavailable"
TRANSFERRING, TRANSFERRED, FAILED = "transferring", "transferred", "failed"
NOT_TAKEN = (DECLINED, TIMEOUT, UNAVAILABLE)
TRANSFER_LABELS = {WAITING: "Waiting for the supervisor", ACCEPTED: "Supervisor accepted", DECLINED: "Supervisor busy",
                   TIMEOUT: "Supervisor didn't answer", UNAVAILABLE: "Supervisor unavailable",
                   TRANSFERRING: "Transferring", TRANSFERRED: "Transferred", FAILED: "Transfer failed"}

DEFAULTS = {
    "phone": os.getenv("TEST_CALL_NUMBER", ""),
    "available": False,  # the supervisor turns this on when he's at his desk
    "wait_seconds": 20,
    "hours": {"enabled": False, "timezone": "America/New_York", "start": "09:00", "end": "17:00",
              "days": ["mon", "tue", "wed", "thu", "fri"]},
}


class SupervisorError(Exception):
    pass


def load() -> dict:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))  # -sig: also a file saved by Notepad
    except (OSError, ValueError):
        raw = {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    out["hours"] = {**DEFAULTS["hours"], **(raw.get("hours") or {})}
    return out


def validate(values: dict) -> dict:
    """Merge `values` into the saved settings and check them. Raises SupervisorError (plain words)."""
    s = load()
    for k in ("phone", "available", "wait_seconds"):
        if k in values:
            s[k] = values[k]
    if "hours" in values:
        s["hours"] = {**s["hours"], **{k: v for k, v in (values["hours"] or {}).items() if k in DEFAULTS["hours"]}}
    s["phone"] = re.sub(r"[\s().-]", "", str(s["phone"] or ""))
    if s["phone"] and not E164.match(s["phone"]):
        raise SupervisorError("The supervisor's phone must be in +1XXXXXXXXXX format.")
    s["available"] = bool(s["available"])
    try:
        s["wait_seconds"] = int(s["wait_seconds"])
    except (TypeError, ValueError):
        raise SupervisorError("Wait time must be a number of seconds.") from None
    if not WAIT_MIN_S <= s["wait_seconds"] <= WAIT_MAX_S:
        raise SupervisorError(f"Wait time must be {WAIT_MIN_S}-{WAIT_MAX_S} seconds.")
    h = s["hours"]
    h["enabled"] = bool(h["enabled"])
    if h["timezone"] not in HOURS_ZONES:
        raise SupervisorError("Working hours must be in US Eastern time or UTC.")
    for k in ("start", "end"):
        if not HHMM.match(str(h[k])):
            raise SupervisorError("Working hours must look like 09:00 (24-hour).")
    if h["start"] == h["end"]:
        raise SupervisorError("Working hours: start and end can't be the same.")
    h["days"] = [d for d in DAYS if d in (h.get("days") or [])]
    if h["enabled"] and not h["days"]:
        raise SupervisorError("Pick at least one working day.")
    return s


def save(values: dict) -> dict:
    s = validate(values)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")
    state = public_state()
    bus.publish({"type": "supervisor_settings", "settings": state})
    return state


def in_hours(h: dict, now_utc: datetime) -> bool:
    """Inside the working hours? A shift may cross midnight (e.g. 18:00-03:00 UTC for US hours);
    the day is the day the shift starts."""
    if not h.get("enabled"):
        return True
    t = now_utc.astimezone(ZoneInfo(h["timezone"]))
    mins = t.hour * 60 + t.minute
    start = int(h["start"][:2]) * 60 + int(h["start"][3:])
    end = int(h["end"][:2]) * 60 + int(h["end"][3:])
    today, yesterday = DAYS[t.weekday()], DAYS[(t.weekday() - 1) % 7]
    if start < end:
        return today in h["days"] and start <= mins < end
    return (today in h["days"] and mins >= start) or (yesterday in h["days"] and mins < end)


def availability(now_utc: datetime | None = None, s: dict | None = None) -> tuple[bool, str]:
    """(can take a transfer now, why not in plain words)."""
    s = s or load()
    if not s["phone"]:
        return False, "no supervisor phone number set"
    if not s["available"]:
        return False, "supervisor switched to unavailable"
    if not in_hours(s["hours"], now_utc or utcnow()):
        return False, "outside the supervisor's working hours"
    return True, ""


def public_state() -> dict:
    s = load()
    ok, why = availability(s=s)
    return {**s, "can_take_calls": ok, "why_not": why, "hours_zones": HOURS_ZONES,
            "wait_min": WAIT_MIN_S, "wait_max": WAIT_MAX_S}


def call_variables(now_utc: datetime | None = None) -> dict[str, str]:
    """Dynamic variables for one call. transfer_number is always a valid E.164 number or empty."""
    s = load()
    ok, _ = availability(now_utc, s)
    return {"transfer_number": s["phone"], "supervisor_status": "available" if ok else "unavailable"}


# ---- what the AI hears back from request_supervisor -----------------------------------

REPLY_AVAILABLE = ("AVAILABLE. Our senior specialist is free right now. Say the connecting line from your instructions "
                   "and use the transfer tool in the same turn.")
REPLY_WAITING = ("WAITING. No answer from our senior specialist yet. Say one short, natural line to keep them on the "
                 "line, like \"Thanks for bearing with me, just a moment.\", then use request_supervisor again.")
REPLY_NOT_AVAILABLE = ("NOT AVAILABLE. Our senior specialist can't take the call right now. Follow \"If the senior "
                       "specialist can't take the call\" in your instructions.")


def reply_for(status: str) -> str:
    if status in (ACCEPTED, TRANSFERRING, TRANSFERRED):
        return REPLY_AVAILABLE
    if status == WAITING:
        return REPLY_WAITING
    return REPLY_NOT_AVAILABLE


class Requests:
    """Open supervisor requests, one per call (in memory; the state is also saved on the Call row)."""

    def __init__(self) -> None:
        self.events: dict[int, asyncio.Event] = {}
        self.timers: dict[int, asyncio.Task] = {}

    def wake(self, call_id: int) -> None:
        if ev := self.events.get(call_id):
            ev.set()

    async def wait(self, call_id: int, seconds: float) -> None:
        ev = self.events.setdefault(call_id, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), seconds)
        except TimeoutError:
            pass

    def start_timer(self, call_id: int, seconds: float, on_timeout) -> None:
        async def run():
            await asyncio.sleep(seconds)
            try:
                on_timeout(call_id)
            except Exception:
                logger.exception("supervisor timeout for call %s failed", call_id)

        self.cancel_timer(call_id)
        self.events[call_id] = asyncio.Event()
        self.timers[call_id] = asyncio.create_task(run())

    def cancel_timer(self, call_id: int) -> None:
        if (t := self.timers.pop(call_id, None)) and not t.done() and t is not asyncio.current_task():
            t.cancel()

    def done(self, call_id: int) -> None:
        self.cancel_timer(call_id)
        self.wake(call_id)


def deadline(requested_at: datetime | None, wait_seconds: int) -> datetime | None:
    return as_utc(requested_at) + timedelta(seconds=wait_seconds) if requested_at else None
