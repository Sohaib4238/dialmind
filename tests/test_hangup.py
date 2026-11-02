"""Offline tests for the hang-up fixes (fake Cartesia, no network, no call)."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import sys
import time

DB = f"{TMP}/hangup_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup  # noqa: E402
from app.db import Call, get_session  # noqa: E402
from app.main import app  # noqa: E402

calls.POLL_EVERY_S = 0.05
calls.AFTER_GOODBYE_CLOSE_S = 0.1
calls.LINE_STILL_OPEN_S = 0.3
calls.SILENCE_MAX_S = 60
calls.HANGUP_CONFIRM_S = 0.6
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_WEBHOOK_SECRET="s3cret")


class Fake:
    def __init__(self):
        self.calls, self.cancels, self.n = {}, [], 0

    async def create_call(self, *a, **k):
        self.n += 1
        cid = f"ac_h{self.n}"
        self.calls[cid] = {"id": cid, "status": "started", "transcript": [], "cancel_works": True}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        return {k: v for k, v in c.items() if k != "cancel_works"}

    async def cancel_call(self, cid):
        self.cancels.append(cid)
        if self.calls[cid]["cancel_works"]:
            self.calls[cid].update(status="completed", end_reason="api_cancelled")
        return {"cancelled": True}


fake = Fake()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fake.create_call, fake.get_call, fake.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync
END_TURN = {"role": "assistant", "text": "Thanks, have a good one!", "start_timestamp": 10.0,
            "tool_calls": [{"id": "tc_end", "name": "end_call", "arguments": {}, "result": "ending"}]}


def events(c, cid):
    return c.get(f"/api/calls/{cid}").json()


with TestClient(app) as c:
    # 1) agent used end_call, Cartesia keeps the line open -> warning + one auto hang-up attempt
    cid = c.post("/api/calls", json={"number": "+17865550100"}).json()["id"]
    room = events(c, cid)["call"]["room"]
    fake.calls[room]["cancel_works"] = False
    fake.calls[room]["transcript"] = [{"role": "user", "text": "Hello?", "start_timestamp": 1.0}, END_TURN]
    time.sleep(3)  # about 1.7 s to say the goodbye, then the close and the warning
    d = events(c, cid)
    steps = [e["text"] for e in d["events"] if e["kind"] == "debug"]
    assert "line_still_open" in steps and fake.cancels.count(room) == 1, steps
    assert d["call"]["status"] == "answered"
    warn = [e["text"] for e in d["events"] if e["kind"] == "log"]
    print("1. line open after end_call ->", warn[0][:110], "...")
    # pressing Hang up now: Cartesia accepts but nothing changes -> clear 502
    r = c.post(f"/api/calls/{cid}/hangup")
    print("   hang up (not effective):", r.status_code, r.json()["detail"][:90], "...")
    assert r.status_code == 502 and "still reports the call as live" in r.json()["detail"]
    # the cap finally ends it
    fake.calls[room].update(status="completed", end_reason="max_duration", start_time="2026-10-27T10:00:00Z", end_time="2026-10-27T10:03:00Z")
    time.sleep(0.3)
    d = events(c, cid)
    assert d["call"]["status"] == "ended" and d["call"]["reason"] == "Max call length reached"
    n_transcript = sum(1 for e in d["events"] if e["kind"] == "transcript")
    # late webhook with the full call after the end -> no duplicate transcript lines
    c.post("/api/cartesia/webhook", headers={"x-webhook-secret": "s3cret"},
           json={"type": "call_completed", "call_id": room, "webhook_request_id": "late1", "call": fake.calls[room]})
    c.post("/api/cartesia/webhook", headers={"x-webhook-secret": "s3cret"},
           json={"type": "call_turn", "call_id": room, "webhook_request_id": "late2", "turn": END_TURN})
    d = events(c, cid)
    assert sum(1 for e in d["events"] if e["kind"] == "transcript") == n_transcript == 2
    assert sum(1 for e in d["events"] if e["text"] == "tool_call") == 1
    print("   late webhooks after the end: no duplicates (2 transcript lines, 1 tool call)")

    # 2) hang up that works -> 200, ended, "Hung up from dashboard"
    cid = c.post("/api/calls", json={"number": "+17865550100"}).json()["id"]
    room = events(c, cid)["call"]["room"]
    fake.calls[room]["transcript"] = [{"role": "user", "text": "Hi", "start_timestamp": 0.5}]
    time.sleep(0.2)
    r = c.post(f"/api/calls/{cid}/hangup")
    d = events(c, cid)
    print("2. hang up (works):", r.status_code, r.json(), "->", d["call"]["status"], d["call"]["reason"])
    assert r.status_code == 200 and r.json()["ended"] and d["call"]["reason"] == "Hung up from dashboard"

    # 3) backend restart with a call left "Live": re-checked and corrected
    fake.calls["ac_left_live"] = {"id": "ac_left_live", "status": "completed", "end_reason": "client_hangup",
                                  "transcript": [{"role": "user", "text": "Bye", "start_timestamp": 3.0}],
                                  "start_time": "2026-10-27T10:00:00Z", "end_time": "2026-10-27T10:00:42Z", "cancel_works": True}
    with get_session() as s:
        row = Call(room="ac_left_live", number="+17865550100", status="answered", source="dashboard")
        s.add(row)
        s.commit()
        s.refresh(row)
        stale_id = row.id

    async def restart():
        m = calls.CallManager()
        m.mark_stale_calls()
        await asyncio.sleep(0.3)

    c.portal.call(restart)
    d = events(c, stale_id)
    print("3. restart re-check:", d["call"]["status"], d["call"]["result"], d["call"]["reason"], d["call"]["duration_s"])
    assert d["call"]["status"] == "ended" and d["call"]["reason"] == "Callee hung up" and d["call"]["duration_s"] == 42.0

    h = c.post("/api/health/check").json()
    m = next(x for x in h["checks"] if x["name"] == "Cartesia minutes")
    print("4. health:", m["detail"])

print("ALL HANG-UP TESTS PASS")
