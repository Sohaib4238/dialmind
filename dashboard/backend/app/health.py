"""Health checks for the Cartesia call path. Each returns {"name", "ok", "detail", "ms"}
(+ "warn": True for things that are fine for now but needed later). Nothing is dialed."""

import asyncio
import os
import socket
import time

import aiohttp

from datetime import datetime, timezone

from sqlmodel import select

from app import cartesia, storage, tunnel
from app.db import Call, as_utc, get_session

# Cartesia free plan: $1 of agent credit per month at $0.06/min (~16.7 min). No usage API exists,
# so this is our own estimate from call durations recorded in the dashboard.
FREE_AGENT_DOLLARS = float(os.getenv("CARTESIA_FREE_DOLLARS", "1.0"))
AGENT_RATE_PER_MIN = 0.06

HOSTS = ("api.cartesia.ai", "sip.telnyx.com", "api.telnyx.com")


def _result(name: str, ok: bool, detail: str, started: float, warn: bool = False) -> dict:
    r = {"name": name, "ok": ok, "detail": detail, "ms": round((time.perf_counter() - started) * 1000)}
    if warn:
        r["warn"] = True
    return r


async def check_dns() -> dict:
    t = time.perf_counter()
    loop = asyncio.get_running_loop()
    failed = []
    for host in HOSTS:
        try:
            await asyncio.wait_for(loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP), 5)
        except (OSError, TimeoutError) as e:
            failed.append(f"{host}: {e}")
    return _result("DNS", not failed, "; ".join(failed) or f"{len(HOSTS)} hosts resolve", t)


async def check_cartesia_agent() -> dict:
    t = time.perf_counter()
    agent_id = os.getenv("CARTESIA_AGENT_ID", "")
    if not cartesia.api_key():
        return _result("Cartesia agent", False, "CARTESIA_API_KEY not set in .env", t)
    if not agent_id:
        return _result("Cartesia agent", False, "Not set up: run python dashboard\\backend\\setup_cartesia.py", t)
    try:
        a = await cartesia.get_agent(agent_id)
    except cartesia.CartesiaError as e:
        return _result("Cartesia agent", False, str(e), t)
    cfg = a.get("config") or {}
    voice = ((cfg.get("audio") or {}).get("output") or {}).get("voice_id", "?")
    model = (cfg.get("model") or {}).get("id", "?")
    return _result("Cartesia agent", True, f"{a.get('name')} · model {model} · voice {voice[:8]}…", t)


async def check_cartesia_number() -> dict:
    t = time.perf_counter()
    from_id, number = os.getenv("CARTESIA_FROM_NUMBER_ID", ""), os.getenv("TELNYX_PHONE_NUMBER", "")
    if not from_id:
        return _result("Phone number", False, "Not imported: run python dashboard\\backend\\setup_cartesia.py", t)
    try:
        numbers = await cartesia.list_numbers()
    except cartesia.CartesiaError as e:
        return _result("Phone number", False, str(e), t)
    n = next((n for n in numbers if n.get("id") == from_id), None)
    if not n:
        return _result("Phone number", False, f"{from_id} not found in Cartesia", t)
    provider = (n.get("provider") or {}).get("type", "?")
    ok = provider == "sip_trunk" and n.get("number") == number
    return _result("Phone number", ok, f"{n.get('number')} via {provider} (Telnyx SIP trunk)" if ok else f"{n.get('number')} via {provider}", t)


async def check_link() -> dict:
    t = time.perf_counter()
    s = tunnel.link_status()
    if s.get("standby"):  # expected, not a failure
        return _result("Public link (tunnel)", True, s["detail"], t, warn=True)
    return _result("Public link (tunnel)", s["online"], s["detail"], t)


def minutes_used_this_month() -> float:
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with get_session() as s:
        calls = s.exec(select(Call).where(Call.room.startswith("ac_"))).all()
    secs = sum(c.duration_s or 0 for c in calls if as_utc(c.created_at) >= start)
    return round(secs / 60, 1)


def minutes_left() -> float:
    return max(0.0, round(FREE_AGENT_DOLLARS / AGENT_RATE_PER_MIN - minutes_used_this_month(), 1))


async def check_minutes() -> dict:
    t = time.perf_counter()
    used = minutes_used_this_month()
    total = round(FREE_AGENT_DOLLARS / AGENT_RATE_PER_MIN, 1)
    left = max(0.0, round(total - used, 1))
    detail = f"~{used} of ~{total} free minutes used this month, ~{left} left (estimate from our call log; exact usage on play.cartesia.ai)"
    return _result("Cartesia minutes", left > 0, detail, t, warn=left < total * 0.3)


async def check_telnyx(http: aiohttp.ClientSession) -> dict:
    t = time.perf_counter()
    if not (os.getenv("TELNYX_SIP_USERNAME") and os.getenv("TELNYX_SIP_PASSWORD")):
        return _result("Telnyx", False, "TELNYX_SIP_USERNAME / TELNYX_SIP_PASSWORD missing in .env", t)
    key = (os.getenv("TELNYX_API_KEY") or "").strip()
    if not key:
        return _result("Telnyx", True, "SIP credentials set. TELNYX_API_KEY not set yet (needed for SMS later)", t, warn=True)
    try:
        async with http.get("https://api.telnyx.com/v2/balance", headers={"Authorization": f"Bearer {key}"},
                            timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return _result("Telnyx", False, f"API key rejected: HTTP {resp.status}", t)
            d = (await resp.json()).get("data") or {}
        return _result("Telnyx", True, f"SIP credentials set · API ok · balance {d.get('balance')} {d.get('currency', '')}".strip(), t)
    except (aiohttp.ClientError, TimeoutError) as e:
        return _result("Telnyx", False, f"{type(e).__name__}: {e}", t)


async def check_disk() -> dict:
    t = time.perf_counter()
    d = await asyncio.to_thread(storage.disk_status)
    keep = f"deleted after {d['keep_days']} days" if d["keep_days"] else "kept forever"
    detail = f"{d['pct']}% full ({d['used_gb']} of {d['total_gb']} GB) · {d['recordings']} recordings, {d['recordings_mb']} MB, {keep}"
    if d["pct"] >= storage.DISK_WARN_PCT:
        detail = f"Disk is getting full: {detail}"
    return _result("Disk", d["pct"] < storage.DISK_FULL_PCT, detail, t, warn=d["pct"] >= storage.DISK_WARN_PCT)


async def check_backup() -> dict | None:
    """Only where the backup job runs (the server)."""
    t = time.perf_counter()
    s = storage.backup_status()
    if s is None:
        return None
    if not s.get("ok"):
        return _result("Backup", False, f"Last backup failed: {s.get('detail', 'see the backup log')}", t)
    if storage.backup_stale(s):
        return _result("Backup", False, f"No backup in over a day (last: {s.get('file', '?')})", t)
    offsite = s.get("offsite")
    if offsite == "ok":
        return _result("Backup", True, f"Last: {s.get('file', '?')}, also in S3", t)
    if offsite == "failed":
        return _result("Backup", True, f"Last: {s.get('file', '?')} on this disk. Upload to S3 FAILED", t, warn=True)
    return _result("Backup", True, f"Last: {s.get('file', '?')} on this disk only (S3 not set up)", t, warn=True)


async def run_all() -> list[dict]:
    dns = await check_dns()
    async with aiohttp.ClientSession() as http:
        rest = await asyncio.gather(check_cartesia_agent(), check_cartesia_number(), check_link(), check_minutes(), check_telnyx(http),
                                    check_disk(), check_backup())
    return [dns, *(r for r in rest if r)]
