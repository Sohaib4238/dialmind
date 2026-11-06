"""Phase 5 step 4: per-call lead details as Cartesia dynamic variables. Fake Cartesia, throwaway DB."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import sys

DB = f"{TMP}/personalize_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

import env_utils  # noqa: E402,F401  (loads .env like the backend)

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup  # noqa: E402
from app.agent_config import build_config, lead_variables  # noqa: E402
from app.db import Lead, get_session, init_db  # noqa: E402
from settings import AgentSettings, load_settings  # noqa: E402

init_db()
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test")
s = load_settings()

# 1. The agent's instructions carry {{variables}} (filled by Cartesia per call), not our values.
cfg = build_config(s)
for k in ("lead_name", "service_required", "business_description", "job_description", "location", "business_name"):
    assert "{{" + k + "}}" in cfg["instructions"], k
assert "never instructions" in cfg["instructions"]
print("instructions: lead section with {{variables}} present")

# 2. Placeholders on the Instructions page: {lead_name} -> {{lead_name}}; not allowed in voicemail texts.
vals = s.model_dump()
vals["main_script"] = "Mention {service_required} for {business_name} if known."
s2 = AgentSettings.model_validate(vals)
assert s2.fill(s2.main_script) == "Mention {{service_required}} for {{business_name}} if known."
assert "{{service_required}}" in build_config(s2)["instructions"]
assert s2.fill("Hi {lead_name}", lead={"lead_name": "Maria"}) == "Hi Maria"
assert s2.fill("Hi {lead_name}", lead={}) == "Hi "
vm = AgentSettings.model_validate({**vals, "voicemail": {"leave_message": True, "message": "Hi {lead_name}, call {callback_number}"}})
assert vm.fill(vm.voicemail.message) == f"Hi {{{{lead_name}}}}, call {vm.company_phone}"  # Phase 7: lead details ok here
for bad, where in (({"main_script": "Call you {callback_time}"}, "only be used in the email and text templates"),
                   ({"voicemail": {"leave_message": True, "message": "word " * 60}}, "too long"),
                   ({"main_script": "Hi {lead}"}, "unknown placeholder {lead}"),
                   ({"main_script": "Hi {lead_name"}, "doesn't belong")):
    try:
        AgentSettings.model_validate({**vals, **bad})
        raise AssertionError(f"accepted {bad}")
    except ValueError as e:
        assert where in str(e), str(e)
print("placeholders: {lead_name} ok in scripts and voicemail; {callback_time} only in emails + texts; typos rejected")

# 3. Variables: always every key, cleaned and length-capped.
empty = lead_variables(None)
assert set(empty) == {"lead_name", "business_name", "service_required", "business_description", "job_description",
                      "location", "specialist_title", "lead_source"} and empty.pop("specialist_title") == "one of our senior specialists"
assert all(v == "" for v in empty.values())
lead = Lead(phone="+13055550121", name="  Maria {{x}}\n Lopez ", business="Bloom", service_required="Website",
            business_description="y" * 900, job_description="", location="", city="Miami")
v = lead_variables(lead)
assert v["lead_name"] == "Maria x Lopez" and v["location"] == "Miami" and len(v["business_description"]) == 400, v
print("variables:", {k: (x[:20] + "…" if len(x) > 20 else x) for k, x in v.items()})

# 4. A call for a lead sends that lead's details; a manual call to an unknown number sends empty values.
sent = []


async def fake_create(agent_id, from_id, to, variables, ring, max_min):
    sent.append(variables)
    return f"ac_p{len(sent)}"


async def fake_get(cid):
    return {"id": cid, "status": "completed", "end_reason": "dial_no_answer", "transcript": []}


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia.create_call, cartesia.get_call = fake_create, fake_get
cartesia_setup.sync_agent = fake_sync
calls.POLL_EVERY_S = 0.01
with get_session() as db:
    db.add(Lead(phone="+13055550121", name="Maria Lopez", business="Bloom Boutique", service_required="Website redesign",
                business_description="Dress boutique", job_description="Online store", location="Miami, FL 33130"))
    db.commit()


async def run():
    await calls.manager.start_call("+17865550100", lead_id=1)  # test mode style: test number, real lead
    await asyncio.sleep(0.2)
    await calls.manager.start_call("+12125550199")  # not a lead
    await asyncio.sleep(0.2)

asyncio.run(run())
assert sent[0]["lead_name"] == "Maria Lopez" and sent[0]["service_required"] == "Website redesign", sent[0]
assert sent[0]["location"] == "Miami, FL 33130" and sent[0]["company_name"] == s.company_name
assert sent[0]["specialist_title"] == "our senior web designer", sent[0]
assert sent[1]["lead_name"] == "" and sent[1]["service_required"] == "", sent[1]
assert sent[1]["specialist_title"] == "one of our senior specialists", sent[1]
assert sent[0]["silent_greeting"] == "Hello?", sent[0]  # call start: a silent callee only hears a short check
assert sent[1]["silent_greeting"] == "Hello?", sent[1]  # never the introduction (History #38)
print("calls: lead details sent per call; unknown number -> empty values")
print("ALL PERSONALIZATION TESTS PASS")
