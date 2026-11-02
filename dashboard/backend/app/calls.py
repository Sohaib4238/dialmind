"""Call manager: Cartesia outbound calls started from the dashboard, plus simulated calls.

A call's progress reaches us two ways, both fed into ingest_call() (idempotent):
  - Cartesia webhooks (call_started / call_turn / call_completed / call_failed) via the tunnel
  - polling GET /agents/calls/{id} every few seconds (fallback if the tunnel is down)
Every change is stored in SQLite (Call / CallEvent) and pushed to the live bus (SSE).
Call.room holds Cartesia's agent_call_id.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import select

from app import cartesia, cartesia_setup, emails, leads, outcomes, sms, storage, supervisor, test_mode
from app.agent_config import EMAIL_REASONS, EMAIL_TOOL_NAME, lead_variables, silent_greeting, supervisor_enabled, time_variables
from app.bus import bus
from app.db import Call, CallEvent, Lead, as_utc, call_to_dict, event_to_dict, get_session, utcnow
from app.timezones import lead_timezone
from settings import SettingsError, load_settings  # shared/settings.py

logger = logging.getLogger("dashboard.calls")

ACTIVE = ("dialing", "ringing", "answered")
FINAL = ("ended", "failed")
E164 = re.compile(r"^\+[1-9]\d{1,14}$")
POLL_EVERY_S = 1.0
POLL_GIVE_UP_S = 45 * 60
RINGING_TIMEOUT_S = 50  # long enough for the carrier's voicemail to pick up first
# Hard cap per call on top of agent_settings' max_call_minutes (free plan: ~17 min/month).
MAX_CALL_MINUTES_CAP = int(os.getenv("CARTESIA_MAX_CALL_MINUTES", "2"))
RECORDINGS_DIR = storage.RECORDINGS_DIR
HANGUP_CONFIRM_S = 10
# Cartesia (SIP trunk) ignores the carrier's BYE when the callee hangs up, and end_call doesn't close
# the line either; only the cancel API does. So the backend closes the line itself:
AFTER_GOODBYE_CLOSE_S = 1.5  # after the goodbye's audio has finished (_line_done_at)
GOODBYE_WAIT_MAX_S = 12  # end_call seen but no goodbye line arrives (a line only reaches us once it's said): close after this
# Silence handling comes from agent_settings.yaml `silence` (read when the call starts):
# Cartesia checks in after silence_check_seconds (the agent says check_message, then close_message +
# end_call); we close the line after the second check-in, or at max_silence_seconds at the latest.
# Voicemail: Cartesia's voicemail tool (like end_call) doesn't close the line on our SIP trunk, so we
# close it ourselves: right away with no voicemail message (voicemail.leave_message off), or just after the
# AI has said the message (we see it in the transcript and estimate how long it takes to say).
VOICEMAIL_CLOSE_S = 1.0
VM_MESSAGE_START_WAIT_S = 8  # message on, greeting heard: time for the beep + the AI to start the message
VM_MESSAGE_MATCH = 0.5  # share of the message's words an agent turn must contain to count as the message
CHECKIN_SLACK_S = 1.5  # an agent turn after >= (silence_check_seconds - this) of quiet = a check-in
CLOSE_AFTER_SECOND_CHECKIN_S = 6  # time for the closing line + end_call before we cut it ourselves
LINE_STILL_OPEN_S = 10  # after our own cancel, still live this long -> warn
BREAK_TAG_RE = re.compile(r"<break\b[^>]*>")

# Cartesia end_reason -> (our status, result). Others: "ended", result from whether it was answered.
END_REASONS = {
    "voicemail_detected": ("ended", "voicemail"),
    "dial_no_answer": ("ended", "no_answer"),
    "dial_timeout": ("ended", "no_answer"),
    "dial_busy": ("ended", "busy"),
    "dial_failed": ("failed", "failed"),
    "concurrency_limit": ("failed", "failed"),
    "agent_error": ("failed", "failed"),
    "network_error": ("failed", "failed"),
    "config_error": ("failed", "failed"),
    "error": ("failed", "failed"),
}
END_LABELS = {
    "voicemail_detected": "Reached voicemail",
    "dial_no_answer": "No answer",
    "dial_timeout": "No answer (ringing timed out)",
    "dial_busy": "Line busy",
    "dial_failed": "Dialing failed",
    "client_hangup": "Callee hung up",
    "agent_hangup": "Agent ended the call",
    "api_cancelled": "Hung up from dashboard",
    "max_duration": "Max call length reached",
    "call_inactivity": "Ended after silence",
    "client_inactivity": "Ended after silence",
    "client_disconnected": "Callee disconnected",
    "concurrency_limit": "Cartesia concurrency limit reached",
    "agent_error": "Cartesia agent error",
    "network_error": "Network error",
    "config_error": "Agent configuration error",
    "error": "Cartesia error",
}


class CallBusy(Exception):
    pass


class HangupFailed(Exception):
    """Cartesia accepted (or refused) the hang-up, but the call is still connected."""


class CallError(Exception):
    """Problem found before dialing (bad number, config, Cartesia refused)."""


def _publish_call(call: Call) -> None:
    bus.publish({"type": "call", "call": call_to_dict(call)})


def _add_event(call_id: int, kind: str, role: str | None, text: str, data: dict | None = None) -> dict:
    with get_session() as s:
        ev = CallEvent(call_id=call_id, kind=kind, role=role, text=text, data=data or {})
        s.add(ev)
        s.commit()
        s.refresh(ev)
    d = event_to_dict(ev)
    bus.publish({"type": "event", "event": d})
    return d


def _debug(call_id: int, step: str, detail: str = "", **data) -> None:
    _add_event(call_id, "debug", "system", step, {"detail": detail, **data})


def apply_status(call_id: int, status: str, detail: str = "", data: dict | None = None,
                 result: str | None = None, transcript: list[dict] | None = None) -> Call:
    data = data or {}
    with get_session() as s:
        call = s.get(Call, call_id)
        if call.status in FINAL:
            return call  # already final
        call.status = status
        now = utcnow()
        if status == "answered" and not call.answered_at:
            call.answered_at = now
        if status in FINAL:
            call.ended_at = now
            call.reason = detail
            call.result = result or ("answered" if call.answered_at else "no_answer" if status == "ended" else "failed")
            if call.answered_at:
                call.duration_s = round((now - as_utc(call.answered_at)).total_seconds(), 1)
            _set_outcome(call, transcript or [], now)
        s.add(call)
        s.commit()
        s.refresh(call)
    _add_event(call_id, "status", "system", status, {"detail": detail, **data})
    if status in FINAL:
        _debug(call_id, "outcome", outcomes.label(call.outcome), outcome=call.outcome, source=call.outcome_source,
               note=call.outcome_note or None, callback_at=call_to_dict(call)["callback_at"])
    _publish_call(call)
    if status in FINAL:
        leads.on_call_finished(call)
        emails.after_call(call_id)  # follow-up email (background), if the outcome calls for one
        sms.after_call(call_id)  # follow-up text (background / queued), if the outcome calls for one
    return call


def _set_outcome(call: Call, transcript: list[dict], now) -> None:
    """The call's final outcome (outcomes.py priority), plus a default callback time if needed."""
    if call.transfer_status == supervisor.WAITING:  # the call ended before the supervisor answered
        call.transfer_status, call.transfer_note = supervisor.TIMEOUT, "the call ended while waiting for the supervisor"
    elif call.transfer_status == supervisor.TRANSFERRING:
        call.transfer_status = supervisor.TRANSFERRED
    outcome, source, note = outcomes.decide(call.outcome, call.outcome_source, call.result, transcript,
                                            call.transfer_status)
    if (outcome, source) != (call.outcome, call.outcome_source):
        call.outcome, call.outcome_source = outcome, source
        if note:
            call.outcome_note = note
    if outcome in outcomes.CALLBACK_OUTCOMES and not call.callback_at:
        call.callback_at = _callback_from_call(call, transcript, now)
    if outcome in outcomes.CALLBACK_OUTCOMES and not call.callback_at:
        try:
            days = load_settings().dialer.callback_default_days
        except SettingsError:
            days = 3
        call.callback_at = outcomes.default_callback(now, days)
    elif outcome not in outcomes.CALLBACK_OUTCOMES:
        call.callback_at = None


def _calling_hours():
    try:
        return load_settings().settings.calling_hours
    except SettingsError:
        return None


def _callback_from_call(call: Call, transcript: list[dict], now) -> datetime | None:
    """The AI gave no callback time, but the day may still be in its note or the lead's last words ("call me tomorrow")."""
    lead = leads.get_lead_row(call.lead_id) if call.lead_id else None
    tz_name, _ = lead_timezone(lead.location if lead else "", lead.phone if lead else call.number)
    hours = _calling_hours()
    for text in [call.outcome_note, *reversed(outcomes._callee_lines(transcript)[-3:])]:
        if at := outcomes.callback_from_words(text, tz_name, now, hours):
            return at
    return None


def _silence_settings() -> dict:
    try:
        s = load_settings()
        q = s.silence
        return {"enabled": q.enabled, "check_s": q.silence_check_seconds, "max_s": q.max_silence_seconds,
                "greet_wait_s": s.call_start.wait_seconds if s.call_start.greet_if_silent else 0}
    except SettingsError:
        return {"enabled": True, "check_s": 8, "max_s": 25, "greet_wait_s": 0}


def _voicemail_message(lead_id: int | None) -> str:
    """The voicemail message as the AI should say it on this call, "" when leave_message is off."""
    try:
        s = load_settings()
    except SettingsError:
        return ""
    if not s.voicemail.leave_message:
        return ""
    lead = leads.get_lead_row(lead_id) if lead_id else None
    return " ".join(s.fill(s.voicemail.message, lead=lead_variables(lead)).split())


def speech_seconds(text: str) -> float:
    """Roughly how long a line takes to say (~14 characters a second)."""
    return min(30.0, len((text or "").strip()) / 14)


_WORD_RE = re.compile(r"[a-z0-9']{3,}")


def is_voicemail_message(text: str, expected: str) -> bool:
    """Does this agent line say our voicemail message (in roughly the same words)?"""
    want = set(_WORD_RE.findall((expected or "").lower()))
    got = set(_WORD_RE.findall((text or "").lower()))
    return len(want) >= 5 and len(want & got) / len(want) >= VM_MESSAGE_MATCH


def _parse_time(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _tool_args(raw) -> dict:
    """A tool call's arguments from the transcript: a dict, or JSON text."""
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _outcome_reply(outcome: str, callback_at: datetime | None, tz_name: str | None) -> str:
    """What the AI hears back from record_outcome (short: it only confirms)."""
    if outcome in outcomes.AI_CALLBACK_OUTCOMES:
        if callback_at:
            t = callback_at.astimezone(ZoneInfo(tz_name or "America/New_York"))
            clock = f"{t.hour % 12 or 12}" + (f":{t.minute:02d}" if t.minute else "") + (" AM" if t.hour < 12 else " PM")
            return f"Saved. Callback {t:%A %B} {t.day} at {clock} their time."
        if outcome == "interested":
            return "Saved."
        return "Saved. We'll call back in a few days."
    return {"do_not_contact": "Saved. They won't be called again.",
            "wrong_number": "Saved. This number won't be called again."}.get(outcome, "Saved.")


class _Tracker:
    """What we've already recorded for one Cartesia call (so webhook + poll don't duplicate)."""

    def __init__(self) -> None:
        self.cartesia_status: str | None = None
        self.seen_turns: set[tuple] = set()
        self.seen_tools: set[str] = set()
        self.answered = False
        self.final = False
        self.poll_errors = 0
        self.close_requested_at: float | None = None  # agent end_call seen, or hang-up sent
        self.close_reason = ""
        self.line_open_warned = False
        self.manual_hangup = False
        self.started_at: float | None = None  # loop time when Cartesia said "started"
        self.last_turn_at: float | None = None  # loop time we received the latest turn
        self.last_turn_end_s: float | None = None  # call-relative end of the latest turn
        self.checkin_at: float | None = None  # latest agent check-in after silence
        self.unanswered_checkins = 0  # check-ins since the callee last spoke
        self.quiet_since: float | None = None  # last callee words or normal agent reply (not a check-in)
        self.agent_spoke = False  # the first agent line after the wait is the silent greeting, not a check-in
        self.silence: dict = {}  # enabled / check_s / max_s, from settings at call start
        self.close_kind = ""  # goodbye / silence / carrier / voicemail
        self.voicemail_at: float | None = None  # voicemail tool used, or a voicemail greeting heard
        self.voicemail_why = ""
        self.vm_message = ""  # the voicemail message the AI should say on this call ("" = none: hang up at once)
        self.vm_msg_done_at: float | None = None  # loop time the AI should have finished saying it
        self.end_call_at: float | None = None
        self.agent_line_done_at: float | None = None  # when the AI's latest line ends, unless the callee spoke since
        self.goodbye_done_at: float | None = None  # when the goodbye's audio ends; None = no goodbye seen yet
        self.auto_cancel_at: float | None = None
        self.auto_close_label = ""
        self.supervisor_waiting = False  # request_supervisor open: no silence hang-up meanwhile
        self.transfer_at: float | None = None  # transfer tool seen: never close the line ourselves after this
        self.transfer_turn_s: float | None = None  # call-relative time of the transfer turn
        self.transfer_failed = False
        self.email_typo_checked: set[str] = set()  # addresses the AI was asked to double-check (collect_email)


def _seed_tracker(call_id: int) -> _Tracker:
    """Rebuild what's already recorded for a call (backend restart, late webhook) from the DB."""
    tr = _Tracker()
    with get_session() as s:
        call = s.get(Call, call_id)
        events = s.exec(select(CallEvent).where(CallEvent.call_id == call_id)).all()
    for e in events:
        d = e.data or {}
        if e.kind == "transcript":
            cart_role = {"agent": "assistant", "callee": "user"}.get(e.role or "", e.role)
            tr.seen_turns.add((cart_role, d.get("start_s"), (e.text or "")[:60]))
        elif e.kind == "debug" and e.text == "tool_call" and d.get("tool_id"):
            tr.seen_tools.add(d["tool_id"])
        elif e.kind == "debug" and e.text == "cartesia_status":
            tr.cartesia_status = d.get("detail")
    if call is not None:
        tr.answered = bool(call.answered_at)
        tr.final = call.status in FINAL
        if call.transfer_status in (supervisor.TRANSFERRING, supervisor.TRANSFERRED):  # backend restarted mid-transfer
            tr.transfer_at = 0.0
        tr.supervisor_waiting = call.transfer_status == supervisor.WAITING
    return tr


class CallManager:
    def __init__(self) -> None:
        self.trackers: dict[int, _Tracker] = {}
        self.tasks: set[asyncio.Task] = set()
        self.seen_webhooks: dict[str, float] = {}
        self.sup = supervisor.Requests()

    def _tracker(self, call_id: int) -> _Tracker:
        if call_id not in self.trackers:
            self.trackers[call_id] = _seed_tracker(call_id)
            if len(self.trackers) > 200:  # keep memory bounded; finished calls re-seed from the DB
                for old in [k for k, t in self.trackers.items() if t.final][:100]:
                    self.trackers.pop(old, None)
        return self.trackers[call_id]

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.error("background task crashed", exc_info=task.exception())

    def active_call(self) -> Call | None:
        cutoff = utcnow() - timedelta(hours=1)
        with get_session() as s:
            calls = s.exec(select(Call).where(Call.status.in_(ACTIVE)).order_by(Call.id.desc())).all()
        return next((c for c in calls if as_utc(c.created_at) > cutoff), None)

    def mark_stale_calls(self) -> None:
        """Calls left 'in progress' by a previous backend run: resume following Cartesia ones."""
        with get_session() as s:
            stale = s.exec(select(Call).where(Call.status.in_(ACTIVE))).all()
        for c in stale:
            if c.source == "dashboard" and c.room.startswith("ac_"):
                # Re-check with Cartesia right away; the poller ends it if Cartesia says it's over.
                _debug(c.id, "recheck_after_restart", "Backend restarted: re-checking this call with Cartesia")
                self._spawn(self._poll(c.id, c.room))
            else:
                apply_status(c.id, "failed", "Dashboard restarted while the call was in progress")

    # ---- starting / ending ---------------------------------------------------------

    async def start_call(self, number: str, lead_id: int | None = None) -> Call:
        if self.active_call():
            raise CallBusy("A call is already in progress. Hang up first.")
        number = (number or "").strip()
        if not E164.match(number):
            raise CallError(f"Phone number must be E.164, like +15551234567 (got {number!r})")
        if blocked := leads.dnc_block_reason(number):
            raise CallError(blocked)  # Do-Not-Contact: never dial
        if lead_id is not None and (blocked := leads.dnc_block_reason_for_lead(lead_id)):
            raise CallError(blocked)  # the lead itself is DNC (test mode dials another number)
        if lead_id is None and (lead := leads.find_by_phone(number)):
            lead_id = lead.id  # a manual call to a lead's number still counts for that lead
        try:
            dial = test_mode.route(number)  # TEST MODE: every call (dialer, retry, callback, manual) -> test number
        except test_mode.TestModeError as e:
            raise CallError(str(e)) from None
        try:
            settings = load_settings()
        except SettingsError as e:
            raise CallError(f"Fix config/agent_settings.yaml first. {e}") from None
        agent_id, from_id = os.getenv("CARTESIA_AGENT_ID", ""), os.getenv("CARTESIA_FROM_NUMBER_ID", "")
        if not agent_id or not from_id:
            raise CallError("Cartesia isn't set up yet: run python dashboard\\backend\\setup_cartesia.py")

        try:
            sync = await cartesia_setup.sync_agent("before a call")  # pushes agent_settings.yaml if it changed
        except cartesia.CartesiaError as e:
            raise CallError(f"Couldn't update the Cartesia agent: {e}") from None

        with get_session() as s:
            call = Call(room=f"pending-{uuid.uuid4().hex[:10]}", number=dial, status="dialing", source="dashboard",
                        lead_id=lead_id)
            s.add(call)
            s.commit()
            s.refresh(call)
        _publish_call(call)
        leads.on_call_started(call)
        _debug(call.id, "agent_config", f"Agent config {sync['action']}", agent_id=sync["agent_id"], hash=sync["hash"])
        if dial != number:
            _debug(call.id, "test_mode", f"TEST MODE: dialing the test number {dial} instead of {number}",
                   requested=number, dialed=dial)

        max_minutes = max(1, min(settings.settings.max_call_minutes, MAX_CALL_MINUTES_CAP))
        lead_row = leads.get_lead_row(lead_id) if lead_id else None
        tz_name, _ = lead_timezone(lead_row.location if lead_row else "", lead_row.phone if lead_row else number)
        sup_vars = supervisor.call_variables()
        lead_vars = lead_variables(lead_row, settings)
        variables = {"caller_name": settings.caller_name, "company_name": settings.company_name,
                     **lead_vars, **time_variables(tz_name, utcnow()), **sup_vars,
                     "silent_greeting": silent_greeting(settings, lead_vars),
                     "lead_email": lead_row.email if lead_row else ""}
        if supervisor_enabled() and sup_vars["supervisor_status"] != "available":  # interested -> callback instead
            with get_session() as s:
                row = s.get(Call, call.id)
                row.transfer_status = supervisor.UNAVAILABLE
                row.transfer_note = supervisor.availability()[1]
                s.add(row)
                s.commit()
        try:
            agent_call_id = await cartesia.create_call(agent_id, from_id, dial, variables, RINGING_TIMEOUT_S, max_minutes)
        except cartesia.CartesiaError as e:
            _debug(call.id, "call_request_failed", str(e))
            apply_status(call.id, "failed", str(e), result="failed")
            raise CallError(str(e)) from None

        with get_session() as s:
            row = s.get(Call, call.id)
            row.room = agent_call_id
            s.add(row)
            s.commit()
            s.refresh(row)
        _publish_call(row)
        _debug(call.id, "call_requested", f"Cartesia call {agent_call_id} to {dial}", agent_call_id=agent_call_id,
               ringing_timeout_s=RINGING_TIMEOUT_S, max_call_minutes=max_minutes, variables=variables)
        self._spawn(self._poll(call.id, agent_call_id))
        return row

    async def hang_up(self, call_id: int, force: bool = False) -> dict:
        """Cancel the Cartesia call and wait until Cartesia confirms it's over.
        Raises HangupFailed with a clear message if the call is still connected."""
        with get_session() as s:
            call = s.get(Call, call_id)
        if call is None or call.status in FINAL:
            return {"ok": True, "ended": True, "detail": "Call already ended"}
        if call.transfer_status in (supervisor.TRANSFERRING, supervisor.TRANSFERRED) and not force:
            raise CallError("This call was transferred to the supervisor. Hanging up could cut him and the lead off. "
                            "Hang up anyway only if the line is really stuck.")
        if call.simulated:
            apply_status(call_id, "ended", "Hung up from dashboard")
            return {"ok": True, "ended": True}
        if not call.room.startswith("ac_"):
            apply_status(call_id, "failed", "Hung up before Cartesia accepted the call", result="failed")
            return {"ok": True, "ended": True}

        tr = self._tracker(call_id)
        tr.manual_hangup = True
        loop = asyncio.get_running_loop()
        _debug(call_id, "hangup_requested", "Hang up requested from dashboard")
        logger.info("hang up %s (%s)", call_id, call.room)
        try:
            resp = await cartesia.cancel_call(call.room)
            _debug(call_id, "cancel_response", "Cartesia accepted the hang-up", response=resp)
        except cartesia.CartesiaError as e:
            _debug(call_id, "cancel_response", f"Cartesia refused the hang-up: {e}", status=e.status, body=e.body)
            if e.status != 409:  # 409 = Cartesia says the call isn't active any more: confirm below
                raise HangupFailed(f"Cartesia refused the hang-up: {e}") from None
        tr.close_requested_at = tr.close_requested_at or loop.time()
        tr.close_reason = tr.close_reason or "hang-up from the dashboard"

        deadline = loop.time() + HANGUP_CONFIRM_S
        while loop.time() < deadline and not tr.final:
            try:
                self.ingest_call(call_id, await cartesia.get_call(call.room), source="hangup-check")
            except cartesia.CartesiaError:
                pass
            if not tr.final:
                await asyncio.sleep(1.0)
        if tr.final:
            return {"ok": True, "ended": True}
        msg = (f"Cartesia accepted the hang-up, but after {HANGUP_CONFIRM_S}s it still reports the call as live. "
               "The phone line is probably still open (the SIP hang-up isn't getting through between Cartesia and "
               f"Telnyx). It will be cut at the {MAX_CALL_MINUTES_CAP}-minute cap.")
        _debug(call_id, "hangup_not_effective", msg)
        _add_event(call_id, "log", "agent", msg, {"level": "error"})
        raise HangupFailed(msg)

    # ---- progress from Cartesia ----------------------------------------------------

    async def _poll(self, call_id: int, agent_call_id: str) -> None:
        tr = self._tracker(call_id)
        loop = asyncio.get_running_loop()
        started = loop.time()
        while not tr.final:
            try:
                data = await cartesia.get_call(agent_call_id)
                if tr.poll_errors:
                    _debug(call_id, "poll_recovered", f"Cartesia reachable again after {tr.poll_errors} failed checks")
                    tr.poll_errors = 0
                self.ingest_call(call_id, data, source="poll")
            except cartesia.CartesiaError as e:
                tr.poll_errors += 1
                if tr.poll_errors == 1:
                    _debug(call_id, "poll_error", "Couldn't read the call from Cartesia", error=str(e))
            if not tr.final:
                await self._auto_close(call_id, agent_call_id, tr)
            if not tr.final and loop.time() - started > POLL_GIVE_UP_S:
                apply_status(call_id, "failed", "Lost track of the call (no final status from Cartesia)", result="failed")
                break
            if not tr.final:
                await asyncio.sleep(POLL_EVERY_S)

    async def _close_line(self, call_id: int, agent_call_id: str, tr: _Tracker, label: str, step: str) -> None:
        """Hang up through Cartesia's API (the only hang-up that reaches the phone line)."""
        tr.auto_cancel_at = asyncio.get_running_loop().time()
        tr.auto_close_label = label
        try:
            resp = await cartesia.cancel_call(agent_call_id)
            _debug(call_id, step, label, response=resp)
        except cartesia.CartesiaError as e:
            if e.status != 409:  # 409 = already over
                _debug(call_id, step, f"{label}, but Cartesia refused: {e}", status=e.status)
        _add_event(call_id, "log", "agent", label, {"level": "info"})
        logger.info("call %s: %s", call_id, label)

    async def _auto_close(self, call_id: int, agent_call_id: str, tr: _Tracker) -> None:
        """Workarounds while Cartesia ignores the callee's hang-up (BYE) on SIP trunk calls."""
        if not tr.answered or tr.manual_hangup:
            return
        if tr.transfer_at is not None and not tr.transfer_failed:
            return  # handed to the supervisor: never cut the line (the lead may be talking to him now)
        now = asyncio.get_running_loop().time()
        if tr.auto_cancel_at is not None:  # we already closed it; warn if Cartesia still says live
            if not tr.line_open_warned and now - tr.auto_cancel_at >= LINE_STILL_OPEN_S:
                tr.line_open_warned = True
                msg = (f"Tried to close the line {now - tr.auto_cancel_at:.0f}s ago, but Cartesia still reports the call as live. "
                       f"It will be cut at the {MAX_CALL_MINUTES_CAP}-minute cap.")
                _debug(call_id, "line_still_open", msg)
                _add_event(call_id, "log", "agent", msg, {"level": "error"})
            return
        if tr.voicemail_at is not None:
            if tr.vm_msg_done_at is not None:  # the AI is saying our message: close right after it
                due, label = tr.vm_msg_done_at + VOICEMAIL_CLOSE_S, "Closed the line after leaving the voicemail message"
            else:
                due, label = tr.voicemail_at + VOICEMAIL_CLOSE_S, f"Closed the line: reached voicemail ({tr.voicemail_why})"
                if tr.vm_message and tr.voicemail_why == "voicemail greeting heard":
                    due = tr.voicemail_at + VM_MESSAGE_START_WAIT_S  # give the AI time to start the message
            if now >= due:
                tr.close_kind = "voicemail"
                await self._close_line(call_id, agent_call_id, tr, label, "closed_on_voicemail")
            return
        if tr.end_call_at is not None:
            done = tr.goodbye_done_at
            no_goodbye = done is None and now - tr.end_call_at >= GOODBYE_WAIT_MAX_S
            if no_goodbye or (done is not None and now >= done + AFTER_GOODBYE_CLOSE_S):
                if tr.unanswered_checkins:  # the goodbye was the "bad line" message after silence
                    tr.close_kind = "silence"
                    await self._close_line(call_id, agent_call_id, tr,
                                           "Closed the line: no reply after two check-ins (bad line or the callee hung up)",
                                           "closed_after_silence")
                elif no_goodbye:
                    tr.close_kind = "goodbye"
                    await self._close_line(call_id, agent_call_id, tr, "Closed the line: the AI ended the call without a goodbye",
                                           "closed_no_goodbye")
                else:
                    tr.close_kind = "goodbye"
                    await self._close_line(call_id, agent_call_id, tr, "Closed the line after goodbye", "closed_after_goodbye")
            return
        q = tr.silence or {"enabled": True, "check_s": 8, "max_s": 25}
        if not q["enabled"] or tr.supervisor_waiting:
            return
        if tr.unanswered_checkins >= 2 and tr.checkin_at is not None and now - tr.checkin_at >= CLOSE_AFTER_SECOND_CHECKIN_S:
            tr.close_kind = "silence"
            await self._close_line(call_id, agent_call_id, tr,
                                   "Closed the line: no reply after two check-ins (bad line or the callee hung up)",
                                   "closed_after_silence")
            return
        quiet_since = tr.quiet_since or tr.started_at
        if quiet_since is not None and now - quiet_since >= q["max_s"]:
            tr.close_kind = "silence"
            await self._close_line(call_id, agent_call_id, tr, f"Closed the line after {q['max_s']}s of silence",
                                   "closed_after_silence")

    def callee_hung_up(self, number: str, detail: str) -> dict:
        """The carrier (Telnyx webhook) says the callee hung up: close the Cartesia call now."""
        active = self.active_call()
        if active is None or active.number != number or not active.room.startswith("ac_"):
            return {"ok": True, "ignored": "no live call to that number"}
        tr = self._tracker(active.id)
        if tr.final or tr.auto_cancel_at is not None or tr.manual_hangup:
            return {"ok": True, "ignored": "already closing"}
        if tr.transfer_at is not None and not tr.transfer_failed:
            return {"ok": True, "ignored": "transferred to the supervisor"}  # that hang-up may be Cartesia's leg only
        _debug(active.id, "carrier_hangup", detail)
        tr.close_kind = "carrier"
        self._spawn(self._close_line(active.id, active.room, tr, "Closed the line: the callee hung up (Telnyx)", "closed_by_carrier"))
        return {"ok": True, "closing": active.id}

    def _ingest_turn(self, call_id: int, tr: _Tracker, turn: dict, source: str) -> None:
        role = {"assistant": "agent", "user": "callee"}.get(turn.get("role"), "system")
        text = BREAK_TAG_RE.sub("", turn.get("text") or "").strip()  # the pause in the welcome message (agent_config)
        turn = {**turn, "text": text}
        key = (turn.get("role"), turn.get("start_timestamp"), text[:60])
        if text and key not in tr.seen_turns:
            tr.seen_turns.add(key)
            self._note_turn(call_id, tr, turn, role)
            start = turn.get("start_timestamp")
            if (role == "callee" and tr.voicemail_at is None and isinstance(start, (int, float))
                    and start <= outcomes.VOICEMAIL_WITHIN_S and (kind := outcomes.voicemail_phrase(text))):
                tr.voicemail_at = asyncio.get_running_loop().time()
                tr.voicemail_why = f"{kind} greeting heard"
                _debug(call_id, "voicemail_heard", f"Sounds like {kind}: \"{text[:80]}\"")
            if (role == "agent" and tr.transfer_at is not None and not tr.transfer_failed and isinstance(start, (int, float))
                    and tr.transfer_turn_s is not None and start > tr.transfer_turn_s + 0.5
                    and not re.search(r"connect|transfer", text, re.I)):
                # Cartesia keeps the call when the carrier refuses the transfer, so the AI talks on.
                self._transfer_failed(call_id, tr, f"the AI kept talking after the transfer: \"{text[:80]}\"")
            if role == "agent" and tr.vm_message and tr.vm_msg_done_at is None and is_voicemail_message(text, tr.vm_message):
                self._voicemail_message_said(call_id, tr, turn, text)
            if role == "system":
                _add_event(call_id, "log", "agent", text)
            else:
                _add_event(call_id, "transcript", role, text, {"start_s": turn.get("start_timestamp"), "via": source})
                timing = {"tts_first_audio": turn.get("tts_ttfb")} if role == "agent" else {"stt_final": turn.get("stt_ttfb")}
                if any(v is not None for v in timing.values()):
                    _add_event(call_id, "timing", role, "", timing)
        for tc in turn.get("tool_calls") or []:
            tid = tc.get("id") or f"{tc.get('name')}:{turn.get('start_timestamp')}"
            if tid in tr.seen_tools:
                continue
            tr.seen_tools.add(tid)
            _debug(call_id, "tool_call", tc.get("name", "?"), arguments=tc.get("arguments"),
                   result=(tc.get("result") or "")[:300] or None, tool_id=tid)
            if tc.get("name") in ("voicemail_detection", "voicemail") and tr.voicemail_at is None:
                tr.voicemail_at = asyncio.get_running_loop().time()
                tr.voicemail_why = "the agent's voicemail tool"
            if tc.get("name") == "record_outcome":  # backup for the webhook tool (tunnel down, late delivery)
                self.record_outcome(call_id, _tool_args(tc.get("arguments")), via="transcript")
            if tc.get("name") == EMAIL_TOOL_NAME:  # same backup; the webhook's own answer decides if it was saved
                self.collect_email(call_id, _tool_args(tc.get("arguments")), via="transcript",
                                   webhook_said=str(tc.get("result") or ""))
            if "transfer" in (tc.get("name") or "") and tr.transfer_at is None:
                self._transfer_started(call_id, tr, turn, tc)
            if tc.get("name") == "end_call" and tr.end_call_at is None:
                tr.end_call_at = asyncio.get_running_loop().time()
                tr.close_requested_at = tr.close_requested_at or tr.end_call_at
                tr.close_reason = tr.close_reason or "the agent used end_call"
                tr.goodbye_done_at = tr.agent_line_done_at  # said in this turn or just before it; else it's still to come

    @staticmethod
    def _line_done_at(tr: _Tracker, turn: dict, text: str, now: float) -> float:
        """When the phone has played this agent line: from its timestamps, or estimated if they don't say.
        Never later than the line's length from now, in case our idea of the call's start is off."""
        start, end = turn.get("start_timestamp"), turn.get("end_timestamp")
        has_start = isinstance(start, (int, float))
        secs = end - start if has_start and isinstance(end, (int, float)) and end > start else speech_seconds(text)
        if tr.started_at is None or not has_start:
            return now + secs
        return min(now + secs, max(now, tr.started_at + start + secs))

    def _voicemail_message_said(self, call_id: int, tr: _Tracker, turn: dict, text: str) -> None:
        now = asyncio.get_running_loop().time()
        start = turn.get("start_timestamp")
        began = tr.started_at + start if tr.started_at is not None and isinstance(start, (int, float)) else now
        secs = speech_seconds(text)
        tr.vm_msg_done_at = max(now, began + secs)
        if tr.voicemail_at is None:
            tr.voicemail_at, tr.voicemail_why = now, "the AI left the voicemail message"
        with get_session() as s:
            call = s.get(Call, call_id)
            call.vm_message_left = True
            s.add(call)
            s.commit()
            s.refresh(call)
        _debug(call_id, "voicemail_message", f"Left the voicemail message (~{secs:.0f}s to say)", seconds=round(secs, 1))
        _add_event(call_id, "log", "agent", "Left the voicemail message", {"level": "info"})
        _publish_call(call)

    # ---- outcome reported by the AI during the call (record_outcome tool) -------------

    def record_outcome_tool(self, payload: dict) -> str:
        """The record_outcome webhook tool (already authenticated). Returns the text the AI gets back."""
        agent_call_id = str(payload.get("call_id") or "").strip()
        with get_session() as s:
            call = s.exec(select(Call).where(Call.room == agent_call_id)).first() if agent_call_id else None
        if call is None:
            logger.info("record_outcome for unknown call %r", agent_call_id[:40])
            return "Noted."  # e.g. a Playground test: nothing to save
        return self.record_outcome(call.id, payload, via="tool")

    def record_outcome(self, call_id: int, args: dict, via: str) -> str:
        """Save the AI's outcome on the call (idempotent; the latest wins, Do-Not-Contact sticks).
        DNC is applied to the lead at once; everything else when the call ends."""
        outcome = str(args.get("outcome") or "").strip()
        if outcome not in outcomes.AI_OUTCOMES:
            _debug(call_id, "record_outcome_rejected", f"Unknown outcome {outcome[:40]!r}", via=via)
            return f"Not saved: outcome must be one of {', '.join(outcomes.AI_OUTCOMES)}."
        note = re.sub(r"\s+", " ", str(args.get("note") or "")).strip()[:300]
        raw_time = str(args.get("callback_time") or "").strip()[:40]
        with get_session() as s:
            call = s.get(Call, call_id)
            if call is None:
                return "Noted."
            if call.status in FINAL:
                return "Noted."  # too late; the call's outcome is already decided
            if call.outcome == "do_not_contact" and call.outcome_source == "ai" and outcome != "do_not_contact":
                return "Saved."  # DNC is never downgraded within a call
            lead = s.get(Lead, call.lead_id) if call.lead_id else None
            tz_name, _ = lead_timezone(lead.location if lead else "", lead.phone if lead else call.number)
            callback_at = None
            if outcome in outcomes.AI_CALLBACK_OUTCOMES:
                now, hours = utcnow(), _calling_hours()
                callback_at = outcomes.parse_callback(raw_time, tz_name, now) or outcomes.callback_from_words(raw_time, tz_name, now, hours)
                if callback_at is None and outcome in outcomes.CALLBACK_OUTCOMES:  # "interested" may have a day in its note for another reason
                    callback_at = outcomes.callback_from_words(note, tz_name, now, hours)
            same =(call.outcome, call.outcome_source, call.outcome_note, as_utc(call.callback_at)) == (outcome, "ai", note, callback_at)
            if not same:
                call.outcome, call.outcome_source, call.outcome_note, call.callback_at = outcome, "ai", note, callback_at
                s.add(call)
                s.commit()
                s.refresh(call)
        if same:
            return _outcome_reply(outcome, callback_at, tz_name)
        _debug(call_id, "record_outcome", outcomes.label(outcome), via=via, outcome=outcome, note=note or None,
               callback_time=raw_time or None, callback_at=callback_at.isoformat() if callback_at else None)
        _add_event(call_id, "log", "agent", f"Outcome: {outcomes.label(outcome)}" + (f" ({note})" if note else ""),
                   {"level": "info"})
        _publish_call(call)
        if outcome == "do_not_contact":  # right away, not at the end of the call
            reason = f"Call #{call_id}: " + (note or "asked not to be contacted")
            if call.lead_id:
                try:
                    leads.set_dnc(call.lead_id, True, reason)
                except leads.LeadError:
                    pass
            else:
                leads._dnc_unknown_number(call)
        return _outcome_reply(outcome, callback_at, tz_name)

    # ---- the lead's email address (collect_email tool) ---------------------------------

    def collect_email_tool(self, payload: dict) -> str:
        """The collect_email webhook tool (already authenticated). Returns the text the AI gets back."""
        agent_call_id = str(payload.get("call_id") or "").strip()
        with get_session() as s:
            call = s.exec(select(Call).where(Call.room == agent_call_id)).first() if agent_call_id else None
        if call is None:
            return "Noted."  # e.g. a Playground test
        return self.collect_email(call.id, payload, via="tool")

    def collect_email(self, call_id: int, args: dict, via: str, webhook_said: str = "") -> str:
        """Save the address the lead confirmed (on the call and the lead; the latest one wins).
        Not saved unless the AI says it read it back; a likely typo (gmial.com) is checked once with the lead."""
        raw = str(args.get("email") or "").strip()[:200]
        reason = str(args.get("reason") or "").strip()
        reason = reason if reason in EMAIL_REASONS else "other"
        confirmed = args.get("confirmed") in (True, "true", "True", "yes", 1)
        if via == "transcript" and webhook_said and not webhook_said.startswith("Saved"):
            return webhook_said  # the webhook already answered (not saved): nothing to redo
        if not confirmed:
            return ("Not saved yet. Read the address back to them first, spelling it, and use collect_email again "
                    "once they say it's right.")
        email, why = leads.normalize_email(raw)
        if not email:
            _debug(call_id, "collect_email_rejected", why, via=via)
            return f"Not saved: {why}. Ask them to spell it again, slowly."
        tr = self._tracker(call_id)
        typo = leads.email_typo(email)
        if typo and email not in tr.email_typo_checked and not webhook_said:
            tr.email_typo_checked.add(email)
            user = email.rsplit("@", 1)[0]
            return (f"Not saved yet: {email.rsplit('@', 1)[1]} looks like a typo of {typo}. Ask once if they meant "
                    f"{user} at {typo.replace('.', ' dot ')}, then use collect_email again with the right address.")
        with get_session() as s:
            call = s.get(Call, call_id)
            if call is None or call.status in FINAL:
                return "Noted."
            same = (call.email_collected, call.email_reason) == (email, reason)
            if not same:
                call.email_collected, call.email_reason = email, reason
                s.add(call)
                lead = s.get(Lead, call.lead_id) if call.lead_id else None
                if lead is not None and lead.email != email:
                    old = lead.email
                    lead.email, lead.updated_at = email, utcnow()
                    s.add(lead)
                    leads._log(s, lead.id, "note", f"Email confirmed on call #{call_id}: {email}"
                               + (f" (was {old})" if old else ""), call_id=call_id, email=email)
                s.commit()
                s.refresh(call)
        if not same:
            _debug(call_id, "collect_email", email, via=via, reason=reason)
            _add_event(call_id, "log", "agent", f"Email confirmed: {email}", {"level": "info"})
            _publish_call(call)
            if call.lead_id and (row := leads.get_lead_row(call.lead_id)):
                leads._publish(row)
        return f"Saved: {email}. We'll email them right after the call."

    # ---- the supervisor: ask first (pop-up), then transfer ------------------------------

    def _set_transfer(self, call_id: int, status: str, note: str = "", **fields) -> Call:
        with get_session() as s:
            call = s.get(Call, call_id)
            call.transfer_status = status
            if note:
                call.transfer_note = note
            for k, v in fields.items():
                setattr(call, k, v)
            s.add(call)
            s.commit()
            s.refresh(call)
        _debug(call_id, "transfer", supervisor.TRANSFER_LABELS.get(status, status), status=status, note=note or None)
        if tr := self.trackers.get(call_id):
            if tr.supervisor_waiting and status != supervisor.WAITING:
                tr.quiet_since = asyncio.get_running_loop().time()  # the silence count starts over after the wait
            tr.supervisor_waiting = status == supervisor.WAITING
        _publish_call(call)
        self._publish_request(call)
        return call

    def request_payload(self, call: Call) -> dict:
        """What the supervisor's pop-up shows: the lead, their local time, the AI's summary, the transcript."""
        lead = leads.get_lead_row(call.lead_id) if call.lead_id else None
        tz_name, _ = lead_timezone(lead.location if lead else "", lead.phone if lead else call.number)
        with get_session() as s:
            events = s.exec(select(CallEvent).where(CallEvent.call_id == call.id, CallEvent.kind == "transcript")
                            .order_by(CallEvent.id)).all()
        wait_s = supervisor.load()["wait_seconds"]
        dl = supervisor.deadline(call.transfer_requested_at, wait_s)
        return {
            "call_id": call.id, "lead_id": call.lead_id, "status": call.transfer_status,
            "label": supervisor.TRANSFER_LABELS.get(call.transfer_status, call.transfer_status),
            "summary": call.transfer_summary, "note": call.transfer_note,
            "requested_at": as_utc(call.transfer_requested_at).isoformat() if call.transfer_requested_at else None,
            "deadline": dl.isoformat() if dl else None, "wait_seconds": wait_s,
            "lead": {**lead_variables(lead), "phone": lead.phone if lead else call.number,
                     **time_variables(tz_name, utcnow())},
            "transcript": [{"role": e.role, "text": e.text} for e in events],
        }

    def _publish_request(self, call: Call) -> None:
        bus.publish({"type": "supervisor_request", "request": self.request_payload(call)})

    def open_requests(self) -> list[dict]:
        with get_session() as s:
            rows = s.exec(select(Call).where(Call.transfer_status == supervisor.WAITING)).all()
        return [self.request_payload(c) for c in rows if c.status not in FINAL]

    async def request_supervisor_tool(self, payload: dict) -> str:
        """The request_supervisor webhook tool (already authenticated). The first use opens the pop-up;
        each use waits up to CHECK_WAIT_S for the supervisor's answer, then says waiting / available / not."""
        agent_call_id = str(payload.get("call_id") or "").strip()
        with get_session() as s:
            call = s.exec(select(Call).where(Call.room == agent_call_id)).first() if agent_call_id else None
        if call is None or call.status in FINAL:
            return supervisor.REPLY_NOT_AVAILABLE  # e.g. a Playground test
        summary = re.sub(r"\s+", " ", str(payload.get("summary") or "")).strip()[:600]
        status = call.transfer_status
        if status in ("", supervisor.UNAVAILABLE):
            sup = supervisor.load()
            ok, why = supervisor.availability(s=sup)
            if not ok:
                if status != supervisor.UNAVAILABLE:
                    self._set_transfer(call.id, supervisor.UNAVAILABLE, why, transfer_summary=summary)
                return supervisor.REPLY_NOT_AVAILABLE
            call = self._set_transfer(call.id, supervisor.WAITING, "", transfer_summary=summary,
                                      transfer_requested_at=utcnow(), transfer_answered_at=None)
            _add_event(call.id, "log", "agent", "Asked the supervisor to take the call", {"level": "info"})
            self.sup.start_timer(call.id, sup["wait_seconds"], self._supervisor_timeout)
            status = supervisor.WAITING
        elif status == supervisor.WAITING and summary and summary != call.transfer_summary:
            self._set_transfer(call.id, supervisor.WAITING, "", transfer_summary=summary)
        if status == supervisor.WAITING:
            dl = supervisor.deadline(call.transfer_requested_at, supervisor.load()["wait_seconds"])
            left = (dl - utcnow()).total_seconds() if dl else 0
            await self.sup.wait(call.id, max(0.0, min(supervisor.CHECK_WAIT_S, left)))
            with get_session() as s:
                status = s.get(Call, call.id).transfer_status
            if status == supervisor.WAITING and dl and utcnow() >= dl:  # past the deadline: don't wait for the timer
                self._supervisor_timeout(call.id)
                status = supervisor.TIMEOUT
        return supervisor.reply_for(status)

    def answer_supervisor(self, call_id: int, accept: bool, by: str = "") -> dict:
        """Accept / Busy from the pop-up. Only an open (waiting) request can be answered."""
        with get_session() as s:
            call = s.get(Call, call_id)
        if call is None:
            raise CallError("No such call.")
        if call.transfer_status != supervisor.WAITING or call.status in FINAL:
            raise CallError("Too late: this request was already answered or timed out.")
        who = f" ({by})" if by else ""
        status = supervisor.ACCEPTED if accept else supervisor.DECLINED
        call = self._set_transfer(call_id, status, ("accepted" if accept else "busy") + who, transfer_answered_at=utcnow())
        _add_event(call_id, "log", "agent", f"Supervisor {'accepted' if accept else 'is busy'}{who}", {"level": "info"})
        self.sup.done(call_id)
        return self.request_payload(call)

    def _supervisor_timeout(self, call_id: int) -> None:
        with get_session() as s:
            call = s.get(Call, call_id)
        if call is None or call.transfer_status != supervisor.WAITING:
            return
        wait_s = supervisor.load()["wait_seconds"]
        self._set_transfer(call_id, supervisor.TIMEOUT, f"no answer within {wait_s}s")
        _add_event(call_id, "log", "agent", f"Supervisor didn't answer within {wait_s}s", {"level": "info"})
        self.sup.done(call_id)

    def _transfer_started(self, call_id: int, tr: _Tracker, turn: dict, tc: dict) -> None:
        tr.transfer_at = asyncio.get_running_loop().time()
        ts = [x for x in (turn.get("end_timestamp"), turn.get("start_timestamp")) if isinstance(x, (int, float))]
        tr.transfer_turn_s = max(ts) if ts else tr.last_turn_end_s
        tr.supervisor_waiting = False
        self._set_transfer(call_id, supervisor.TRANSFERRING, "the AI used the transfer tool")
        _add_event(call_id, "log", "agent", "Transferring to the supervisor", {"level": "info"})
        result = str(tc.get("result") or "")
        if re.search(r"\b(error|fail|failed|unable|reject|could not|couldn't)\b", result, re.I):
            self._transfer_failed(call_id, tr, f"Cartesia: {result[:120]}")

    def _transfer_failed(self, call_id: int, tr: _Tracker, why: str) -> None:
        tr.transfer_failed = True
        tr.quiet_since = asyncio.get_running_loop().time()  # the normal hang-up rules apply again from now
        self._set_transfer(call_id, supervisor.FAILED, why)
        _add_event(call_id, "log", "agent", f"Transfer didn't go through ({why})", {"level": "error"})

    def _note_turn(self, call_id: int, tr: _Tracker, turn: dict, role: str) -> None:
        now = asyncio.get_running_loop().time()
        start, end = turn.get("start_timestamp"), turn.get("end_timestamp")
        gap = (start - tr.last_turn_end_s) if isinstance(start, (int, float)) and tr.last_turn_end_s is not None else None
        check_s = (tr.silence or {}).get("check_s", 8)
        if role == "agent":
            tr.agent_line_done_at = self._line_done_at(tr, turn, (turn.get("text") or "").strip(), now)
            if tr.end_call_at is not None:
                tr.goodbye_done_at = tr.agent_line_done_at  # the goodbye line, after the end_call tool
            greeting = not tr.agent_spoke and gap is not None and gap <= (tr.silence or {}).get("greet_wait_s", 0) + CHECKIN_SLACK_S
            tr.agent_spoke = True
            if not greeting and gap is not None and gap >= check_s - CHECKIN_SLACK_S:
                tr.checkin_at = now  # Cartesia's silence check-in; doesn't count as the line being alive
                tr.unanswered_checkins += 1
                _debug(call_id, "silence_checkin", f"Check-in {tr.unanswered_checkins} after {gap:.1f}s of silence",
                       gap_s=round(gap, 1), checkin=tr.unanswered_checkins)
            elif tr.unanswered_checkins == 0:
                tr.quiet_since = now  # a normal reply: the conversation is still going
        elif role == "callee":
            tr.agent_line_done_at = None  # the AI's last line was before this, so it wasn't a goodbye
            tr.unanswered_checkins = 0  # they spoke: start the silence count over
            tr.checkin_at = None
            tr.quiet_since = now
        tr.last_turn_at = now
        ends = [x for x in (end, start, tr.last_turn_end_s) if isinstance(x, (int, float))]
        tr.last_turn_end_s = max(ends) if ends else tr.last_turn_end_s

    def ingest_call(self, call_id: int, data: dict, source: str) -> None:
        """Apply a Cartesia call object (GET /agents/calls/{id} or a webhook's `call`). Idempotent."""
        tr = self._tracker(call_id)
        if tr.final:
            return
        status = data.get("status")
        if status and status != tr.cartesia_status:
            tr.cartesia_status = status
            _debug(call_id, "cartesia_status", status, via=source, end_reason=data.get("end_reason"),
                   connection=(data.get("telephony_params") or {}).get("connection_type"))
        for turn in data.get("transcript") or []:
            self._ingest_turn(call_id, tr, turn, source)
        if status == "started" and not tr.answered:
            tr.answered = True
            tr.started_at = tr.quiet_since = asyncio.get_running_loop().time()
            tr.silence = _silence_settings()
            with get_session() as s:
                tr.vm_message = _voicemail_message(s.get(Call, call_id).lead_id)
            if tr.last_turn_end_s is None:
                tr.last_turn_end_s = 0.0  # so a first agent line after dead air also counts as a check-in
            apply_status(call_id, "answered", "Call connected (Cartesia session started)")
        if status in ("completed", "failed"):
            tr.final = True
            self._finish(call_id, data, source)

    def _finish(self, call_id: int, data: dict, source: str) -> None:
        reason = data.get("end_reason") or ("error" if data.get("status") == "failed" else "")
        status, result = END_REASONS.get(reason, ("ended", None))
        detail = END_LABELS.get(reason, reason or "Call ended")
        if data.get("error_message"):
            detail = f"{detail}: {data['error_message']}"
        with get_session() as s:
            answered = bool(s.get(Call, call_id).answered_at)
        heard_callee = any(t.get("role") == "user" and (t.get("text") or "").strip() for t in data.get("transcript") or [])
        tr = self.trackers.get(call_id)
        if result is None:  # a hang-up / silence / max-length kind of ending
            result = "answered" if (answered or heard_callee) else "no_answer"
            if tr and tr.auto_close_label and reason in ("api_cancelled", "agent_hangup", "client_hangup", "call_inactivity", "client_inactivity"):
                detail = tr.auto_close_label
                if tr.close_kind == "silence":
                    result = "bad_line"
                elif tr.close_kind == "voicemail":
                    result = "voicemail"
            elif reason == "api_cancelled" and result == "no_answer":
                detail = "Hung up from dashboard before answer"
        _debug(call_id, "end_reason", reason or "(none)", via=source, error_message=data.get("error_message"),
               summary=(data.get("summary") or "")[:500] or None)
        start, end = _parse_time(data.get("start_time")), _parse_time(data.get("end_time"))
        talk = round((end - start).total_seconds(), 1) if start and end else None
        apply_status(call_id, status, detail, {"end_reason": reason, "talk_seconds": talk}, result=result,
                     transcript=data.get("transcript") or [])
        if talk is not None and result in ("answered", "voicemail", "bad_line"):
            with get_session() as s:  # Cartesia's own start/end times are more exact than ours
                call = s.get(Call, call_id)
                call.duration_s = talk
                s.add(call)
                s.commit()
                s.refresh(call)
            _publish_call(call)

    def ingest_webhook(self, payload: dict) -> dict:
        """A Cartesia webhook delivery (already authenticated). Duplicates are ignored."""
        rid = payload.get("webhook_request_id")
        now = asyncio.get_running_loop().time()
        self.seen_webhooks = {k: t for k, t in self.seen_webhooks.items() if now - t < 3600}
        if rid and rid in self.seen_webhooks:
            return {"ok": True, "duplicate": True}
        if rid:
            self.seen_webhooks[rid] = now
        agent_call_id = payload.get("call_id") or (payload.get("call") or {}).get("id")
        with get_session() as s:
            call = s.exec(select(Call).where(Call.room == agent_call_id)).first() if agent_call_id else None
        if call is None:
            return {"ok": True, "ignored": "unknown call"}  # e.g. a Playground test call
        kind = payload.get("type")
        logger.info("webhook %s for call %s (%s)", kind, call.id, agent_call_id)
        tr = self._tracker(call.id)
        if kind == "call_turn" and payload.get("turn"):
            self._ingest_turn(call.id, tr, payload["turn"], "webhook")
        elif kind in ("call_started", "call_completed", "call_failed") and payload.get("call"):
            self.ingest_call(call.id, payload["call"], "webhook")
        elif kind == "post_call_analysis":
            summary = ((payload.get("analysis") or {}).get("summary") or "")[:1000]
            _debug(call.id, "post_call_analysis", summary or "(no summary)")
        return {"ok": True, "type": kind}

    async def recording(self, call_id: int) -> tuple[bytes, str] | None:
        """The call's recording (WAV from Cartesia), cached in data/recordings/."""
        with get_session() as s:
            call = s.get(Call, call_id)
        if call is None or not call.room.startswith("ac_") or call.status not in FINAL:
            return None
        if storage.expired(call.created_at):  # past the keep period: don't fetch it back from Cartesia
            return None
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        cached = RECORDINGS_DIR / f"{call.room}.wav"
        if cached.exists():
            return cached.read_bytes(), "audio/wav"
        audio, mime = await cartesia.call_audio(call.room)
        if audio:
            cached.write_bytes(audio)
        return audio, mime or "audio/wav"

    # ---- simulated call (dev only) -------------------------------------------------

    def start_simulation(self, number: str) -> Call:
        if self.active_call():
            raise CallBusy("A call is already in progress. Hang up first.")
        with get_session() as s:
            call = Call(room=f"sim-{uuid.uuid4().hex[:8]}", number=number or "+15555550100",
                        status="dialing", source="simulated", simulated=True)
            s.add(call)
            s.commit()
            s.refresh(call)
        _publish_call(call)
        apply_status(call.id, "dialing", f"Dialing {call.number} (simulated)")
        self._spawn(self._run_simulation(call.id))
        return call

    async def _run_simulation(self, call_id: int) -> None:
        def ended() -> bool:
            with get_session() as s:
                return s.get(Call, call_id).status in ("ended", "failed")

        async def wait(seconds: float) -> bool:
            await asyncio.sleep(seconds)
            return not ended()

        def say(role: str, text: str, timing: dict | None = None) -> None:
            _add_event(call_id, "state", role, "speaking")
            _add_event(call_id, "transcript", role, text)
            if timing:
                _add_event(call_id, "timing", role, "", timing)

        def quiet(role: str) -> None:
            _add_event(call_id, "state", role, "listening")

        script = [
            ("callee", "Hello?", {"stt_final": 0.41, "end_of_turn": 0.82}),
            ("agent", "Hey, this is Taylor from BrightPixel Web Design. Did I catch you at an okay time?",
             {"reply_gap": 0.95, "llm_first_token": None, "tts_first_audio": 0.71, "playback": 0.06}),
            ("callee", "Uh, yeah, I've got a minute. What's this about?", {"stt_final": 0.38, "end_of_turn": 0.84}),
            ("agent", "Got it, thanks. Quick one, what kind of business do you run?",
             {"reply_gap": 1.62, "llm_first_token": 0.48, "llm_first_sentence": 0.61, "tts_first_audio": 0.83, "playback": 0.07}),
            ("callee", "We do custom furniture, mostly online orders.", {"stt_final": 0.44, "end_of_turn": 0.9}),
            ("agent", "Oh nice. How's your website doing for those online orders?",
             {"reply_gap": 1.48, "llm_first_token": 0.51, "llm_first_sentence": 0.58, "tts_first_audio": 0.77, "playback": 0.06}),
            ("callee", "Honestly it's slow and kind of dated.", {"stt_final": 0.39, "end_of_turn": 0.8}),
            ("agent", "Makes sense. Would a free fifteen minute call with our senior designer help?",
             {"reply_gap": 1.55, "llm_first_token": 0.47, "llm_first_sentence": 0.66, "tts_first_audio": 0.88, "playback": 0.07}),
            ("callee", "Sure, that works.", {"stt_final": 0.36, "end_of_turn": 0.81}),
            ("agent", "Perfect. I'll set that up. Thanks for your time, have a good one!",
             {"reply_gap": 1.41, "llm_first_token": 0.45, "llm_first_sentence": 0.52, "tts_first_audio": 0.74, "playback": 0.06}),
        ]

        t0 = asyncio.get_running_loop().time()

        def trace(step: str, detail: str, **data) -> None:
            t = round(asyncio.get_running_loop().time() - t0, 2)
            _add_event(call_id, "debug", "system", step, {"detail": detail, "t": t, **data})

        try:
            trace("room_created", "sim room")
            trace("agent_dispatched", "dialmind-agent (simulated)")
            trace("dial_sent", "CreateSIPParticipant (simulated)", ringing_timeout_s=40)
            if not await wait(0.6):
                return
            trace("sip_participant_joined", "SIP participant is in the room")
            trace("sip_status", "dialing", attributes={"sip.callStatus": "dialing"})
            if not await wait(0.6):
                return
            trace("sip_status", "ringing", attributes={"sip.callStatus": "ringing"})
            apply_status(call_id, "ringing", "Ringing (simulated)")
            if not await wait(3.0):
                return
            trace("sip_status", "active", attributes={"sip.callStatus": "active"})
            trace("dial_result", "Dial request returned: answered", ok=True)
            apply_status(call_id, "answered", "Answered (simulated)", {"via": "sip.callStatus=active"})
            _add_event(call_id, "agent", "agent", "answered", {"detail": "Agent connected to the call"})
            for role, text, timing in script:
                if not await wait(1.0 if role == "agent" else 1.4):
                    return
                say(role, text, timing)
                if not await wait(min(4.5, 0.9 + len(text) / 18)):
                    return
                quiet(role)
            if await wait(1.0):
                trace("sip_status", "hangup", attributes={"sip.callStatus": "hangup"})
                trace("sip_participant_left", "SIP participant left the room", last_sip_status="hangup")
                apply_status(call_id, "ended", "Callee hung up (simulated)", {"ended_by": "callee"})
                trace("room_deleted", "sim room")
        except Exception as e:
            logger.exception("simulation crashed")
            apply_status(call_id, "failed", f"Simulation error: {e}")


manager = CallManager()
