"""Background sounds for the Voice page: the catalog, and the ffmpeg processing every sound goes through.

No sound files ship with this repo (see app/sounds/README.md). WAVs you add to app/sounds/ with process_sounds.py are
picked up automatically. Uploaded ones are in <data>/sounds/ with a small list next to them, so they survive a deploy. Cartesia gets the processed file.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from app.db import DATA_DIR

BUILTIN_DIR = Path(__file__).resolve().parent / "sounds"
DEFAULT_SOUND = "office"  # the settings default; there may be no such file yet (the feature is off by default)
UPLOAD_DIR = DATA_DIR / "sounds"
UPLOAD_LIST = UPLOAD_DIR / "sounds.json"

RATE = 16000  # Cartesia's docs name no format; its own example is a .wav. Wide enough for the 4 kHz band below.
TARGET_LUFS = -26.0  # every sound the same loudness; the volume on the Voice page scales down from here
LIMIT = 0.16  # about -16 dBFS: 10 dB over the loudness, so no sudden spike sticks out
MAX_SECONDS = 180  # long enough to not sound repetitive; longer only makes the upload bigger
MIN_CALL_SECONDS = 120  # a short loop is repeated up to this, in case Cartesia plays a file only once
MIN_SECONDS = 5  # too short to loop: it would be heard as a pattern
CROSSFADE_S = 2.0
SKIP_START_S = 1.0  # recordings often fade in
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # Caddy refuses bigger bodies too (deploy/Caddyfile)
FFMPEG_TIMEOUT_S = 300
_ALLOWED_FORMATS = {"mp3", "wav"}
_processing = threading.Lock()  # one at a time: the server is small


class SoundError(Exception):
    """Shown to the user as it is."""


def builtin() -> dict[str, str]:
    """id -> label for the WAVs in app/sounds/ (office.wav -> "Office", busy_office.wav -> "Busy office")."""
    return {p.stem: p.stem.replace("_", " ").capitalize() for p in sorted(BUILTIN_DIR.glob("*.wav"))
            if re.fullmatch(r"[a-z0-9_]{1,40}", p.stem)}


def _uploads() -> dict[str, str]:
    try:
        return json.loads(UPLOAD_LIST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def catalog() -> list[dict]:
    """Every sound the Voice page can choose: the files in app/sounds/, then uploads."""
    return [{"id": k, "label": v, "builtin": True} for k, v in builtin().items()] + \
           [{"id": k, "label": v, "builtin": False} for k, v in _uploads().items()]


def exists(sound: str) -> bool:
    return sound in builtin() or sound in _uploads()


def path_for(sound: str) -> Path:
    return (BUILTIN_DIR if sound in builtin() else UPLOAD_DIR) / f"{sound}.wav"


@lru_cache
def _hash_of(path: Path, mtime_ns: int) -> str:
    return sha256(path.read_bytes()).hexdigest()


def file_hash(sound: str) -> str | None:
    """None if the file isn't there."""
    p = path_for(sound)
    try:
        return _hash_of(p, p.stat().st_mtime_ns)
    except OSError:
        return None


def _ffmpeg(*args: str) -> str:
    """Runs ffmpeg on local files only; its log is returned (it writes measurements there)."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SoundError("ffmpeg isn't installed here")
    try:
        r = subprocess.run([exe, "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe", *args],
                           capture_output=True, timeout=FFMPEG_TIMEOUT_S, text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise SoundError("This file took too long to process") from None
    if r.returncode != 0:
        raise SoundError("Couldn't read this file as audio")
    return r.stderr


def _probe(src: Path) -> tuple[str, float]:
    """(format, seconds), from what the file really is, not its name."""
    exe = shutil.which("ffprobe")
    if not exe:
        raise SoundError("ffprobe isn't installed here")
    try:
        r = subprocess.run([exe, "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "a:0", "-show_entries",
                            "format=format_name,duration:stream=codec_type", "-of", "json", str(src)],
                           capture_output=True, timeout=60, text=True, encoding="utf-8", errors="replace")
        info = json.loads(r.stdout or "{}")
        return info["format"]["format_name"], float(info["format"]["duration"])
    except (subprocess.TimeoutExpired, KeyError, ValueError, TypeError):
        raise SoundError("Couldn't read this file as audio") from None


def measure_lufs(path: Path) -> float:
    """Integrated loudness. Quiet parts count too: this is room sound, not speech."""
    log = _ffmpeg("-i", str(path), "-af", "ebur128=framelog=quiet", "-f", "null", "-")
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
    if not found:
        raise SoundError("Couldn't measure this file's loudness")
    return float(found[-1])


def process(src: Path, dst: Path) -> dict:
    """Any MP3/WAV -> a loopable phone-sounding room sound: mono, 16 kHz, 200 Hz-4 kHz, even loudness, spikes limited."""
    fmt, seconds = _probe(src)
    if not _ALLOWED_FORMATS & set(fmt.split(",")):
        raise SoundError("Please use an MP3 or WAV file")
    start = SKIP_START_S if seconds > MIN_SECONDS + 2 * SKIP_START_S else 0.0
    length = min(seconds - start, MAX_SECONDS + CROSSFADE_S)
    if length < MIN_SECONDS:
        raise SoundError(f"This sound is too short: it needs at least {MIN_SECONDS} seconds")
    fade = min(CROSSFADE_S, length / 4)
    with tempfile.TemporaryDirectory(prefix="sound-") as tmp:
        t = Path(tmp)
        # 1. Mono, phone band; the compressor evens out the level, gently.
        _ffmpeg("-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src), "-vn", "-ac", "1", "-ar", str(RATE), "-af",
                "highpass=f=200,lowpass=f=4000,acompressor=threshold=0.05:ratio=2.5:attack=10:release=250", "-c:a", "pcm_s16le", str(t / "band.wav"))
        # 2. To the target loudness, spikes limited; twice, because the limiter takes a little off.
        gain = TARGET_LUFS - measure_lufs(t / "band.wav")
        limiter = f"alimiter=limit={LIMIT}:attack=5:release=80:level=disabled"
        _ffmpeg("-i", str(t / "band.wav"), "-af", f"volume={gain:.2f}dB,{limiter}", "-c:a", "pcm_s16le", str(t / "level1.wav"))
        gain += TARGET_LUFS - measure_lufs(t / "level1.wav")
        _ffmpeg("-i", str(t / "band.wav"), "-af", f"volume={gain:.2f}dB,{limiter}", "-c:a", "pcm_s16le", str(t / "level.wav"))
        # 3. Loop: the end fades out while the start fades in over it, and the file then goes on from where that fade-in
        #    stopped. The last sample leads straight into the first one.
        end = length - fade
        _ffmpeg("-i", str(t / "level.wav"), "-filter_complex",
                f"[0:a]asplit=3[a][b][c];"
                f"[a]atrim=start={fade:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[mid];"
                f"[b]atrim=start={end:.3f},asetpts=PTS-STARTPTS,afade=t=out:d={fade:.3f}:curve=qsin[tail];"
                f"[c]atrim=end={fade:.3f},asetpts=PTS-STARTPTS,afade=t=in:d={fade:.3f}:curve=qsin[head];"
                f"[tail][head]amix=inputs=2:normalize=0:duration=longest[joined];"
                f"[mid][joined]concat=n=2:v=0:a=1[out]",
                "-map", "[out]", "-c:a", "pcm_s16le", str(t / "loop.wav"))
        # 4. A short loop is repeated whole, so every join is still the seamless one.
        loop_s = length - fade
        repeats = max(1, -int(-MIN_CALL_SECONDS // loop_s))
        _ffmpeg("-stream_loop", str(repeats - 1), "-i", str(t / "loop.wav"), "-c", "copy", str(t / "final.wav"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(t / "final.wav"), dst)
    return {"seconds": round(loop_s * repeats, 1), "lufs": round(measure_lufs(dst), 1), "bytes": dst.stat().st_size}


def _label_from(filename: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", Path(filename.replace("\\", "/")).name)
    label = re.sub(r"\s+", " ", re.sub(r"[^\w .,'()-]+", " ", stem.replace("_", " "))).strip()
    return label[:60] or "My sound"


def add_upload(content: bytes, filename: str) -> dict:
    """Process an uploaded file and add it to the list. Nothing is kept if it can't be processed."""
    if not content:
        raise SoundError("That file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise SoundError("That file is too big: 20 MB at most")
    if not re.search(r"\.(mp3|wav)$", filename, re.I):
        raise SoundError("Please use an MP3 or WAV file")
    sound = f"custom_{uuid.uuid4().hex[:10]}"
    with _processing, tempfile.TemporaryDirectory(prefix="upload-") as tmp:
        src = Path(tmp) / ("input" + Path(filename).suffix.lower())
        src.write_bytes(content)
        info = process(src, UPLOAD_DIR / f"{sound}.wav")
        uploads = {**_uploads(), sound: _label_from(filename)}
        tmp_list = UPLOAD_LIST.with_suffix(".tmp")
        tmp_list.write_text(json.dumps(uploads, indent=2), encoding="utf-8")
        os.replace(tmp_list, UPLOAD_LIST)
    return {"id": sound, "label": uploads[sound], "builtin": False, **info}
