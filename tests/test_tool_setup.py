"""Phase 5 step 7: record_outcome tool setup logic + agent config. Fake Cartesia; .env and state
writes go to memory / a temp file (nothing real is touched)."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import sys

os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile
import env_utils  # noqa: E402,F401

from app import agent_config, cartesia, cartesia_setup  # noqa: E402
from settings import load_settings  # noqa: E402

env = {}
cartesia_setup.save_env = lambda k, v: (env.__setitem__(k, v), os.environ.__setitem__(k, v))
cartesia_setup.STATE_PATH = __import__("pathlib").Path(f"{TMP}/tool_state.json")
cartesia_setup.STATE_PATH.unlink(missing_ok=True)
os.environ["CARTESIA_OUTCOME_TOOL_ID"] = ""
os.environ["CARTESIA_TOOL_SECRET"] = "s3cret-tool"


class FakeTools:
    def __init__(self):
        self.tools, self.log, self.n = {}, [], 0

    async def create_tool(self, body):
        self.n += 1
        tid = f"agent_tool_{self.n}"
        self.tools[tid] = body
        self.log.append(("create", tid))
        return {"id": tid, **body}

    async def update_tool(self, tid, body):
        self.tools[tid] = {**self.tools[tid], **body}
        self.log.append(("update", tid))
        return self.tools[tid]

    async def get_tool(self, tid):
        if tid not in self.tools:
            raise cartesia.CartesiaError("not found", 404)
        return self.tools[tid]


f = FakeTools()
cartesia.create_tool, cartesia.update_tool, cartesia.get_tool = f.create_tool, f.update_tool, f.get_tool


async def run():
    # no tool yet: config has no tools and no outcome section (the AI isn't told about a missing tool)
    cfg = agent_config.build_config(load_settings())
    assert cfg["tools"] == [] and "# Call outcome" not in cfg["instructions"]
    r = await cartesia_setup.ensure_outcome_tool("https://a.trycloudflare.com")
    assert r["action"] == "created" and env["CARTESIA_OUTCOME_TOOL_ID"] == r["tool_id"]
    body = f.tools[r["tool_id"]]
    api = body["api_schema"]
    assert api["url"] == "https://a.trycloudflare.com/api/cartesia/tools/record_outcome" and api["method"] == "POST"
    assert api["authentication"]["mode"] == "bearer" and api["authentication"]["token"]["secret_value"] == "s3cret-tool"
    props = api["request_body_schema"]["properties"]
    assert props["call_id"] == {"type": "string", "dynamic_variable": "system__conversation_id"}
    assert "call_id" not in api["request_body_schema"]["required"]
    assert body["execution_mode"] == "async"
    # same URL again: nothing to do
    assert (await cartesia_setup.ensure_outcome_tool("https://a.trycloudflare.com"))["action"] == "unchanged"
    # tunnel restarted: new URL -> updated in place, same ID
    r2 = await cartesia_setup.ensure_outcome_tool("https://b.trycloudflare.com/")
    assert r2["action"] == "updated" and r2["tool_id"] == r["tool_id"]
    assert f.tools[r["tool_id"]]["api_schema"]["url"].startswith("https://b.trycloudflare.com/api/")
    # tool deleted in Cartesia: recreated
    f.tools.clear()
    r3 = await cartesia_setup.ensure_outcome_tool("https://b.trycloudflare.com")
    assert r3["action"] == "created" and r3["tool_id"] != r["tool_id"]
    print("tool setup:", [x[0] for x in f.log])

    # with the tool: config references it and the prompt explains the outcomes
    cfg = agent_config.build_config(load_settings())
    assert cfg["tools"] == [{"id": r3["tool_id"]}], cfg["tools"]
    ins = cfg["instructions"]
    for k in ("# Call outcome", "record_outcome", "do_not_contact", "gatekeeper", "{{lead_local_time}}", "YYYY-MM-DDTHH:MM"):
        assert k in ins, k
    assert cfg["system_tools"]["voicemail_detection"]["message"] is None  # the AI says our message itself
    print("agent config: tool attached, outcome rules in the prompt, no voicemail message")


asyncio.run(run())
cartesia_setup.STATE_PATH.unlink(missing_ok=True)
print("ALL TOOL SETUP TESTS PASS")
