"""Offline tests for the auto-dialer (fake Cartesia, fixed clock, throwaway DB; no call placed)."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

DB = f"{TMP}/dialer_test.db"
STATE = f"{TMP}/dialer_state_test.json"  # never the real switch
for f in (DB, STATE):
    if os.path.exists(f):
        os.remove(f)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["DIALER_STATE_PATH"] = STATE
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
import app.dialer as dialer_mod  # noqa: E402
from app import cartesia, cartesia_setup, health  # noqa: E402
from app.db import Lead, get_session  # noqa: E402
from app.main import app, dialer  # noqa: E402

TEST = os.environ["TEST_CALL_NUMBER"]
calls.POLL_EVERY_S = 0.05
dialer_mod.GAP_BETWEEN_CALLS_S = 0
dialer_mod.IDLE_RECHECK_S = 1
# Tuesday 2026-10-27 15:00 UTC = 11:00 New York, 10:00 Chicago, 08:00 Los Angeles (outside 09:00-18:00)
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
dialer_mod.utcnow = lambda: NOW
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_WEBHOOK_SECRET="s3cret")


class Fake:
    def __init__(self):
        self.dialed, self.outcomes, self.calls, self.n = [], [], {}, 0

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.dialed.append(to)
        self.n += 1
        cid = f"ac_d{self.n}"
        reason = self.outcomes.pop(0) if self.outcomes else "client_hangup"
        self.calls[cid] = {"t0": time.monotonic(), "reason": reason}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        if time.monotonic() - c["t0"] < 0.15:
            return {"id": cid, "status": "started", "transcript": []}
        if c["reason"] in ("dial_failed",):
            return {"id": cid, "status": "failed", "end_reason": "dial_failed", "error_message": "SIP 403"}
        t = [{"role": "user", "text": "Hi", "start_timestamp": 0.5}] if c["reason"] == "client_hangup" else []
        return {"id": cid, "status": "completed", "end_reason": c["reason"], "transcript": t,
                "start_time": "2026-10-27T15:00:00Z", "end_time": "2026-10-27T15:00:20Z"}

    async def cancel_call(self, cid):
        return {"cancelled": True}


fake = Fake()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fake.create_call, fake.get_call, fake.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync


def wait_state(c, pred, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get("/api/dialer").json()
        if pred(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"timeout; dialer = {s['state']} {s['reason']!r} {s['wait_detail']!r}")


def lead_by(c, name):
    return next(x for x in c.get("/api/leads").json()["leads"] if x["name"] == name)


def reset_leads(c, csv):
    with get_session() as s:
        for x in s.exec(__import__("sqlmodel").select(Lead)).all():
            s.delete(x)
        s.commit()
    c.post("/api/leads/import", json={"csv": csv, "filename": "t.csv"})


try:
    with TestClient(app) as c:
        assert dialer.state == "off"  # never auto-starts
        assert c.post("/api/dialer/test-mode", json={"enabled": True}).json()["test_mode"] is True

        # 1) test mode, calling hours, DNC, order, one at a time
        reset_leads(c, "phone,name\n(305) 555-0101,Miami New\n(415) 555-0102,SF New\n(212) 555-0103,NY New\n(786) 555-0104,DNC Lead\n")
        c.post(f"/api/leads/{lead_by(c, 'DNC Lead')['id']}/dnc", json={"dnc": True})
        fake.outcomes = ["client_hangup", "dial_no_answer"]
        snap = c.get("/api/dialer").json()
        print("next before ON:", snap["next"]["name"], snap["next"]["local_time"], "| queue:", snap["queue"])
        c.post("/api/dialer/on")
        s = wait_state(c, lambda s: s["session_calls"] == 2 and not s["current"] and s["wait_detail"].startswith("Nothing"))
        print("1.", s["state"], "| calls:", s["session_calls"], "| dialed:", fake.dialed, "| wait:", s["wait_detail"])
        assert fake.dialed == [TEST, TEST]  # test mode: every call goes to the test number
        st = {n: lead_by(c, n)["status"] for n in ("Miami New", "NY New", "SF New", "DNC Lead")}
        print("   lead statuses:", st)
        assert st == {"Miami New": "needs_review", "NY New": "no_answer", "SF New": "new", "DNC Lead": "do_not_contact"}
        assert "outside calling hours" in s["wait_detail"]
        log = [e["text"] for e in s["log"]]
        assert any("Calling Miami New" in t and "test number" in t for t in log)
        c.post("/api/dialer/off")

        # 2) retry rules: no_answer due after 2 h, max 2 extra tries, answered never retried
        ny = lead_by(c, "NY New")
        with get_session() as db:
            row = db.get(Lead, ny["id"])
            row.last_call_at = NOW - timedelta(hours=1)
            db.add(row)
            db.commit()
        lead, info = dialer_mod.pick_next()
        assert lead is None and info["retries_waiting"] == 1, info
        lead, info = dialer_mod.pick_next(NOW + timedelta(hours=1, minutes=1))
        # 09:01 in San Francisco now: the new SF lead goes first, the NY retry is due behind it
        assert lead and lead.name == "SF New" and info["retries_due"] == 1, (lead and lead.name, info)
        with get_session() as db:
            row = db.get(Lead, ny["id"])
            row.attempts = 3
            db.add(row)
            db.commit()
        lead, info = dialer_mod.pick_next(NOW + timedelta(hours=5))
        # 1 call + 2 retries used up for NY; Miami (answered) never retried; only SF (new) is left
        assert lead.name == "SF New" and info["retries_due"] == 0 and info["retries_waiting"] == 0, info
        print("2. retries: not due before 2 h, due after, stop after 2 extra tries; answered never retried")

        # 3) max calls per session (3) then OFF by itself
        reset_leads(c, "phone,name\n" + "".join(f"(305) 555-01{10 + i},Lead {i}\n" for i in range(6)))
        fake.dialed.clear()
        c.post("/api/dialer/on")
        s = wait_state(c, lambda s: s["state"] == "off")
        print("3.", s["state"], "|", s["reason"], "| calls:", s["session_calls"])
        assert s["session_calls"] == 3 and "max calls this session" in s["reason"] and len(fake.dialed) == 3

        # 4) 3 failed calls in a row -> paused with the reason
        reset_leads(c, "phone,name\n" + "".join(f"(305) 555-01{30 + i},Fail {i}\n" for i in range(5)))
        fake.outcomes = ["dial_failed"] * 5
        c.post("/api/dialer/on")
        s = wait_state(c, lambda s: s["state"] == "paused")
        print("4.", s["state"], "|", s["reason"])
        assert s["failures_in_row"] == 3 and "3 failed calls in a row" in s["reason"]
        assert lead_by(c, "Fail 0")["status"] == "failed"
        c.post("/api/dialer/off")

        # 5) low free minutes -> stops before calling
        real = health.minutes_left
        health.minutes_left = lambda: 1.5
        fake.dialed.clear()
        c.post("/api/dialer/on")
        s = wait_state(c, lambda s: s["state"] == "off")
        print("5.", s["state"], "|", s["reason"])
        assert "Cartesia minutes left" in s["reason"] and fake.dialed == []
        health.minutes_left = real

        # 6) never two calls at once: a manual call is live -> the dialer waits
        reset_leads(c, "phone,name\n(305) 555-0150,Wait Lead\n")
        fake.outcomes = ["client_hangup"]
        calls_before = len(fake.dialed)
        manual = c.post("/api/calls", json={"number": "+17865550100"}).json()["id"]
        c.post("/api/dialer/on")
        s = c.get("/api/dialer").json()
        print("6. while a manual call is live:", s["wait_detail"] or "(no wait yet)")
        s = wait_state(c, lambda s: s["session_calls"] == 1 and not s["current"])
        assert len(fake.dialed) == calls_before + 2  # manual + dialer, one after the other
        c.post("/api/dialer/off")

        # 7) test mode off needs confirmation; not allowed while the dialer is on
        assert c.post("/api/dialer/test-mode", json={"enabled": False}).status_code == 400
        c.post("/api/dialer/on")
        assert c.post("/api/dialer/test-mode", json={"enabled": False, "confirm": True}).status_code == 409
        c.post("/api/dialer/off")
        time.sleep(1.2)
        r = c.post("/api/dialer/test-mode", json={"enabled": False, "confirm": True}).json()
        assert r["test_mode"] is False
        assert dialer_mod.AutoDialer(None).state == "off" and dialer_mod.AutoDialer(None).test_mode is False  # remembered
        c.post("/api/dialer/test-mode", json={"enabled": True})
        print("7. test mode: off needs confirm, blocked while ON, remembered across restarts; a new dialer starts OFF")
finally:
    os.path.exists(STATE) and os.remove(STATE)

print("ALL DIALER TESTS PASS")
