"""Disk space: recordings past settings.recordings_keep_days are deleted (0 = keep forever), and the numbers for Health.

Age counts from the call, not the file: a recording opened late is cached late but is still that old. Only our local
copy goes; Cartesia keeps its own under its own rules. A deleted recording isn't fetched again (calls.recording).
"""

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.db import Call, DATA_DIR, as_utc, get_session
from settings import SettingsError, load_settings  # shared/settings.py

logger = logging.getLogger("dashboard.storage")

RECORDINGS_DIR = DATA_DIR / "recordings"
BACKUP_STATUS_PATH = DATA_DIR / "backup_status.json"  # written by deploy/backup_db.sh on the server
DEFAULT_KEEP_DAYS = 30
CHECK_EVERY_S = 6 * 3600
DISK_WARN_PCT, DISK_FULL_PCT = 80, 95
BACKUP_STALE_S = 26 * 3600


def keep_days() -> int:
    try:
        return load_settings().settings.recordings_keep_days
    except SettingsError:
        return DEFAULT_KEEP_DAYS  # a broken settings file mustn't stop the clean-up


def expired(created_at: datetime, now: datetime | None = None, days: int | None = None) -> bool:
    days = keep_days() if days is None else days
    return days > 0 and as_utc(created_at) < (now or datetime.now(timezone.utc)) - timedelta(days=days)


def purge_recordings(now: datetime | None = None) -> dict:
    """Delete recordings past the keep period. A file with no call in the database goes by its own date."""
    now = now or datetime.now(timezone.utc)
    files = list(RECORDINGS_DIR.glob("*.wav")) if RECORDINGS_DIR.is_dir() else []
    days = keep_days()
    if not files or days == 0:
        return {"deleted": 0, "freed_mb": 0.0}
    with get_session() as s:
        made = {room: created for room, created in s.exec(select(Call.room, Call.created_at)).all()}
    deleted, freed = 0, 0
    for f in files:
        try:
            when = made.get(f.stem) or datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
            if expired(when, now, days):
                size = f.stat().st_size
                f.unlink()
                deleted, freed = deleted + 1, freed + size
        except OSError as e:
            logger.warning("couldn't delete recording %s: %s", f.name, e)
    if deleted:
        logger.info("deleted %d recordings past %d days (%.1f MB)", deleted, days, freed / 1e6)
    return {"deleted": deleted, "freed_mb": round(freed / 1e6, 1)}


async def watch() -> None:
    while True:
        try:
            await asyncio.to_thread(purge_recordings)
        except Exception:  # noqa: BLE001
            logger.exception("recording clean-up failed")
        await asyncio.sleep(CHECK_EVERY_S)


def disk_status() -> dict:
    """The disk that holds our data, plus how much of it is recordings."""
    u = shutil.disk_usage(DATA_DIR)
    files = list(RECORDINGS_DIR.glob("*.wav")) if RECORDINGS_DIR.is_dir() else []
    return {"pct": round(u.used / u.total * 100), "used_gb": round(u.used / 1e9, 1), "total_gb": round(u.total / 1e9, 1),
            "recordings": len(files), "recordings_mb": round(sum(f.stat().st_size for f in files) / 1e6, 1),
            "keep_days": keep_days()}


def backup_status() -> dict | None:
    """What the last backup run wrote. None where there's no backup job (the laptop)."""
    try:
        s = json.loads(BACKUP_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return s if isinstance(s, dict) else None


def backup_stale(s: dict, now: float | None = None) -> bool:
    return (now or time.time()) - float(s.get("at") or 0) > BACKUP_STALE_S
