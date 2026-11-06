"""Several supervisors (2026-11-05): own login, own phone, own "available" switch, own working hours. The pop-up goes
to every available one at once; the FIRST Accept wins and the transfer goes to THAT person's phone (request_supervisor's
JSON answer -> Cartesia's assignment -> {{transfer_number}}); Busy from one leaves it open for the others; History
shows who took it. Also: the old one-supervisor file, the Cartesia assignment fallback, manage_users.py phone.
Fake Cartesia, fake logins, throwaway DB. No call is placed."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.update(DASHBOARD_USERNAME="", DASHBOARD_PASSWORD_HASH="", DASHBOARD_SESSION_SECRET="test-session-secret",
                  CARTESIA_CALLS_DISABLED="1", CARTESIA_EMAIL_TOOL_ID="", SMS_SENDING_DISABLED="1",
                  TELNYX_MESSAGING_PROFILE_ID="")

import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import auth, cartesia, cartesia_setup, supervisor, tunnel  # noqa: E402
from app.db import Call, Lead, get_session  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel
from app.main import app  # noqa: E402

SECRET = "sups-test-secret"
os.environ.update(CARTESIA_SUPERVISOR_TOOL_ID="sup_tool", CARTESIA_OUTCOME_TOOL_ID="out_tool", CARTESIA_AGENT_ID="agent_test",
                  CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_TOOL_SECRET=SECRET, CARTESIA_WEBHOOK_SECRET="hook-secret")
AUTH = {"Authorization": f"Bearer {SECRET}"}
PROXY = {"x-forwarded-for": "203.0.113.20", "x-forwarded-host": "dialer.example"}
calls.POLL_EVERY_S = 0.05
supervisor.CHECK_WAIT_S = 0.4
SARA, OMAR = "+15125550301", "+15125550302"


class Fake:
    def __init__(self):
        self.calls, self.n, self.vars = {}, 0, []

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.n += 1
        cid = f"ac_m{self.n}"
        self.vars.append(variables)
        self.calls[cid] = {"status": "started", "end_reason": None, "transcript": []}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        return {"id": cid, "status": c["status"], "end_reason": c["end_reason"], "transcript": list(c["transcript"]),
                "summary": c.get("summary", ""), "start_time": "2026-11-05T15:00:00Z", "end_time": "2026-11-05T15:01:00Z"}

    async def cancel_call(self, cid):
        return {}


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


def new_lead(name, phone):
    with get_session() as s:
        x = Lead(phone=phone, name=name, location="Austin, TX 78701", service_required="new website")
        s.add(x)
        s.commit()
        return x.id


def tool(c, room):
    r = c.post("/api/cartesia/tools/request_supervisor", headers=AUTH, json={"call_id": room, "summary": "Bakery, wants a site."})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json"), r.text
    return r.json()


def login(c, name):
    r = c.post("/api/auth/login", headers=PROXY, json={"username": name, "password": f"{name}-password-123"})
    assert r.status_code == 200, r.text
    token = r.cookies[auth.COOKIE]
    c.cookies.clear()
    return {**PROXY, "Cookie": f"{auth.COOKIE}={token}"}


def answer(c, cid, hdr, accept, **kw):
    return c.post(f"/api/supervisor/requests/{cid}/answer", headers=hdr, json={"accept": accept, **kw})


# ---- 1. the old one-supervisor file is read as one person: the only supervisor login ---------------------
auth.write_users_file({"sara": {"role": "supervisor", "hash": auth.hash_password("sara-password-123")},
                       "boss": {"role": "admin", "hash": auth.hash_password("boss-password-123")}})
old = {"phone": SARA, "available": True, "wait_seconds": 25,
       "hours": {"enabled": False, "timezone": "UTC", "start": "18:00", "end": "03:00", "days": ["mon"]}}
supervisor.SETTINGS_PATH.write_text(json.dumps(old), encoding="utf-8")
st = supervisor.load()
assert st["wait_seconds"] == 25 and len(st["people"]) == 1, st
p = st["people"][0]
assert p["key"] == "sara" and p["phone"] == SARA and p["available"] and p["hours"]["timezone"] == "UTC", p
auth.write_users_file({**auth.read_users_file(),
                       "omar": {"role": "supervisor", "hash": auth.hash_password("omar-password-123")},
                       "viewer": {"role": "supervisor", "hash": auth.hash_password("viewer-password-123")}})
assert supervisor.load()["people"][0]["key"] == "sara"  # still Sara: the file is rewritten on the next save
print("old file: one supervisor, linked to the only supervisor login; wait time and hours kept")

with TestClient(app) as c:
    # ---- 2. two supervisors, each with their own phone and switch --------------------------------------------
    c.put("/api/supervisor/people/sara", json={"name": "Sara", "phone": SARA, "available": True})
    st = c.put("/api/supervisor/people/omar", json={"name": "Omar", "phone": "+1 (512) 555-0302", "available": False}).json()
    assert [(p["key"], p["phone"]) for p in st["people"]] == [("sara", SARA), ("omar", OMAR)], st
    assert "people" in json.loads(supervisor.SETTINGS_PATH.read_text(encoding="utf-8"))  # new form on disk
    assert c.put("/api/supervisor/people/x", json={"key": "omar"}).status_code == 400  # two with one login
    omar, sara, viewer, boss = (login(c, n) for n in ("omar", "sara", "viewer", "boss"))
    me = c.get("/api/supervisor", headers=omar).json()
    assert me["me"] == "omar" and me["me_supervisor"] and not me["is_admin"] and me["logins"] == []
    assert c.get("/api/supervisor", headers=boss).json()["logins"] == ["boss", "omar", "sara", "viewer"]
    # a supervisor flips only their own switch (a key for someone else is ignored); one not on the list can't
    st = c.post("/api/supervisor/availability", headers=omar, json={"available": True, "key": "sara"}).json()
    assert [p["available"] for p in st["people"]] == [True, True], st
    assert c.post("/api/supervisor/availability", headers=viewer, json={"available": True}).status_code == 403
    assert c.put("/api/supervisor/people/omar", headers=omar, json={"phone": SARA}).status_code == 403  # admin only
    st = c.post("/api/supervisor/availability", headers=boss, json={"available": True, "key": "omar"}).json()  # admin: anyone
    assert st["can_take_calls"]
    print("two supervisors: own phone, own switch (only their own; admin may flip anyone's); not on the list -> 403")

    # ---- 3. both rung at once; Sara busy, Omar accepts: the transfer goes to OMAR's phone --------------------
    lid = new_lead("Two Tina", "+15125550311")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    assert fake.vars[-1]["transfer_number"] == SARA and fake.vars[-1]["supervisor_status"] == "available"  # the first, at dial
    assert tool(c, room)["answer"] == "WAITING"
    reqs = c.get("/api/supervisor/requests", headers=omar).json()
    assert [o["key"] for o in reqs[0]["offered"]] == ["sara", "omar"], reqs
    assert row(Call, cid).transfer_offered == ["sara", "omar"]
    r = answer(c, cid, sara, False)
    assert r.status_code == 200 and r.json()["status"] == "waiting" and r.json()["declined_by"] == ["sara"], r.text
    assert tool(c, room)["answer"] == "WAITING"  # Omar can still take it
    assert answer(c, cid, viewer, True).status_code == 409  # not on the list
    out = {}
    th = threading.Thread(target=lambda: out.update(r=tool(c, room)))
    supervisor.CHECK_WAIT_S = 2.0
    th.start()
    time.sleep(0.15)
    r = answer(c, cid, omar, True)
    th.join()
    supervisor.CHECK_WAIT_S = 0.4
    assert r.status_code == 200 and r.json()["taken_by"] == "Omar" and r.json()["status"] == "accepted", r.text
    assert out["r"] == {"answer": "AVAILABLE", "what_to_do": supervisor.REPLY_AVAILABLE, "transfer_number": OMAR}, out
    late = answer(c, cid, sara, True)
    assert late.status_code == 409 and "Omar already took" in late.json()["detail"], late.text
    cc = row(Call, cid)
    assert (cc.transfer_to, cc.transfer_to_name, cc.transfer_to_phone) == ("omar", "Omar", OMAR)
    fake.calls[room]["transcript"].append({"role": "assistant", "text": "Connecting you now.", "start_timestamp": 9,
                                           "end_timestamp": 10, "tool_calls": [{"id": "t1", "name": "transfer_to_number"}]})
    wait(lambda: row(Call, cid).transfer_status == "transferring")
    fake.calls[room].update(status="completed", end_reason="client_hangup", summary="Tina wants a bakery site; went to Omar.")
    wait(lambda: row(Call, cid).status == "ended")
    h = c.get(f"/api/calls/{cid}").json()["call"]
    assert h["outcome"] == "transferred" and h["transfer_to_name"] == "Omar" and h["transfer_declined_by"] == ["sara"], h
    assert h["ai_summary"].startswith("Tina wants") and row(Lead, lid).status == "transferred"
    assert any("Omar accepted" in e["text"] for e in c.get(f"/api/calls/{cid}").json()["events"])
    print("both rung; Sara busy keeps it open; Omar's Accept wins -> AVAILABLE + Omar's number; late Accept 409;"
          " History: taken by Omar, Cartesia summary kept")

    # ---- 4. everyone busy -> declined -> the AI books a callback --------------------------------------------
    lid = new_lead("Busy Ben", "+15125550312")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    assert tool(c, room)["answer"] == "WAITING"
    answer(c, cid, omar, False)
    r = answer(c, cid, sara, False)
    assert r.json()["status"] == "declined" and "Sara" in r.json()["note"] and "Omar" in r.json()["note"], r.text
    assert tool(c, room) == {"answer": "NOT AVAILABLE", "what_to_do": supervisor.REPLY_NOT_AVAILABLE}
    c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "interested", "note": "",
                                                                     "callback_time": ""})
    fake.calls[room].update(status="completed", end_reason="client_hangup")
    wait(lambda: row(Call, cid).status == "ended")
    assert row(Call, cid).outcome == "transfer_declined", row(Call, cid).outcome
    wait(lambda: row(Lead, lid).status == "callback")
    print("all busy: declined -> NOT AVAILABLE -> callback (Interested – callback)")

    # ---- 5. Omar outside his own working hours: only Sara is rung --------------------------------------------
    today = datetime.now(ZoneInfo("UTC")).strftime("%a").lower()
    other = next(d for d in supervisor.DAYS if d != today and d != supervisor.DAYS[(supervisor.DAYS.index(today) - 1) % 7])
    c.put("/api/supervisor/people/omar", json={"hours": {"enabled": True, "timezone": "UTC", "start": "09:00",
                                                          "end": "17:00", "days": [other]}})
    st = c.get("/api/supervisor").json()
    assert [p["can_take_calls"] for p in st["people"]] == [True, False] and "working hours" in st["people"][1]["why_not"]
    lid = new_lead("Hours Hal", "+15125550313")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    assert tool(c, room)["answer"] == "WAITING" and row(Call, cid).transfer_offered == ["sara"]
    r = answer(c, cid, boss, True)  # an admin not on the list: the only one rung takes it
    assert r.status_code == 200 and r.json()["taken_by"] == "Sara", r.text
    assert tool(c, room)["transfer_number"] == SARA
    fake.calls[room].update(status="completed", end_reason="client_hangup")
    wait(lambda: row(Call, cid).status == "ended")
    print("working hours per person: Omar outside his -> only Sara rung; admin's click answers for the only one rung")

    # ---- 6. two Accepts at the same moment: exactly one wins -------------------------------------------------
    c.put("/api/supervisor/people/omar", json={"hours": {"enabled": False}})
    lid = new_lead("Race Rita", "+15125550314")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    tool(c, room)
    res = []
    ts = [threading.Thread(target=lambda h=h: res.append(answer(c, cid, h, True).status_code)) for h in (sara, omar)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(res) == [200, 409], res
    winner = row(Call, cid).transfer_to
    assert tool(c, room)["transfer_number"] == {"sara": SARA, "omar": OMAR}[winner]
    fake.calls[room].update(status="completed", end_reason="client_hangup")
    wait(lambda: row(Call, cid).status == "ended")
    print(f"race: two Accepts at once -> one 200, one 409; the transfer number is the winner's ({winner})")

    # ---- 7. nobody available: no pop-up, NOT AVAILABLE at once ----------------------------------------------
    for k in ("sara", "omar"):
        c.post("/api/supervisor/availability", json={"available": False, "key": k})
    lid = new_lead("Nobody Ned", "+15125550315")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    assert fake.vars[-1]["supervisor_status"] == "unavailable" and row(Call, cid).transfer_status == "unavailable"
    assert tool(c, room)["answer"] == "NOT AVAILABLE" and c.get("/api/supervisor/requests").json() == []
    assert "Sara: switched to unavailable" in row(Call, cid).transfer_note
    fake.calls[room].update(status="completed", end_reason="client_hangup")
    wait(lambda: row(Call, cid).status == "ended")
    assert c.delete("/api/supervisor/people/omar").status_code == 200
    assert c.delete("/api/supervisor/people/omar").status_code == 404
    print("nobody available: NOT AVAILABLE at once, no pop-up; remove a supervisor")

# ---- 8. Cartesia: the tool asks for the assignment; if Cartesia refuses it, the tool still works ----------
from app import agent_config  # noqa: E402

body = agent_config.supervisor_tool_body("https://x/api/cartesia/tools/request_supervisor", "s")
assert body["assignments"] == supervisor.ASSIGNMENTS == [{"dynamic_variable": "transfer_number", "value_path": "transfer_number"}]
sent = []


async def get_tool(tid):
    return {"id": tid, "api_schema": {"url": "https://old"}}


async def update_tool(tid, b):
    sent.append(b)
    if "assignments" in b and refuse:
        raise cartesia.CartesiaError("assignments: unknown field", 422)
    return {}

cartesia.get_tool, cartesia.update_tool = get_tool, update_tool
refuse = True
asyncio.run(cartesia_setup.ensure_supervisor_tool("https://dialer.example"))
assert ["assignments" in b for b in sent] == [True, False], sent
st = cartesia_setup._state()
assert st["supervisor_assignments"] is False and "unknown field" in st["supervisor_assignments_error"]
sent.clear()
asyncio.run(cartesia_setup.ensure_supervisor_tool("https://dialer.example"))
assert all("assignments" not in b for b in sent)  # not asked again for the same definition
refuse = False
cartesia_setup._save_state(supervisor_assignments=None, supervisor_assignments_tried=None)
sent.clear()
asyncio.run(cartesia_setup.ensure_supervisor_tool("https://dialer.example"))
assert "assignments" in sent[0] and cartesia_setup._state()["supervisor_assignments"] is True
print("Cartesia tool: asks for the transfer_number assignment; refused -> saved without it, flagged, not re-asked")

# ---- 9. the command: a supervisor login with their own phone, and changing the phone later -----------------
MANAGE = [sys.executable, str(REPO / "dashboard" / "backend" / "manage_users.py")]


def manage(*args, password=None):
    return subprocess.run(MANAGE + list(args), input=password, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})


# right after the deploy the old file may still be there: a 2nd supervisor login must not take the 1st one's place
auth.write_users_file({k: v for k, v in auth.read_users_file().items() if k in ("boss", "sara")})
supervisor.SETTINGS_PATH.write_text(json.dumps(old), encoding="utf-8")
r = manage("set", "zed", "--role", "supervisor", "--phone", "+15125550398", "--password-stdin", password="zed-password-1234\n")
assert r.returncode == 0, r.stdout + r.stderr
assert [(p["key"], p["phone"]) for p in supervisor.load()["people"]] == [("sara", SARA), ("zed", "+15125550398")]
r = manage("set", "nina", "--role", "supervisor", "--phone", "+15125550399", "--display", "Nina", "--password-stdin",
           password="nina-password-123\n")
assert r.returncode == 0 and "transfers go to +15125550399" in r.stdout, r.stdout + r.stderr
assert auth.read_users_file()["nina"]["role"] == "supervisor"
n = supervisor.person("nina")
assert n["phone"] == "+15125550399" and n["name"] == "Nina" and n["available"] is False, n
r = manage("phone", "nina", "+1 512 555 0398")
assert r.returncode == 0 and supervisor.person("nina")["phone"] == "+15125550398", r.stdout + r.stderr
assert manage("phone", "ghost", "+15125550397").returncode != 0
assert manage("phone", "nina", "12345").returncode != 0 and supervisor.person("nina")["phone"] == "+15125550398"
print("command: set NAME --role supervisor --phone ... adds them with their own phone; phone NAME ... changes it")

print("ALL SUPERVISORS TESTS PASS")
