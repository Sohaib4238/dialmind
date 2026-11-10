"""Calls the phone company refused before they rang, told apart from real busy lines.

Cartesia reports every refused SIP INVITE as end_reason "dial_busy" ("busy or declined; trunk rejections such as
auth failures or carrier blocks also report this reason"). On 2026-11-06 the Telnyx balance hit $0, Telnyx
deactivated the account and refused every call: 12 leads in a row came back "busy" within 3 s (History #209-#220).
A real busy line is rare (5 of the 208 calls before), so STREAK calls in a row that all end "busy" without being
answered, with the same error, are trunk rejections, not busy lines:
  - those calls get result "trunk_rejected" (outcome "failed": nothing was reached),
  - their leads get the attempt back (leads.undo_attempt), and a follow-up text still waiting is cancelled,
  - listeners (the auto-dialer) switch the dialer off and show the reason in red.
Any other kind of ending (rang, answered, no answer, voicemail) breaks the streak.
"""

import logging
from typing import Callable

from sqlmodel import select

from app import leads
from app.bus import bus
from app.db import Call, SmsMessage, call_to_dict, get_session

logger = logging.getLogger("dashboard.rejections")

STREAK = 3
RESULT = "trunk_rejected"
NOTE = "refused before it rang (not a real busy line)"
listeners: list[Callable[[str], None]] = []


def _counts(c: Call) -> bool:
    """Calls that take part in the streak: our own real outbound calls that have ended."""
    return c.direction == "outbound" and not c.simulated and c.status in ("ended", "failed")


def _rejected_kind(c: Call) -> bool:
    return c.result in ("busy", RESULT) and c.answered_at is None


def reason_text(reason: str) -> str:
    why = reason.split(": ", 1)[-1] if reason else "busy or declined"
    return (f"{STREAK} calls in a row were refused before they rang ({why}). That's not {STREAK} busy lines: "
            "usually the Telnyx balance is $0 (the account is switched off), or the trunk or caller ID is blocked. "
            "Check Health (Telnyx balance) and the Telnyx portal, then turn the dialer on again. Those leads keep "
            "their attempts.")


def _streak(s, upto_id: int | None = None) -> list[Call]:
    """The latest run of busy/rejected calls (newest first), up to and including call upto_id."""
    stmt = select(Call).where(Call.direction == "outbound").order_by(Call.id.desc()).limit(400)
    if upto_id is not None:
        stmt = stmt.where(Call.id <= upto_id)
    run = []
    for c in s.exec(stmt).all():
        if not _counts(c):
            continue
        if not _rejected_kind(c) or (run and c.reason != run[0].reason):
            break
        run.append(c)
    return run


def _mark(call_ids: list[int]) -> None:
    for cid in call_ids:
        with get_session() as s:
            c = s.get(Call, cid)
            if c is None or c.result == RESULT:
                continue
            c.result, c.status, c.outcome, c.outcome_source = RESULT, "failed", "failed", "call"
            c.outcome_note = NOTE
            c.callback_at = None
            for m in s.exec(select(SmsMessage).where(SmsMessage.call_id == cid, SmsMessage.status == "queued")).all():
                m.status, m.detail, m.send_after = "not_sent", "not sent: the call never reached the lead (refused before it rang)", None
                s.add(m)
            s.add(c)
            s.commit()
            s.refresh(c)
        leads.undo_attempt(cid, NOTE)
        bus.publish({"type": "call", "call": call_to_dict(c)})
        logger.warning("call %s: %s", cid, NOTE)


def after_call(call: Call) -> str | None:
    """Called when a call has ended. Returns the warning if this call completed (or continues) a streak."""
    if not _counts(call) or not _rejected_kind(call):
        return None
    with get_session() as s:
        run = _streak(s, call.id)
    if len(run) < STREAK:
        return None
    _mark([c.id for c in run])
    why = reason_text(call.reason)
    for fn in listeners:
        try:
            fn(why)
        except Exception:  # noqa: BLE001
            logger.exception("rejection listener failed")
    return why


def repair_history() -> int:
    """Past runs of STREAK+ refused calls (before this check existed): relabel them and give the attempts back.
    Safe to run on every start: calls already marked are skipped."""
    with get_session() as s:
        calls = [c for c in s.exec(select(Call).where(Call.direction == "outbound").order_by(Call.id)).all() if _counts(c)]
    fixed, run = 0, []

    def flush():
        nonlocal fixed
        todo = [c.id for c in run if c.result != RESULT]
        if len(run) >= STREAK and todo:
            _mark(todo)
            fixed += len(todo)

    for c in calls:
        if _rejected_kind(c) and (not run or c.reason == run[-1].reason):
            run.append(c)
            continue
        flush()
        run = [c] if _rejected_kind(c) else []
    flush()
    return fixed
