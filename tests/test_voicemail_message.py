"""Phase 7 part A: leave a voicemail message (ON/OFF). Fake Cartesia, throwaway DB, no calls."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import sys
import time

DB = f"{TMP}/voicemail_message_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # no email follow-up here (test_emails.py)
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile
import env_utils  # noqa: E402,F401

import app.calls as calls  # noqa: E402
from app import agent_config, cartesia, cartesia_setup  # noqa: E402
from app.db import Call, Lead, LeadEvent, get_session, init_db  # noqa: E402
from settings import load_settings  # noqa: E402
from sqlmodel import select  # noqa: E402

init_db()
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test")
calls.POLL_EVERY_S = 0.05
calls.VM_MESSAGE_START_WAIT_S = 2.5  # 8 s in real use

REAL = load_settings()
ON = REAL.model_copy(update={"voicemail": REAL.voicemail.model_copy(update={"leave_message": True})})
OFF = REAL.model_copy(update={"voicemail": REAL.voicemail.model_copy(update={"leave_message": False})})


class Fake:
    def __init__(self):
        self.calls, self.n, self.cancels = {}, 0, []

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.n += 1
        cid = f"ac_m{self.n}"
        self.calls[cid] = {"status": "started", "end_reason": None, "transcript": []}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        return {"id": cid, "status": c["status"], "end_reason": c["end_reason"], "transcript": c["transcript"],
                "start_time": "2026-10-27T15:00:00Z", "end_time": "2026-10-27T15:00:19Z"}

    async def cancel_call(self, cid):
        self.cancels.append((cid, time.monotonic()))
        self.calls[cid].update(status="completed", end_reason="api_cancelled")
        return {}


fake = Fake()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fake.create_call, fake.get_call, fake.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync


def lead(name, service="a new mobile app"):
    with get_session() as s:
        x = Lead(phone=f"+130555{6000 + fake.n:04d}", name=name, service_required=service, location="Miami, FL 33130")
        s.add(x)
        s.commit()
        return x.id


def row(model, i):
    with get_session() as s:
        return s.get(model, i)


async def scenario(settings, name, turns, finish_after=None, wait=14):
    calls.load_settings = lambda: settings
    lid = lead(name)
    c = await calls.manager.start_call("+17865550100", lead_id=lid)
    room = c.room
    await asyncio.sleep(0.2)  # answered
    t0 = time.monotonic()
    fake.calls[room]["transcript"] = turns
    end = time.monotonic() + (finish_after or wait)
    while time.monotonic() < end and row(Call, c.id).status not in ("ended", "failed"):
        await asyncio.sleep(0.05)
    if finish_after and fake.calls[room]["status"] == "started":
        fake.calls[room].update(status="completed", end_reason="client_hangup")
    for _ in range(100):
        if row(Lead, lid).status != "calling":
            break
        await asyncio.sleep(0.05)
    cancel = next((t for cid, t in fake.cancels if cid == room), None)
    return row(Call, c.id), row(Lead, lid), (cancel - t0 if cancel else None)


GREETING = {"role": "user", "text": "Hi, you've reached Maria at Bloom Bakery. Please leave a message after the tone.",
            "start_timestamp": 0.3}
MESSAGE = ("Hi Maria, this is Taylor from Example Agency. We saw you're looking for a new mobile app. "
           "Give us a call back at nine four five, nine four five, zero two zero eight. Thanks!")


async def run():
    # 1. Message ON: the AI says it after the beep, then the voicemail tool. We close the line just after it.
    secs = calls.speech_seconds(MESSAGE)
    call, ld, closed = await scenario(ON, "Maria", [GREETING, {
        "role": "assistant", "text": MESSAGE, "start_timestamp": 0.5,
        "tool_calls": [{"id": "tc_vm1", "name": "voicemail_detection", "arguments": {}, "result": "ok"}]}])
    assert call.vm_message_left and call.outcome == "voicemail" and call.result == "voicemail", (call.vm_message_left, call.outcome)
    assert ld.status == "voicemail" and ld.vm_count == 1, (ld.status, ld.vm_count)
    assert closed is not None and secs - 0.5 <= closed <= secs + 3, (closed, secs)  # not cut off, no long dead air
    assert "after leaving the voicemail message" in call.reason, call.reason
    with get_session() as s:
        ev = [e.text for e in s.exec(select(LeadEvent).where(LeadEvent.lead_id == ld.id)).all()]
    assert any("Voicemail" in e and "message left" in e for e in ev), ev
    print(f"ON: message (~{secs:.1f}s) left, line closed {closed:.1f}s after it started; lead: voicemail, retried later")

    # 2. Message ON, the greeting is heard but the AI hasn't spoken yet: wait for it (not the 1 s fast hang-up).
    call, ld, closed = await scenario(ON, "Slow AI", [GREETING])
    assert closed is not None and closed >= calls.VM_MESSAGE_START_WAIT_S - 0.3, closed
    assert not call.vm_message_left and call.outcome == "voicemail"
    print(f"ON, AI silent after the greeting: waited {closed:.1f}s for the message, then closed")

    # 3. Message ON but the mailbox is full: nothing to leave, close at once.
    call, ld, closed = await scenario(ON, "Full", [
        {"role": "user", "text": "Sorry, the mailbox is full and cannot accept any new messages.", "start_timestamp": 0.5}])
    assert closed is not None and closed < 2.5 and not call.vm_message_left, closed
    print(f"ON, mailbox full: closed {closed:.1f}s later, no message")

    # 4. Message ON, the AI uses the voicemail tool without the message: close at once.
    call, ld, closed = await scenario(ON, "Tool only", [
        {"role": "user", "text": "Hello, you've reached Bob.", "start_timestamp": 0.5},
        {"role": "assistant", "text": "", "start_timestamp": 2.0,
         "tool_calls": [{"id": "tc_vm2", "name": "voicemail_detection", "arguments": {}, "result": "ok"}]}])
    assert closed is not None and closed < 2.5 and not call.vm_message_left and call.outcome == "voicemail"
    print(f"ON, tool without a message: closed {closed:.1f}s later")

    # 5. Message ON, a real person: the normal opening line is not mistaken for the message.
    call, ld, closed = await scenario(ON, "Person", [
        {"role": "user", "text": "Hello?", "start_timestamp": 0.5},
        {"role": "assistant", "text": "Hey, this is Taylor from Example Agency. Did I catch you at an okay time?",
         "start_timestamp": 1.5}], finish_after=1.5)
    assert closed is None and not call.vm_message_left and call.outcome == "needs_review", (closed, call.outcome)
    print("ON, real person: no voicemail, not closed")

    # 6. Message OFF: the voicemail tool -> fast hang-up, as before. A message-like line isn't watched for.
    call, ld, closed = await scenario(OFF, "Off", [GREETING, {
        "role": "assistant", "text": "", "start_timestamp": 3.0,
        "tool_calls": [{"id": "tc_vm3", "name": "voicemail_detection", "arguments": {}, "result": "ok"}]}])
    assert closed is not None and closed < 2.5 and not call.vm_message_left and ld.status == "voicemail", closed
    print(f"OFF: fast hang-up {closed:.1f}s after the tool, no message")


asyncio.run(run())
calls.load_settings = load_settings

# Agent config: the instructions carry the message (lead details as {{variables}}); the tool has no message.
cfg = agent_config.build_config(ON)
ins = cfg["instructions"]
assert "Wait until the greeting has finished" in ins and "{{lead_name}}" in ins.split("# Voicemail")[1]
assert ON.company_phone in ins.split("# Voicemail")[1] and cfg["system_tools"]["voicemail_detection"]["message"] is None
off = agent_config.build_config(OFF)["instructions"]
assert "use the voicemail tool right away" in off and "Wait until the greeting" not in off
assert agent_config.config_hash(cfg) != agent_config.config_hash(agent_config.build_config(OFF))  # switch -> re-sync
print("config: message in the prompt when ON, fast hang-up rule when OFF")

# Matching: the expected message for a lead, said in slightly different words.
exp = " ".join(ON.fill(ON.voicemail.message, lead={"lead_name": "Maria", "service_required": "a new mobile app"}).split())
assert calls.is_voicemail_message(MESSAGE, exp)
assert not calls.is_voicemail_message("Hey, this is Taylor from Example Agency. Got a minute?", exp)
assert not calls.is_voicemail_message("Thanks, bye!", exp)
print("ALL VOICEMAIL MESSAGE TESTS PASS")
