"""Offline tests: Cartesia call flow with a fake Cartesia API (no network, no call)."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import sys
import time

DB = f"{TMP}/cartesia_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""
os.environ["DASHBOARD_PASSWORD_HASH"] = ""  # no remote login: tunnel requests get 404  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup  # noqa: E402
from app.main import app  # noqa: E402

calls.POLL_EVERY_S = 0.05
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_WEBHOOK_SECRET="s3cret")


class FakeCartesia:
    """Scripted Cartesia call: timeline of (seconds, call object)."""

    def __init__(self):
        self.script, self.t0, self.cancelled, self.n = [], 0.0, False, 0
        self.refuse = False  # (not by number: test mode sends every call to the test number)

    def load(self, script):
        self.script, self.t0, self.cancelled = script, time.monotonic(), False

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.n += 1
        self.t0 = time.monotonic()
        if self.refuse:
            raise cartesia.CartesiaError("Cartesia refused the call: concurrency limit", 200)
        return f"ac_test{self.n}"

    async def get_call(self, call_id):
        now = time.monotonic() - self.t0
        cur = {"id": call_id, "status": "created", "transcript": []}
        for at, obj in self.script:
            if now >= at:
                cur = {"id": call_id, **obj}
        if self.cancelled and cur["status"] in ("created", "started"):
            cur = {"id": call_id, "status": "completed", "end_reason": "api_cancelled", "transcript": cur.get("transcript", [])}
        return cur

    async def cancel_call(self, call_id):
        self.cancelled = True
        return {"cancelled": True}


fake = FakeCartesia()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fake.create_call, fake.get_call, fake.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync

T = [{"role": "user", "text": "Hello?", "start_timestamp": 0.4, "stt_ttfb": 0.21},
     {"role": "assistant", "text": "Hey, this is Taylor from Example Agency.", "start_timestamp": 1.2, "tts_ttfb": 0.33,
      "tool_calls": [{"id": "tc1", "name": "end_call", "arguments": {}, "result": "ok"}]}]


def wait_final(c, cid, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/calls/{cid}").json()
        if d["call"]["status"] in ("ended", "failed"):
            return d
        time.sleep(0.05)
    raise AssertionError(f"call {cid} never finished: {d['call']}")


with TestClient(app) as c:
    def scenario(name, script, number="+17865550100", hangup_after=None):
        fake.load(script)
        r = c.post("/api/calls", json={"number": number})
        if r.status_code != 200:
            print(f"== {name}: HTTP {r.status_code} {r.json()['detail']}")
            return None
        cid = r.json()["id"]
        if hangup_after is not None:
            time.sleep(hangup_after)
            assert c.post(f"/api/calls/{cid}/hangup").status_code == 200
        d = wait_final(c, cid)
        call = d["call"]
        kinds = [(e["kind"], e["text"]) for e in d["events"]]
        print(f"== {name}: {call['status']} / {call['result']} / {call['reason']!r} / talk {call['duration_s']}s")
        print("   events:", [k for k in kinds if k[0] != "status"][:14])
        return call, d["events"]

    call, ev = scenario("answered, callee hangs up", [
        (0.0, {"status": "created", "transcript": []}),
        (0.2, {"status": "started", "start_time": "2026-10-27T10:00:00Z", "transcript": T[:1]}),
        (0.4, {"status": "completed", "end_reason": "client_hangup", "start_time": "2026-10-27T10:00:00Z",
               "end_time": "2026-10-27T10:00:21.5Z", "transcript": T, "summary": "Greeted, callee hung up."})])
    assert call["result"] == "answered" and call["duration_s"] == 21.5 and call["reason"] == "Callee hung up"
    assert sum(1 for e in ev if e["kind"] == "transcript") == 2  # no duplicates across polls
    assert any(e["kind"] == "timing" for e in ev) and any(e["text"] == "tool_call" for e in ev)

    call, _ = scenario("voicemail", [(0.2, {"status": "completed", "end_reason": "voicemail_detected", "transcript": []})])
    assert call["result"] == "voicemail" and call["reason"] == "Reached voicemail"
    call, _ = scenario("no answer", [(0.2, {"status": "completed", "end_reason": "dial_no_answer"})])
    assert call["result"] == "no_answer"
    call, _ = scenario("busy", [(0.2, {"status": "completed", "end_reason": "dial_busy"})])
    assert call["result"] == "busy"
    call, _ = scenario("dial failed", [(0.2, {"status": "failed", "end_reason": "dial_failed", "error_message": "SIP 403"})])
    assert call["status"] == "failed" and "SIP 403" in call["reason"]
    call, _ = scenario("hang up while ringing", [(0.0, {"status": "created"})], hangup_after=0.2)
    assert call["result"] == "no_answer" and call["reason"] == "Hung up from dashboard before answer"
    call, _ = scenario("hang up after answer", [(0.0, {"status": "started", "transcript": T[:1]})], hangup_after=0.3)
    assert call["result"] == "answered" and call["reason"] == "Hung up from dashboard"
    fake.refuse = True
    assert scenario("Cartesia refuses", [], number="+15550000000") is None
    fake.refuse = False
    assert c.get("/api/calls/active").json()["call"] is None  # the refused call is not left "dialing"

    # Webhook auth + ingestion + dedupe
    fake.load([(0.0, {"status": "started", "transcript": []})])
    cid = c.post("/api/calls", json={"number": "+17865550100"}).json()["id"]
    room = c.get(f"/api/calls/{cid}").json()["call"]["room"]
    turn = {"type": "call_turn", "call_id": room, "webhook_request_id": "wr1",
            "turn": {"role": "user", "text": "Who is this?", "start_timestamp": 5.0}}
    assert c.post("/api/cartesia/webhook", json=turn).status_code == 401
    assert c.post("/api/cartesia/webhook", json=turn, headers={"x-webhook-secret": "wrong"}).status_code == 401
    ok = c.post("/api/cartesia/webhook", json=turn, headers={"x-webhook-secret": "s3cret"}).json()
    dup = c.post("/api/cartesia/webhook", json=turn, headers={"x-webhook-secret": "s3cret"}).json()
    print("webhook:", ok, dup)
    assert ok.get("type") == "call_turn" and dup.get("duplicate") is True
    other = c.post("/api/cartesia/webhook", json={"type": "call_started", "call_id": "ac_playground"}, headers={"x-webhook-secret": "s3cret"}).json()
    assert other.get("ignored") == "unknown call"
    done = {"type": "call_completed", "call_id": room, "webhook_request_id": "wr2",
            "call": {"id": room, "status": "completed", "end_reason": "agent_hangup", "transcript": []}}
    c.post("/api/cartesia/webhook", json=done, headers={"x-webhook-secret": "s3cret"})
    d = wait_final(c, cid)
    assert d["call"]["result"] == "answered" and any(e["kind"] == "transcript" and e["text"] == "Who is this?" for e in d["events"])
    print("webhook-driven call:", d["call"]["status"], d["call"]["result"], d["call"]["reason"])

    # Tunnel guard: only the webhook + ping are public
    cf = {"cf-ray": "abc", "cf-connecting-ip": "192.0.2.4"}
    assert c.get("/api/calls", headers=cf).status_code == 404
    assert c.post("/api/calls", json={"number": "+1"}, headers=cf).status_code == 404
    assert c.get("/api/settings", headers=cf).status_code == 404
    assert c.get("/api/public/ping", headers=cf).json() == {"ok": True}
    assert c.post("/api/cartesia/webhook", json=turn, headers=cf).status_code == 401  # reachable, but needs the secret
    assert c.get("/api/calls").status_code == 200  # local requests unaffected
    print("tunnel guard ok")

print("ALL CARTESIA CALL TESTS PASS")
