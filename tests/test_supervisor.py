"""Phase 6: supervisor settings, request_supervisor (ask first), Accept / Busy / timeout, transfer tracking,
outcomes, no auto hang-up after a transfer. Fake Cartesia, throwaway DB + supervisor.json, no calls."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

DB = f"{TMP}/supervisor_test.db"
SUP = f"{TMP}/supervisor_test.json"
for f in (DB, SUP):
    if os.path.exists(f):
        os.remove(f)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["SUPERVISOR_SETTINGS_PATH"] = SUP
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup, supervisor, tunnel  # noqa: E402
from app.db import Call, Lead, get_session  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel  # never touch the real webhook/tools from a test
from app.main import app  # noqa: E402

SECRET = "sup-test-secret"
os.environ.update(CARTESIA_SUPERVISOR_TOOL_ID="sup_tool", CARTESIA_OUTCOME_TOOL_ID="out_tool",  # hand-off on
                  CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_TOOL_SECRET=SECRET,
                  CARTESIA_WEBHOOK_SECRET="hook-secret")
AUTH = {"Authorization": f"Bearer {SECRET}"}
calls.POLL_EVERY_S = 0.05
supervisor.CHECK_WAIT_S = 0.6
supervisor.WAIT_MIN_S = 1  # so the timeout test doesn't take 10 s
calls._silence_settings = lambda: {"enabled": True, "check_s": 5, "max_s": 1}  # 1 s of quiet would close the line


class Fake:
    def __init__(self):
        self.calls, self.n, self.vars, self.cancels = {}, 0, [], []

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.n += 1
        cid = f"ac_s{self.n}"
        self.vars.append(variables)
        self.calls[cid] = {"status": "started", "end_reason": None, "transcript": []}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        return {"id": cid, "status": c["status"], "end_reason": c["end_reason"], "transcript": list(c["transcript"]),
                "start_time": "2026-10-28T15:00:00Z", "end_time": "2026-10-28T15:01:00Z"}

    async def cancel_call(self, cid):
        self.cancels.append(cid)
        return {}

    def say(self, cid, role, text, start, tools=None):
        self.calls[cid]["transcript"].append({"role": role, "text": text, "start_timestamp": start,
                                              "end_timestamp": start + 1.5, "tool_calls": tools or []})

    def finish(self, cid, reason="client_hangup"):
        self.calls[cid].update(status="completed", end_reason=reason)


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


def row(model, i):
    with get_session() as s:
        return s.get(model, i)


def new_lead(name, phone, **kw):
    with get_session() as s:
        x = Lead(phone=phone, name=name, location=kw.pop("location", "Austin, TX 78701"), **kw)
        s.add(x)
        s.commit()
        return x.id


def tool(c, room, summary="Owner of a bakery, wants a new site with online orders."):
    return c.post("/api/cartesia/tools/request_supervisor", headers=AUTH, json={"call_id": room, "summary": summary})


def transfer_turn(cid, start):
    fake.say(cid, "assistant", "Great news, they're free. Connecting you with our senior web designer now, please stay on the line.", start,
             [{"id": f"tr{start}", "name": "transfer_to_number", "arguments": "{}"}])


# ---- 1. settings: validation, hours across midnight (a night shift) ------------------------------
utc = timezone.utc
h = {"enabled": True, "timezone": "UTC", "start": "18:00", "end": "03:00", "days": ["mon", "tue", "wed", "thu", "fri"]}
# Wed 2026-10-28 20:00 -> in; Thu 02:00 (Wed shift) -> in; Sat 02:00 (Fri shift) -> in
assert supervisor.in_hours(h, datetime(2026, 10, 28, 20, 0, tzinfo=utc))
assert supervisor.in_hours(h, datetime(2026, 10, 29, 2, 0, tzinfo=utc))
assert supervisor.in_hours(h, datetime(2026, 10, 31, 2, 0, tzinfo=utc))
assert not supervisor.in_hours(h, datetime(2026, 10, 28, 13, 0, tzinfo=utc))  # Wed 13:00
assert not supervisor.in_hours(h, datetime(2026, 10, 31, 20, 0, tzinfo=utc))  # Sat 20:00
assert not supervisor.in_hours(h, datetime(2026, 11, 2, 2, 0, tzinfo=utc))  # Mon 02:00 = Sunday's shift (off)
he = {"enabled": True, "timezone": "America/New_York", "start": "09:00", "end": "17:00", "days": ["mon"]}
assert supervisor.in_hours(he, datetime(2026, 11, 2, 14, 0, tzinfo=utc)) and not supervisor.in_hours(he, datetime(2026, 11, 2, 23, 0, tzinfo=utc))
for bad in ({"phone": "555"}, {"wait_seconds": 500}, {"hours": {"timezone": "Europe/London"}}, {"hours": {"start": "9am"}},
            {"hours": {"start": "10:00", "end": "10:00"}}, {"hours": {"enabled": True, "days": []}}, {"key": "bad name"}):
    try:
        supervisor.save_wait(bad["wait_seconds"]) if "wait_seconds" in bad else supervisor.save_person("supervisor", bad)
        raise AssertionError(f"accepted {bad}")
    except supervisor.SupervisorError:
        pass
assert not os.path.exists(SUP)  # nothing invalid was ever written
print("settings: validation OK; working hours incl. an overnight shift OK")

with TestClient(app) as c:
    st = c.get("/api/supervisor").json()
    p0 = st["people"][0]
    assert len(st["people"]) == 1 and p0["key"] == "supervisor", st  # no file yet: one supervisor, as before
    assert p0["phone"] == os.environ["TEST_CALL_NUMBER"] and p0["available"] is False and st["wait_seconds"] == 20
    assert st["can_take_calls"] is False and "unavailable" in st["why_not"]
    assert c.put("/api/supervisor/people/supervisor", json={"phone": "12"}).status_code == 400
    st = c.put("/api/supervisor/people/supervisor", json={"phone": "+1 (786) 555-0100", "available": True}).json()
    assert c.put("/api/supervisor", json={"wait_seconds": 15}).json()["wait_seconds"] == 15
    assert st["people"][0]["phone"] == "+17865550100" and st["can_take_calls"] is True, st
    print("settings API: defaults (off, test number, 20 s), phone cleaned up, bad input refused")

    # ---- 2. tool auth + unknown call ------------------------------------------------------------------
    assert c.post("/api/cartesia/tools/request_supervisor", json={"call_id": "x"}).status_code == 401
    assert c.post("/api/cartesia/tools/request_supervisor", json={"call_id": "x"},
                  headers={"Authorization": "Bearer nope"}).status_code == 401
    assert tool(c, "ac_unknown").json()["answer"] == "NOT AVAILABLE"
    assert c.post("/api/cartesia/tools/request_supervisor", headers={"cf-ray": "1", **AUTH},
                  json={"call_id": "ac_unknown"}).status_code == 200  # reachable through the tunnel
    print("tool: 401 without / wrong secret; unknown call -> NOT AVAILABLE; public through the tunnel")

    # ---- 3. Accept -> transfer -> no auto hang-up -> transferred -----------------------------------------
    lid = new_lead("Bakery Beth", "+15125550201", business="Beth's Bakes", service_required="new website",
                   job_description="online ordering")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    v = fake.vars[-1]
    assert v["transfer_number"] == "+17865550100" and v["supervisor_status"] == "available", v
    assert row(Call, cid).transfer_status == ""
    fake.say(room, "user", "Yeah, we need a new website with online orders.", 3)
    wait(lambda: any(e["kind"] == "transcript" for e in c.get(f"/api/calls/{cid}").json()["events"]))
    t0 = time.time()
    r = tool(c, room)
    assert r.json()["answer"] == "WAITING" and 0.5 <= time.time() - t0 < 2, (r.text, time.time() - t0)
    assert "transfer_number" not in r.json() and r.headers["content-type"].startswith("application/json")
    reqs = c.get("/api/supervisor/requests").json()
    assert len(reqs) == 1 and reqs[0]["call_id"] == cid and reqs[0]["status"] == "waiting", reqs
    p = reqs[0]
    assert p["lead"]["lead_name"] == "Bakery Beth" and p["lead"]["business_name"] == "Beth's Bakes"
    assert p["lead"]["lead_timezone"] == "Central" and p["lead"]["lead_local_time"] and p["deadline"]
    assert p["summary"].startswith("Owner of a bakery") and p["transcript"][0]["text"].startswith("Yeah, we need")
    # silence while waiting must not close the line (max_s is 1 s here)
    time.sleep(1.5)
    assert room not in fake.cancels
    # Accept while the AI is waiting: its wait ends early with AVAILABLE
    out = {}
    supervisor.CHECK_WAIT_S = 2.0  # long wait, so an early return can only come from the click
    th = threading.Thread(target=lambda: out.update(r=tool(c, room), t=time.time()))
    th.start()
    time.sleep(0.15)
    t_click = time.time()
    a = c.post(f"/api/supervisor/requests/{cid}/answer", json={"accept": True})
    th.join()
    assert a.status_code == 200 and a.json()["status"] == "accepted", a.text
    assert out["r"].json()["answer"] == "AVAILABLE" and out["t"] - t_click < 1.0, (out["r"].text, out["t"] - t_click)
    assert out["r"].json()["transfer_number"] == "+17865550100"  # Cartesia saves it into {{transfer_number}}
    assert row(Call, cid).transfer_to == "supervisor" and row(Call, cid).transfer_offered == ["supervisor"]
    supervisor.CHECK_WAIT_S = 0.6
    assert c.post(f"/api/supervisor/requests/{cid}/answer", json={"accept": False}).status_code == 409  # already answered
    assert tool(c, room).json()["answer"] == "AVAILABLE"  # asking again: same answer
    assert c.get("/api/supervisor/requests").json() == []
    transfer_turn(room, 20)
    wait(lambda: row(Call, cid).transfer_status == "transferring")
    time.sleep(2)  # well past max_s of silence: still no hang-up from us
    assert room not in fake.cancels, fake.cancels
    r = c.post(f"/api/calls/{cid}/hangup")
    assert r.status_code == 409 and "transferred" in r.json()["detail"]
    # a carrier hang-up (Telnyx webhook) is ignored after a transfer
    assert calls.manager.callee_hung_up(row(Call, cid).number, "test")["ignored"] == "transferred to the supervisor"
    fake.finish(room, "agent_hangup")
    wait(lambda: row(Call, cid).status == "ended")
    cc = row(Call, cid)
    assert cc.outcome == "transferred" and cc.outcome_source == "transfer" and cc.transfer_status == "transferred", cc
    assert cc.transfer_summary.startswith("Owner of a bakery")
    wait(lambda: row(Lead, lid).status == "transferred")
    print("accept: WAITING after 0.6 s, pop-up data OK, Accept wakes the AI at once -> AVAILABLE; transfer seen;"
          " no auto hang-up / carrier hang-up ignored / manual hang-up guarded; outcome transferred")

    # ---- 4. Busy -> AI books a callback -> transfer_declined (callback) -----------------------------------
    lid = new_lead("Busy Bob", "+15125550202")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    assert tool(c, room).json()["answer"] == "WAITING"
    assert c.post(f"/api/supervisor/requests/{cid}/answer", json={"accept": False}).status_code == 200
    assert tool(c, room).json()["answer"] == "NOT AVAILABLE"
    day = date.today() + timedelta(days=1)
    r = c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={
        "call_id": room, "outcome": "interested", "note": "wants a call from the manager", "callback_time": f"{day}T11:00"})
    assert r.text.startswith("Saved. Callback"), r.text
    fake.finish(room)
    wait(lambda: row(Call, cid).status == "ended")
    cc = row(Call, cid)
    assert cc.outcome == "transfer_declined" and cc.transfer_status == "declined" and cc.callback_at, cc
    wait(lambda: row(Lead, lid).status == "callback")
    assert row(Lead, lid).callback_at is not None
    print("busy: NOT AVAILABLE -> callback time saved -> outcome transfer_declined, lead callback")

    # ---- 5. No click -> timeout; answering late is refused ------------------------------------------------
    c.put("/api/supervisor", json={"wait_seconds": 2})
    lid = new_lead("Slow Sam", "+15125550203")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    assert tool(c, room).json()["answer"] == "WAITING"
    replies = [tool(c, room).json()["answer"] for _ in range(5)]
    assert replies[-1] == "NOT AVAILABLE", replies
    assert row(Call, cid).transfer_status == "timeout" and "2s" in row(Call, cid).transfer_note
    assert c.post(f"/api/supervisor/requests/{cid}/answer", json={"accept": True}).status_code == 409
    fake.finish(room)
    wait(lambda: row(Call, cid).status == "ended")
    assert row(Call, cid).outcome == "transfer_declined"  # no record_outcome: still an interested lead
    print("timeout: NOT AVAILABLE after the wait; late Accept refused; outcome transfer_declined")

    # ---- 6. Transfer refused by the carrier: the AI talks on -> transfer_failed; normal hang-up rules return -------
    lid = new_lead("Fail Fay", "+15125550204")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    tool(c, room)
    c.post(f"/api/supervisor/requests/{cid}/answer", json={"accept": True})
    transfer_turn(room, 10)
    wait(lambda: row(Call, cid).transfer_status == "transferring")
    fake.say(room, "assistant", "Sorry, looks like he just stepped away. When's a good time for him to call you back?", 14)
    wait(lambda: row(Call, cid).transfer_status == "failed")
    wait(lambda: room in fake.cancels, timeout=4)  # 1 s of silence -> our normal close again
    fake.finish(room, "api_cancelled")
    wait(lambda: row(Call, cid).status == "ended")
    assert row(Call, cid).outcome == "transfer_failed", row(Call, cid).outcome
    wait(lambda: row(Lead, lid).status == "callback")
    print("transfer failed: detected from the AI talking on; normal hang-up rules back; outcome transfer_failed")

    # ---- 7. Supervisor off when we dial: variables say unavailable; AI interested -> transfer_declined -------------
    c.post("/api/supervisor/availability", json={"available": False})
    lid = new_lead("Off Olive", "+15125550205")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    assert fake.vars[-1]["supervisor_status"] == "unavailable" and row(Call, cid).transfer_status == "unavailable"
    t0 = time.time()
    assert tool(c, room).json()["answer"] == "NOT AVAILABLE" and time.time() - t0 < 0.3  # no waiting, no pop-up
    assert c.get("/api/supervisor/requests").json() == []
    c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "interested", "note": ""})
    fake.finish(room)
    wait(lambda: row(Call, cid).status == "ended")
    assert row(Call, cid).outcome == "transfer_declined"
    # ... but a not-interested lead on such a call stays not_interested
    lid = new_lead("Nope Ned", "+15125550206")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "not_interested", "note": ""})
    fake.finish(room)
    wait(lambda: row(Call, cid).status == "ended")
    assert row(Call, cid).outcome == "not_interested"
    print("supervisor off: unavailable at dial, instant NOT AVAILABLE; interested -> transfer_declined; others unchanged")

    # ---- 8. The lead hangs up while we wait: request closed (timeout), pop-up gone -----------------------------
    c.post("/api/supervisor/availability", json={"available": True})
    c.put("/api/supervisor", json={"wait_seconds": 30})
    lid = new_lead("Gone Gus", "+15125550207")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    tool(c, room)
    assert len(c.get("/api/supervisor/requests").json()) == 1
    fake.finish(room)
    wait(lambda: row(Call, cid).status == "ended")
    cc = row(Call, cid)
    assert cc.transfer_status == "timeout" and "ended while waiting" in cc.transfer_note and cc.outcome == "transfer_declined"
    assert c.get("/api/supervisor/requests").json() == []
    print("lead hung up while waiting: request closed, outcome transfer_declined")

print("ALL SUPERVISOR TESTS PASS")
