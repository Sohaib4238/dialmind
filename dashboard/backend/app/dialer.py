"""Auto-dialer: calls leads one at a time while it's ON (Leads page switch).

Order: callbacks that are due, then status "new" (sheet order: top to bottom, first tab first; or newest
first: the dialer_order setting, sheet_sync.queue_order), then retries that are due (oldest last call
first). Never calls a Do-Not-Contact lead, a lead handled by sales, or one that's already being called. A lead is only
called inside calling hours in its own time zone (zip / state in its location, else the area
code; unknown -> skipped).

Safety:
- Always starts OFF (also after a backend restart); stops after max_calls_per_session.
- Pauses after pause_after_failures failed calls in a row, and stops when the estimated free
  Cartesia minutes left this month (plan dollars / agent rate) drop below min_free_minutes.
- TEST MODE (default ON, app/test_mode.py): the dialer walks the real leads list (statuses/history
  update on the real lead) but every call dials TEST_CALL_NUMBER. It's enforced for every call in
  CallManager.start_call and cartesia.create_call, not here.
"""

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta

from sqlmodel import select

from app import health, rejections, sheet_sync, test_mode
from app.area_codes import zone
from app.timezones import lead_timezone
from app.bus import bus
from app.calls import FINAL, CallBusy, CallError
from app.db import Call, Lead, as_utc, get_session, utcnow
from app.leads import format_phone
from settings import SettingsError, load_settings

logger = logging.getLogger("dashboard.dialer")

GAP_BETWEEN_CALLS_S = 5  # breathing room after a call ends
IDLE_RECHECK_S = 30  # nobody to call right now: look again after this
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _in_hours(lead: Lead, hours, now_utc: datetime) -> tuple[bool | None, str]:
    """(inside calling hours?, local time label). None = the lead's time zone is unknown."""
    tz_name, _ = lead_timezone(lead.location, lead.phone)
    if tz_name is None:
        return None, "time zone unknown"
    local = now_utc.astimezone(zone(tz_name))
    day_ok = DAYS[local.weekday()] in hours.days
    hhmm = local.strftime("%H:%M")
    return day_ok and hours.start <= hhmm < hours.end, f"{hhmm} {local.strftime('%a')} ({tz_name.split('/')[-1].replace('_', ' ')})"


def _slot(lead: Lead, at_utc: datetime) -> str | None:
    """'morning' / 'afternoon' in the lead's own time zone (voicemail retries alternate)."""
    tz_name, _ = lead_timezone(lead.location, lead.phone)
    return None if tz_name is None else ("morning" if at_utc.astimezone(zone(tz_name)).hour < 12 else "afternoon")


def pick_next(now_utc: datetime | None = None) -> tuple[Lead | None, dict]:
    """The next lead to call, plus counts explaining the queue.
    Order: callbacks that are due (they asked for it), new leads, then retries (oldest call first).
    Interested / not interested / bad number / needs review / not a fit / DNC are never called again."""
    s = load_settings()
    d, hours = s.dialer, s.settings.calling_hours
    now_utc = now_utc or utcnow()
    gap = timedelta(hours=d.retry_gap_hours)

    def gap_ok(x: Lead) -> bool:
        return not x.last_call_at or now_utc - as_utc(x.last_call_at) >= gap

    with get_session() as db:
        # never DNC, never a lead the sales team has (green/yellow row in their sheet)
        leads = db.exec(select(Lead).where(Lead.dnc == False, Lead.sales_handled == False)).all()  # noqa: E712
    callbacks = [x for x in leads if x.status == "callback" and x.callback_at]
    cb_due = sorted((x for x in callbacks if as_utc(x.callback_at) <= now_utc), key=lambda x: as_utc(x.callback_at))
    new = sheet_sync.queue_order([x for x in leads if x.status == "new"])
    retry_ok = [x for x in leads if x.status in d.retry_statuses and x.attempts < 1 + d.max_retries]
    # voicemail: up to voicemail_retries more tries, each at another time of day than the last one
    vm_ok = [x for x in leads if x.status == "voicemail" and x.vm_count <= d.voicemail_retries]
    due = [x for x in retry_ok if gap_ok(x)]
    due += [x for x in vm_ok if gap_ok(x) and (not x.last_call_at or _slot(x, now_utc) != _slot(x, as_utc(x.last_call_at)))]
    due.sort(key=lambda x: as_utc(x.last_call_at) or now_utc)
    info = {"new": len(new), "retries_due": len(due), "retries_waiting": len(retry_ok) + len(vm_ok) - len(due),
            "callbacks_due": len(cb_due), "callbacks_waiting": len(callbacks) - len(cb_due), "outside_hours": 0,
            "unknown_tz": 0}
    for lead in cb_due + new + due:
        ok, local = _in_hours(lead, hours, now_utc)
        if ok:
            return lead, {**info, "local_time": local}
        info["unknown_tz" if ok is None else "outside_hours"] += 1
    return None, info


def _why_idle(info: dict) -> str:
    if not info["new"] and not info["retries_due"] and not info.get("callbacks_due"):
        waiting = []
        if info["retries_waiting"]:
            waiting.append(f"{info['retries_waiting']} retr{'y' if info['retries_waiting'] == 1 else 'ies'}")
        if info.get("callbacks_waiting"):
            waiting.append(f"{info['callbacks_waiting']} callback{'' if info['callbacks_waiting'] == 1 else 's'}")
        if waiting:
            return f"Nothing to call right now: {' and '.join(waiting)} not due yet"
        return "Nothing to call: no new leads and no retries left"
    if info.get("unknown_tz") and not info["outside_hours"]:
        return f"Nothing to call: {info['unknown_tz']} lead(s) have no known time zone (add a zip or state to the location)"
    tail = f"; {info['unknown_tz']} with no known time zone" if info.get("unknown_tz") else ""
    return f"Nothing to call right now: {info['outside_hours']} lead(s) outside calling hours in their time zone{tail}"


class AutoDialer:
    def __init__(self, manager) -> None:
        self.manager = manager
        self.state = "off"  # off / on / paused
        self.reason = ""
        self.alert = ""  # a red warning on the dialer panel (calls refused, Telnyx balance); cleared on turn-on
        self.session_calls = 0
        self.failures_in_row = 0
        self.current: dict | None = None
        self.wait_detail = ""
        self.log: deque[dict] = deque(maxlen=200)
        self._task: asyncio.Task | None = None
        rejections.listeners.append(self.stop_for_rejections)

    # ---- state ---------------------------------------------------------------------

    @property
    def test_mode(self) -> bool:
        return test_mode.is_on()

    def _note(self, text: str, level: str = "info") -> None:
        self.log.append({"ts": time.time(), "text": text, "level": level})
        logger.info("dialer: %s", text)
        self.publish()

    def snapshot(self) -> dict:
        try:
            s = load_settings()
            d, hours = s.dialer, s.settings.calling_hours
            limits = {"max_calls": d.max_calls_per_session, "max_retries": d.max_retries, "retry_gap_hours": d.retry_gap_hours,
                      "pause_after_failures": d.pause_after_failures, "min_free_minutes": d.min_free_minutes,
                      "hours": f"{hours.start}-{hours.end}", "days": hours.days}
            nxt, info = pick_next() if self.current is None else (None, {})
        except SettingsError as e:
            limits, nxt, info = {"error": str(e)}, None, {}
        return {
            "state": self.state,
            "reason": self.reason,
            "alert": self.alert,
            "test_mode": self.test_mode,
            "test_number": test_mode.number(),
            "session_calls": self.session_calls,
            "failures_in_row": self.failures_in_row,
            "current": self.current,
            "next": ({"lead_id": nxt.id, "name": nxt.name, "phone": nxt.phone, "status": nxt.status,
                      "attempts": nxt.attempts, "local_time": info.get("local_time")} if nxt else None),
            "queue": info,
            "wait_detail": self.wait_detail,
            "minutes_left": health.minutes_left(),
            "limits": limits,
            "order": sheet_sync.load()["dialer_order"],
            "orders": sheet_sync.DIALER_ORDERS,
            "log": list(self.log)[-60:],
        }

    def publish(self) -> None:
        bus.publish({"type": "dialer", "dialer": self.snapshot()})

    # ---- controls ------------------------------------------------------------------

    def turn_on(self) -> dict:
        if self.state == "on":
            return self.snapshot()
        self.state, self.reason, self.alert = "on", "", ""
        self.session_calls = 0
        self.failures_in_row = 0
        self._note(f"Dialer ON{' (TEST MODE: every call goes to ' + format_phone(test_mode.number()) + ')' if self.test_mode else ''}")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            self._task.add_done_callback(self._loop_done)
        return self.snapshot()

    def turn_off(self, reason: str = "Turned off") -> dict:
        if self.state != "off":
            self.state, self.reason = "off", reason
            tail = " (the call in progress finishes normally)" if self.current else ""
            self._note(f"Dialer OFF: {reason}{tail}")
        return self.snapshot()

    def stop_for_rejections(self, why: str) -> None:
        """rejections.py: calls are being refused before they ring. Off (also shown when it was already off)."""
        self.alert = why
        self.failures_in_row = max(self.failures_in_row, rejections.STREAK)
        if self.state != "off":
            self.state, self.reason = "off", "Calls are being refused (see the red warning)"
        self._note(f"Dialer OFF: {why}", "error")

    def _stop_alert(self, why: str) -> None:
        self.alert = why
        self.turn_off(why)
        self.log[-1]["level"] = "error"
        self.publish()

    def _pause(self, reason: str) -> None:
        self.state, self.reason = "paused", reason
        self._note(f"Paused: {reason}", "warn")

    def set_test_mode(self, on: bool) -> dict:
        if self.state != "off" and not on:
            raise CallError("Turn the dialer off before turning test mode off.")
        test_mode.set_on(on)
        self._note("Test mode ON: every call goes to the test number" if on else "Test mode OFF: leads are called at their own numbers", "info" if on else "warn")
        return self.snapshot()

    def _loop_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logger.error("dialer loop crashed", exc_info=task.exception())
            self.state, self.reason = "paused", f"Dialer error: {task.exception()}"
            self._note(self.reason, "error")

    # ---- the loop ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self.state == "on":
            if self.manager.active_call():  # a call (ours or a manual one) is still going
                self._set_wait("Waiting for the current call to end")
                await asyncio.sleep(1)
                continue
            try:
                d = load_settings().dialer
            except SettingsError as e:
                self._pause(f"Settings file invalid: {e}")
                break
            if self.session_calls >= d.max_calls_per_session:
                self.turn_off(f"Reached max calls this session ({d.max_calls_per_session})")
                break
            await health.refresh_usage()  # Cartesia's real usage when there's an admin key (at most every 5 min)
            left = health.minutes_left()
            if left < d.min_free_minutes:
                self.turn_off(f"Only ~{left} Cartesia minutes left this month (stops below {d.min_free_minutes})")
                break
            await health.refresh_telnyx()  # at most every 5 min
            bal = health.telnyx_balance()
            if bal is not None and bal < health.TELNYX_STOP_BELOW:
                self._stop_alert(f"Telnyx balance is ${bal:.2f} (stops below ${health.TELNYX_STOP_BELOW:.2f}). At $0 Telnyx "
                                 "switches the account off and every call is refused. Top up in the Telnyx portal.")
                break
            lead, info = pick_next()
            if lead is None:
                self._set_wait(_why_idle(info))
                for _ in range(IDLE_RECHECK_S):
                    if self.state != "on":
                        break
                    await asyncio.sleep(1)
                continue
            await self._call(lead, info, d)
            for _ in range(GAP_BETWEEN_CALLS_S):
                if self.state != "on":
                    break
                await asyncio.sleep(1)
        self.current = None
        self.wait_detail = ""
        self.publish()

    def _set_wait(self, text: str) -> None:
        if text != self.wait_detail:
            self.wait_detail = text
            if not text.startswith("Waiting for the current call"):
                self._note(text)
            else:
                self.publish()

    async def _call(self, lead: Lead, info: dict, d) -> None:
        dial = test_mode.number() if self.test_mode else lead.phone  # for the log; start_call enforces it
        who = lead.name or format_phone(lead.phone)
        if self.test_mode and not dial:
            self._pause("TEST MODE is on but TEST_CALL_NUMBER is not set in .env")
            return
        attempt = lead.attempts + 1
        self.wait_detail = ""
        try:
            call = await self.manager.start_call(dial, lead_id=lead.id)
        except CallBusy:
            await asyncio.sleep(2)
            return
        except CallError as e:
            self.failures_in_row += 1
            self._note(f"Couldn't call {who}: {e}", "error")
            self._check_failures(d)
            return
        self.session_calls += 1
        self.current = {"lead_id": lead.id, "name": lead.name, "phone": lead.phone, "dialed": dial,
                        "call_id": call.id, "attempt": attempt, "local_time": info.get("local_time")}
        via = f" via test number {format_phone(dial)}" if self.test_mode else ""
        self._note(f"Calling {who} ({format_phone(lead.phone)}, attempt {attempt}, {info.get('local_time')}){via}")
        result, reason = await self._wait_for_end(call.id)
        self.failures_in_row = self.failures_in_row + 1 if result in ("failed", rejections.RESULT) else 0
        self._note(f"{who}: {result or 'ended'}{f' ({reason})' if reason else ''}", "error" if result == "failed" else "info")
        self.current = None
        self._check_failures(d)

    def _check_failures(self, d) -> None:
        if self.state == "on" and self.failures_in_row >= d.pause_after_failures:
            self._pause(f"{self.failures_in_row} failed calls in a row. Check Health and the last call's Call debug, then turn it on again.")

    async def _wait_for_end(self, call_id: int) -> tuple[str | None, str]:
        while True:
            with get_session() as s:
                c = s.get(Call, call_id)
            if c is None or c.status in FINAL:
                return (c.result if c else "failed"), (c.reason if c else "call disappeared")
            await asyncio.sleep(1)
