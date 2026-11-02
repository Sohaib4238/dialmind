"""Phase 5 step 8: voicemail -> close the line fast; phrase backup. Fake Cartesia, throwaway DB, no calls."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import sys
import time

DB = f"{TMP}/voicemail_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile
import env_utils  # noqa: E402,F401

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup, outcomes  # noqa: E402
from app.db import Call, CallEvent, Lead, get_session, init_db  # noqa: E402
from sqlmodel import select  # noqa: E402

init_db()
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test")
calls.POLL_EVERY_S = 0.05


class Fake:
    def __init__(self):
        self.calls, self.n, self.cancels = {}, 0, []

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.n += 1
        cid = f"ac_v{self.n}"
        self.calls[cid] = {"status": "started", "end_reason": None, "transcript": []}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        return {"id": cid, "status": c["status"], "end_reason": c["end_reason"], "transcript": c["transcript"],
                "start_time": "2026-10-27T15:00:00Z", "end_time": "2026-10-27T15:00:09Z"}

    async def cancel_call(self, cid):
        self.cancels.append((cid, time.monotonic()))
        self.calls[cid].update(status="completed", end_reason="api_cancelled")
        return {}


fake = Fake()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fake.create_call, fake.get_call, fake.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync


def lead(name, phone):
    with get_session() as s:
        x = Lead(phone=phone, name=name, location="Miami, FL 33130")
        s.add(x)
        s.commit()
        return x.id


def row(model, i):
    with get_session() as s:
        return s.get(model, i)


async def scenario(name, turns, finish_after=None, finish_reason="client_hangup"):
    lid = lead(name, f"+130555{5000 + fake.n:04d}")
    c = await calls.manager.start_call("+17865550100", lead_id=lid)
    room = c.room
    await asyncio.sleep(0.2)  # answered
    t0 = time.monotonic()
    fake.calls[room]["transcript"] = turns
    end = time.monotonic() + (finish_after or 4)
    while time.monotonic() < end and row(Call, c.id).status not in ("ended", "failed"):
        await asyncio.sleep(0.05)
    if finish_after and fake.calls[room]["status"] == "started":
        fake.calls[room].update(status="completed", end_reason=finish_reason)
    for _ in range(100):
        if row(Lead, lid).status != "calling":
            break
        await asyncio.sleep(0.05)
    cancel = next((t for cid, t in fake.cancels if cid == room), None)
    return row(Call, c.id), row(Lead, lid), (cancel - t0 if cancel else None)


async def run():
    # 1. The agent's voicemail tool (no message configured) -> we close the line about 1 s later.
    call, ld, closed_after = await scenario("VM tool", [
        {"role": "user", "text": "Hi, you've reached Bob.", "start_timestamp": 1.0},
        {"role": "assistant", "text": "", "start_timestamp": 3.0,
         "tool_calls": [{"id": "tc_vm", "name": "voicemail_detection", "arguments": {}, "result": "ok"}]},
    ])
    assert call.result == "voicemail" and call.outcome == "voicemail" and ld.status == "voicemail" and ld.vm_count == 1, \
        (call.result, call.outcome, ld.status)
    assert closed_after is not None and closed_after < 2.5, closed_after
    assert "reached voicemail" in call.reason, call.reason
    print(f"voicemail tool: line closed {closed_after:.1f}s later -> {call.reason}")

    # 2. The AI missed it, but the greeting is plain: closed by our phrase check.
    call, ld, closed_after = await scenario("VM greeting", [
        {"role": "user", "text": "The person you are trying to reach is not available. At the tone, please record your message.",
         "start_timestamp": 0.8},
        {"role": "assistant", "text": "Hey, this is Taylor from Example Agency, did I catch you at an okay time?", "start_timestamp": 6.0},
    ])
    assert call.result == "voicemail" and ld.status == "voicemail" and closed_after is not None and closed_after < 2.5, \
        (call.result, ld.status, closed_after)
    print(f"greeting heard: line closed {closed_after:.1f}s later")

    # 3. "Mailbox is full" -> voicemail with a note (still retried).
    call, ld, _ = await scenario("VM full", [
        {"role": "user", "text": "Sorry, the mailbox is full and cannot accept any new messages.", "start_timestamp": 1.0}])
    assert call.outcome == "voicemail" and ld.status == "voicemail", (call.outcome, ld.status)
    with get_session() as s:
        dbg = [e.text for e in s.exec(select(CallEvent).where(CallEvent.call_id == call.id)).all()]
    assert "voicemail_heard" in dbg
    print("mailbox full: voicemail")

    # 4. A real person who says "leave a message" later in a conversation: no hang-up.
    call, ld, closed_after = await scenario("Real person", [
        {"role": "user", "text": "Hello?", "start_timestamp": 0.5},
        {"role": "assistant", "text": "Hey, this is Taylor. Got a minute?", "start_timestamp": 1.5},
        {"role": "user", "text": "Sure. Actually just leave a message with my office after the tone thing, ha.", "start_timestamp": 40.0},
    ], finish_after=1.5)
    assert closed_after is None and call.outcome == "needs_review", (closed_after, call.outcome)
    print("real person later in the call: not closed, needs review")

    # 5. Normal hello: nothing happens.
    call, ld, closed_after = await scenario("Hello", [{"role": "user", "text": "Hello, this is Dana.", "start_timestamp": 0.5}],
                                            finish_after=1.0)
    assert closed_after is None and call.outcome == "needs_review"

    # 6. Cartesia ends the call itself with voicemail_detected (if it ever does): still voicemail.
    call, ld, _ = await scenario("Cartesia VM", [], finish_after=0.3, finish_reason="voicemail_detected")
    assert call.result == "voicemail" and ld.status == "voicemail"
    print("Cartesia voicemail_detected end: voicemail")


asyncio.run(run())

# Phrase unit checks
yes = ["Please leave a message after the beep", "You've reached Sam, leave me your name and number",
       "The number you have dialed has a voicemail box that has not been set up yet", "Record your message at the tone"]
no = ["Hello?", "Who is this?", "I'm not available right now, call me tomorrow", "Can you leave me alone"]
assert all(outcomes.voicemail_phrase(x) for x in yes), [x for x in yes if not outcomes.voicemail_phrase(x)]
assert not any(outcomes.voicemail_phrase(x) for x in no), [x for x in no if outcomes.voicemail_phrase(x)]
assert outcomes.voicemail_phrase(yes[2]) == "mailbox full / not set up"
print("ALL VOICEMAIL TESTS PASS")
