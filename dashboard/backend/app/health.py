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

# Cartesia Pro plan (since 2026-11-04): $6 of Voice Agent dollars a month, Managed Agents $0.03/min, overages OFF
# (calls fail once it's used up). Real usage: GET /usage/agents, which needs an ADMIN key (CARTESIA_ADMIN_API_KEY,
# optional, made in the Playground under Keys -> Admin). Without one: an estimate from our own call log.
PLAN_NAME = os.getenv("CARTESIA_PLAN_NAME", "Pro")
PLAN_DOLLARS = float(os.getenv("CARTESIA_PLAN_DOLLARS", "6.0"))
AGENT_RATE_PER_MIN = float(os.getenv("CARTESIA_AGENT_RATE", "0.03"))
USAGE_REFRESH_S = 300  # ask Cartesia at most this often
USAGE_MAX_AGE_S = 1800  # older than this: back to our estimate
_usage: dict = {"at": 0.0, "cents": None, "minutes": None, "calls": None, "error": None}

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


def _month_start() -> datetime:
    return datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def refresh_usage(force: bool = False) -> dict:
    """This month's agent usage from Cartesia (admin key only). Never raises; the key is never logged."""
    key = (os.getenv("CARTESIA_ADMIN_API_KEY") or "").strip()
    if not key:
        _usage["error"] = "no admin key"
        return _usage
    if not force and time.time() - _usage["at"] < USAGE_REFRESH_S and _usage["cents"] is not None:
        return _usage
    url = f"{cartesia.BASE_URL}/usage/agents"
    params = {"start_ts": _month_start().strftime("%Y-%m-%dT%H:%M:%SZ")}
    headers = {"Authorization": f"Bearer {key}", "Cartesia-Version": cartesia.API_VERSION}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            async with http.get(url, params=params, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}: {str((body or {}).get('message', ''))[:120]}")
        rows = body.get("data") or []
        _usage.update(at=time.time(), cents=sum(int(r.get("cents") or 0) for r in rows),
                      minutes=round(sum(float(r.get("minutes") or 0) for r in rows), 1),
                      calls=sum(int(r.get("calls") or 0) for r in rows), error=None)
    except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as e:
        _usage["error"] = f"Cartesia usage API: {e}"
    return _usage


def budget() -> dict:
    """The plan's agent dollars this month: used, left, and how many minutes that is at the agent rate."""
    real = _usage["cents"] is not None and time.time() - _usage["at"] < USAGE_MAX_AGE_S
    used_min = _usage["minutes"] if real else minutes_used_this_month()
    used = _usage["cents"] / 100 if real else used_min * AGENT_RATE_PER_MIN
    left = max(0.0, PLAN_DOLLARS - used)
    return {"plan": PLAN_NAME, "plan_dollars": PLAN_DOLLARS, "rate_per_min": AGENT_RATE_PER_MIN,
            "used_dollars": round(used, 2), "left_dollars": round(left, 2), "used_minutes": round(used_min, 1),
            "minutes_left": round(left / AGENT_RATE_PER_MIN, 1), "calls": _usage["calls"] if real else None,
            "source": "cartesia" if real else "estimate", "error": _usage["error"]}


def minutes_left() -> float:
    return budget()["minutes_left"]


async def check_minutes() -> dict:
    t = time.perf_counter()
    await refresh_usage(force=True)
    b = budget()
    head = (f"{b['plan']} plan: ${b['left_dollars']:.2f} of ${b['plan_dollars']:.2f} left this month, "
            f"about {b['minutes_left']:g} min at ${b['rate_per_min']:.2f}/min. ")
    if b["source"] == "cartesia":
        tail = f"Used ${b['used_dollars']:.2f} ({b['used_minutes']:g} min, {b['calls']} calls), from Cartesia."
    else:
        why = "Add CARTESIA_ADMIN_API_KEY for Cartesia's real numbers" if b["error"] == "no admin key" else b["error"]
        tail = f"Estimate from our call log (~{b['used_minutes']:g} min used; LLM tokens not counted). {why}."
    tail += " Overages are off: calls stop at $0."
    return _result("Cartesia balance", b["minutes_left"] > 0, head + tail, t,
                   warn=b["left_dollars"] < b["plan_dollars"] * 0.3 or b["source"] != "cartesia")


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
