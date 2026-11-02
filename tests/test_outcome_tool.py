"""Phase 5 step 6: record_outcome webhook tool + transcript backup. Fake Cartesia, throwaway DB, no calls."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import sys
import time

DB = f"{TMP}/outcome_tool_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup, tunnel  # noqa: E402
from app.db import Call, Lead, get_session  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel  # never touch the real webhook from a test
from app.main import app  # noqa: E402

SECRET = "tool-test-secret"
os.environ["DASHBOARD_PASSWORD_HASH"] = ""  # no remote login: tunnel requests get 404
os.environ.update(CARTESIA_SUPERVISOR_TOOL_ID="",  # Phase 5 behaviour: no hand-off to the supervisor
                  CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_TOOL_SECRET=SECRET,
                  CARTESIA_WEBHOOK_SECRET="hook-secret")
TEST = os.environ["TEST_CALL_NUMBER"]
calls.POLL_EVERY_S = 0.05
AUTH = {"Authorization": f"Bearer {SECRET}"}


class Fake:
    """Calls stay 'started' until finish() is called; the transcript is whatever the test sets."""

    def __init__(self):
        self.calls, self.n, self.vars = {}, 0, []

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.n += 1
        cid = f"ac_t{self.n}"
        self.vars.append(variables)
        self.calls[cid] = {"status": "started", "end_reason": None, "transcript": []}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        return {"id": cid, "status": c["status"], "end_reason": c["end_reason"], "transcript": c["transcript"],
                "start_time": "2026-10-27T15:00:00Z", "end_time": "2026-10-27T15:00:30Z"}

    async def cancel_call(self, cid):
        return {}

    def finish(self, cid, reason="client_hangup"):
        self.calls[cid].update(status="completed", end_reason=reason)


fake = Fake()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fake.create_call, fake.get_call, fake.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync


def wait(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError("timeout")


def row(model, i):
    with get_session() as s:
        return s.get(model, i)


def new_lead(name, phone, location="Austin, TX 78701"):
    with get_session() as s:
        x = Lead(phone=phone, name=name, location=location)
        s.add(x)
        s.commit()
        return x.id


with TestClient(app) as c:
    # 1. Auth: no secret / wrong secret / right secret
    body = {"call_id": "ac_nope", "outcome": "interested"}
    assert c.post("/api/cartesia/tools/record_outcome", json=body).status_code == 401
    assert c.post("/api/cartesia/tools/record_outcome", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.post("/api/cartesia/tools/record_outcome", json=body, headers=AUTH)
    assert r.status_code == 200 and r.text == "Noted.", r.text  # unknown call: harmless
    # through the tunnel: allowed path; other paths still blocked
    tun = {"cf-ray": "x", **AUTH}
    assert c.post("/api/cartesia/tools/record_outcome", json=body, headers=tun).status_code == 200
    assert c.get("/api/leads", headers={"cf-ray": "x"}).status_code == 404
    print("auth: 401 without / wrong secret; unknown call ignored; reachable through the tunnel")

    # 2. Not now with a time (lead in Austin = Central): saved on the call, lead updated when the call ends.
    lid = new_lead("Callback Carl", "+15125550101")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    v = fake.vars[-1]
    assert v["lead_name"] == "Callback Carl" and v["lead_timezone"] == "Central" and v["lead_local_time"], v
    from datetime import date, timedelta
    day = date.today() + timedelta(days=2)
    when = f"{day.isoformat()}T15:00"
    r = c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={
        "call_id": room, "outcome": "not_now", "note": "in a meeting, try at 3pm", "callback_time": when})
    assert r.status_code == 200 and r.text == f"Saved. Callback {day:%A %B} {day.day} at 3 PM their time.", r.text
    again = c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={
        "call_id": room, "outcome": "not_now", "note": "in a meeting, try at 3pm", "callback_time": when})
    assert again.text == r.text
    ev = c.get(f"/api/calls/{cid}").json()["events"]
    assert sum(1 for e in ev if e["text"] == "record_outcome") == 1  # duplicate ignored
    assert row(Lead, lid).status == "calling"  # nothing changes on the lead until the call ends
    fake.finish(room)
    wait(lambda: row(Lead, lid).status != "calling")
    lead = row(Lead, lid)
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo  # 3 PM Central in UTC: 20:00 in summer, 21:00 in winter (DST)
    want = _dt(day.year, day.month, day.day, 15, tzinfo=ZoneInfo("America/Chicago")).astimezone(ZoneInfo("UTC"))
    assert lead.status == "callback" and lead.callback_at.isoformat().startswith(want.strftime("%Y-%m-%dT%H:00")), (lead.status, lead.callback_at)
    print("not now: saved once, lead -> callback at", lead.callback_at, "UTC (3 PM Central)")

    # 3. Abusive: DNC at once, while the call is still live; never downgraded within the call.
    lid = new_lead("Rude Rick", "+15125550102")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    r = c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "do_not_contact",
                                                                          "note": "swore, said never call again"})
    assert r.text == "Saved. They won't be called again."
    lead = row(Lead, lid)
    assert lead.dnc and lead.status == "do_not_contact" and row(Call, cid).status == "answered", (lead.dnc, lead.status)
    c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "interested"})
    assert row(Call, cid).outcome == "do_not_contact"
    fake.finish(room)
    wait(lambda: any(e["kind"] == "status" and e["text"] == "ended" for e in c.get(f"/api/calls/{cid}").json()["events"]))
    time.sleep(0.3)
    assert row(Lead, lid).dnc and row(Call, cid).outcome == "do_not_contact"
    assert c.post(f"/api/leads/{lid}/call").status_code == 400  # refused from now on
    print("do not contact: lead DNC during the call; later outcomes can't undo it; calls refused")

    # 4. Bad input: unknown outcome rejected (AI is told), garbage callback time -> default 3 days.
    lid = new_lead("Gate Gary", "+15125550103")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    r = c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "maybe"})
    assert r.text.startswith("Not saved"), r.text
    assert c.post("/api/cartesia/tools/record_outcome", headers=AUTH, content=b"not json").status_code == 400
    r = c.post("/api/cartesia/tools/record_outcome", headers=AUTH,
               json={"call_id": room, "outcome": "gatekeeper", "note": "owner out", "callback_time": "after lunch"})
    assert r.text == "Saved. We'll call back in a few days.", r.text
    fake.finish(room)
    wait(lambda: row(Lead, lid).status != "calling")
    assert row(Lead, lid).status == "callback" and row(Call, cid).callback_at is not None
    print("bad outcome rejected; unreadable callback time -> default")

    # 4b. History #32: "we will talk tomorrow", the AI leaves callback_time empty -> tomorrow 10:00 their time, not the default.
    from app.db import as_utc
    from datetime import timezone as _tz

    def tomorrow_ten(zone):
        local = _dt.now(ZoneInfo(zone)).date() + timedelta(days=1)
        while local.weekday() > 4:  # Mon-Fri calling days (test settings)
            local += timedelta(days=1)
        return _dt(local.year, local.month, local.day, 10, tzinfo=ZoneInfo(zone)).astimezone(_tz.utc)

    lid = new_lead("Tomorrow Tom", "+15125550105")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    r = c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={
        "call_id": room, "outcome": "not_now", "note": "Said to call back tomorrow, no specific time given.", "callback_time": ""})
    assert r.text.startswith("Saved. Callback ") and "at 10 AM their time" in r.text, r.text
    fake.finish(room)
    wait(lambda: row(Lead, lid).status != "calling")
    assert as_utc(row(Call, cid).callback_at) == tomorrow_ten("America/Chicago") == as_utc(row(Lead, lid).callback_at)

    # 4c. The day only in the lead's own words (no useful note): found when the call ends.
    lid = new_lead("Words Wendy", "+15125550106")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "not_now", "note": "busy right now", "callback_time": ""})
    fake.calls[room]["transcript"] = [{"role": "user", "text": "I can't decide it, so we will talk tomorrow.", "start_timestamp": 71.4},
                                      {"role": "user", "text": "At any time you can call me.", "start_timestamp": 75.3}]
    fake.finish(room)
    wait(lambda: row(Lead, lid).status != "calling")
    assert as_utc(row(Call, cid).callback_at) == tomorrow_ten("America/Chicago"), row(Call, cid).callback_at

    # 4d. No day anywhere -> still the default days. The AI's exact time still wins over the words.
    lid = new_lead("Default Dan", "+15125550107")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "not_now", "note": "busy right now", "callback_time": ""})
    fake.finish(room)
    wait(lambda: row(Lead, lid).status != "calling")
    gap = as_utc(row(Call, cid).callback_at) - _dt.now(_tz.utc)
    assert timedelta(days=2, hours=23) < gap <= timedelta(days=3), gap
    lid = new_lead("Exact Eve", "+15125550108")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    r = c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={
        "call_id": room, "outcome": "not_now", "note": "call tomorrow", "callback_time": when})
    assert " at 3 PM their time" in r.text, r.text
    fake.finish(room)
    wait(lambda: row(Lead, lid).status != "calling")
    print("day in the note or the lead's words -> that day 10:00; no day -> default; exact time wins")

    # 5. Tunnel down: the tool call is only in the transcript -> same result (backup).
    lid = new_lead("Transcript Tina", "+15125550104")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    fake.calls[room]["transcript"] = [
        {"role": "user", "text": "Sounds great, yes I'm interested.", "start_timestamp": 3.0},
        {"role": "assistant", "text": "Awesome, our designer will reach out.", "start_timestamp": 5.0,
         "tool_calls": [{"id": "tc_ro1", "name": "record_outcome",
                         "arguments": '{"outcome": "interested", "note": "wants a quote"}', "result": "error: unreachable"}]},
    ]
    wait(lambda: row(Call, cid).outcome == "interested")
    fake.finish(room)
    wait(lambda: row(Lead, lid).status != "calling")
    assert row(Lead, lid).status == "interested" and row(Call, cid).outcome_note == "wants a quote"
    print("transcript backup: outcome read from the tool call in the transcript")

    # 6. Manual call (no lead) to the test phone: DNC never lists the test number.
    call = c.post("/api/calls", json={"number": TEST}).json()
    cid, room = call["id"], call["room"]
    c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "do_not_contact"})
    fake.finish(room)
    wait(lambda: row(Call, cid).status == "ended")
    time.sleep(0.3)
    assert c.post("/api/calls", json={"number": TEST}).status_code == 200
    print("test phone never put on Do Not Contact")

print("ALL OUTCOME TOOL TESTS PASS")
