"""Phase 7 part B: collect_email tool (read back + confirm before saving). Fake Cartesia, fake tool POSTs,
throwaway DB, no calls, no emails (EMAIL_SENDING_DISABLED)."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import sys
import time

DB = f"{TMP}/email_tool_test.db"
SUP = f"{TMP}/email_tool_supervisor.json"
for f in (DB, SUP):
    if os.path.exists(f):
        os.remove(f)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["SUPERVISOR_SETTINGS_PATH"] = SUP
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["EMAIL_SENDING_DISABLED"] = "1"  # this test is about collecting; sending is test_emails.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile
import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import agent_config, cartesia, cartesia_setup, tunnel  # noqa: E402
from app.db import Call, Lead, LeadEvent, get_session  # noqa: E402
from settings import load_settings  # noqa: E402
from sqlmodel import select  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel  # never touch the real webhook/tools from a test
from app.main import app  # noqa: E402

SECRET = "email-test-secret"
os.environ.update(CARTESIA_EMAIL_TOOL_ID="email_tool", CARTESIA_OUTCOME_TOOL_ID="out_tool", CARTESIA_SUPERVISOR_TOOL_ID="",
                  CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_TOOL_SECRET=SECRET)
AUTH = {"Authorization": f"Bearer {SECRET}"}
calls.POLL_EVERY_S = 0.05
calls._silence_settings = lambda: {"enabled": False, "check_s": 8, "max_s": 25}  # don't hang up while we test


class Fake:
    def __init__(self):
        self.calls, self.n, self.vars = {}, 0, []

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.n += 1
        cid = f"ac_e{self.n}"
        self.vars.append(variables)
        self.calls[cid] = {"status": "started", "end_reason": None, "transcript": []}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        return {"id": cid, "status": c["status"], "end_reason": c["end_reason"], "transcript": list(c["transcript"]),
                "start_time": "2026-10-28T15:00:00Z", "end_time": "2026-10-28T15:01:00Z"}

    async def cancel_call(self, cid):
        return {}

    def say(self, cid, role, text, start, tools=None):
        self.calls[cid]["transcript"].append({"role": role, "text": text, "start_timestamp": start,
                                              "end_timestamp": start + 1.5, "tool_calls": tools or []})

    def finish(self, cid):
        self.calls[cid].update(status="completed", end_reason="client_hangup")


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
        x = Lead(phone=phone, name=name, location="Austin, TX 78701", **kw)
        s.add(x)
        s.commit()
        return x.id


def tool(c, room, email, confirmed=True, reason="details"):
    r = c.post("/api/cartesia/tools/collect_email", headers=AUTH,
               json={"call_id": room, "email": email, "confirmed": confirmed, "reason": reason})
    assert r.status_code == 200, r.text
    return r.text


def events(lid):
    with get_session() as s:
        return [e.text for e in s.exec(select(LeadEvent).where(LeadEvent.lead_id == lid)).all()]


# ---- 1. agent config: the tool + the read-back rules only when the tool exists and emails are on -------------
s = load_settings()
cfg = agent_config.build_config(s)
assert {"id": "email_tool"} in cfg["tools"], cfg["tools"]
ins = cfg["instructions"]
for k in ("# Their email address", "{{lead_email}}", "s-a-r-a-h at example dot com", "read the whole corrected address",
          "Only after they say yes", "collect_email"):
    assert k in ins, k
off = s.model_copy(update={"emails": s.emails.model_copy(update={"enabled": False})})
assert "# Their email address" not in agent_config.build_config(off)["instructions"]
assert {"id": "email_tool"} not in agent_config.build_config(off)["tools"]
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""
assert "# Their email address" not in agent_config.build_config(s)["instructions"]
os.environ["CARTESIA_EMAIL_TOOL_ID"] = "email_tool"
body = agent_config.email_tool_body("https://x.trycloudflare.com/api/cartesia/tools/collect_email", "sec")
props = body["api_schema"]["request_body_schema"]["properties"]
assert body["execution_mode"] == "immediate" and props["call_id"]["dynamic_variable"] == "system__conversation_id"
assert props["reason"]["enum"] == ["details", "interested", "not_now", "other"] and props["confirmed"]["type"] == "boolean"
assert body["api_schema"]["authentication"]["mode"] == "bearer"
print("config: collect_email attached + read-back rules (only with the tool and emails on)")

with TestClient(app) as c:
    # ---- 2. auth + unknown call ---------------------------------------------------------------------------
    assert c.post("/api/cartesia/tools/collect_email", json={"call_id": "x"}).status_code == 401
    assert c.post("/api/cartesia/tools/collect_email", json={"call_id": "x"},
                  headers={"Authorization": "Bearer nope"}).status_code == 401
    assert tool(c, "ac_unknown", "a@b.test") == "Noted."
    assert c.post("/api/cartesia/tools/collect_email", headers={"cf-ray": "1", **AUTH},
                  json={"call_id": "ac_unknown", "email": "a@b.test", "confirmed": True, "reason": "details"}).status_code == 200
    print("tool: 401 without / wrong secret; unknown call -> Noted; public through the tunnel")

    # ---- 3. a call: not confirmed, not valid, then the wrong spelling is corrected before saving ---------------
    lid = new_lead("Sarra Diaz", "+15125550301", service_required="new logo")
    call = c.post(f"/api/leads/{lid}/call").json()
    cid, room = call["id"], call["room"]
    assert fake.vars[-1]["lead_email"] == "", fake.vars[-1]
    assert tool(c, room, "sarra@example.com", confirmed=False).startswith("Not saved yet. Read the address back")
    assert tool(c, room, "sarra at gmail").startswith("Not saved: 'sarra at gmail' isn't a full email address")
    assert row(Lead, lid).email == ""
    fake.say(room, "user", "Sure, send me the details. It's sarra at example dot com.", 2)
    fake.say(room, "assistant", "Great. That's s-a-r-a-h at example dot com, right?", 4)
    fake.say(room, "user", "No, it's s-a-r-r-a. Two r's, no h.", 6)
    fake.say(room, "assistant", "Got it, that's s-a-r-r-a at example dot com, right?", 8)
    fake.say(room, "user", "Yes, that's it.", 10)
    r = tool(c, room, "Sarra@Example.com", reason="details")
    assert r == "Saved: sarra@example.com. We'll email them right after the call.", r
    # the same tool call shows up in the transcript (with the webhook's answer): saved once, not twice
    fake.say(room, "assistant", "Perfect, I'll send that right after the call.", 12,
             [{"id": "tc_mail1", "name": "collect_email", "result": r,
               "arguments": {"email": "Sarra@Example.com", "confirmed": True, "reason": "details"}}])
    wait(lambda: any(e["text"] == "tool_call" for e in c.get(f"/api/calls/{cid}").json()["events"]))
    ld, cl = row(Lead, lid), row(Call, cid)
    assert ld.email == "sarra@example.com" and cl.email_collected == "sarra@example.com" and cl.email_reason == "details"
    assert sum("Email confirmed" in e for e in events(lid)) == 1, events(lid)
    print("call: unconfirmed and half addresses refused; corrected spelling saved once (webhook + transcript)")

    # ---- 4. the AI saved too early, the lead corrects it: the latest address wins -----------------------------
    assert tool(c, room, "sarah@example.com", reason="interested").startswith("Saved: sarah@example.com")
    assert tool(c, room, "sarra@example.com", reason="interested").startswith("Saved: sarra@example.com")
    ld, cl = row(Lead, lid), row(Call, cid)
    assert ld.email == "sarra@example.com" and cl.email_reason == "interested"
    assert any("(was sarah@example.com)" in e for e in events(lid)), events(lid)
    fake.finish(room)
    wait(lambda: row(Call, cid).status == "ended")
    assert tool(c, room, "late@example.com") == "Noted."  # after the call: ignored
    assert row(Lead, lid).email == "sarra@example.com"
    print("correction after an early save: latest wins; nothing changes after the call ended")

    # ---- 5. a likely typo is checked once with the lead; the same address again is saved ----------------------
    lid2 = new_lead("Gus Typo", "+15125550302", email="old@example.com")
    call2 = c.post(f"/api/leads/{lid2}/call").json()
    room2, cid2 = call2["room"], call2["id"]
    assert fake.vars[-1]["lead_email"] == "old@example.com"  # the AI can ask "is this still the best one?"
    r = tool(c, room2, "gus@gmial.com", reason="not_now")
    assert r.startswith("Not saved yet: gmial.com looks like a typo of gmail.com") and "gus at gmail dot com" in r, r
    assert row(Lead, lid2).email == "old@example.com"
    assert tool(c, room2, "gus@gmial.com", reason="not_now").startswith("Saved: gus@gmial.com")  # they insist
    assert row(Lead, lid2).email == "gus@gmial.com"
    fake.finish(room2)
    wait(lambda: row(Call, cid2).status == "ended")
    print("typo: checked once, then saved as they said it")

    # ---- 6. tunnel down: the webhook never arrives, the transcript's tool call still saves it -----------------
    lid3 = new_lead("Tess Backup", "+15125550303")
    call3 = c.post(f"/api/leads/{lid3}/call").json()
    room3, cid3 = call3["room"], call3["id"]
    fake.say(room3, "assistant", "That's t-e-s-s at example dot org, right?", 3)
    fake.say(room3, "user", "Yep.", 5)
    fake.say(room3, "assistant", "Great, I'll email you after the call.", 6,
             [{"id": "tc_mail3", "name": "collect_email", "result": "",
               "arguments": '{"email": "tess at example dot org", "confirmed": true, "reason": "interested"}'}])
    wait(lambda: row(Lead, lid3).email == "tess@example.org")
    # a not-confirmed call in the transcript is not saved
    fake.say(room3, "assistant", "", 8, [{"id": "tc_mail4", "name": "collect_email", "result": "",
                                          "arguments": {"email": "x@y.test", "confirmed": False, "reason": "other"}}])
    time.sleep(0.4)
    assert row(Lead, lid3).email == "tess@example.org" and row(Call, cid3).email_reason == "interested"
    fake.finish(room3)
    wait(lambda: row(Call, cid3).status == "ended")
    print("transcript backup: saved without the webhook (spoken form cleaned up); unconfirmed ignored")

# ---- 7. one-time setup: create the tool, then only re-point it when the tunnel URL changes (fake Cartesia) ------
import asyncio  # noqa: E402
import pathlib  # noqa: E402

env = {}
cartesia_setup.save_env = lambda k, v: (env.__setitem__(k, v), os.environ.__setitem__(k, v))
cartesia_setup.STATE_PATH = pathlib.Path(f"{TMP}/email_tool_state.json")
cartesia_setup.STATE_PATH.unlink(missing_ok=True)
tools, log = {}, []


async def create_tool(b):
    tools["agent_tool_mail"] = b
    log.append("create")
    return {"id": "agent_tool_mail"}


async def get_tool(tid):
    if tid not in tools:
        raise cartesia.CartesiaError("not found", status=404)
    return tools[tid]


async def update_tool(tid, b):
    tools[tid] = {**tools[tid], **b}
    log.append("update")
    return tools[tid]

cartesia.create_tool, cartesia.get_tool, cartesia.update_tool = create_tool, get_tool, update_tool
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""


async def setup():
    a = await cartesia_setup.ensure_email_tool("https://a.trycloudflare.com")
    b = await cartesia_setup.ensure_email_tool("https://a.trycloudflare.com")
    c2 = await cartesia_setup.ensure_email_tool("https://b.trycloudflare.com/")
    return a, b, c2

a, b, c2 = asyncio.run(setup())
assert (a["action"], b["action"], c2["action"]) == ("created", "unchanged", "updated"), (a, b, c2)
assert env["CARTESIA_EMAIL_TOOL_ID"] == "agent_tool_mail" and log == ["create", "update"]
assert tools["agent_tool_mail"]["api_schema"]["url"] == "https://b.trycloudflare.com/api/cartesia/tools/collect_email"
cartesia_setup.STATE_PATH.unlink(missing_ok=True)
print("setup: created once, unchanged, re-pointed at the new tunnel URL")
print("ALL EMAIL TOOL TESTS PASS")
