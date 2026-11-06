"""Call start (the AI greets if the callee stays silent) + background sound. Fake Cartesia, throwaway DB, no call."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import json
import os
import wave
from pathlib import Path

DB = f"{TMP}/call_start_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

import env_utils  # noqa: E402,F401  (loads .env like the backend)
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import agent_config, ambience, cartesia, cartesia_setup, settings_store, tunnel  # noqa: E402
from app.db import CallEvent, get_session, init_db  # noqa: E402
from app.main import app  # noqa: E402
from settings import SETTINGS_PATH, AgentSettings, load_settings  # noqa: E402
from sqlmodel import select  # noqa: E402



async def no_watch() -> None:
    return None

tunnel.watch = no_watch
init_db()
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test")
s = load_settings()
vals = s.model_dump()


def settings(**kw) -> AgentSettings:
    return AgentSettings.model_validate({**vals, **kw})


# 1. Default: a 4 s pause, then the per-call greeting. Off: the callee speaks first, as before.
cfg = agent_config.build_config(s)
assert s.call_start.wait_seconds == 4 and s.call_start.greet_if_silent
assert cfg["initial_message"] == '<break time="4s"/>{{silent_greeting}}', cfg["initial_message"]
assert '"{{silent_greeting}}"' in cfg["instructions"] and "never twice" in cfg["instructions"]
assert 'Your introduction is exactly this line: "{{intro_line}}"' in cfg["instructions"]  # per call (agent_config.introduction)
assert f'"{s.fill(s.silence.check_message)}" are never your introduction' in cfg["instructions"]  # the cut-off "Hello," of #38
assert "voicemail tool" in cfg["instructions"]  # the voicemail rules stay
off = settings(call_start={**vals["call_start"], "greet_if_silent": False})
assert agent_config.build_config(off)["initial_message"] is None
assert "{{silent_greeting}}" not in agent_config.build_config(off)["instructions"]
six = settings(call_start={**vals["call_start"], "wait_seconds": 6})
assert agent_config.build_config(six)["initial_message"].startswith('<break time="6s"/>')
assert agent_config.config_hash(cfg) != agent_config.config_hash(agent_config.build_config(six))  # change -> re-sync
for bad in (1, 9):
    try:
        settings(call_start={**vals["call_start"], "wait_seconds": bad})
        raise AssertionError(f"accepted a wait of {bad}")
    except ValueError:
        pass
print("config: pause + greeting in the welcome message, off = callee first, wait limited to 2-8 s")

# 2. A silent callee only hears a short check ("Hello?"), never the introduction (History #38).
empty = agent_config.lead_variables(None, s)
assert s.call_start.silent_greeting == "Hello?"
assert agent_config.silent_greeting(s, {**empty, "lead_name": "Maria"}) == "Hello?"
assert agent_config.silent_greeting(s, empty) == "Hello?"
named = settings(call_start={**vals["call_start"], "silent_greeting": "Hello? Is this {lead_name}?"})
assert agent_config.silent_greeting(named, {**empty, "lead_name": "Maria"}) == "Hello? Is this Maria?"
assert agent_config.silent_greeting(named, empty) == "Hello?"  # no name: that sentence is left out
assert "<" not in agent_config.silent_greeting(named, {**empty, "lead_name": '<break time="9s"/>Bob'})
only_name = settings(call_start={**vals["call_start"], "silent_greeting": "Is this {lead_name}?"})
assert agent_config.silent_greeting(only_name, empty) == "Hello?"  # never falls back to the introduction
for bad in ("Hi, this is {caller_name} from {company_name}.", "Hello? We are {company_name}.",
            "Hello, I hope you are having a really lovely day today?", "Two\nlines"):
    try:
        settings(call_start={**vals["call_start"], "silent_greeting": bad})
        raise AssertionError(f"accepted {bad!r}")
    except ValueError:
        pass
# The server's settings file still has the old default (a whole introduction): it loads as "Hello?".
old_default = settings(call_start={**vals["call_start"], "silent_greeting": "Hi, is this {lead_name}? This is {caller_name} from {company_name}."})
assert old_default.call_start.silent_greeting == "Hello?"
print("silent callee: only \"Hello?\" (also from the old default), an introduction there is refused")

# 3. An old settings file (the server's) has neither section: still valid, sound off, greeting on.
old = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
del old["call_start"], old["voice"]["ambience"]
s_old = AgentSettings.model_validate(old)
assert s_old.call_start.wait_seconds == 4 and not s_old.voice.ambience.enabled and s_old.voice.ambience.volume_percent == 30
print("old settings file: loads with the defaults")


# 4. A call sends the greeting; the pause tag never reaches the transcript; the greeting isn't a check-in.
tr = calls._Tracker()
tr.silence = {"enabled": True, "check_s": 5, "max_s": 25, "greet_wait_s": 4}
tr.last_turn_end_s = 0.0


async def ingest() -> None:
    calls.manager._ingest_turn(1, tr, {"role": "assistant", "text": '<break time="4s"/>Hello?',
                                       "start_timestamp": 4.0, "end_timestamp": 4.6}, "poll")
    assert tr.unanswered_checkins == 0, "the greeting counted as a silence check-in"
    calls.manager._ingest_turn(1, tr, {"role": "assistant", "text": "Hello, can you hear me okay?",
                                       "start_timestamp": 12.0, "end_timestamp": 14.0}, "poll")
    assert tr.unanswered_checkins == 1, "a real check-in after the greeting must still count"

asyncio.run(ingest())
with get_session() as db:
    texts = [e.text for e in db.exec(select(CallEvent).where(CallEvent.call_id == 1, CallEvent.kind == "transcript"))]
assert texts == ["Hello?", "Hello, can you hear me okay?"], texts
print("transcript: no speech tag; the greeting isn't a check-in, a later silence is")

# 4b. Without the greeting a first line after dead air is still a check-in (the old behaviour).
tr2 = calls._Tracker()
tr2.silence = {"enabled": True, "check_s": 8, "max_s": 25, "greet_wait_s": 0}
tr2.last_turn_end_s = 0.0


async def ingest2() -> None:
    calls.manager._ingest_turn(2, tr2, {"role": "assistant", "text": "Hello?", "start_timestamp": 8.0, "end_timestamp": 9.0}, "poll")

asyncio.run(ingest2())
assert tr2.unanswered_checkins == 1
print("greeting off: first line after dead air is still a check-in")

# 5. No sound files ship with the repo, so make two silent ones in a temp folder for the sync checks below.
ambience.BUILTIN_DIR = Path(TMP) / "sounds_builtin"
ambience.BUILTIN_DIR.mkdir()
for name in ("office", "busy_office"):
    with wave.open(str(ambience.BUILTIN_DIR / f"{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(name.encode() + b"\0" * 1_100_000)  # different bytes, so a different hash per sound
assert set(ambience.builtin()) == {"office", "busy_office"}
print("sound files: temp fixtures ready")

# 6. Sync: off = nothing uploaded, and the config says null. On = one upload, then reused.
uploads, patches = [], []
fail_upload, refuse_sound = [False], [False]


async def fake_upload(filename, content, purpose):
    if fail_upload[0]:
        raise cartesia.CartesiaError("HTTP 413", 413)
    uploads.append((filename, purpose, len(content)))
    return {"id": f"file_{len(uploads)}"}


async def fake_update(agent_id, config, description):
    if refuse_sound[0] and config["audio"]["output"]["background_sound"]:
        raise cartesia.CartesiaError("HTTP 402: not on this plan", 402)
    patches.append(config)
    return {}

cartesia.upload_file, cartesia.update_agent_config = fake_upload, fake_update


def save_ambience(**kw) -> None:
    voice = {**load_settings().voice.model_dump()}
    voice["ambience"] = {**voice["ambience"], **kw}
    settings_store.save_voice(voice, None)


def sync() -> dict:
    return asyncio.run(cartesia_setup.sync_agent("test"))


r = sync()
assert not uploads and patches[-1]["audio"]["output"]["background_sound"] is None and not r["ambience_error"]

save_ambience(enabled=True)
r = sync()
assert uploads == [("office.wav", "agent_background_sound", uploads[0][2])] and uploads[0][2] > 1_000_000
assert patches[-1]["audio"]["output"]["background_sound"] == {"file_id": "file_1", "volume": 0.3}
assert r["action"] == "updated" and cartesia_setup.sync_status()["in_sync"]
assert sync()["action"] == "unchanged" and len(uploads) == 1

save_ambience(volume_percent=50, sound="busy_office")
sync()
assert len(uploads) == 2 and patches[-1]["audio"]["output"]["background_sound"] == {"file_id": "file_2", "volume": 0.5}
save_ambience(sound="office")
sync()
assert len(uploads) == 2 and patches[-1]["audio"]["output"]["background_sound"]["file_id"] == "file_1"  # kept

save_ambience(enabled=False)
sync()
assert patches[-1]["audio"]["output"]["background_sound"] is None
print("sync: off sends null; on uploads once per sound; volume 30% = 0.3")

# 7. The sound never stops a call: a refused upload or config still syncs the agent, and says so.
state = json.loads(cartesia_setup.STATE_PATH.read_text(encoding="utf-8"))
state.pop("ambience_files")
cartesia_setup.STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
fail_upload[0] = True
save_ambience(enabled=True)
r = sync()
assert "upload" in r["ambience_error"] and patches[-1]["audio"]["output"]["background_sound"] is None
st = cartesia_setup.sync_status()
assert not st["in_sync"] and "Background sound" in st["detail"], st
fail_upload[0] = False
refuse_sound[0] = True
r = sync()
assert "refused" in r["ambience_error"] and patches[-1]["audio"]["output"]["background_sound"] is None
assert not cartesia_setup.sync_status()["in_sync"]
refuse_sound[0] = False
r = sync()  # tries again, and works once Cartesia accepts it
assert not r["ambience_error"] and patches[-1]["audio"]["output"]["background_sound"]["file_id"]
assert cartesia_setup.sync_status()["in_sync"]
save_ambience(enabled=False)
print("failures: the agent still syncs without the sound, the page says why, the next sync retries")

# 8. Dashboard: Instructions form has the section; Voice page gets and saves the sound; preview file served.
with TestClient(app) as client:
    data = client.get("/api/settings").json()
    section = next(f for f in data["fields"] if f["key"] == "call_start")
    keys = {f["key"]: f for f in section["fields"]}
    assert keys["wait_seconds"]["kind"] == "number" and keys["wait_seconds"]["min"] == 2 and keys["wait_seconds"]["max"] == 8
    assert keys["greet_if_silent"]["kind"] == "switch" and keys["silent_greeting"]["help"]
    v = client.get("/api/voice").json()
    assert v["voice"]["ambience"] == {"enabled": False, "sound": "office", "volume_percent": 50}, v["voice"]  # as saved above
    bad = {**v["voice"], "ambience": {**v["voice"]["ambience"], "sound": "beach"}}
    assert client.put("/api/voice", json={"voice": bad, "version": v["version"]}).status_code == 422
    bad = {**v["voice"], "ambience": {**v["voice"]["ambience"], "volume_percent": 150}}
    assert client.put("/api/voice", json={"voice": bad, "version": v["version"]}).status_code == 422
    good = {**v["voice"], "ambience": {"enabled": True, "sound": "busy_office", "volume_percent": 20}}
    res = client.put("/api/voice", json={"voice": good, "version": v["version"]})
    assert res.status_code == 200 and res.json()["cartesia"]["ok"], res.text
    assert load_settings().voice.ambience.sound == "busy_office"
    r = client.get("/api/voice/ambience/office")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    assert client.get("/api/voice/ambience/nope").status_code == 404
    assert client.get("/api/voice/ambience/..%2F..%2Fmain").status_code == 404
print("dashboard: form section, voice save + validation, preview")

# The introduction (2026-11-05): source + need + business, missing parts left out, company only when asked
from settings import OLD_OPENING_LINE, DEFAULT_OPENING_LINE  # noqa: E402

assert settings(opening_line=OLD_OPENING_LINE).opening_line == DEFAULT_OPENING_LINE  # a server's old default moves over
for bad, where in (("Hi[ there", "never closed"), ("Hi [a [b]]", "inside another"), ("Hi]", "not closed"),
                   ("Hi, {source_line}\nIs now ok?", "one line")):
    try:
        settings(opening_line=bad)
        raise AssertionError(f"accepted {bad!r}")
    except ValueError as e:
        assert where in str(e), str(e)
try:
    settings(main_script="Talk about {source_line}.")
    raise AssertionError("accepted {source_line} outside the opening line")
except ValueError as e:
    assert "only be used in the opening line" in str(e), str(e)
intro = agent_config.introduction
caller = s.caller_name
assert intro(s, {"lead_source": "LinkedIn Sales Navigator", "service_required": "SEO"}) ==     f"Hi, this is {caller}. I came across your business on LinkedIn Sales Navigator. Is now an okay time?"
assert intro(s, {"lead_source": "Bark", "business_description": "roofing company"}) ==     f"Hi, this is {caller}. I saw your request on Bark for your roofing company. Is now an okay time?"
assert intro(s, {}) == f"Hi, this is {caller}. Is now an okay time?"
assert s.company_name not in intro(s, {"lead_source": "Bark", "service_required": "a logo", "business_name": "Bo's"})
own = settings(opening_line="Hey[ {lead_name}], it's {caller_name}. {source_line} Got a minute?")
assert intro(own, {"lead_name": "Maria", "lead_source": "Bark"}) == f"Hey Maria, it's {caller}. I saw your request on Bark. Got a minute?"
assert intro(own, {}) == f"Hey, it's {caller}. Got a minute?"
print("introduction: old default moved over; [optional] parts checked and left out when empty; request vs found source;"
      " company name never in it; {source_line} only in the opening line")

print("OK")
