"""Offline tests: the voice provider switch and the Phonic provider, with a fake Phonic API (no network, no call).

Covers: the PHONIC_TEST_ONLY guard (only TEST_CALL_NUMBER), kill switches, standby, the per-call config (same
instructions as Cartesia), the conversation -> call mapping, a whole Phonic call through the dashboard API
(provider saved on the call), a carrier refusal (busy), the tools' call_info payload, and Cartesia staying the default.
"""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import time

os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/phonic_test.db"
os.environ.update(CARTESIA_SUPERVISOR_TOOL_ID="", CARTESIA_EMAIL_TOOL_ID="", DASHBOARD_PASSWORD_HASH="",
                  SMS_SENDING_DISABLED="1", TELNYX_MESSAGING_PROFILE_ID="", PHONIC_API_KEY="fake-phonic-key",
                  PHONIC_PUBLIC_URL="https://dialer.example.test", VOICE_PROVIDER_PATH=f"{TMP}/voice_provider.json",
                  CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test")
os.environ.pop("PHONIC_TEST_ONLY", None)  # unset = ON

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup, phonic, providers, standby, test_mode, tunnel  # noqa: E402
from app.agent_config import build_instructions  # noqa: E402
from app.main import app as main_app  # noqa: E402
from app.providers import phonic_provider  # noqa: E402
from app.providers.base import CallNotConnected  # noqa: E402
from settings import load_settings  # noqa: E402

calls.POLL_EVERY_S = 0.05
TEST_PHONE = "+17865550100"  # TEST_CALL_NUMBER in tests/test.env


async def _no_watch():
    return None

tunnel.watch = _no_watch


class FakePhonic:
    def __init__(self):
        self.requests, self.tools, self.script, self.t0 = [], {}, [], 0.0
        self.cancelled, self.refuse_sip, self.n = False, None, 0

    async def request(self, method, path, *, json=None, params=None, headers=None, timeout=None):
        self.requests.append((method, path, json, dict(headers or {})))
        if method != "GET" and standby.on() and not phonic.test_only():  # same rule as the real request()
            raise phonic.PhonicBlocked("Standby")
        if path == "/conversations/sip/outbound_call":
            if json["dry_run"]:
                return {"conversation_id": None, "twilio_call_sid": None, "dry_run": True}
            if self.refuse_sip:
                raise phonic.PhonicError("Phonic POST: HTTP 409", 409, {"error": {
                    "code": "sip_call_failed", "message": "Call not completed", "sip_status_code": self.refuse_sip}})
            self.n += 1
            self.t0, self.cancelled = time.monotonic(), False
            return {"conversation_id": f"conv_test{self.n}", "dry_run": False}
        if path == "/tools":
            if method == "GET":
                return {"tools": list(self.tools.values())}
            self.tools[json["name"]] = {"id": f"tool_{json['name']}", **json}
            return self.tools[json["name"]]
        if path.startswith("/tools/"):
            self.tools[path[7:]].update(json)
            return self.tools[path[7:]]
        if path.endswith("/cancel"):
            self.cancelled = True
            return {"success": True}
        if path.startswith("/conversations/"):
            now = time.monotonic() - self.t0
            cur = {"id": path.split("/")[2], "items": []}
            for at, obj in self.script:
                if now >= at:
                    cur = {"id": path.split("/")[2], **obj}
            if self.cancelled and not cur.get("ended_at"):
                cur = {**cur, "ended_at": "2026-11-10T10:00:30Z", "ended_by": "user_canceled"}
            return {"conversation": cur}
        if path == "/voices":
            return {"voices": [{"id": "grant", "name": "Grant", "description": None, "audio_url": "https://x"}]}
        raise AssertionError(f"unexpected Phonic request {method} {path}")


fake = FakePhonic()
phonic.request = fake.request


async def cartesia_create(agent_id, from_id, to, variables, ring, max_min):
    return "ac_cart1"


async def cartesia_get(call_id):
    return {"id": call_id, "status": "completed", "end_reason": "dial_no_answer", "transcript": []}


async def cartesia_cancel(call_id):
    return {}


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia.create_call, cartesia.get_call, cartesia.cancel_call = cartesia_create, cartesia_get, cartesia_cancel
cartesia_setup.sync_agent = fake_sync


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---- 1. the guard: only the test phone, whatever asks -------------------------------------------------------
cfg = {"system_prompt": "x"}
for bad in ("+15550000000", "+13025550199", "+1786555010"):
    try:
        run(phonic.outbound_call(bad, cfg, dry_run=True))
        raise AssertionError(f"{bad} was not blocked")
    except phonic.PhonicBlocked as e:
        assert "PHONIC_TEST_ONLY" in str(e)
assert not fake.requests, "a blocked number reached Phonic"
os.environ["PHONIC_TEST_ONLY"] = "0"
test_mode.set_on(True)  # test-only off: the dashboard's test mode still guards
try:
    run(phonic.outbound_call("+15550000000", cfg, dry_run=True))
    raise AssertionError("test mode did not block")
except phonic.PhonicBlocked as e:
    assert "TEST MODE" in str(e)
os.environ.pop("PHONIC_TEST_ONLY")
print("guard: other numbers blocked before anything is sent")

# ---- 2. dry run to the test phone: headers, trunk, kill switch ----------------------------------------------
reply = run(phonic.outbound_call(TEST_PHONE, cfg, dry_run=True))
assert reply == {"conversation_id": None, "twilio_call_sid": None, "dry_run": True}
method, path, body, headers = fake.requests[-1]
assert headers["X-Sip-Address"] == "sip.telnyx.com:5061" and ";" not in headers["X-Sip-Address"]
assert headers["X-Sip-Auth-Username"] == "fake-sip-user" and headers["X-Sip-Auth-Password"] == "fake-sip-password"
assert body["sip"] == {"transport": "tls", "media_encryption": "required"} and body["dry_run"] is True
assert body["from_phone_number"] == "+13025550199" and body["to_phone_number"] == TEST_PHONE
try:  # CARTESIA_CALLS_DISABLED=1 (_setup.py) also stops a real Phonic call
    run(phonic.outbound_call(TEST_PHONE, cfg))
    raise AssertionError("kill switch ignored")
except phonic.PhonicBlocked as e:
    assert "disabled" in str(e)
os.environ["DIALER_STANDBY"] = "1"
os.environ["PHONIC_TEST_ONLY"] = "0"
try:
    run(phonic.cancel("conv_x"))
    raise AssertionError("standby ignored")
except phonic.PhonicBlocked:
    pass
os.environ["PHONIC_TEST_ONLY"] = "1"
assert run(phonic.cancel("conv_x")) == {"success": True}  # standby + test-only: allowed
os.environ.pop("DIALER_STANDBY")
print("dry run ok; kill switch and standby respected")

# ---- 3. the per-call config reuses the Cartesia instructions ------------------------------------------------
s = load_settings()
variables = {"lead_name": "Test Lead", "business_name": "Test Plumbing", "silent_greeting": "Hello?", "intro_line": "Hi"}
opts = {"voice_id": "grant", "audio_speed": 1.1, "background_noise": "", "background_noise_level": 0.0}
conf = phonic_provider.build_call_config(s, variables, 2, {"record_outcome"}, opts)
assert conf["system_prompt"] == build_instructions(s, outcome=True, supervisor=False, email=False,
                                                   end_tool="natural_conversation_ending", voicemail_tool=False)
assert "{{lead_name}}" in conf["system_prompt"] and conf["template_variables"]["lead_name"] == "Test Lead"
assert "voicemail tool" not in conf["system_prompt"] and "Never say supervisor" not in conf["system_prompt"]
assert conf["tools"] == ["natural_conversation_ending", "record_outcome"]
assert conf["background_noise"] is None and conf["background_noise_level"] == 0 and conf["audio_speed"] == 1.1
assert conf["max_conversation_duration_sec"] == 120 and conf["welcome_message"] is None
noisy = phonic_provider.build_call_config(s, variables, 2, set(), {**opts, "background_noise": "office",
                                                                     "background_noise_level": 0.2})
assert noisy["background_noise"] == "office" and noisy["background_noise_level"] == 0.2
assert noisy["tools"] == ["natural_conversation_ending"]  # no tools: no outcome rules either
assert "record_outcome" not in noisy["system_prompt"].split("# Voicemail")[0]
print("config: same instructions as Cartesia, Phonic voice/speed/background")

# ---- 4. conversation -> call shape ------------------------------------------------------------------------------
ITEMS = [
    {"item_idx": 0, "role": "user", "live_transcript": "Hello?", "started_at": "2026-11-10T10:00:01Z", "duration_ms": 600,
     "tool_calls": []},
    {"item_idx": 1, "role": "assistant", "live_transcript": "Hi, this is Alex.", "started_at": "2026-11-10T10:00:02Z",
     "duration_ms": 1500, "tool_calls": []},
    {"item_idx": 2, "role": "assistant", "live_transcript": "Thanks, bye!", "started_at": "2026-11-10T10:00:10Z",
     "duration_ms": 900, "tool_calls": [
         {"id": "tc1", "tool": {"id": "t", "name": "record_outcome"},
          "request_body": {"outcome": "not_interested", "note": "said no", "callback_time": ""}, "response_body": "Saved."},
         {"id": "tc2", "tool": {"id": "t", "name": "natural_conversation_ending"}, "request_body": None, "response_body": None}]},
]
live = phonic_provider.normalize({"id": "conv_a", "started_at": "2026-11-10T10:00:00Z", "items": ITEMS})
assert live["status"] == "started" and len(live["transcript"]) == 2  # the newest turn is held back while live
assert live["transcript"][1]["start_timestamp"] == 2.0 and live["transcript"][1]["end_timestamp"] == 3.5
done = phonic_provider.normalize({"id": "conv_a", "started_at": "2026-11-10T10:00:00Z",
                                  "ended_at": "2026-11-10T10:00:12Z", "ended_by": "assistant", "items": ITEMS})
assert done["status"] == "completed" and done["end_reason"] == "agent_hangup" and len(done["transcript"]) == 3
assert [t["name"] for t in done["transcript"][2]["tool_calls"]] == ["record_outcome", "end_call"]
assert phonic_provider.normalize({"ended_at": "2026-11-10T10:00:12Z", "ended_by": "error"})["status"] == "failed"
assert phonic_provider.normalize({"id": "conv_b"})["status"] is None
print("normalize ok")

# ---- 5. the setting ---------------------------------------------------------------------------------------------
assert providers.current_name() == "cartesia"  # no file = Cartesia
try:
    providers.save("phonic", {"audio_speed": 5})
    raise AssertionError("bad speed saved")
except providers.ProviderSettingsError:
    pass
assert providers.current_name() == "cartesia"
assert providers.is_live_ref("ac_1") and providers.is_live_ref("conv_1") and not providers.is_live_ref("pending-1")


def wait_final(c, cid, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/calls/{cid}").json()
        if d["call"]["status"] in ("ended", "failed"):
            return d
        time.sleep(0.05)
    raise AssertionError(f"call {cid} never finished: {d['call']}")


with TestClient(main_app) as c:
    # Cartesia stays the default and still works
    r = c.post("/api/calls", json={"number": TEST_PHONE})
    assert r.status_code == 200 and r.json()["provider"] == "cartesia" and r.json()["room"] == "ac_cart1", r.json()
    assert wait_final(c, r.json()["id"])["call"]["result"] == "no_answer"
    print("Cartesia path unchanged (default)")

    vp = c.get("/api/voice-provider").json()
    assert vp["provider"] == "cartesia" and vp["features"]["phonic"]["voicemail_detection"] is False
    assert vp["phonic_test_only"] is True and vp["phonic_test_number"] == TEST_PHONE
    assert c.put("/api/voice-provider", json={"provider": "phonic", "phonic": {"background_noise": "beach"}}).status_code == 422
    vp = c.put("/api/voice-provider", json={"provider": "phonic", "phonic": {"voice_id": "grant", "audio_speed": 1.05,
                                                                              "background_noise": "office",
                                                                              "background_noise_level": 0.1}}).json()
    assert vp["provider"] == "phonic" and vp["phonic"]["audio_speed"] == 1.05
    assert c.get("/api/phonic/voices").json()["voices"][0]["id"] == "grant"

    # A whole Phonic call (kill switch lifted in-process for the fake only)
    os.environ["CARTESIA_CALLS_DISABLED"] = "0"
    fake.script = [
        (0.0, {"started_at": "2026-11-10T10:00:00Z", "items": ITEMS[:2]}),
        (0.3, {"started_at": "2026-11-10T10:00:00Z", "items": ITEMS}),
        (0.5, {"started_at": "2026-11-10T10:00:00Z", "ended_at": "2026-11-10T10:00:12Z", "ended_by": "assistant",
               "items": ITEMS}),
    ]
    r = c.post("/api/calls", json={"number": TEST_PHONE})
    assert r.status_code == 200, r.json()
    call = r.json()
    assert call["provider"] == "phonic" and call["room"] == "conv_test1"
    sent = [x for x in fake.requests if x[1] == "/conversations/sip/outbound_call"][-1][2]
    assert sent["config"]["voice_id"] == "grant" and sent["config"]["background_noise"] == "office"
    assert "record_outcome" in sent["config"]["tools"] and sent["dry_run"] is False
    assert fake.tools["record_outcome"]["endpoint_url"] == "https://dialer.example.test/api/cartesia/tools/record_outcome"
    assert fake.tools["record_outcome"]["endpoint_headers"] == {"Authorization": "Bearer test-tool-secret"}
    # the tool request as Phonic sends it: arguments at the root, the conversation in call_info
    tool = c.post("/api/cartesia/tools/record_outcome", headers={"Authorization": "Bearer test-tool-secret"},
                  json={"outcome": "not_interested", "note": "said no", "callback_time": "",
                        "call_info": {"conversation_id": "conv_test1", "from_phone_number": "+13025550199",
                                      "to_phone_number": TEST_PHONE}})
    assert tool.status_code == 200 and tool.text == "Saved.", tool.text
    d = wait_final(c, call["id"])
    fin = d["call"]
    print("phonic call:", fin["status"], fin["result"], fin["outcome"], repr(fin["reason"]), fin["duration_s"])
    assert fin["provider"] == "phonic" and fin["result"] == "answered" and fin["outcome"] == "not_interested"
    assert fin["duration_s"] == 12.0
    texts = [e["text"] for e in d["events"] if e["kind"] == "transcript"]
    assert texts == ["Hello?", "Hi, this is Alex.", "Thanks, bye!"], texts  # each line once
    assert any(e["text"] == "call_requested" and e["data"].get("provider") == "phonic" for e in d["events"])

    # Carrier refusal (busy) comes back from the request itself
    fake.refuse_sip = 486
    r = c.post("/api/calls", json={"number": TEST_PHONE})
    assert r.status_code == 200 and r.json()["result"] == "busy" and r.json()["status"] == "ended", r.json()
    fake.refuse_sip = None
    assert c.get("/api/calls/active").json()["call"] is None

    # Test mode OFF on this copy: a lead's real number is still blocked by PHONIC_TEST_ONLY, and logged
    test_mode.set_on(False)
    r = c.post("/api/calls", json={"number": "+15550000000"})
    assert r.status_code >= 400 and "PHONIC_TEST_ONLY" in r.json()["detail"], r.json()
    test_mode.set_on(True)
    os.environ["CARTESIA_CALLS_DISABLED"] = "1"

    # Back to Cartesia
    assert c.put("/api/voice-provider", json={"provider": "cartesia"}).json()["provider"] == "cartesia"

# ---- ensure_tools: all four tools in the account; in test-only mode no transfer to a number the agent picks ----
os.environ.update(CARTESIA_SUPERVISOR_TOOL_ID="agent_tool_sup", CARTESIA_OUTCOME_TOOL_ID="agent_tool_out",
                  CARTESIA_TOOL_SECRET="test-tool-secret")
fake.tools.clear()
real_phone = phonic_provider._single_supervisor_phone
phonic_provider._single_supervisor_phone = lambda: None  # no fixed number (several supervisors, or none)
done, note = run(phonic_provider.ensure_tools())
assert set(fake.tools) == {"record_outcome", "collect_email", "request_supervisor", "transfer_to_specialist"}, fake.tools
assert all(t.get("endpoint_url", "").startswith("https://dialer.example.test/api/cartesia/tools/")
           for t in fake.tools.values() if t["type"] == "custom_webhook")
assert "transfer_to_specialist" not in done and "transfer not offered" in note, (done, note)
cfg_tools = phonic_provider.build_call_config(load_settings(), {}, 2, done, providers.PHONIC_DEFAULTS)["tools"]
assert "transfer_to_specialist" not in cfg_tools and "request_supervisor" not in cfg_tools, cfg_tools
phonic_provider._single_supervisor_phone = lambda: "+15551230000"  # one supervisor: a fixed number is allowed
done, _ = run(phonic_provider.ensure_tools())
assert "transfer_to_specialist" in done and fake.tools["transfer_to_specialist"]["phone_number"] == "+15551230000"
phonic_provider._single_supervisor_phone = real_phone
os.environ.update(CARTESIA_SUPERVISOR_TOOL_ID="", CARTESIA_OUTCOME_TOOL_ID="")
print("tools: four created; test only + no fixed number = no transfer offered")

print("ALL PHONIC TESTS PASS")
