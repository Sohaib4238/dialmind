"""Conversation rules from History #37/#38: a silent callee only hears "Hello?" (never the introduction), the
introduction is said once, a hand-over gets a short re-introduction with context, odd-sounding words are asked about,
and voicemail detection still works. Fake Cartesia + fake transcripts, throwaway DB, no calls. The AI's own words
can't be tested offline: this checks what it's told (the prompt) and what our backend does with each call."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import time

os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""
os.environ["SMS_SENDING_DISABLED"] = "1"
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""

import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import agent_config, cartesia, cartesia_setup, tunnel  # noqa: E402
from app.db import Call, Lead, get_session  # noqa: E402
from settings import load_settings  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel
from app.main import app  # noqa: E402

os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_OUTCOME_TOOL_ID="out_tool")
calls.POLL_EVERY_S = 0.05
calls._silence_settings = lambda: {"enabled": True, "check_s": 8, "max_s": 25, "greet_wait_s": 4}


class Fake:
    def __init__(self):
        self.calls, self.n, self.vars, self.cancels = {}, 0, [], []

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.n += 1
        cid = f"ac_c{self.n}"
        self.vars.append(variables)
        self.calls[cid] = {"status": "started", "end_reason": None, "transcript": []}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        return {"id": cid, "status": c["status"], "end_reason": c["end_reason"], "transcript": list(c["transcript"]),
                "start_time": "2026-11-04T15:00:00Z", "end_time": "2026-11-04T15:01:00Z"}

    async def cancel_call(self, cid):
        self.cancels.append(cid)
        self.calls[cid].update(status="completed", end_reason="api_cancelled")
        return {}

    def say(self, cid, role, text, start, tools=None):
        self.calls[cid]["transcript"].append({"role": role, "text": text, "start_timestamp": start,
                                              "end_timestamp": start + max(0.6, len(text) / 14), "tool_calls": tools or []})


fake = Fake()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fake.create_call, fake.get_call, fake.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync


def wait(pred, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError("timeout")


def row(i):
    with get_session() as s:
        return s.get(Call, i)


def tracker(i):
    return calls.manager.trackers[i]


s = load_settings()
intro = agent_config.introduction(s, {"lead_source": "Bark", "service_required": "a new website"})
cfg = agent_config.build_config(s)
ins = cfg["instructions"]

# ---- the prompt -----------------------------------------------------------------------------------------
assert cfg["initial_message"] == f'<break time="{s.call_start.wait_seconds}s"/>{{{{silent_greeting}}}}'
assert 'Your introduction is exactly this line: "{{intro_line}}"' in ins
assert intro == f"Hi, this is {s.caller_name}. I saw your request on Bark for a new website. Is now an okay time?", intro
assert s.company_name not in intro and f"Say it only when they ask who you're with" in ins  # company only when asked
assert "If they speak first, your first reply is your introduction." in ins
assert 'say only "{{silent_greeting}}": a check that they can hear you, not your introduction' in ins
assert "once per person, never twice" in ins and 'even if they say "Hello?" again' in ins
assert "even if one got cut off" in ins  # the lone "Hello," of #38 was a cut-off check-in
assert "# When someone else comes to the phone" in ins
assert f'"Hi! This is {s.caller_name}. I hear you handle the website?"' in ins
assert "Don't start the pitch over" in ins and "don't ask again what the first person already told you" in ins
assert "# If something they said sounds wrong" in ins and '"Sorry, who was that?"' in ins
assert '"Sorry, what\'s the name of your business?"' in ins and "don't build on it" in ins
assert s.fill(s.speaking_style) in ins  # the Instructions page's speaking style stays, word for word
assert "voicemail tool" in ins
inb = agent_config.build_inbound_config(s, "")["instructions"]
assert "# When someone else comes to the phone" in inb and "# If something they said sounds wrong" in inb
print("prompt: intro once and only after they speak, cut-off checks aren't intros, hand-over + mishearing rules, "
      "speaking style kept (outbound and inbound)")

with TestClient(app) as c:
    # ---- 1. silent pickup: after the wait only "Hello?", then the introduction once ----------------------
    with get_session() as db:
        lead = Lead(phone="+15125550101", name="Maria Lopez", business="Lopez Bakery", service_required="website")
        db.add(lead)
        db.commit()
        lid = lead.id
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    assert fake.vars[-1]["silent_greeting"] == "Hello?", fake.vars[-1]  # not "This is ... from ..." (History #38)
    wait(lambda: row(cid).status == "answered")
    fake.say(room, "assistant", '<break time="4s"/>Hello?', 4.1)
    fake.say(room, "user", "Hello.", 6.0)
    fake.say(room, "assistant", intro, 7.0)
    fake.say(room, "user", "Yeah, sure, what's up?", 13.0)
    wait(lambda: len([k for k in tracker(cid).seen_turns]) >= 4)
    tr = tracker(cid)
    assert tr.unanswered_checkins == 0 and tr.voicemail_at is None, (tr.unanswered_checkins, tr.voicemail_at)
    time.sleep(0.3)
    assert room not in fake.cancels  # a normal call goes on
    texts = [e["text"] for e in c.get(f"/api/calls/{cid}").json()["events"] if e["kind"] == "transcript"]
    assert texts[0] == "Hello?" and sum(intro in t for t in texts) == 1, texts
    c.post(f"/api/calls/{cid}/hangup")
    wait(lambda: row(cid).status == "ended")
    print("silent pickup: \"Hello?\" after the wait (no intro, not a check-in), intro once after their hello")

    # ---- 2. callee speaks first: the introduction is the first reply ----------------------------------
    call = c.post("/api/calls", json={"number": "+15125550102"}).json()
    cid, room = call["id"], call["room"]
    wait(lambda: row(cid).status == "answered")
    fake.say(room, "user", "Hello?", 1.0)
    fake.say(room, "assistant", intro, 2.2)
    wait(lambda: len(tracker(cid).seen_turns) >= 2)
    assert tracker(cid).unanswered_checkins == 0 and tracker(cid).voicemail_at is None
    c.post(f"/api/calls/{cid}/hangup")
    wait(lambda: row(cid).status == "ended")
    print("callee first: intro is the first reply; nothing else happens")

    # ---- 3. hand-over to a new person: a short wait doesn't close the line ---------------------------
    call = c.post("/api/calls", json={"number": "+15125550103"}).json()
    cid, room = call["id"], call["room"]
    wait(lambda: row(cid).status == "answered")
    fake.say(room, "user", "Hello?", 1.0)
    fake.say(room, "assistant", intro, 2.0)
    fake.say(room, "user", "That's my wife, let me get her.", 8.0)
    fake.say(room, "assistant", "Sure, thanks.", 10.5)
    fake.say(room, "user", "Hello, this is his wife.", 16.0)  # 4.5 s later: under the 8 s check-in
    handover = f"Hi! This is {s.caller_name}. I hear you handle the website?"
    fake.say(room, "assistant", handover, 17.5)
    wait(lambda: len(tracker(cid).seen_turns) >= 6)
    tr = tracker(cid)
    assert tr.unanswered_checkins == 0 and room not in fake.cancels and row(cid).status == "answered"
    c.post(f"/api/calls/{cid}/hangup")
    wait(lambda: row(cid).status == "ended")
    print("hand-over: the wait for the new person isn't silence; short re-intro with context")

    # ---- 4. voicemail: the greeting interrupts the pause, the line is closed, no talking over it ---------
    call = c.post("/api/calls", json={"number": "+15125550104"}).json()
    cid, room = call["id"], call["room"]
    wait(lambda: row(cid).status == "answered")
    fake.say(room, "user", "Hi, you've reached Lopez Bakery. Please leave a message after the tone.", 0.8)
    wait(lambda: room in fake.cancels)
    wait(lambda: row(cid).status == "ended")
    assert row(cid).result == "voicemail", (row(cid).result, row(cid).reason)
    agent_lines = [e for e in c.get(f"/api/calls/{cid}").json()["events"] if e["kind"] == "transcript" and e["role"] == "agent"]
    assert agent_lines == [], agent_lines
    print("voicemail: greeting heard during the pause -> closed as voicemail, the AI said nothing")

print("ALL CONVERSATION TESTS PASSED")
