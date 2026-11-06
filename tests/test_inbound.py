"""Inbound calls: the switch (Cartesia agent, tools, trunk allow-list, number assignment), the inbound agent's config,
webhooks / tools of a call to our number (known lead, new caller, Do-Not-Contact), History + lead page, no texts,
our own length cap. Fake Cartesia, throwaway DB, no calls."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import re
import time

os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # no email tool here (test_email_tool.py)
os.environ["SMS_SENDING_DISABLED"] = "1"
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""

import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import app.calls as calls  # noqa: E402
from app import agent_config, cartesia, cartesia_setup, inbound, leads, sms, standby, supervisor, tunnel  # noqa: E402
from app.db import Call, Lead, LeadEvent, SmsMessage, get_session  # noqa: E402
from settings import AgentSettings, Inbound, SettingsError, load_settings  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel  # never touch the real webhook/tools from a test
from app.main import app  # noqa: E402

SECRET = "inbound-test-secret"
OUR = os.environ["TELNYX_PHONE_NUMBER"]
os.environ.update(CARTESIA_SUPERVISOR_TOOL_ID="sup_tool", CARTESIA_OUTCOME_TOOL_ID="out_tool", CARTESIA_TOOL_SECRET=SECRET,
                  CARTESIA_WEBHOOK_SECRET="hook-secret", CARTESIA_WEBHOOK_ID="wh_test", CARTESIA_FROM_NUMBER_ID="ap_ours",
                  CARTESIA_SIP_PROVIDER_ID="ata_ours", CARTESIA_INBOUND_AGENT_ID="", CARTESIA_LOOKUP_TOOL_ID="",
                  CARTESIA_SAVE_CALLER_TOOL_ID="", PUBLIC_BASE_URL="https://dialer.example.com")
AUTH = {"Authorization": f"Bearer {SECRET}"}
HOOK = {"X-Webhook-Secret": "hook-secret"}
calls.POLL_EVERY_S = 0.05
calls._silence_settings = lambda: {"enabled": True, "check_s": 5, "max_s": 60, "greet_wait_s": 4}


class Fake:
    """Cartesia: agents, tools, the number, the trunk, calls."""

    def __init__(self):
        self.writes, self.calls, self.cancels = [], {}, []
        self.number_agent = None
        self.provider = {"id": "ata_ours", "outbound": {"address": "sip.telnyx.com"}}
        self.tools, self.agents, self.hooks = {}, {}, {}

    def w(self, *what):
        self.writes.append(what)

    async def get_number(self, nid):
        assert nid == "ap_ours"
        return {"id": nid, "number": OUR, "agent": {"id": self.number_agent, "name": "x"} if self.number_agent else None}

    async def assign_number(self, nid, agent):
        self.w("assign", agent)
        self.number_agent = agent
        return {}

    async def get_provider(self, pid):
        return self.provider

    async def update_provider_inbound(self, pid, inb):
        self.w("trunk", inb)
        self.provider = {**self.provider, "inbound": {**inb, "allowed_numbers": [], "auth_username": ""}}
        return self.provider

    async def create_tool(self, body):
        tid = f"tool_{body['name']}"
        self.tools[tid] = body
        self.w("create_tool", body["name"])
        return {"id": tid}

    async def get_tool(self, tid):
        if tid not in self.tools:
            raise cartesia.CartesiaError("not found", 404)
        return self.tools[tid]

    async def update_tool(self, tid, body):
        self.w("update_tool", tid)
        self.tools[tid] = {**self.tools[tid], **body}
        return {}

    async def create_agent(self, name, desc, config):
        self.w("create_agent", name)
        self.agents["agent_in"] = config
        return {"id": "agent_in"}

    async def update_agent_config(self, aid, config, why):
        self.w("update_agent", aid)
        self.agents[aid] = config
        return {}

    async def get_agent_record(self, aid):
        return {"id": aid, "webhook_id": self.hooks.get(aid)}

    async def attach_webhook(self, aid, wid):
        self.w("attach_webhook", aid, wid)
        self.hooks[aid] = wid
        return {}

    async def get_call(self, cid):
        c = self.calls[cid]
        return {"id": cid, "agent_id": c.get("agent_id", "agent_in"), "status": c["status"], "end_reason": c["end_reason"],
                "transcript": list(c["transcript"]), "telephony_params": {"from": c["caller"], "to": OUR, "direction": c.get("direction", "inbound")},
                "start_time": "2026-11-04T15:00:00Z", "end_time": "2026-11-04T15:01:00Z"}

    async def cancel_call(self, cid):
        self.cancels.append(cid)
        self.finish(cid, "api_cancelled")
        return {}

    async def create_call(self, *a, **k):
        raise AssertionError("no outbound call in this test")

    # a call to our number
    def ring(self, cid, caller):
        self.calls[cid] = {"status": "started", "end_reason": None, "transcript": [], "caller": caller}

    def say(self, cid, role, text, start, tools=None):
        self.calls[cid]["transcript"].append({"role": role, "text": text, "start_timestamp": start,
                                              "end_timestamp": start + 1.5, "tool_calls": tools or []})

    def finish(self, cid, reason="client_hangup"):
        self.calls[cid].update(status="completed", end_reason=reason)


fake = Fake()
for name in ("get_number", "assign_number", "get_provider", "update_provider_inbound", "create_tool", "get_tool",
             "update_tool", "create_agent", "update_agent_config", "get_agent_record", "attach_webhook", "get_call",
             "cancel_call", "create_call"):
    setattr(cartesia, name, getattr(fake, name))


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync  # the outbound agent isn't what this suite tests


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


def call_by_room(room):
    with get_session() as s:
        return s.exec(select(Call).where(Call.room == room)).first()


def leads_events(lead_id):
    with get_session() as s:
        return s.exec(select(LeadEvent).where(LeadEvent.lead_id == lead_id, LeadEvent.kind == "status")).all()


def new_lead(name, phone, **kw):
    with get_session() as s:
        x = Lead(phone=phone, name=name, location=kw.pop("location", "Austin, TX 78701"), **kw)
        s.add(x)
        s.commit()
        return x.id


def set_inbound(c, on, **extra):
    data = c.get("/api/settings").json()
    values = data["values"]
    values["inbound"] = {**values["inbound"], "enabled": on, **extra}
    r = c.put("/api/settings", json={"values": values, "version": data["version"]})
    assert r.status_code == 200, r.text
    return r.json()


def started_hook(cid, caller, agent="agent_in"):
    return {"type": "call_started", "webhook_request_id": f"rq-{cid}",
            "call": {"id": cid, "agent_id": agent, "status": "started", "transcript": [],
                     "telephony_params": {"from": caller, "to": OUR, "direction": "inbound"}}}


def outcome_turn(cid, outcome, note, start, callback_time=""):
    fake.say(cid, "assistant", "Thanks so much, talk soon. Bye!", start,
             [{"id": f"o{start}", "name": "record_outcome",
               "arguments": {"outcome": outcome, "note": note, "callback_time": callback_time}},
              {"id": f"e{start}", "name": "end_call", "arguments": {}}])


# ---- 1. settings: default off, greeting can't use lead details ------------------------------------------
s = load_settings()
assert s.inbound.enabled is False and "{company_name}" in s.inbound.greeting
assert AgentSettings.model_fields["inbound"].default_factory().enabled is False  # an older settings file: off
for bad in ("Hi {lead_name}, thanks for calling", "Two\nlines"):
    try:
        Inbound(greeting=bad)
        raise AssertionError(f"accepted {bad!r}")
    except ValueError:
        pass
print("settings: inbound off by default (also for an older file); greeting can't use {lead_name}")

# ---- 2. the inbound agent's config ---------------------------------------------------------------------
os.environ.update(CARTESIA_LOOKUP_TOOL_ID="tl", CARTESIA_SAVE_CALLER_TOOL_ID="ts")
cfg = agent_config.build_inbound_config(s, "+15555550100")
assert cfg["initial_message"] == s.fill(s.inbound.greeting) and "{" not in cfg["initial_message"]
assert not re.search(r"\{\{", cfg["instructions"]), "no per-call variables exist on an inbound call"
assert "[their name]" in agent_config.inbound_labels("{{lead_name}}") and "[specialist title]" in cfg["instructions"]
assert "lookup_caller" in cfg["instructions"] and "save_caller" in cfg["instructions"]
assert "do_not_contact: yes" in cfg["instructions"] and "known: no" in cfg["instructions"]
assert cfg["system_tools"]["voicemail_detection"] is None and cfg["system_tools"]["end_call"]["pre_tool_speech"] == "force"
assert cfg["system_tools"]["transfer_to_number"]["transfers"][0]["destination"]["phone_number"] == "+15555550100"
assert [t["id"] for t in cfg["tools"]] == ["tl", "ts", "out_tool", "sup_tool"], cfg["tools"]
cfg2 = agent_config.build_inbound_config(s, "")  # no supervisor phone: no transfer, the AI books a callback
assert cfg2["system_tools"]["transfer_to_number"] is None and "sup_tool" not in [t["id"] for t in cfg2["tools"]]
assert "request_supervisor" not in cfg2["instructions"] and "callback_time" in cfg2["instructions"]
out = agent_config.build_config(s)  # the outbound agent is unchanged: still per-call variables + voicemail tool
assert "{{silent_greeting}}" in (out["initial_message"] or "") and out["system_tools"]["voicemail_detection"] is not None
assert agent_config.lookup_tool_body("https://x/t", "s")["api_schema"]["request_body_schema"]["properties"]["caller"] == \
    {"type": "string", "dynamic_variable": "system__caller_id"}
os.environ.update(CARTESIA_LOOKUP_TOOL_ID="", CARTESIA_SAVE_CALLER_TOOL_ID="")
print("config: greeting first, no {{variables}}, [labels], no voicemail tool, transfer to the supervisor's phone; outbound unchanged")

# ---- 3. the switch -----------------------------------------------------------------------------------------
supervisor.save_person("supervisor", {"phone": "+15555550100", "available": True})
with TestClient(app) as c:
    st = c.get("/api/settings").json()["inbound"]
    assert st["enabled"] is False and st["number"] == OUR
    fake.writes.clear()
    res = set_inbound(c, False)  # saving with it off: nothing changes in Cartesia
    assert res["inbound"]["state"] == "off" and fake.writes == [], (res["inbound"], fake.writes)

    res = set_inbound(c, True)
    assert res["inbound"]["state"] == "on", res["inbound"]
    names = [w[1] for w in fake.writes if w[0] == "create_tool"]
    assert names == ["lookup_caller", "save_caller"], fake.writes
    assert ("create_agent", cartesia_setup.INBOUND_AGENT_NAME) in fake.writes
    assert os.environ["CARTESIA_INBOUND_AGENT_ID"] == "agent_in" and os.environ["CARTESIA_LOOKUP_TOOL_ID"] == "tool_lookup_caller"
    assert ("attach_webhook", "agent_in", "wh_test") in fake.writes
    trunk = next(w[1] for w in fake.writes if w[0] == "trunk")
    assert "192.76.120.10" in trunk["allowed_addresses"] and "64.16.250.10" in trunk["allowed_addresses"]
    assert trunk["media_encryption"] == "allowed" and "outbound" not in trunk
    assert fake.number_agent == "agent_in"
    tool_url = fake.tools["tool_lookup_caller"]["api_schema"]["url"]
    assert tool_url == "https://dialer.example.com/api/cartesia/tools/lookup_caller", tool_url
    ag = fake.agents["agent_in"]
    assert [t["id"] for t in ag["tools"]][:3] == ["tool_lookup_caller", "tool_save_caller", "out_tool"]
    print("ON: lookup_caller + save_caller tools, inbound agent, webhook, trunk allow-list (Telnyx IPs only), number assigned")

    fake.writes.clear()
    assert set_inbound(c, True)["inbound"]["state"] == "on" and fake.writes == [], fake.writes  # nothing to do twice
    supervisor_res = c.put("/api/supervisor/people/supervisor", json={"phone": "+15555550111"})
    assert supervisor_res.status_code == 200
    wait(lambda: ("update_agent", "agent_in") in fake.writes)
    assert fake.agents["agent_in"]["system_tools"]["transfer_to_number"]["transfers"][0]["destination"]["phone_number"] == "+15555550111"
    # a second supervisor with a phone: the destination becomes the per-call variable the Accept answer fills in
    fake.writes.clear()
    assert c.put("/api/supervisor/people/sara", json={"phone": "+15555550122", "name": "Sara"}).status_code == 200
    wait(lambda: ("update_agent", "agent_in") in fake.writes)
    assert fake.agents["agent_in"]["system_tools"]["transfer_to_number"]["transfers"][0]["destination"]["phone_number"] == "{{transfer_number}}"
    fake.writes.clear()
    assert c.delete("/api/supervisor/people/sara").status_code == 200
    wait(lambda: ("update_agent", "agent_in") in fake.writes)
    assert fake.agents["agent_in"]["system_tools"]["transfer_to_number"]["transfers"][0]["destination"]["phone_number"] == "+15555550111"
    print("idempotent; a new supervisor phone re-syncs the inbound agent's transfer; 2 supervisors -> {{transfer_number}}")

    fake.writes.clear()
    res = set_inbound(c, False)
    assert res["inbound"]["state"] == "off" and fake.writes == [("assign", None)] and fake.number_agent is None
    fake.number_agent = "agent_someone_else"  # connected by hand in the Playground: not ours to undo
    fake.writes.clear()
    asyncio.run(inbound.apply("test"))
    assert fake.writes == [] and "Playground" in inbound.state["detail"]
    fake.number_agent = None
    os.environ["DIALER_STANDBY"] = "1"
    assert asyncio.run(inbound.apply("test"))["state"] == "standby" and fake.writes == []
    os.environ["DIALER_STANDBY"] = ""
    print("OFF: number unassigned (only from our own agent); standby changes nothing")
    res = set_inbound(c, True)
    assert res["inbound"]["state"] == "on" and fake.number_agent == "agent_in"

    # ---- 4. tools: auth + public through the tunnel -------------------------------------------------------
    for path in ("lookup_caller", "save_caller"):
        assert c.post(f"/api/cartesia/tools/{path}", json={"call_id": "x"}).status_code == 401
        assert c.post(f"/api/cartesia/tools/{path}", headers={"cf-ray": "1", **AUTH}, json={"call_id": "pg"}).status_code == 200
    print("tools: 401 without the secret; reachable through the tunnel")

    # ---- 5. a known lead calls back ----------------------------------------------------------------------
    lid = new_lead("Sarah Lee", "+15125550301", business="Lee Bakes", service_required="website redesign")
    with get_session() as db:  # our earlier call to her
        db.add(Call(room="ac_out1", number="+15125550301", status="ended", result="no_answer", outcome="no_answer", lead_id=lid))
        db.commit()
    fake.ring("ac_in1", "+15125550301")
    assert c.post("/api/cartesia/webhook", headers=HOOK, json=started_hook("ac_in1", "+15125550301")).status_code == 200
    call = call_by_room("ac_in1")
    assert call and call.direction == "inbound" and call.source == "inbound" and call.lead_id == lid and call.number == "+15125550301"
    lead = row(Lead, lid)
    assert lead.attempts == 0 and lead.last_call_id == call.id  # not one of our dialing attempts
    wait(lambda: row(Call, call.id).status == "answered")
    r = c.post("/api/cartesia/tools/lookup_caller", headers=AUTH, json={"call_id": "ac_in1", "caller": "+15125550301"})
    text = r.text
    assert "known: yes" in text and "their name: Sarah Lee" in text and "do_not_contact: no" in text, text
    assert "why we called: We called them today" in text and "website redesign" in text and "No answer" in text, text
    assert "specialist title: our senior web designer" in text and "specialist status: available" in text, text
    assert "their time zone: Central" in text
    fake.say("ac_in1", "assistant", "Thanks for calling Example Agency, this is Taylor. How can I help you?", 0.3)
    fake.say("ac_in1", "user", "Hi, I missed a call from this number. Please leave a message after the tone, ha.", 3)
    time.sleep(0.4)
    assert "ac_in1" not in fake.cancels  # no voicemail guess on a call to us
    fake.say("ac_in1", "assistant", "Oh hi Sarah! Yes, we tried you earlier about your website.", 6)
    outcome_turn("ac_in1", "interested", "wants a quote for a redesign", 12)
    wait(lambda: row(Lead, lid).status != "calling")  # ended, and the lead updated after it
    call = row(Call, call.id)
    assert call.outcome == "interested" and "ac_in1" in fake.cancels  # closed after the goodbye, like outbound
    assert row(Lead, lid).status == "interested"
    wait(lambda: True, 0.3)
    with get_session() as db:
        assert db.exec(select(SmsMessage).where(SmsMessage.call_id == call.id)).first() is None  # no texts after inbound
    assert sms.for_call(call.id) is None
    hist = c.get("/api/calls").json()
    assert next(h for h in hist if h["id"] == call.id)["direction"] == "inbound"
    page = c.get(f"/api/leads/{lid}").json()
    assert [x["direction"] for x in page["calls"]][:1] == ["inbound"]
    assert any("Inbound call" in e["text"] for e in page["events"])
    print("known lead: linked by number, greeted from lookup (name, why we called), no voicemail guess, outcome, "
          "History + lead page say inbound, no text")

    # ---- 6. a new caller: save_caller makes the lead ---------------------------------------------------
    fake.ring("ac_in2", "+13125550444")
    c.post("/api/cartesia/webhook", headers=HOOK, json=started_hook("ac_in2", "+1 (312) 555-0444"))
    call = call_by_room("ac_in2")
    assert call.lead_id is None and call.number == "+13125550444"
    text = c.post("/api/cartesia/tools/lookup_caller", headers=AUTH, json={"call_id": "ac_in2", "caller": "+13125550444"}).text
    assert "known: no" in text and "their name" not in text and "A new caller" in text, text
    r = c.post("/api/cartesia/tools/save_caller", headers=AUTH, json={
        "call_id": "ac_in2", "name": "Tom Reyes", "business_name": "Reyes Plumbing", "service": "a new website",
        "note": "found us on Google", "location": "Chicago, IL"})
    assert r.text.startswith("Saved.") and "our senior web designer" in r.text, r.text
    call = row(Call, call.id)
    lead = row(Lead, call.lead_id)
    assert lead.name == "Tom Reyes" and lead.business == "Reyes Plumbing" and lead.service_required == "a new website"
    assert lead.source == "inbound call" and "found us on Google" in lead.notes and lead.phone == "+13125550444"
    r = c.post("/api/cartesia/tools/save_caller", headers=AUTH, json={  # a correction: same lead, updated
        "call_id": "ac_in2", "name": "Tom Reyes Jr", "business_name": "", "service": "", "note": "", "location": ""})
    assert row(Lead, call.lead_id).name == "Tom Reyes Jr" and row(Call, call.id).lead_id == lead.id
    bad = c.post("/api/cartesia/tools/save_caller", headers=AUTH, json={"call_id": "ac_in2", "name": "", "service": ""}).text
    assert bad.startswith("Not saved")
    outcome_turn("ac_in2", "not_now", "call him tomorrow", 20, "2026-11-05T10:00")
    wait(lambda: row(Lead, row(Call, call.id).lead_id).status != "calling")  # ended, and the lead updated after it
    lead = row(Lead, call.lead_id)
    assert lead.status in ("callback", "needs_review") and lead.attempts == 0, lead.status
    with get_session() as db:
        assert db.exec(select(LeadEvent).where(LeadEvent.lead_id == lead.id, LeadEvent.kind == "imported")).first()
    print("new caller: lookup says new, save_caller creates the lead (and corrects it), callback booked")

    # ---- 7. a new caller who wants a callback but was never saved: a lead at the end -------------------
    fake.ring("ac_in3", "+14155550555")
    c.post("/api/cartesia/webhook", headers=HOOK, json=started_hook("ac_in3", "+14155550555"))
    call = call_by_room("ac_in3")
    wait(lambda: row(Call, call.id).status == "answered")
    outcome_turn("ac_in3", "not_now", "busy, call back next week", 8)
    wait(lambda: row(Call, call.id).lead_id and row(Lead, row(Call, call.id).lead_id).status == "callback")
    call = row(Call, call.id)
    assert call.lead_id and row(Lead, call.lead_id).source == "inbound call" and row(Lead, call.lead_id).callback_at
    fake.ring("ac_in3b", "+14155550666")  # a wrong number: no lead
    c.post("/api/cartesia/webhook", headers=HOOK, json=started_hook("ac_in3b", "+14155550666"))
    wait(lambda: call_by_room("ac_in3b") and row(Call, call_by_room("ac_in3b").id).status == "answered")
    outcome_turn("ac_in3b", "wrong_number", "dialed by mistake", 5)
    wait(lambda: row(Call, call_by_room("ac_in3b").id).status == "ended")
    assert call_by_room("ac_in3b").lead_id is None and leads.find_by_phone("+14155550666") is None
    print("unsaved caller who wants a callback -> a lead at the end; wrong number -> none")

    # ---- 8. Do-Not-Contact caller: no pitch, no callback, stays DNC -------------------------------------
    dnc = new_lead("Rude Rick", "+12145550777", service_required="seo")
    leads.set_dnc(dnc, True, "asked us to stop")
    fake.ring("ac_in4", "+12145550777")
    c.post("/api/cartesia/webhook", headers=HOOK, json=started_hook("ac_in4", "+12145550777"))
    call = call_by_room("ac_in4")
    text = c.post("/api/cartesia/tools/lookup_caller", headers=AUTH, json={"call_id": "ac_in4", "caller": "+12145550777"}).text
    assert "do_not_contact: yes" in text and "specialist status: unavailable" in text, text
    outcome_turn("ac_in4", "not_now", "asked about prices", 9, "2026-11-05T10:00")
    wait(lambda: row(Call, call.id).status == "ended")
    wait(lambda: any("Call #" in e.text for e in leads_events(dnc)))  # the lead updated after the call
    lead = row(Lead, dnc)
    assert lead.dnc and lead.status == "do_not_contact" and lead.callback_at is None
    print("Do-Not-Contact caller: lookup says so, no transfer, never a callback, stays DNC")

    # ---- 9. our own length cap; a turn webhook of a call we never heard start ---------------------------
    calls.MAX_CALL_MINUTES_CAP = 1
    fake.ring("ac_in5", "+13035550888")
    c.post("/api/cartesia/webhook", headers=HOOK, json=started_hook("ac_in5", "+13035550888"))
    call = call_by_room("ac_in5")
    wait(lambda: row(Call, call.id).status == "answered")
    tr = calls.manager.trackers[call.id]
    assert tr.inbound and tr.max_s == 60 and tr.vm_message == ""
    tr.max_s = 0.5
    wait(lambda: "ac_in5" in fake.cancels)
    wait(lambda: row(Call, call.id).status == "ended")
    assert "max call length" in row(Call, call.id).reason, row(Call, call.id).reason
    fake.ring("ac_in6", "+13035550999")
    turn = {"type": "call_turn", "call_id": "ac_in6", "webhook_request_id": "t6",
            "turn": {"role": "user", "text": "Hello?", "start_timestamp": 1, "end_timestamp": 2}}
    assert c.post("/api/cartesia/webhook", headers=HOOK, json=turn).json()["ignored"] == "unknown call"
    wait(lambda: call_by_room("ac_in6") is not None)
    assert call_by_room("ac_in6").direction == "inbound" and call_by_room("ac_in6").number == "+13035550999"
    fake.calls["ac_out9"] = {"status": "started", "end_reason": None, "transcript": [], "caller": "+1", "agent_id": "agent_other",
                        "direction": "outbound"}
    unknown = dict(started_hook("ac_out9", "+15555550123", agent="agent_other"))
    unknown["call"] = {**unknown["call"], "telephony_params": {"from": OUR, "to": "+15555550123", "direction": "outbound"}}
    c.post("/api/cartesia/webhook", headers=HOOK, json=unknown)
    time.sleep(0.3)
    assert call_by_room("ac_out9") is None  # a Playground / other agent's call: still ignored
    fake.finish("ac_in6")
    print("inbound calls closed at our own length cap; a missed start is picked up from a turn; other calls ignored")

    st = c.get("/api/settings").json()["inbound"]
    assert st["state"] == "on" and st["enabled"] is True
    set_inbound(c, False)

print("ALL INBOUND TESTS PASSED")
