"""Phase 6: hand interested leads to a supervisor ("ask first, then transfer"). Several supervisors since 2026-11-05.

  1. The AI uses the request_supervisor webhook tool (a short summary of the call so far).
  2. Every AVAILABLE supervisor (own login, own phone, own switch, own working hours) gets a pop-up with a sound,
     all at once: Accept / Busy.
  3. The tool answers "waiting" after a few seconds, so the AI says a filler line and asks again; the lead
     never sits in dead air. After wait_seconds without an Accept it counts as busy (timeout).
  4. The FIRST Accept wins: the tool's answer carries that supervisor's phone, and Cartesia saves it into
     {{transfer_number}} (the tool's "assignments"), so transfer_to_number rings the one who accepted. The pop-up
     closes for the others ("taken by X"). Busy from one leaves it open for the rest; all busy = declined.
     Busy / timeout / nobody available -> the AI asks when we can call back (record_outcome).

Settings live in data/supervisor.json (not agent_settings.yaml: flipping "available" must not create a new
Cartesia agent version):
    {"wait_seconds": 20, "people": [{"key": "<login name>", "name": "Sara", "phone": "+1...", "available": false,
                                     "hours": {...}}]}
The old one-supervisor file ({"phone", "available", "wait_seconds", "hours"}) is read as one person whose key is the
only login with the supervisor role (else "supervisor"); the next save writes the new form.
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
KEY = re.compile(r"^[A-Za-z0-9._-]{1,64}$")  # same as a login name (manage_users.py)
HOURS_ZONES = {"America/New_York": "US Eastern time", "UTC": "UTC"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WAIT_MIN_S, WAIT_MAX_S = 10, 60
MAX_PEOPLE = 10
DEFAULT_WAIT = 20
CHECK_WAIT_S = 5.0  # one request_supervisor call waits at most this long, then says "still waiting"

# Call.transfer_status values
WAITING, ACCEPTED, DECLINED, TIMEOUT, UNAVAILABLE = "waiting", "accepted", "declined", "timeout", "unavailable"
TRANSFERRING, TRANSFERRED, FAILED = "transferring", "transferred", "failed"
NOT_TAKEN = (DECLINED, TIMEOUT, UNAVAILABLE)
TRANSFER_LABELS = {WAITING: "Waiting for a supervisor", ACCEPTED: "Supervisor accepted", DECLINED: "Supervisors busy",
                   TIMEOUT: "No supervisor answered", UNAVAILABLE: "No supervisor available",
                   TRANSFERRING: "Transferring", TRANSFERRED: "Transferred", FAILED: "Transfer failed"}

DEFAULT_HOURS = {"enabled": False, "timezone": "America/New_York", "start": "09:00", "end": "17:00",
                 "days": ["mon", "tue", "wed", "thu", "fri"]}


class SupervisorError(Exception):
    pass


# ---- settings --------------------------------------------------------------------------

def _default_key() -> str:
    """The old single supervisor belongs to the only login with the supervisor role, if there's exactly one."""
    try:
        from app import auth

        sups = [n for n, u in auth.read_users_file().items() if u.get("role") == "supervisor"]
    except Exception:  # noqa: BLE001 - no users file: a placeholder key
        sups = []
    return sups[0] if len(sups) == 1 else "supervisor"


def _person(raw: dict) -> dict:
    hours = raw.get("hours") if isinstance(raw.get("hours"), dict) else {}
    p = {"key": str(raw.get("key") or "").strip(), "name": re.sub(r"\s+", " ", str(raw.get("name") or "")).strip(),
         "phone": str(raw.get("phone") or ""), "available": bool(raw.get("available", False)),
         "hours": {**DEFAULT_HOURS, **{k: v for k, v in hours.items() if k in DEFAULT_HOURS}}}
    p["name"] = p["name"] or p["key"]
    return p


def load() -> dict:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))  # -sig: also a file saved by Notepad
    except (OSError, ValueError):
        raw = None
    old_file = isinstance(raw, dict) and "people" not in raw
    if not isinstance(raw, dict):  # no file yet: one supervisor, off, on the test phone (as before)
        raw = {"phone": os.getenv("TEST_CALL_NUMBER", "")}
    if "people" not in raw:  # the old one-supervisor file
        key = _default_key()
        people = [_person({**raw, "key": key, "name": "Supervisor" if key == "supervisor" else key})]
    else:
        people = [_person(p) for p in raw.get("people") or [] if isinstance(p, dict) and p.get("key")]
    try:
        wait = int(raw.get("wait_seconds", DEFAULT_WAIT))
    except (TypeError, ValueError):
        wait = DEFAULT_WAIT
    out = {"wait_seconds": wait, "people": people}
    if old_file:  # save the new form at once: the owner must not change when a 2nd supervisor login is added later
        try:
            tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
            tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
            os.replace(tmp, SETTINGS_PATH)
            logger.info("supervisor.json: moved to the several-supervisors form (owner: %s)", people[0]["key"])
        except OSError:
            pass
    return out


def _check_person(p: dict) -> dict:
    p = _person(p)
    if not KEY.match(p["key"]):
        raise SupervisorError("The login name must be letters, numbers, dots, dashes or underscores.")
    p["name"] = p["name"][:60]
    p["phone"] = re.sub(r"[\s().-]", "", p["phone"])
    if p["phone"] and not E164.match(p["phone"]):
        raise SupervisorError(f"{p['name']}: the phone must be in +1XXXXXXXXXX format.")
    h = p["hours"]
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
    return p


def _write(s: dict) -> dict:
    keys = [p["key"] for p in s["people"]]
    if len(set(keys)) != len(keys):
        raise SupervisorError("Each supervisor needs their own login name.")
    if len(keys) > MAX_PEOPLE:
        raise SupervisorError(f"At most {MAX_PEOPLE} supervisors.")
    if not WAIT_MIN_S <= s["wait_seconds"] <= WAIT_MAX_S:
        raise SupervisorError(f"Wait time must be {WAIT_MIN_S}-{WAIT_MAX_S} seconds.")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)
    state = public_state()
    try:
        bus.publish({"type": "supervisor_settings", "settings": state})
    except RuntimeError:  # no event loop (manage_users.py): no dashboard to tell
        pass
    return state


def save_wait(wait_seconds) -> dict:
    s = load()
    try:
        s["wait_seconds"] = int(wait_seconds)
    except (TypeError, ValueError):
        raise SupervisorError("Wait time must be a number of seconds.") from None
    s["people"] = [_check_person(p) for p in s["people"]]
    return _write(s)


def save_person(key: str, values: dict) -> dict:
    """Create or change one supervisor (admin; also manage_users.py). values: key (rename), name, phone, available,
    hours. Raises SupervisorError (plain words)."""
    s = load()
    found = next((p for p in s["people"] if p["key"] == key), None)
    p = dict(found) if found else _person({"key": key})
    for k in ("name", "phone", "available"):
        if values.get(k) is not None:
            p[k] = values[k]
    if isinstance(values.get("hours"), dict):
        p["hours"] = {**p["hours"], **{k: v for k, v in values["hours"].items() if k in DEFAULT_HOURS}}
    p["key"] = str(values.get("key") or key).strip()
    p = _check_person(p)
    s["people"] = [p if x["key"] == key else x for x in s["people"]] if found else [*s["people"], p]
    return _write(s)


def remove_person(key: str) -> dict:
    s = load()
    if not any(p["key"] == key for p in s["people"]):
        raise SupervisorError(f"No supervisor with the login {key}.")
    s["people"] = [p for p in s["people"] if p["key"] != key]
    return _write(s)


def set_available(key: str, available: bool) -> dict:
    if not person(key):
        raise SupervisorError(f"{key} isn't on the supervisor list. An admin adds them on the Supervisor page.")
    return save_person(key, {"available": bool(available)})


def person(key: str | None, s: dict | None = None) -> dict | None:
    if not key:
        return None
    return next((p for p in (s or load())["people"] if p["key"] == key), None)


def phones() -> list[str]:
    return [p["phone"] for p in load()["people"] if p["phone"]]


# ---- who can take a call now -----------------------------------------------------------

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


def person_availability(p: dict, now_utc: datetime | None = None) -> tuple[bool, str]:
    """(this supervisor can take a transfer now, why not in plain words)."""
    if not p["phone"]:
        return False, "no phone number set"
    if not p["available"]:
        return False, "switched to unavailable"
    if not in_hours(p["hours"], now_utc or utcnow()):
        return False, "outside their working hours"
    return True, ""


def available_people(now_utc: datetime | None = None, s: dict | None = None) -> list[dict]:
    """Everyone who can take a transfer right now, in list order."""
    return [p for p in (s or load())["people"] if person_availability(p, now_utc)[0]]


def availability(now_utc: datetime | None = None, s: dict | None = None) -> tuple[bool, str]:
    """(anyone can take a transfer now, why nobody can, in plain words)."""
    s = s or load()
    if not s["people"]:
        return False, "no supervisors set up"
    if available_people(now_utc, s):
        return True, ""
    return False, "; ".join(f"{p['name']}: {person_availability(p, now_utc)[1]}" for p in s["people"])


def public_state() -> dict:
    s = load()
    ok, why = availability(s=s)
    people = [{**p, "can_take_calls": (pa := person_availability(p))[0], "why_not": pa[1]} for p in s["people"]]
    return {"wait_seconds": s["wait_seconds"], "people": people, "can_take_calls": ok, "why_not": why,
            "hours_zones": HOURS_ZONES, "wait_min": WAIT_MIN_S, "wait_max": WAIT_MAX_S}


def call_variables(now_utc: datetime | None = None) -> dict[str, str]:
    """Dynamic variables for one call. transfer_number starts as the first available supervisor's phone (a valid
    E.164 number or empty); an Accept replaces it during the call (request_supervisor's assignment)."""
    s = load()
    avail = available_people(now_utc, s)
    first = avail[0]["phone"] if avail else next((p["phone"] for p in s["people"] if p["phone"]), "")
    return {"transfer_number": first, "supervisor_status": "available" if avail else "unavailable"}


# ---- what the AI hears back from request_supervisor -----------------------------------

REPLY_AVAILABLE = ("AVAILABLE. Our senior specialist is free right now. Say the connecting line from your instructions "
                   "and use the transfer tool in the same turn.")
REPLY_WAITING = ("WAITING. No answer from our senior specialist yet. Say one short, natural line to keep them on the "
                 "line, like \"Thanks for bearing with me, just a moment.\", then use request_supervisor again.")
REPLY_NOT_AVAILABLE = ("NOT AVAILABLE. Our senior specialist can't take the call right now. Follow \"If the senior "
                       "specialist can't take the call\" in your instructions.")
# The tool's assignment (cartesia_setup): this field of our JSON answer -> the {{transfer_number}} variable
ASSIGNMENTS = [{"dynamic_variable": "transfer_number", "value_path": "transfer_number"}]


def reply_for(status: str, phone: str = "") -> dict:
    """request_supervisor's answer (JSON). AVAILABLE carries the accepting supervisor's phone; Cartesia saves it into
    {{transfer_number}}, so the transfer rings that person. Without the field the variable keeps its value."""
    if status in (ACCEPTED, TRANSFERRING, TRANSFERRED):
        out = {"answer": "AVAILABLE", "what_to_do": REPLY_AVAILABLE}
        if phone:
            out["transfer_number"] = phone
        return out
    if status == WAITING:
        return {"answer": "WAITING", "what_to_do": REPLY_WAITING}
    return {"answer": "NOT AVAILABLE", "what_to_do": REPLY_NOT_AVAILABLE}


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
