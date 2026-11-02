"""Phase 6: request_supervisor tool setup + transfer_to_number in the agent config. Fake Cartesia;
.env and state writes go to memory / a temp file (nothing real is touched)."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import sys

os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile
import env_utils  # noqa: E402,F401

from app import agent_config, cartesia, cartesia_setup, tunnel  # noqa: E402
from settings import load_settings  # noqa: E402

env = {}
cartesia_setup.save_env = lambda k, v: (env.__setitem__(k, v), os.environ.__setitem__(k, v))
cartesia_setup.STATE_PATH = __import__("pathlib").Path(f"{TMP}/transfer_state.json")
cartesia_setup.STATE_PATH.unlink(missing_ok=True)
os.environ.update(CARTESIA_OUTCOME_TOOL_ID="agent_tool_outcome", CARTESIA_SUPERVISOR_TOOL_ID="", CARTESIA_TOOL_SECRET="s3cret")


class FakeTools:
    def __init__(self):
        self.tools, self.log, self.n = {"agent_tool_outcome": {"api_schema": {"url": ""}}}, [], 0

    async def create_tool(self, body):
        self.n += 1
        tid = f"agent_tool_sup{self.n}"
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
    s = load_settings()
    # before the tool exists: no transfer, no hand-off section (the AI isn't told about missing tools)
    cfg = agent_config.build_config(s)
    assert "transfer_to_number" not in cfg["system_tools"] and cfg["tools"] == [{"id": "agent_tool_outcome"}]
    assert "request_supervisor" not in cfg["instructions"]
    print("no supervisor tool: no transfer, no hand-off instructions")

    r = await cartesia_setup.ensure_supervisor_tool("https://a.trycloudflare.com")
    assert r["action"] == "created" and env["CARTESIA_SUPERVISOR_TOOL_ID"] == r["tool_id"]
    body = f.tools[r["tool_id"]]
    api = body["api_schema"]
    assert body["name"] == "request_supervisor" and body["execution_mode"] == "immediate"
    assert 5 < body["response_timeout_secs"] <= 120  # Cartesia: 1-120; each use answers within ~5 s
    assert api["url"] == "https://a.trycloudflare.com/api/cartesia/tools/request_supervisor"
    assert api["authentication"]["mode"] == "bearer" and api["authentication"]["token"]["secret_value"] == "s3cret"
    props = api["request_body_schema"]["properties"]
    assert props["call_id"] == {"type": "string", "dynamic_variable": "system__conversation_id"}
    assert api["request_body_schema"]["required"] == ["summary"]
    print("request_supervisor created: immediate, bearer secret, call_id from Cartesia, summary from the AI")

    cfg = agent_config.build_config(s)
    tr = cfg["system_tools"]["transfer_to_number"]
    assert tr["transfers"] == [{"destination": {"type": "phone", "phone_number": "{{transfer_number}}"},
                                "condition": "Only right after request_supervisor answered AVAILABLE."}]
    assert cfg["tools"] == [{"id": "agent_tool_outcome"}, {"id": r["tool_id"]}]
    ins = cfg["instructions"]
    assert "{{supervisor_status}}" in ins and "Let me check if {{specialist_title}} is free right now, one moment." in ins
    assert "my manager" not in ins and "our manager" not in ins
    assert s.fill(s.transfer_intro) in ins and s.fill(s.senior_unavailable.message) in ins
    assert "outcome interested and their callback_time" in ins
    assert "end_call" in cfg["system_tools"] and "voicemail_detection" in cfg["system_tools"]
    assert cfg["system_tools"]["end_call"]["pre_tool_speech"] == "force"  # the AI must speak before it hangs up (History #32)
    assert cfg["system_tools"]["voicemail_detection"]["pre_tool_speech"] == "off"  # silent on a voicemail greeting
    assert "Never use the end call tool without saying a goodbye first" in ins
    assert "A day without an hour means that day at 10:00" in ins and "same hour as now" not in ins
    print("agent config: transfer_to_number -> {{transfer_number}}, both tools attached, hand-off + callback rules in the prompt")

    # same URL again: nothing changes; new tunnel URL: updated in place
    assert (await cartesia_setup.ensure_supervisor_tool("https://a.trycloudflare.com"))["action"] == "unchanged"
    r2 = await cartesia_setup.ensure_supervisor_tool("https://b.trycloudflare.com")
    assert r2["action"] == "updated" and r2["tool_id"] == r["tool_id"]
    assert f.tools[r["tool_id"]]["api_schema"]["url"].startswith("https://b.")
    # deleted in Cartesia: created again
    del f.tools[r["tool_id"]]
    r3 = await cartesia_setup.ensure_supervisor_tool("https://b.trycloudflare.com")
    assert r3["action"] == "created" and r3["tool_id"] != r["tool_id"]
    print("re-point on a new tunnel URL; re-created if deleted")

    # the tunnel watcher sets both tools up and syncs the agent when the supervisor tool is new
    synced = []

    async def fake_sync(reason=""):
        synced.append(reason)
        return {"action": "updated"}

    async def fake_webhook(url):
        return {"url": url + "/api/cartesia/webhook", "action": "unchanged", "attached": True}

    async def fake_outcome(url):
        return {"tool_id": "agent_tool_outcome", "url": url, "action": "unchanged"}

    async def fake_url(http, metrics_url=""):
        return "https://dash.trycloudflare.com" if metrics_url else "https://c.trycloudflare.com"

    async def fake_ping(http, base):
        return True

    cartesia_setup.sync_agent, cartesia_setup.ensure_webhook, cartesia_setup.ensure_outcome_tool = fake_sync, fake_webhook, fake_outcome
    tunnel._tunnel_url, tunnel._public_ping = fake_url, fake_ping
    del f.tools[r3["tool_id"]]
    await tunnel.check_once(None)
    assert tunnel.state["supervisor_tool"]["action"] == "created" and synced[:1] == ["request_supervisor tool + transfer attached"]
    assert tunnel.state["email_tool"]["action"] == "created" and synced[1:] == ["collect_email tool attached"], synced  # Phase 7
    assert tunnel.state["dashboard_url"] == "https://dash.trycloudflare.com"
    print("tunnel watcher: supervisor tool created + agent synced; dashboard tunnel URL found")


asyncio.run(run())
print("ALL TRANSFER CONFIG TESTS PASS")
