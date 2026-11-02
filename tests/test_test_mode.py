"""Offline tests: TEST MODE sends EVERY call to TEST_CALL_NUMBER (fake Cartesia, throwaway DB + state file; no call).

Bug 2026-10-28 (History #24, #26): "Call this lead" dialed the lead's real number with test mode ON, because
only the auto-dialer swapped in the test number. Now calls.start_call routes every call and
cartesia.create_call refuses anything but the test number while test mode is on.
Paths: Call page (any number), "Call this lead", auto-dialer (new lead), retry, callback; plus the hard guard.
"""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

DIR = TMP
DB, STATE = f"{DIR}/test_mode_test.db", f"{DIR}/test_mode_state.json"
for f in (DB, STATE):
    if os.path.exists(f):
        os.remove(f)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["DIALER_STATE_PATH"] = STATE  # never the real data/dialer_state.json
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""
os.environ["DASHBOARD_PASSWORD_HASH"] = ""
TEST = "+17865550100"
os.environ["TEST_CALL_NUMBER"] = TEST
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import app.calls as calls  # noqa: E402
import app.dialer as dialer_mod  # noqa: E402
from app import cartesia, cartesia_setup, health, test_mode, tunnel  # noqa: E402
from app.db import Call, Lead, get_session  # noqa: E402
from app.main import app, dialer  # noqa: E402

calls.POLL_EVERY_S = 0.05
dialer_mod.GAP_BETWEEN_CALLS_S = 0
dialer_mod.IDLE_RECHECK_S = 1
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)  # Tue 11:00 in Miami: inside calling hours
dialer_mod.utcnow = lambda: NOW
health.minutes_left = lambda: 50.0
tunnel.watch = lambda *a, **k: asyncio.sleep(0)  # never touch the real webhook / tools
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_WEBHOOK_SECRET="s3cret")

real_create_call = cartesia.create_call
dialed, counter = [], []


async def fake_create(agent_id, from_id, to, variables, ring, max_min):
    test_mode.guard(to)  # the fake keeps the real last check (the real create_call is tested below)
    dialed.append(to)
    counter.append(1)
    return f"ac_tm{len(counter)}"


async def fake_get(cid):
    return {"id": cid, "status": "completed", "end_reason": "dial_no_answer", "transcript": []}


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia.create_call, cartesia.get_call, cartesia_setup.sync_agent = fake_create, fake_get, fake_sync
cartesia.cancel_call = lambda cid: asyncio.sleep(0, {"cancelled": True})


def wait_idle(c, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if c.get("/api/calls/active").json()["call"] is None:
            return
        time.sleep(0.05)
    raise AssertionError("call never ended")


def lead_row(name):
    with get_session() as s:
        return s.exec(select(Lead).where(Lead.name == name)).one()


def call_rows():
    with get_session() as s:
        return s.exec(select(Call).order_by(Call.id)).all()


# 0) the switch: missing or broken file = ON, the real file is never used here
assert test_mode.state_path().as_posix() == STATE and test_mode.is_on()
open(STATE, "w").write("{not json")
assert test_mode.is_on()
os.remove(STATE)
print("0. missing / broken state file -> test mode ON")

with TestClient(app) as c:
    assert c.get("/api/test-mode").json() == {"on": True, "number": TEST}
    c.post("/api/leads/import", json={"csv": "phone,name,location\n"
           "(305) 555-0201,Manual Lead,Miami FL\n(305) 555-0202,New Lead,Miami FL\n"
           "(305) 555-0203,Retry Lead,Miami FL\n(305) 555-0204,Callback Lead,Miami FL\n", "filename": "t.csv"})
    manual = lead_row("Manual Lead")

    # 1) Call page, any number -> test number (and a lead's number still counts for that lead)
    r = c.post("/api/calls", json={"number": "+12125550199"})
    assert r.status_code == 200 and r.json()["number"] == TEST and dialed == [TEST], (r.json(), dialed)
    wait_idle(c)
    r = c.post("/api/calls", json={"number": manual.phone})
    assert r.status_code == 200 and r.json()["number"] == TEST and r.json()["lead_id"] == manual.id
    wait_idle(c)
    ev = c.get(f"/api/calls/{r.json()['id']}").json()["events"]
    assert any(e["text"] == "test_mode" and (e["data"] or {}).get("requested") == manual.phone for e in ev), ev
    print("1. Call page:", dialed)

    # 2) "Call this lead" (the bug: it dialed the lead's own number)
    dialed.clear()
    r = c.post(f"/api/leads/{manual.id}/call")
    assert r.status_code == 200 and dialed == [TEST] and r.json()["lead_id"] == manual.id
    wait_idle(c)
    print("2. Call this lead:", dialed)

    # 3-5) auto-dialer: a due callback, a new lead, a due retry -> all to the test number
    with get_session() as s:
        for name, kw in (("Manual Lead", {"status": "not_interested"}),
                         ("Retry Lead", {"status": "no_answer", "attempts": 1, "last_call_at": NOW - timedelta(hours=3)}),
                         ("Callback Lead", {"status": "callback", "attempts": 1, "last_call_at": NOW - timedelta(days=2),
                                            "callback_at": NOW - timedelta(minutes=5)})):
            row = s.exec(select(Lead).where(Lead.name == name)).one()
            for k, v in kw.items():
                setattr(row, k, v)
            s.add(row)
        s.commit()
    dialed.clear()
    n_before = len(call_rows())
    c.post("/api/dialer/on")
    end = time.time() + 10
    while time.time() < end and dialer.state == "on":
        time.sleep(0.1)
    new_calls = call_rows()[n_before:]
    by_lead = {lead_row(n).id: n for n in ("Callback Lead", "New Lead", "Retry Lead")}
    order = [by_lead.get(x.lead_id) for x in new_calls]
    print("3-5. dialer:", order, dialed, "|", dialer.reason)
    assert order == ["Callback Lead", "New Lead", "Retry Lead"], order  # callback, new, retry
    assert dialed == [TEST, TEST, TEST] and all(x.number == TEST for x in new_calls)

    # 6) test mode ON but no TEST_CALL_NUMBER -> refused, nothing dialed
    dialed.clear()
    os.environ.pop("TEST_CALL_NUMBER")  # in-process only (.env was loaded once at import)
    r = c.post(f"/api/leads/{manual.id}/call")
    assert r.status_code == 400 and "TEST_CALL_NUMBER is not set" in r.json()["detail"] and dialed == [], r.json()
    os.environ["TEST_CALL_NUMBER"] = TEST
    print("6. no test number ->", r.json()["detail"])

    # 7) test mode OFF (with confirmation): the lead's own number
    assert c.post("/api/dialer/test-mode", json={"enabled": False, "confirm": True}).json()["test_mode"] is False
    assert c.get("/api/test-mode").json()["on"] is False
    r = c.post(f"/api/leads/{lead_row('New Lead').id}/call")
    assert r.status_code == 200 and dialed == [lead_row("New Lead").phone], dialed
    wait_idle(c)
    c.post("/api/dialer/test-mode", json={"enabled": True})
    assert test_mode.is_on()
    print("7. test mode off -> lead's own number; back on")


# 8) the hard guard in the REAL cartesia.create_call: refuses + logs, the request never goes out
sent = []


async def fake_request(method, path, **kw):
    sent.append(kw["json"]["outbound_calls"][0]["to_number"])
    return {"calls": [{"agent_call_id": "ac_guard"}]}

records = []
logging.getLogger("dashboard.test_mode").addHandler(type("H", (logging.Handler,), {"emit": lambda self, r: records.append(r)})())
cartesia.request = fake_request  # no network
kill = os.environ.get("CARTESIA_CALLS_DISABLED")
os.environ["CARTESIA_CALLS_DISABLED"] = "0"  # lift the kill switch in-process only; request is fake
try:
    try:
        asyncio.run(real_create_call("agent_test", "ap_test", "+16025550105", {}, 30, 2))
        raise AssertionError("guard did not block")
    except cartesia.CartesiaError as e:
        assert "Blocked: TEST MODE is on" in str(e) and sent == []
    assert any(r.levelno == logging.ERROR and "+16025550105" in r.getMessage() for r in records)
    assert asyncio.run(real_create_call("agent_test", "ap_test", TEST, {}, 30, 2)) == "ac_guard" and sent == [TEST]
    test_mode.set_on(False)
    assert asyncio.run(real_create_call("agent_test", "ap_test", "+16025550105", {}, 30, 2)) == "ac_guard"
    test_mode.set_on(True)
finally:
    if kill is None:
        os.environ.pop("CARTESIA_CALLS_DISABLED", None)
    else:
        os.environ["CARTESIA_CALLS_DISABLED"] = kill
    os.path.exists(STATE) and os.remove(STATE)
print("8. guard: blocked + error logged; test number allowed; test mode off allows the lead")
print("ALL TEST MODE TESTS PASS")
