"""Recordings past the keep period are deleted (and not fetched back); Health shows the disk with a warning at 80%."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import json
import os
import shutil
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import env_utils  # noqa: E402,F401

import yaml  # noqa: E402

from app import cartesia, health, storage  # noqa: E402
from app.calls import manager  # noqa: E402
from app.db import Call, get_session, init_db  # noqa: E402
from settings import SETTINGS_PATH, load_settings  # noqa: E402

init_db()
NOW = datetime.now(timezone.utc)


def set_days(days):
    d = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    if days is None:
        d["settings"].pop("recordings_keep_days", None)
    else:
        d["settings"]["recordings_keep_days"] = days
    SETTINGS_PATH.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")


def add_call(room, age_days):
    with get_session() as s:
        c = Call(room=room, number="+15125550100", status="ended", source="dashboard", created_at=NOW - timedelta(days=age_days))
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def wav(name, size=1000, file_age_days=0):
    storage.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    f = storage.RECORDINGS_DIR / f"{name}.wav"
    f.write_bytes(b"x" * size)
    t = time.time() - file_age_days * 86400
    os.utime(f, (t, t))
    return f


# The setting
assert load_settings().settings.recordings_keep_days == 30 and storage.keep_days() == 30
set_days(None)
assert load_settings().settings.recordings_keep_days == 30  # an older settings file without it
set_days(30)
print("setting: default 30, also in a settings file that doesn't have it yet")

# Purge goes by the call's date, not the file's
old = add_call("ac_old", 31)
fresh = add_call("ac_fresh", 29)
late = add_call("ac_opened_late", 40)
fold, ffresh = wav("ac_old"), wav("ac_fresh")
flate = wav("ac_opened_late")  # file made today, call 40 days ago
orphan_old, orphan_new = wav("ac_gone", file_age_days=31), wav("ac_gone2", file_age_days=1)
other = storage.RECORDINGS_DIR / "notes.txt"
other.write_text("keep")
r = storage.purge_recordings()
assert r["deleted"] == 3 and r["freed_mb"] == 0.0, r
assert not fold.exists() and not flate.exists() and not orphan_old.exists()
assert ffresh.exists() and orphan_new.exists() and other.exists()
assert storage.purge_recordings()["deleted"] == 0
print("purge: old calls' files go (even if cached late); fresh ones and other files stay; second run does nothing")

# A deleted recording isn't fetched back from Cartesia; a fresh one is, and is served from the cache
fetched = []


async def fake_audio(room):
    fetched.append(room)
    return b"audio", "audio/wav"

cartesia.call_audio = fake_audio
assert asyncio.run(manager.recording(old)) is None and fetched == []
assert asyncio.run(manager.recording(fresh))[0] == b"x" * 1000 and fetched == []
ffresh.unlink()
assert asyncio.run(manager.recording(fresh))[0] == b"audio" and fetched == ["ac_fresh"]
print("past the keep period: not fetched back; within it: cached or fetched as before")

# Other keep periods; 0 = forever
f2 = wav("ac_old")
set_days(0)
assert storage.purge_recordings()["deleted"] == 0 and f2.exists()
assert asyncio.run(manager.recording(old))[0] == b"x" * 1000
set_days(60)
assert storage.purge_recordings()["deleted"] == 0 and f2.exists()
set_days(10)
assert storage.purge_recordings()["deleted"] >= 1 and not f2.exists()
set_days(30)
for bad in (-1, 99999):
    set_days(bad)
    try:
        load_settings()
    except Exception:  # noqa: BLE001
        pass
    else:
        raise AssertionError(f"{bad} accepted")
    assert storage.keep_days() == 30  # broken settings: the default, not a crash or "delete all"
set_days(30)
print("keep period follows the setting; 0 = forever; invalid values refused")

# Disk on Health: normal, warning at 80, failure at 95
Usage = namedtuple("Usage", "total used free")
real_usage = shutil.disk_usage


def disk_at(pct):
    shutil.disk_usage = lambda p: Usage(100 * 10**9, pct * 10**9, (100 - pct) * 10**9)
    return asyncio.run(health.check_disk())

c = disk_at(31)
assert c["name"] == "Disk" and c["ok"] and not c.get("warn") and "31% full" in c["detail"] and "after 30 days" in c["detail"], c
c = disk_at(79)
assert c["ok"] and not c.get("warn")
c = disk_at(80)
assert c["ok"] and c["warn"] and "getting full" in c["detail"], c
c = disk_at(94)
assert c["ok"] and c["warn"]
c = disk_at(95)
assert not c["ok"]
shutil.disk_usage = real_usage
assert asyncio.run(health.check_disk())["ms"] >= 0
print("Health disk: fine below 80%, warning at 80%, failure at 95%")

# Backup on Health: only where the job runs
assert not storage.BACKUP_STATUS_PATH.exists() and asyncio.run(health.check_backup()) is None


def backup(**kw):
    storage.BACKUP_STATUS_PATH.write_text(json.dumps({"ok": True, "at": int(time.time()), "file": "f.db.gz", "offsite": "ok", **kw}))
    return asyncio.run(health.check_backup())

c = backup()
assert c["ok"] and not c.get("warn") and "S3" in c["detail"]
c = backup(offsite="off")
assert c["ok"] and c["warn"] and "not set up" in c["detail"]
c = backup(offsite="failed")
assert c["ok"] and c["warn"] and "FAILED" in c["detail"]
assert not backup(ok=False, detail="sqlite backup failed")["ok"]
assert not backup(at=int(time.time()) - 27 * 3600)["ok"]  # the job stopped running
storage.BACKUP_STATUS_PATH.write_text("not json")
assert asyncio.run(health.check_backup()) is None
storage.BACKUP_STATUS_PATH.unlink()
print("Health backup: S3 ok / not set up / upload failed / failed / stale; hidden on the laptop")

print("ALL STORAGE TESTS PASS")
