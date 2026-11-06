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
REAL_WAIT = calls.vm_wait_s
calls.vm_wait_s = lambda message: 2.5  # in real use: the message's length + 25 s (greeting, beep, reply time)

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


async def scenario(settings, name, turns, finish_after=None, wait=14, later=None):
    """later: (seconds, turns) appended to the transcript after that long (a line reaches us once it's said)."""
    calls.load_settings = lambda: settings
    lid = lead(name)
    c = await calls.manager.start_call("+17865550100", lead_id=lid)
    room = c.room
    await asyncio.sleep(0.2)  # answered
    t0 = time.monotonic()
    fake.calls[room]["transcript"] = list(turns)
    if later:
        async def add():
            await asyncio.sleep(later[0])
            fake.calls[room]["transcript"] = fake.calls[room]["transcript"] + later[1]
        asyncio.create_task(add())
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
    # 1. Message ON: the AI says it after the beep, then end_call (no voicemail tool when ON). Closed ~1.5 s after it.
    secs = calls.speech_seconds(MESSAGE)
    call, ld, closed = await scenario(ON, "Maria", [GREETING, {
        "role": "assistant", "text": MESSAGE, "start_timestamp": 0.5,
        "tool_calls": [{"id": "tc_end1", "name": "end_call", "arguments": {}, "result": "ok"}]}])
    assert call.vm_message_left and call.outcome == "voicemail" and call.result == "voicemail", (call.vm_message_left, call.outcome)
    assert ld.status == "voicemail" and ld.vm_count == 1, (ld.status, ld.vm_count)
    assert closed is not None and secs + 1.0 <= closed <= secs + 3.5, (closed, secs)  # whole message + ~1.5 s, no long dead air
    assert "after leaving the voicemail message" in call.reason, call.reason
    with get_session() as s:
        ev = [e.text for e in s.exec(select(LeadEvent).where(LeadEvent.lead_id == ld.id)).all()]
    assert any("Voicemail" in e and "message left" in e for e in ev), ev
    print(f"ON: message (~{secs:.1f}s) left, line closed {closed:.1f}s after it started; lead: voicemail, retried later")

    # 2. Message ON, the greeting is heard but the AI never speaks: wait (not the 1 s fast hang-up), then close.
    call, ld, closed = await scenario(ON, "Slow AI", [GREETING])
    assert closed is not None and closed >= 2.5 - 0.3, closed
    assert not call.vm_message_left and call.outcome == "voicemail" and "didn't leave the message" in call.reason
    print(f"ON, AI silent after the greeting: waited {closed:.1f}s for the message, then closed")

    # 2b. History #40 / #42 replayed: the greeting is heard, the message reaches us only once it has been said (later
    # than the old 8 s): the line stays open until the message has finished, then closes ~1.5 s after it.
    msg = {"role": "assistant", "text": MESSAGE, "start_timestamp": 14.0, "end_timestamp": 14.0 + secs,
           "tool_calls": [{"id": "tc_end2", "name": "end_call", "arguments": {}, "result": "ok"}]}
    calls.vm_wait_s = lambda message: 6.0
    call, ld, closed = await scenario(ON, "Late", [GREETING], later=(2.0, [msg]), wait=20)
    calls.vm_wait_s = lambda message: 2.5
    assert call.vm_message_left and closed is not None and closed >= 2.0 + 1.2, closed
    assert "after leaving the voicemail message" in call.reason, call.reason
    real = REAL_WAIT(" ".join(ON.fill(ON.voicemail.message).split()))
    assert real >= 30, real  # real use: far longer than the 8 s that cut History #40 / #42
    print(f"ON, message reaching us late: line kept open, closed {closed:.1f}s in, after the message (real wait up to {real:.0f}s)")

    # 2c. Message ON, the AI ends with some other line (not recognised as the message): close ~1.5 s after it.
    calls.vm_wait_s = lambda message: 6.0  # so this closes by the line's end (1 s line + 1.5 s), not the wait
    call, ld, closed = await scenario(ON, "Other line", [GREETING, {
        "role": "assistant", "text": "Sorry, wrong number. Goodbye.", "start_timestamp": 9.0, "end_timestamp": 10.0,
        "tool_calls": [{"id": "tc_end3", "name": "end_call", "arguments": {}, "result": "ok"}]}])
    calls.vm_wait_s = lambda message: 2.5
    assert closed is not None and 2.0 <= closed < 4.0 and not call.vm_message_left and call.outcome == "voicemail", closed
    assert "last line on voicemail" in call.reason, call.reason
    print(f"ON, the AI ended with another line: closed {closed:.1f}s later")

    # 3. Message ON but the mailbox is full: nothing to leave, close at once.
    call, ld, closed = await scenario(ON, "Full", [
        {"role": "user", "text": "Sorry, the mailbox is full and cannot accept any new messages.", "start_timestamp": 0.5}])
    assert closed is not None and closed < 2.5 and not call.vm_message_left, closed
    print(f"ON, mailbox full: closed {closed:.1f}s later, no message")

    # 4. Message ON but the agent still had the voicemail tool (as on History #40-#42): it ends the AI's side, so close at once.
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
vm = ins.split("# Voicemail")[1]
assert "Let the greeting finish" in vm and "{{lead_name}}" in vm and "use end_call in the same turn, right after the message" in vm
assert ON.company_phone in vm and ON.company_name in vm and "voicemail tool" not in vm
assert cfg["system_tools"]["voicemail_detection"] is None  # ON: no tool that hangs up without a word (History #40-#42)
assert cfg["system_tools"]["end_call"]["pre_tool_speech"] == "force"  # the AI has to speak (the message) before it
off_cfg = agent_config.build_config(OFF)
off = off_cfg["instructions"]
assert "use the voicemail tool right away" in off and "Let the greeting finish" not in off
assert off_cfg["system_tools"]["voicemail_detection"] == {"description": None, "pre_tool_speech": "off", "message": None}
assert agent_config.config_hash(cfg) != agent_config.config_hash(agent_config.build_config(OFF))  # switch -> re-sync
print("config: ON = message in the prompt, no voicemail tool, end_call after it; OFF = voicemail tool, fast hang-up")

# Matching: the expected message for a lead, said in slightly different words.
exp = " ".join(ON.fill(ON.voicemail.message, lead={"lead_name": "Maria", "service_required": "a new mobile app"}).split())
assert calls.is_voicemail_message(MESSAGE, exp)
assert not calls.is_voicemail_message("Hey, this is Taylor from Example Agency. Got a minute?", exp)
assert not calls.is_voicemail_message("Thanks, bye!", exp)
print("ALL VOICEMAIL MESSAGE TESTS PASS")
