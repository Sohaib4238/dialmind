"""Voice providers: who runs the agent on a call (Cartesia, the default, or Phonic).

The choice and Phonic's own options live in <data>/voice_provider.json (VOICE_PROVIDER_PATH moves it), set on the
Instructions page. A missing or broken file = Cartesia. Each call stores the provider it used (Call.provider), and
everything after the start (poll, hang-up, recording) goes to that provider, even if the setting changed since.
The agent's wording and rules come from config/agent_settings.yaml for both (agent_config.build_instructions).
"""

import json
import logging
import os
import re
from pathlib import Path

from app.db import DATA_DIR

logger = logging.getLogger("dashboard.providers")

DEFAULT = "cartesia"
NAMES = ("cartesia", "phonic")
PHONIC_NOISES = ("office", "call-center", "coffee-shop")  # Phonic's background_noise values ("" = none)
PHONIC_DEFAULTS = {"voice_id": "grant", "audio_speed": 1.0, "background_noise": "", "background_noise_level": 0.0}
SPEED_RANGE = (0.5, 2.0)  # Phonic documents no range (default 1); kept to a sane span
NOISE_LEVEL_RANGE = (0.0, 1.0)
_VOICE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class ProviderSettingsError(ValueError):
    pass


def settings_path() -> Path:
    return Path(os.getenv("VOICE_PROVIDER_PATH") or DATA_DIR / "voice_provider.json")


def load() -> dict:
    """{"provider": "cartesia" | "phonic", "phonic": {...}} with defaults filled in."""
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
        raw = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        raw = {}
    provider = raw.get("provider") if raw.get("provider") in NAMES else DEFAULT
    phonic = {**PHONIC_DEFAULTS, **{k: v for k, v in (raw.get("phonic") or {}).items() if k in PHONIC_DEFAULTS}}
    try:
        phonic = validate_phonic(phonic)
    except ProviderSettingsError:
        phonic = dict(PHONIC_DEFAULTS)
    return {"provider": provider, "phonic": phonic}


def validate_phonic(p: dict) -> dict:
    voice = str(p.get("voice_id") or "").strip()
    if not _VOICE_RE.match(voice):
        raise ProviderSettingsError("Pick a Phonic voice.")
    try:
        speed = float(p.get("audio_speed"))
        level = float(p.get("background_noise_level") or 0)
    except (TypeError, ValueError):
        raise ProviderSettingsError("Speed and background level must be numbers.") from None
    if not SPEED_RANGE[0] <= speed <= SPEED_RANGE[1]:
        raise ProviderSettingsError(f"Speed must be between {SPEED_RANGE[0]} and {SPEED_RANGE[1]}.")
    if not NOISE_LEVEL_RANGE[0] <= level <= NOISE_LEVEL_RANGE[1]:
        raise ProviderSettingsError(f"Background level must be between {NOISE_LEVEL_RANGE[0]} and {NOISE_LEVEL_RANGE[1]}.")
    noise = str(p.get("background_noise") or "").strip()
    if noise and noise not in PHONIC_NOISES:
        raise ProviderSettingsError(f"Background sound must be none or one of {', '.join(PHONIC_NOISES)}.")
    return {"voice_id": voice, "audio_speed": round(speed, 2), "background_noise": noise,
            "background_noise_level": round(level, 2) if noise else 0.0}


def save(provider: str, phonic: dict | None = None) -> dict:
    if provider not in NAMES:
        raise ProviderSettingsError(f"Voice provider must be one of {', '.join(NAMES)}.")
    current = load()
    data = {"provider": provider, "phonic": validate_phonic({**current["phonic"], **(phonic or {})})}
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    logger.info("voice provider: %s", provider)
    return data


def current_name() -> str:
    return load()["provider"]


def get(name: str | None = None):
    """The provider object by name (None = the one chosen in the settings)."""
    name = name or current_name()
    if name == "phonic":
        from app.providers.phonic_provider import PhonicProvider

        return PhonicProvider()
    from app.providers.cartesia_provider import CartesiaProvider

    return CartesiaProvider()


def for_call(call) -> object:
    """The provider a stored call was made with (older rows: Cartesia)."""
    return get(getattr(call, "provider", "") or DEFAULT)


def is_live_ref(call_ref: str) -> bool:
    """A real provider call id (not "pending-..." before the provider answered, nor a simulated call)."""
    return any(get(n).owns(call_ref) for n in NAMES)


def features() -> dict:
    return {n: {"label": get(n).label, **get(n).features} for n in NAMES}
