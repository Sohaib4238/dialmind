"""Background sounds: the ffmpeg processing, the sound catalog, and the Voice page upload. Fake Cartesia, nothing is sent.
The parts that need ffmpeg are skipped when it isn't installed."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import array
import math
import os
import random
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

DB = f"{TMP}/sounds_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

import env_utils  # noqa: E402,F401  (loads .env like the backend)
from fastapi.testclient import TestClient  # noqa: E402

from app import ambience, cartesia, cartesia_setup, tunnel  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from settings import AgentSettings, load_settings  # noqa: E402


async def no_watch() -> None:
    return None

tunnel.watch = no_watch
init_db()
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test")
WORK = Path(TMP) / "sound_work"
WORK.mkdir(exist_ok=True)
ambience.BUILTIN_DIR = WORK / "builtin"  # never the repo's own folder
ambience.BUILTIN_DIR.mkdir()


def samples(path: Path) -> tuple[array.array, int, int]:
    with wave.open(str(path)) as w:
        return array.array("h", w.readframes(w.getnframes())), w.getframerate(), w.getnchannels()


def seam_ok(a: array.array) -> bool:
    """The jump from the last sample to the first is no bigger than the file's own ordinary big steps."""
    steps = sorted(abs(a[i + 1] - a[i]) for i in range(0, len(a) - 1, 11))
    return abs(a[0] - a[-1]) <= steps[int(len(steps) * 0.99)]


def peak_db(a: array.array) -> float:
    return 20 * math.log10(max(max(a), -min(a), 1) / 32768)


def band_rms_db(path: Path, band: str) -> float:
    log = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path), "-af", f"{band},astats=measure_overall=RMS_level:measure_perchannel=0",
                          "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    return float(re.findall(r"RMS level dB: (-?[\d.]+|-inf)", log)[-1].replace("-inf", "-200"))


def noisy_wav(path: Path, seconds: float, level: float, spikes: bool = False, channels: int = 2, rate: int = 44100) -> Path:
    """Quiet random noise, optionally with short sharp spikes."""
    rng = random.Random(7)
    a = array.array("h", (int(rng.gauss(0, level * 32767)) for _ in range(int(seconds * rate) * channels)))
    if spikes:
        for at in range(rate * 2 * channels, len(a), rate * 3 * channels):
            for j in range(600):
                a[at + j] = int(30000 * (1 if j % 2 else -1) * (1 - j / 600))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(a.tobytes())
    return path


# 1. No sound ships with the repo: the catalog starts empty, the feature is off, and the repo's folder holds no audio.
assert ambience.catalog() == []
assert not load_settings().voice.ambience.enabled and load_settings().voice.ambience.volume_percent == 30
assert not list((REPO / "dashboard" / "backend" / "app" / "sounds").glob("*.mp3"))
noisy_wav(ambience.BUILTIN_DIR / "busy_office.wav", 1, 0.01)
noisy_wav(ambience.BUILTIN_DIR / "Not A Valid Id.wav", 1, 0.01)
assert [(x["id"], x["label"]) for x in ambience.catalog()] == [("busy_office", "Busy office")]
(ambience.BUILTIN_DIR / "busy_office.wav").unlink()
(ambience.BUILTIN_DIR / "Not A Valid Id.wav").unlink()
print("catalog: empty by default; a WAV added to the folder appears under its own name")

# 2. An old settings file naming the removed sound still loads.
vals = load_settings().model_dump()
vals["voice"]["ambience"]["sound"] = "call_center"
assert AgentSettings.model_validate(vals).voice.ambience.sound == "busy_office"
print("old setting: call_center becomes busy_office")

if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: no ffmpeg here, so the processing and upload parts weren't run")
    print("OK")
    sys.exit(0)

# 3. Processing: any level comes out the same, mono, in the phone band, spikes limited, loop clean.
quiet = ambience.process(noisy_wav(WORK / "quiet.wav", 40, 0.002, spikes=True), WORK / "quiet_out.wav")
loud = ambience.process(noisy_wav(WORK / "loud.wav", 40, 0.2), WORK / "loud_out.wav")
assert abs(quiet["lufs"] - ambience.TARGET_LUFS) < 2 and abs(loud["lufs"] - ambience.TARGET_LUFS) < 2, (quiet, loud)
assert abs(quiet["lufs"] - loud["lufs"]) < 2, (quiet, loud)
for f in ("quiet_out.wav", "loud_out.wav"):
    a, rate, channels = samples(WORK / f)
    assert rate == 16000 and channels == 1
    assert peak_db(a) < -13, (f, peak_db(a))
    assert seam_ok(a), f
    whole = band_rms_db(WORK / f, "anull")
    assert band_rms_db(WORK / f, "highpass=f=6500,highpass=f=6500") < whole - 12, f
    assert band_rms_db(WORK / f, "lowpass=f=60,lowpass=f=60") < whole - 12, f
print("processing: same loudness from quiet and loud files, phone band, spikes limited, loop join clean")

# 4. Length: a short file is repeated past 2 minutes; a long one is cut to 3.
assert loud["seconds"] >= 120 and loud["seconds"] <= 160, loud
long_in = WORK / "long.wav"
subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-f", "lavfi", "-i", "anoisesrc=d=200:c=pink:r=16000:a=0.05", str(long_in)], capture_output=True, check=True)
long_out = ambience.process(long_in, WORK / "long_out.wav")
assert 170 <= long_out["seconds"] <= 181, long_out
print("length:", loud["seconds"], "s from 40 s;", long_out["seconds"], "s from 200 s")

# 5. Upload route: an MP3 works; the name can't reach the path; bad files leave nothing behind.
mp3 = WORK / "My Nice_Office.mp3"
subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-i", str(noisy_wav(WORK / "for_mp3.wav", 12, 0.02)), str(mp3)], capture_output=True, check=True)
uploaded, cartesia_down = [], [False]


async def fake_upload(filename, content, purpose):
    if cartesia_down[0]:
        raise cartesia.CartesiaError("HTTP 500", 500)
    uploaded.append((filename, purpose, len(content)))
    return {"id": f"file_{len(uploaded)}"}


async def fake_update(agent_id, config, description):
    return {}

cartesia.upload_file, cartesia.update_agent_config = fake_upload, fake_update


def files_in_uploads() -> set[str]:
    return {p.name for p in ambience.UPLOAD_DIR.glob("*")} if ambience.UPLOAD_DIR.exists() else set()


def post(client, name: str, body: bytes):
    return client.post("/api/voice/sounds", params={"filename": name}, content=body, headers={"Content-Type": "application/octet-stream"})


with TestClient(app) as c:
    r = post(c, "My Nice_Office.mp3", mp3.read_bytes())
    assert r.status_code == 200 and r.json()["cartesia"]["ok"], r.text
    one = r.json()
    assert one["label"] == "My Nice Office" and one["id"].startswith("custom_") and not one["builtin"]
    assert uploaded == [(f"{one['id']}.wav", "agent_background_sound", one["bytes"])]
    a, rate, channels = samples(ambience.path_for(one["id"]))
    assert rate == 16000 and channels == 1 and len(a) / rate >= 120

    v = c.get("/api/voice").json()
    assert [x["id"] for x in v["sounds"]] == [one["id"]]
    assert c.get(f"/api/voice/ambience/{one['id']}").content[:4] == b"RIFF"

    # Chosen and saved: the agent gets its Cartesia file, without a second upload.
    voice = {**v["voice"], "ambience": {"enabled": True, "sound": one["id"], "volume_percent": 30}}
    res = c.put("/api/voice", json={"voice": voice, "version": v["version"]})
    assert res.status_code == 200 and res.json()["cartesia"]["ok"], res.text
    assert len(uploaded) == 1 and cartesia_setup._ambience_file_id(load_settings()) == "file_1"
    v = c.get("/api/voice").json()
    voice = {**v["voice"], "ambience": {**v["voice"]["ambience"], "sound": "no_such_sound"}}
    assert c.put("/api/voice", json={"voice": voice, "version": v["version"]}).status_code == 422
    voice = {**v["voice"], "ambience": {**v["voice"]["ambience"], "sound": "../../etc"}}
    assert c.put("/api/voice", json={"voice": voice, "version": v["version"]}).status_code == 422

    # A hostile name only becomes a label; the file has our own ID.
    r = post(c, "..\\..\\..\\evil<script>.mp3", mp3.read_bytes())
    assert r.status_code == 200 and "/" not in r.json()["label"] and "<" not in r.json()["label"] and "\\" not in r.json()["label"], r.text
    assert ambience.path_for(r.json()["id"]).parent == ambience.UPLOAD_DIR
    assert r.json()["id"] != one["id"]

    # Cartesia down: the sound is still added, and the page says so.
    cartesia_down[0] = True
    r = post(c, "later.mp3", mp3.read_bytes())
    assert r.status_code == 200 and not r.json()["cartesia"]["ok"] and "not on Cartesia yet" in r.json()["cartesia"]["detail"], r.text
    cartesia_down[0] = False

    # Refused: nothing is added.
    before, count = files_in_uploads(), len(c.get("/api/voice").json()["sounds"])
    short = noisy_wav(WORK / "short.wav", 3, 0.02)
    for name, body, why in (("not_audio.mp3", b"this is not audio at all" * 200, "read this file"),
                            ("sound.ogg", mp3.read_bytes(), "MP3 or WAV"),
                            ("empty.wav", b"", "empty"),
                            ("short.wav", short.read_bytes(), "too short")):
        r = post(c, name, body)
        assert r.status_code == 422 and why in r.json()["detail"], (name, r.text)
    big = ambience.MAX_UPLOAD_BYTES
    ambience.MAX_UPLOAD_BYTES = 1000
    assert post(c, "big.mp3", mp3.read_bytes()).status_code == 413
    ambience.MAX_UPLOAD_BYTES = big
    assert files_in_uploads() == before and len(c.get("/api/voice").json()["sounds"]) == count

    assert c.get("/api/voice/ambience/nope").status_code == 404
print("upload: processed and sent once, hostile name harmless, bad files refused and nothing left behind")
print("OK")
