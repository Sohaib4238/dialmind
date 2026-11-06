"""Public URL for Cartesia webhooks: PUBLIC_BASE_URL on the server, a Cloudflare quick tunnel on the laptop.

Laptop (started by start_all.ps1): cloudflared runs with `--metrics 127.0.0.1:20241`; its /quicktunnel endpoint tells us the
random https://*.trycloudflare.com hostname. The URL changes on every tunnel start, so
this watcher re-points the Cartesia webhook, the webhook tools and (Phase 7b) our messaging profile's
webhook whenever it changes, then checks that the
public URL really reaches this backend (/api/public/ping).
"""

import asyncio
import logging
import os
import time

import aiohttp

from app import cartesia, cartesia_setup, inbound, sms, standby

logger = logging.getLogger("dashboard.tunnel")

METRICS_URL = f"http://{os.getenv('CLOUDFLARED_METRICS', '127.0.0.1:20241')}/quicktunnel"
# Second quick tunnel -> the Next.js dashboard (:3000), for the supervisor's laptop (login required, auth.py)
DASHBOARD_METRICS_URL = f"http://{os.getenv('CLOUDFLARED_DASHBOARD_METRICS', '127.0.0.1:20242')}/quicktunnel"
CHECK_EVERY_S = 10

state: dict = {
    "public_url": None,  # https://....trycloudflare.com
    "reachable": None,  # public ping worked
    "webhook": None,  # result of ensure_webhook
    "tool": None,  # result of ensure_outcome_tool
    "supervisor_tool": None,  # result of ensure_supervisor_tool
    "email_tool": None,  # result of ensure_email_tool
    "sms_webhook": None,  # result of sms.ensure_webhook (messaging profile -> /api/telnyx/messaging)
    "dashboard_url": None,  # https://....trycloudflare.com of the dashboard tunnel (remote login)
    "error": None,
    "checked_at": None,
}


async def _tunnel_url(http: aiohttp.ClientSession, metrics_url: str = "") -> str | None:
    try:
        async with http.get(metrics_url or METRICS_URL, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            host = (await resp.json(content_type=None)).get("hostname") if resp.status == 200 else None
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    return f"https://{host}" if host else None


async def _public_ping(http: aiohttp.ClientSession, base: str) -> bool:
    try:
        async with http.get(f"{base}/api/public/ping", timeout=aiohttp.ClientTimeout(total=8)) as resp:
            return resp.status == 200 and (await resp.json(content_type=None)).get("ok") is True
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return False


def fixed_url() -> str:
    """PUBLIC_BASE_URL (the server): the one address for the webhook and tools. Empty on the laptop: quick tunnel."""
    return os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


async def check_once(http: aiohttp.ClientSession) -> None:
    fixed = fixed_url()
    if standby.on():  # the live backend owns the webhook + tools: never re-point them from here
        state.update(checked_at=time.time(), dashboard_url=fixed or None)
        return
    state["dashboard_url"] = fixed or await _tunnel_url(http, DASHBOARD_METRICS_URL)
    url = fixed or await _tunnel_url(http)
    state["checked_at"] = time.time()
    if not url:
        state.update(public_url=None, reachable=False, error="Tunnel not running (start_all.ps1 starts it)")
        return
    # Also when one of them failed last time: a fixed URL never changes, so nothing else would retry it.
    if url != state["public_url"] or not all(state[k] for k in ("webhook", "tool", "supervisor_tool", "email_tool")):
        state.update(public_url=url, reachable=None, webhook=None)
        try:
            state["webhook"] = await cartesia_setup.ensure_webhook(url)
            logger.info("webhook -> %s (%s)", state["webhook"]["url"], state["webhook"]["action"])
            state["error"] = None
        except cartesia.CartesiaError as e:
            state["error"] = f"Couldn't register the webhook: {e}"
            logger.warning(state["error"])
        try:  # the record_outcome tool lives at the same public URL
            state["tool"] = await cartesia_setup.ensure_outcome_tool(url)
            logger.info("record_outcome tool -> %s (%s)", state["tool"]["url"], state["tool"]["action"])
            if state["tool"]["action"] == "created":
                await cartesia_setup.sync_agent("record_outcome tool attached")
        except cartesia.CartesiaError as e:
            state["tool"] = None
            state["error"] = state["error"] or f"Couldn't register the record_outcome tool: {e}"
            logger.warning("record_outcome tool: %s", e)
        try:  # request_supervisor (Phase 6): same public URL; a new tool turns on the hand-off + transfer
            state["supervisor_tool"] = await cartesia_setup.ensure_supervisor_tool(url)
            logger.info("request_supervisor tool -> %s (%s)", state["supervisor_tool"]["url"], state["supervisor_tool"]["action"])
            if state["supervisor_tool"]["action"] == "created":
                await cartesia_setup.sync_agent("request_supervisor tool + transfer attached")
        except cartesia.CartesiaError as e:
            state["supervisor_tool"] = None
            state["error"] = state["error"] or f"Couldn't register the request_supervisor tool: {e}"
            logger.warning("request_supervisor tool: %s", e)
        try:  # collect_email (Phase 7): a new tool turns on "ask for their email"
            state["email_tool"] = await cartesia_setup.ensure_email_tool(url)
            logger.info("collect_email tool -> %s (%s)", state["email_tool"]["url"], state["email_tool"]["action"])
            if state["email_tool"]["action"] == "created":
                await cartesia_setup.sync_agent("collect_email tool attached")
        except cartesia.CartesiaError as e:
            state["email_tool"] = None
            state["error"] = state["error"] or f"Couldn't register the collect_email tool: {e}"
            logger.warning("collect_email tool: %s", e)
        # Phase 7b: replies to our texts (STOP) come in through our own messaging profile's webhook
        state["sms_webhook"] = await asyncio.to_thread(sms.ensure_webhook, url)
        logger.info("messaging webhook: %s", state["sms_webhook"]["detail"])
    if state["webhook"]:  # inbound calls: switch on/off to match the settings (app/inbound.py)
        await inbound.maybe_apply(url)
    # A new trycloudflare hostname can take a few seconds to resolve.
    state["reachable"] = await _public_ping(http, url)


async def watch() -> None:
    async with aiohttp.ClientSession() as http:
        while True:
            try:
                await check_once(http)
            except Exception:
                logger.exception("tunnel check failed")
            await asyncio.sleep(CHECK_EVERY_S)


def link_status() -> dict:
    """For /api/health and the sidebar: can Cartesia reach us right now?"""
    if standby.on():
        return {"online": False, "standby": True, "public_url": None, "detail": standby.DETAIL, "webhook_url": None, "tool_url": None}
    ok = bool(state["public_url"] and state["reachable"] and state["webhook"] and state["webhook"].get("attached"))
    if ok:
        detail = f"Webhook live at {state['public_url']}"
    elif state["error"]:
        detail = state["error"]
    elif state["public_url"] and not state["reachable"]:
        detail = "Tunnel up, waiting for the public URL to answer…"
    else:
        detail = "Checking the tunnel…"
    return {"online": ok, "public_url": state["public_url"], "detail": detail,
            "webhook_url": (state["webhook"] or {}).get("url"), "tool_url": (state["tool"] or {}).get("url")}
