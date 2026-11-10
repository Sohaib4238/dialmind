"""Small async client for the Phonic API (https://docs.phonic.co), used by providers/phonic_provider.py.

Outbound calls go through our own Telnyx SIP trunk: POST /v1/conversations/sip/outbound_call with the trunk's
address and credentials in X-Sip-* headers (the same Telnyx credential connection Cartesia uses).

Safety, checked right before every outbound request (dry runs included):
  PHONIC_TEST_ONLY  on unless set to "0": ONLY TEST_CALL_NUMBER may be called (none if it is empty); anything else is blocked and logged.
  test_mode.guard   the dashboard's TEST MODE, as for Cartesia.
  kill switches     PHONIC_CALLS_DISABLED or CARTESIA_CALLS_DISABLED: no real call (a dry run still validates).
  standby           DIALER_STANDBY: no write to Phonic unless PHONIC_TEST_ONLY is on.
The API key, SIP username and password never appear in errors or logs.
"""

import logging
import os

import aiohttp

from app import standby, test_mode
from app.providers.base import CallNotConnected, ProviderError

logger = logging.getLogger("dashboard.phonic")

BASE_URL = "https://api.phonic.ai/v1"
TIMEOUT = aiohttp.ClientTimeout(total=20)
# The outbound request may only answer once the call is set up (a carrier rejection comes back as HTTP 409).
DIAL_TIMEOUT = aiohttp.ClientTimeout(total=90)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=60)

# SIP status from a refused call -> our call result
BUSY_CODES = {486, 600}
NO_ANSWER_CODES = {408, 480, 487}


class PhonicError(ProviderError):
    pass


class PhonicBlocked(PhonicError):
    """Refused by our own guard before anything was sent to Phonic."""


def api_key() -> str:
    return (os.getenv("PHONIC_API_KEY") or "").strip()


def configured() -> bool:
    return bool(api_key())


def test_only() -> bool:
    return (os.getenv("PHONIC_TEST_ONLY") or "1").strip() != "0"


def calls_disabled() -> bool:
    return any((os.getenv(k) or "").strip() not in ("", "0") for k in ("PHONIC_CALLS_DISABLED", "CARTESIA_CALLS_DISABLED"))


def test_only_number() -> str:
    """The only number Phonic may call while PHONIC_TEST_ONLY is on: TEST_CALL_NUMBER from .env."""
    return (os.getenv("TEST_CALL_NUMBER") or "").strip()


def guard_number(to_number: str) -> None:
    """Last check before Phonic is asked to dial (also for dry runs)."""
    allowed = test_only_number()
    if test_only() and (not allowed or to_number != allowed):
        logger.error("BLOCKED: PHONIC_TEST_ONLY is on but a Phonic call to %s was requested (only %s is allowed). "
                     "No call placed.", to_number, allowed or "none: TEST_CALL_NUMBER is empty")
        raise PhonicBlocked(f"Blocked: PHONIC_TEST_ONLY is on, so Phonic may only call {allowed or 'TEST_CALL_NUMBER (not set)'}, "
                            f"not {to_number}. No call placed.")
    try:
        test_mode.guard(to_number)
    except test_mode.TestModeError as e:
        raise PhonicBlocked(str(e)) from None


def _headers(extra: dict | None = None) -> dict[str, str]:
    key = api_key()
    if not key:
        raise PhonicError("PHONIC_API_KEY is not set in .env")
    return {"Authorization": f"Bearer {key}", **(extra or {})}


def _error_text(body: object) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            parts = [str(err.get(k)) for k in ("code", "message") if err.get(k)]
            if err.get("sip_status_code"):
                parts.append(f"SIP {err['sip_status_code']} {err.get('sip_status') or ''}".strip())
            if body.get("param_errors") or err.get("param_errors"):
                parts.append(str(body.get("param_errors") or err.get("param_errors"))[:300])
            if parts:
                return ": ".join(parts)[:400]
        for k in ("message", "detail", "error"):
            if body.get(k):
                return str(body[k])[:400]
    return str(body)[:400]


async def request(method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                  headers: dict | None = None, timeout: aiohttp.ClientTimeout = TIMEOUT):
    if method != "GET" and standby.on() and not test_only():
        raise PhonicBlocked(f"Standby: Phonic {method} {path} not sent (DIALER_STANDBY is set and PHONIC_TEST_ONLY is off)")
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.request(method, BASE_URL + path, json=json, params=params, headers=_headers(headers)) as resp:
                raw = await resp.read()
                body: object = {}
                if raw:
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:
                        body = raw[:400].decode("utf-8", "replace")
                if resp.status >= 400:
                    raise PhonicError(f"Phonic {method} {path}: HTTP {resp.status}: {_error_text(body)}", resp.status, body)
                return body
    except aiohttp.ClientError as e:
        raise PhonicError(f"Couldn't reach Phonic ({type(e).__name__})") from None
    except TimeoutError:
        raise PhonicError("Phonic did not answer in time") from None


def sip_target() -> tuple[dict, dict]:
    """(headers, sip) for our Telnyx trunk: the same address, credentials, TLS and SRTP as the Cartesia trunk."""
    address = (os.getenv("PHONIC_SIP_ADDRESS") or "sip.telnyx.com:5061").strip()
    user = (os.getenv("PHONIC_SIP_USERNAME") or os.getenv("TELNYX_SIP_USERNAME") or "").strip()
    password = (os.getenv("PHONIC_SIP_PASSWORD") or os.getenv("TELNYX_SIP_PASSWORD") or "").strip()
    headers = {"X-Sip-Address": address}
    if user:
        headers["X-Sip-Auth-Username"] = user
    if password:
        headers["X-Sip-Auth-Password"] = password
    sip = {"transport": (os.getenv("PHONIC_SIP_TRANSPORT") or os.getenv("CARTESIA_SIP_TRANSPORT") or "tls").strip(),
           "media_encryption": (os.getenv("PHONIC_SIP_MEDIA_ENCRYPTION") or os.getenv("CARTESIA_SIP_MEDIA_ENCRYPTION")
                                or "required").strip()}
    return headers, sip


def from_number() -> str:
    return (os.getenv("PHONIC_FROM_NUMBER") or os.getenv("TELNYX_PHONE_NUMBER") or "").strip()


async def outbound_call(to_number: str, config: dict, *, dry_run: bool = False) -> dict:
    """Place (or, dry_run, only validate) one call through our SIP trunk. Returns Phonic's reply
    ({"conversation_id", "dry_run", ...}). Raises CallNotConnected when the carrier refused the call."""
    guard_number(to_number)
    if not dry_run and calls_disabled():
        raise PhonicBlocked("Calls are disabled on this backend (PHONIC_CALLS_DISABLED / CARTESIA_CALLS_DISABLED is set)")
    caller = from_number()
    if not caller:
        raise PhonicError("No caller ID: set TELNYX_PHONE_NUMBER (or PHONIC_FROM_NUMBER) in .env")
    headers, sip = sip_target()
    body = {"from_phone_number": caller, "to_phone_number": to_number, "dry_run": bool(dry_run), "sip": sip,
            "config": config}
    try:
        reply = await request("POST", "/conversations/sip/outbound_call", json=body, headers=headers,
                              timeout=DIAL_TIMEOUT)
    except PhonicError as e:
        err = e.body.get("error") if isinstance(e.body, dict) else None
        code = err.get("sip_status_code") if isinstance(err, dict) else None
        if e.status == 409 and isinstance(code, int):
            result = "busy" if code in BUSY_CODES else "no_answer" if code in NO_ANSWER_CODES else "failed"
            raise CallNotConnected(str(e), result, e.status, e.body) from None
        raise
    return reply if isinstance(reply, dict) else {}


async def get_conversation(conversation_id: str) -> dict:
    body = await request("GET", f"/conversations/{conversation_id}", params={"audio_container": "wav"})
    return (body or {}).get("conversation") or {}


async def cancel(conversation_id: str) -> dict:
    return await request("POST", f"/conversations/{conversation_id}/cancel")


async def list_voices() -> list[dict]:
    body = await request("GET", "/voices")
    return [v for v in (body or {}).get("voices") or [] if isinstance(v, dict)]


async def list_tools() -> list[dict]:
    body = await request("GET", "/tools")
    return [t for t in (body or {}).get("tools") or [] if isinstance(t, dict)]


async def create_tool(body: dict) -> dict:
    return await request("POST", "/tools", json=body)


async def update_tool(name_or_id: str, body: dict) -> dict:
    return await request("PATCH", f"/tools/{name_or_id}", json=body)


async def download(url: str) -> tuple[bytes, str]:
    """A presigned file (the call's audio). No API key: the URL carries its own signature."""
    try:
        async with aiohttp.ClientSession(timeout=DOWNLOAD_TIMEOUT) as http:
            async with http.get(url) as resp:
                if resp.status >= 400:
                    raise PhonicError(f"Phonic audio: HTTP {resp.status}", resp.status)
                return await resp.read(), resp.headers.get("Content-Type", "audio/wav")
    except aiohttp.ClientError as e:
        raise PhonicError(f"Couldn't download the Phonic audio ({type(e).__name__})") from None
