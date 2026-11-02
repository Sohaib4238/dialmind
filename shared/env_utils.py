"""Shared .env and log helpers for the dialer scripts and the dashboard backend."""

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv, set_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# DIALER_ENV_FILE / DIALER_LOG_DIR move .env and logs/ (tests use a temp fake .env, never the real one)
ENV_PATH = Path(os.getenv("DIALER_ENV_FILE") or PROJECT_ROOT / ".env")
load_dotenv(ENV_PATH)

E164 = re.compile(r"^\+[1-9]\d{1,14}$")


def require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"ERROR: {name} is missing in .env")
    return value


def require_e164(name: str, value: str) -> str:
    value = value.strip()
    if not E164.match(value):
        sys.exit(f"ERROR: {name} must be E.164 like +15551234567, got {value!r}")
    return value


def file_log(filename: str) -> None:
    """Send INFO+ log records (ours and the LiveKit SDK's, e.g. API retries) to logs/<filename>."""
    log_dir = Path(os.getenv("DIALER_LOG_DIR") or PROJECT_ROOT / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / filename, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)


def save_env(key: str, value: str) -> None:
    """Write key=value into .env (keeps the other lines) and the current process env."""
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    if text and not text.endswith("\n"):
        ENV_PATH.write_text(text + "\n", encoding="utf-8")
    set_key(str(ENV_PATH), key, value, quote_mode="never")
    os.environ[key] = value
