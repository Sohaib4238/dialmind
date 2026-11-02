"""Offline tests: two-step silence handling, bad_line, close after goodbye, Telnyx webhook (fake Cartesia, no call)."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import base64
import json
import os
import sys
import time

DB = f"{TMP}/silence_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup  # noqa: E402
from app.main import app  # noqa: E402

calls.POLL_EVERY_S = 0.05
calls.AFTER_GOODBYE_CLOSE_S = 0.3
calls.GOODBYE_WAIT_MAX_S = 1.0
calls.CLOSE_AFTER_SECOND_CHECKIN_S = 0.4
calls.LINE_STILL_OPEN_S = 0.4
SILENCE = {"enabled": True, "check_s": 8, "max_s": 1.5}  # check_s: transcript seconds; max_s: wall seconds (shortened)
calls._silence_settings = lambda: dict(SILENCE)
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_WEBHOOK_SECRET="s3cret")


class Fake:
    def __init__(self):
        self.calls, self.n = {}, 0

    def script(self, turns, cancel_works=True):
        self.next = {"turns": turns, "cancel_works": cancel_works}

    async def create_call(self, *a, **k):
        self.n += 1
        cid = f"ac_s{self.n}"
        self.calls[cid] = {**self.next, "t0": time.monotonic(), "cancelled": False, "cancels": 0, "cancel_after": None}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        el = time.monotonic() - c["t0"]
        transcript = [t for at, t in c["turns"] if el >= at]
        if c["cancelled"]:
            return {"id": cid, "status": "completed", "end_reason": "api_cancelled", "transcript": transcript}
        return {"id": cid, "status": "started", "transcript": transcript}

    async def cancel_call(self, cid):
        c = self.calls[cid]
        c["cancels"] += 1
        c["cancel_after"] = time.monotonic() - c["t0"]
        if c["cancel_works"]:
            c["cancelled"] = True
        return {"cancelled": True}


fake = Fake()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fake.create_call, fake.get_call, fake.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync


def U(t0, t1, text):
    return {"role": "user", "text": text, "start_timestamp": t0, "end_timestamp": t1}


def A(t0, t1, text, tools=()):
    return {"role": "assistant", "text": text, "start_timestamp": t0, "end_timestamp": t1,
            "tool_calls": [{"id": f"tc{t0}", "name": n, "arguments": {}, "result": "ok"} for n in tools]}


def end_live(c):
    for fc in fake.calls.values():
        fc["cancelled"] = True
    time.sleep(0.3)


def run(c, name, turns, wait, cancel_works=True, lead_id=None):
    fake.script(turns, cancel_works)
    r = c.post(f"/api/leads/{lead_id}/call") if lead_id else c.post("/api/calls", json={"number": "+17865550100"})
    cid = r.json()["id"]
    end = time.time() + wait
    while time.time() < end:
        if c.get(f"/api/calls/{cid}").json()["call"]["status"] in ("ended", "failed"):
            break
        time.sleep(0.05)
    d = c.get(f"/api/calls/{cid}").json()
    room = d["call"]["room"]
    steps = [e["text"] for e in d["events"] if e["kind"] == "debug" and e["text"] not in ("agent_config", "call_requested", "cartesia_status")]
    print(f"== {name}: {d['call']['status']} / {d['call']['result']} / {d['call']['reason']!r} | cancels {fake.calls[room]['cancels']}")
    print("   steps:", steps)
    return d, fake.calls[room], steps


GREETING = [(0.1, U(1.0, 1.6, "Hello?")), (0.2, A(2.0, 4.0, "Hey, this is Taylor. Got a minute?"))]
with TestClient(app) as c:
    csv = "phone,name\n(786) 555-0100,Test Lead\n"
    c.post("/api/leads/import", json={"csv": csv, "filename": "t.csv"})
    lead = c.get("/api/leads").json()["leads"][0]

    # 1) callee gone: check-in 1 ("can you hear me?"), check-in 2 (bad line + end_call) -> close, lead = bad_line
    d, f, steps = run(c, "two check-ins, then close", GREETING + [
        (0.4, A(12.5, 14.0, "Hello, can you hear me okay?")),  # 8.5 s gap -> check-in 1
        (0.6, A(22.5, 22.5, "", tools=["end_call"])),
        (0.7, A(22.5, 26.0, "Sorry, I think the line is bad. I'll try you again another time.")),  # check-in 2
    ], wait=6, lead_id=lead["id"])
    assert steps.count("silence_checkin") == 2 and "closed_after_silence" in steps
    assert d["call"]["result"] == "bad_line" and d["call"]["reason"].startswith("Closed the line: no reply after two check-ins")
    ld = c.get(f"/api/leads/{lead['id']}").json()["lead"]
    print("   lead status:", ld["status"])
    assert ld["status"] == "bad_line"

    # 2) callee answers after check-in 1 -> count resets, call goes on
    d, f, steps = run(c, "answer after check-in 1", GREETING + [
        (0.3, A(12.5, 14.0, "Hello, can you hear me okay?")),
        (0.45, U(14.5, 15.0, "Yes, sorry, I'm here.")),
        (0.55, A(15.5, 17.0, "Great. What kind of business do you run?")),
        (0.7, A(25.5, 27.0, "Hello, can you hear me okay?")),  # new silence: check-in 1 again (count was reset)
    ], wait=1.0)
    assert d["call"]["status"] == "answered" and f["cancels"] == 0
    end_live(c)

    # 3) two check-ins but the agent never calls end_call -> we close a few seconds after the 2nd
    d, f, steps = run(c, "2nd check-in, no end_call", GREETING + [
        (0.3, A(12.5, 14.0, "Hello, can you hear me okay?")),
        (0.5, A(22.5, 25.0, "Sorry, I think the line is bad.")),
    ], wait=3)
    assert d["call"]["result"] == "bad_line" and f["cancels"] == 1

    # 4) backstop: dead air -> closed at max_silence_seconds, bad_line
    d, f, steps = run(c, "dead air", [], wait=4)
    assert d["call"]["result"] == "bad_line" and d["call"]["reason"].startswith("Closed the line after")

    # 5) a long agent pitch while the callee listens never trips it (normal replies keep it alive)
    d, f, steps = run(c, "long agent turns", [(0.1, U(1.0, 1.6, "Hello?"))] + [(0.2 + 0.25 * i, A(2.0 + 3 * i, 4.5 + 3 * i, f"part {i}")) for i in range(8)], wait=2.2)
    assert d["call"]["status"] == "answered" and f["cancels"] == 0
    end_live(c)

    # 6) silence handling off -> no check-in closing, no backstop
    SILENCE["enabled"] = False
    d, f, steps = run(c, "silence off", GREETING, wait=2.0)
    assert d["call"]["status"] == "answered" and f["cancels"] == 0
    end_live(c)
    SILENCE["enabled"] = True

    # 7) normal goodbye (no check-ins) -> "Closed the line after goodbye", result answered
    d, f, steps = run(c, "goodbye", GREETING + [(0.3, U(5.0, 5.5, "Not interested, bye.")),
                                                (0.4, A(6.0, 6.0, "", tools=["end_call"])),
                                                (0.5, A(5.9, 8.0, "No worries, have a good one!"))], wait=5)
    assert d["call"]["result"] == "answered" and d["call"]["reason"] == "Closed the line after goodbye"
    # its 2.1 s of audio + the 0.3 s buffer, from when the line reached us: past GOODBYE_WAIT_MAX_S, and not cut at it
    assert f["cancel_after"] >= 0.5 + 2.1 + 0.3, f["cancel_after"]

    # 7b) the goodbye's audio is already over when the line reaches us: only the buffer is left
    # (the text alone would be estimated at about 7 s: the timestamps win)
    long_bye = "Thanks so much for your time today, I'll let the team know, and we'll talk again soon. Take care, bye!"
    d, f, steps = run(c, "goodbye already said", [(0.1, U(0.01, 0.02, "Bye.")), (0.2, A(0.03, 0.05, long_bye, tools=["end_call"]))], wait=3)
    assert d["call"]["reason"] == "Closed the line after goodbye" and 0.5 <= f["cancel_after"] < 2.5, f["cancel_after"]

    # 7c) the goodbye arrives just before the end_call turn: it still counts
    d, f, steps = run(c, "goodbye, then end_call", GREETING + [(0.3, U(5.0, 5.5, "Bye.")), (0.4, A(6.0, 6.5, "Have a good one!")),
                                                               (0.5, A(6.6, 6.6, "", tools=["end_call"]))], wait=3)
    assert d["call"]["reason"] == "Closed the line after goodbye", d["call"]["reason"]

    # 7d) History #32: the AI's last line was a question, the lead answered, then end_call with no words at all.
    # Never reported as a goodbye; closed only after waiting for one. The AI's earlier question doesn't count.
    d, f, steps = run(c, "end_call without a goodbye", GREETING + [
        (0.3, A(64.6, 69.3, "No problem at all. Would tomorrow morning or tomorrow afternoon be better?")),
        (0.4, U(71.4, 77.3, "We will talk tomorrow.")),
        (0.5, A(78.8, 78.8, "", tools=["end_call"])),
    ], wait=3)
    assert d["call"]["reason"] == "Closed the line: the AI ended the call without a goodbye" and "closed_no_goodbye" in steps
    assert d["call"]["result"] == "answered" and f["cancel_after"] >= 0.5 + 1.0, f["cancel_after"]

    # 8) Telnyx call.hangup webhook (signed) closes the live call immediately
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    os.environ["TELNYX_PUBLIC_KEY"] = base64.b64encode(pub).decode()
    raw = json.dumps({"data": {"id": "ev1", "event_type": "call.hangup", "payload": {
        "to": "sip:+17865550100@sip.telnyx.com", "hangup_cause": "normal_clearing", "hangup_source": "callee"}}}).encode()
    ts = str(int(time.time()))
    hdr = {"telnyx-signature-ed25519": base64.b64encode(priv.sign(f"{ts}|".encode() + raw)).decode(), "telnyx-timestamp": ts}
    fake.script(GREETING)
    cid = c.post("/api/calls", json={"number": "+17865550100"}).json()["id"]
    time.sleep(0.4)
    assert c.post("/api/telnyx/webhook", content=raw, headers=hdr).json().get("closing") == cid
    time.sleep(0.4)
    assert c.get(f"/api/calls/{cid}").json()["call"]["reason"] == "Closed the line: the callee hung up (Telnyx)"
    print("== telnyx hang-up: closed")

print("ALL SILENCE TESTS PASS")
