"""Cartesia voice catalog for the Voice page.

- List: GET /voices?language=en (all English voices: public library + ours), paged, cached in
  data/voice_cache.json for CACHE_TTL_S. "Refresh" bypasses the cache.
- Preview: Cartesia's preview files need our API key, so the browser gets them through
  /api/voice/preview/{id}; the WAV is cached in data/voice_previews/. Voices without a
  preview file can get a generated sample of the opening line (TTS credits; the UI warns first).
"""

import asyncio
import hashlib
import json
import os
import re
import time
from urllib.parse import urlparse

import aiohttp

from app import cartesia
from app.db import DATA_DIR

CACHE_PATH = DATA_DIR / "voice_cache.json"
PREVIEW_DIR = DATA_DIR / "voice_previews"
CACHE_TTL_S = 24 * 3600
TTS_MODEL = os.getenv("CARTESIA_TTS_MODEL", "sonic-3.6")
VOICE_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
_lock = asyncio.Lock()


class VoiceApiError(Exception):
    pass


def _read_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, CACHE_PATH)


def normalize(v: dict) -> dict:
    accents = v.get("accents") or []
    accent_names = []
    for a in accents:
        name = (a.get("accent") or "").replace("-", " ").strip()
        if name and name.title() not in accent_names:
            accent_names.append(name.title())
    locales = [a.get("locale") for a in accents if a.get("locale")]
    return {
        "id": v.get("id"),
        "name": v.get("name") or v.get("id"),
        "gender": {"masculine": "Male", "feminine": "Female", "gender_neutral": "Neutral"}.get(v.get("gender") or "", ""),
        "accent": ", ".join(accent_names),
        "locale": ", ".join(locales) or (v.get("language") or ""),
        "tagline": (v.get("tagline") or "").strip(),
        "description": (v.get("description") or "").strip(),
        "is_owner": bool(v.get("is_owner")),
        "has_preview": bool(v.get("preview_file_url")),
    }


async def _fetch_all() -> list[dict]:
    voices, after = [], None
    for _ in range(40):  # 40 x 100 voices is far more than the English library
        params = {"limit": "100", "language": "en", "expand[]": "preview_file_url"}
        if after:
            params["starting_after"] = after
        page = await cartesia.request("GET", "/voices", params=params)
        voices += [normalize(v) for v in page.get("data", []) if v.get("status", "active") == "active"]
        after = page.get("next_page")
        if not page.get("has_more") or not after:
            break
    seen, unique = set(), []
    for v in voices:
        if v["id"] not in seen:
            seen.add(v["id"])
            unique.append(v)
    return sorted(unique, key=lambda v: (not v["is_owner"], v["name"].lower()))


async def list_voices(refresh: bool = False) -> dict:
    async with _lock:
        entry = _read_cache().get("cartesia")
        if entry and not refresh and time.time() - entry.get("fetched_at", 0) < CACHE_TTL_S:
            return {"voices": entry["voices"], "fetched_at": entry["fetched_at"], "cached": True}
        try:
            voices = await _fetch_all()
        except cartesia.CartesiaError as e:
            if entry:  # serve the old list rather than nothing
                return {"voices": entry["voices"], "fetched_at": entry["fetched_at"], "cached": True, "stale": str(e)}
            raise VoiceApiError(str(e)) from None
        _write_cache({"cartesia": {"fetched_at": time.time(), "voices": voices}})
        return {"voices": voices, "fetched_at": time.time(), "cached": False}


async def voice_info(voice_id: str) -> dict | None:
    for v in (_read_cache().get("cartesia") or {}).get("voices", []):
        if v["id"] == voice_id:
            return v
    try:
        return normalize(await cartesia.request("GET", f"/voices/{voice_id}"))
    except cartesia.CartesiaError:
        return None


async def _preview_url(voice_id: str) -> str | None:
    # GET /voices/{id} ignores expand[], so look the voice up in the list (search by ID).
    page = await cartesia.request("GET", "/voices", params={"q": voice_id, "limit": "5", "expand[]": "preview_file_url"})
    return next((v.get("preview_file_url") for v in page.get("data", []) if v.get("id") == voice_id), None)


async def preview(voice_id: str) -> tuple[bytes, str]:
    """The voice's own preview file (free). Raises VoiceApiError if it has none."""
    if not VOICE_ID.match(voice_id):
        raise VoiceApiError("Not a Cartesia voice ID")
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    cached = PREVIEW_DIR / f"{voice_id}.wav"
    if cached.exists():
        return cached.read_bytes(), "audio/wav"
    try:
        url = await _preview_url(voice_id)
    except cartesia.CartesiaError as e:
        raise VoiceApiError(str(e)) from None
    if not url:
        raise VoiceApiError("This voice has no preview file")
    if not (urlparse(url).hostname or "").endswith("cartesia.ai"):
        raise VoiceApiError("Unexpected preview host")  # never send our key anywhere else
    headers = {"X-API-Key": cartesia.api_key(), "Authorization": f"Bearer {cartesia.api_key()}",
               "Cartesia-Version": cartesia.API_VERSION}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
            async with http.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise VoiceApiError(f"Cartesia preview download failed: HTTP {resp.status}")
                audio, mime = await resp.read(), resp.headers.get("Content-Type", "audio/wav")
    except aiohttp.ClientError as e:
        raise VoiceApiError(f"Couldn't download the preview ({type(e).__name__})") from None
    if mime.startswith("audio/wav") or mime == "audio/x-wav":
        cached.write_bytes(audio)
    return audio, mime


async def generate_sample(voice_id: str, text: str, speed: float = 1.0, volume: float = 1.0) -> tuple[bytes, str]:
    """Speak `text` with the voice (uses Cartesia TTS credits). Cached per voice + text."""
    if not VOICE_ID.match(voice_id):
        raise VoiceApiError("Not a Cartesia voice ID")
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{text}|{speed}|{volume}".encode()).hexdigest()[:10]
    cached = PREVIEW_DIR / f"{voice_id}.{key}.mp3"
    if cached.exists():
        return cached.read_bytes(), "audio/mpeg"
    body = {
        "model_id": TTS_MODEL,
        "transcript": text,
        "voice": voice_id,
        "language": "en",
        "output_format": {"container": "mp3", "sample_rate": 24000, "bit_rate": 64000},
        "generation_config": {"speed": speed, "volume": volume},
    }
    try:
        audio, mime = await cartesia.request("POST", "/tts/bytes", json=body, raw=True,
                                             timeout=aiohttp.ClientTimeout(total=30))
    except cartesia.CartesiaError as e:
        raise VoiceApiError(f"Couldn't generate a sample: {e}") from None
    cached.write_bytes(audio)
    return audio, mime or "audio/mpeg"
