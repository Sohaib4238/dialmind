"""Small async client for the Cartesia API (Managed Agents, calls, phone numbers, webhooks).

Docs: https://docs.cartesia.ai (API version 2026-08-14). Managed agents live under
/v1/agents; calls, phone numbers and webhooks under /agents/... The API key never
appears in errors or logs.
"""

import os

import aiohttp

from app import standby, test_mode

BASE_URL = "https://api.cartesia.ai"
API_VERSION = "2026-08-14"
TIMEOUT = aiohttp.ClientTimeout(total=20)
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)


class CartesiaError(Exception):
    def __init__(self, message: str, status: int = 0, body: object = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def api_key() -> str:
    return (os.getenv("CARTESIA_API_KEY") or "").strip()


def _headers() -> dict[str, str]:
    key = api_key()
    if not key:
        raise CartesiaError("CARTESIA_API_KEY is not set in .env")
    return {"X-API-Key": key, "Cartesia-Version": API_VERSION}


def _error_text(body: object) -> str:
    if isinstance(body, dict):
        for k in ("message", "detail", "error", "title"):
            if body.get(k):
                return str(body[k])[:400]
    return str(body)[:400]


async def request(method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                  data: aiohttp.FormData | None = None, raw: bool = False, timeout: aiohttp.ClientTimeout = TIMEOUT):
    """JSON (or raw bytes) from Cartesia; raises CartesiaError on HTTP errors and network failures."""
    if method != "GET" and standby.on():  # every write to Cartesia passes here
        raise CartesiaError(f"Standby: Cartesia {method} {path} not sent (DIALER_STANDBY is set)")
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.request(method, BASE_URL + path, json=json, params=params, data=data, headers=_headers()) as resp:
                if raw and resp.status == 200:
                    return await resp.read(), resp.headers.get("Content-Type", "application/octet-stream")
                if resp.status == 204 or not await resp.read():  # e.g. DELETE: success with an empty body
                    if resp.status >= 400:
                        raise CartesiaError(f"Cartesia {method} {path}: HTTP {resp.status}", resp.status, None)
                    return {}
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = (await resp.text())[:400]
                if resp.status >= 400:
                    raise CartesiaError(f"Cartesia {method} {path}: HTTP {resp.status}: {_error_text(body)}", resp.status, body)
                return body
    except aiohttp.ClientResponseError as e:  # reached Cartesia, but couldn't read the reply
        raise CartesiaError(f"Cartesia {method} {path}: unreadable reply ({e.message})", e.status) from None
    except aiohttp.ClientError as e:
        raise CartesiaError(f"Couldn't reach Cartesia ({type(e).__name__})") from None
    except TimeoutError:
        raise CartesiaError("Cartesia did not answer in time") from None


def _items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("data", "items", "agents", "phone_numbers", "providers", "webhooks"):
            if isinstance(body.get(k), list):
                return body[k]
    return []


# ---- agents (managed) ----------------------------------------------------------------

async def upload_file(filename: str, content: bytes, purpose: str) -> dict:
    form = aiohttp.FormData()
    form.add_field("purpose", purpose)
    form.add_field("file", content, filename=filename, content_type="audio/wav")
    return await request("POST", "/files", data=form, timeout=UPLOAD_TIMEOUT)


async def get_agent(agent_id: str) -> dict:
    return await request("GET", f"/v1/agents/{agent_id}")


async def create_agent(name: str, description: str, config: dict) -> dict:
    return await request("POST", "/v1/agents", json={"name": name, "description": description, "config": config})


async def update_agent_config(agent_id: str, config: dict, version_description: str) -> dict:
    return await request("PATCH", f"/v1/agents/{agent_id}",
                         json={"config": config, "version_description": version_description[:200]})


async def attach_webhook(agent_id: str, webhook_id: str | None) -> dict:
    # Webhook attachment is on the agent record (Update Agent: webhook_id).
    return await request("PATCH", f"/agents/{agent_id}", json={"webhook_id": webhook_id})


async def create_tool(body: dict) -> dict:
    return await request("POST", "/v1/agents/tools", json=body)


async def update_tool(tool_id: str, body: dict) -> dict:
    return await request("PATCH", f"/v1/agents/tools/{tool_id}", json=body)


async def get_tool(tool_id: str) -> dict:
    return await request("GET", f"/v1/agents/tools/{tool_id}")


async def get_agent_record(agent_id: str) -> dict:
    return await request("GET", f"/agents/{agent_id}")


# ---- calls ---------------------------------------------------------------------------

async def create_call(agent_id: str, from_number_id: str, to_number: str, dynamic_variables: dict,
                      ringing_timeout_s: int, max_minutes: int) -> str:
    """Start one outbound call. Returns the agent_call_id."""
    if os.getenv("CARTESIA_CALLS_DISABLED", "").strip() not in ("", "0"):
        # Hard kill switch for demos/tests that run the real backend: nothing can dial.
        raise CartesiaError("Calls are disabled on this backend (CARTESIA_CALLS_DISABLED is set)")
    try:
        test_mode.guard(to_number)  # TEST MODE: only the test number, whatever path got here
    except test_mode.TestModeError as e:
        raise CartesiaError(str(e)) from None
    body = await request("POST", "/agents/calls", json={
        "agent_id": agent_id,
        "from_number_id": from_number_id,
        "ringing_timeout_seconds": ringing_timeout_s,
        "max_call_duration_minutes": max_minutes,
        "outbound_calls": [{"to_number": to_number, "dynamic_variables": dynamic_variables}],
    })
    calls = body.get("calls") or []
    if not calls:
        raise CartesiaError("Cartesia accepted the request but returned no call", 200, body)
    entry = calls[0]
    if entry.get("error") or not entry.get("agent_call_id"):
        raise CartesiaError(f"Cartesia refused the call: {_error_text(entry.get('error') or entry)}", 200, entry)
    return entry["agent_call_id"]


async def get_call(call_id: str) -> dict:
    return await request("GET", f"/agents/calls/{call_id}")


async def cancel_call(call_id: str) -> dict:
    return await request("POST", f"/agents/calls/{call_id}/cancel")


async def call_audio(call_id: str) -> tuple[bytes, str]:
    return await request("GET", f"/agents/calls/{call_id}/audio", raw=True,
                         timeout=aiohttp.ClientTimeout(total=60))


# ---- phone numbers + SIP trunk providers ---------------------------------------------

async def list_providers() -> list[dict]:
    return _items(await request("GET", "/agents/phone-numbers/providers"))


async def create_sip_provider(label: str, address: str, transport: str, username: str, password: str,
                              media_encryption: str = "disabled") -> dict:
    return await request("POST", "/agents/phone-numbers/providers", json={
        "type": "sip_trunk",
        "label": label,
        "outbound": {
            "address": address,
            "transport": transport,
            "destination_country": "US",
            "media_encryption": media_encryption,
            "credentials": {"username": username, "password": password},
        },
    })


async def get_provider(provider_id: str) -> dict:
    return await request("GET", f"/agents/phone-numbers/providers/{provider_id}")


async def _delete(path: str, check_path: str) -> dict:
    """DELETE, then confirm it's gone. Cartesia's DELETE replies are brotli-compressed even when we
    ask for gzip/identity, which aiohttp can't read; the follow-up GET (404 = gone) is the proof."""
    try:
        await request("DELETE", path)
    except CartesiaError as e:
        if "brotli" not in str(e):
            raise
    try:
        await request("GET", check_path)
    except CartesiaError as e:
        if e.status == 404:
            return {"deleted": True}
        raise
    raise CartesiaError(f"Cartesia DELETE {path}: still there afterwards")


async def delete_provider(provider_id: str) -> dict:
    p = f"/agents/phone-numbers/providers/{provider_id}"
    return await _delete(p, p)


async def delete_number(number_id: str) -> dict:
    p = f"/agents/phone-numbers/{number_id}"
    return await _delete(p, p)


async def list_numbers() -> list[dict]:
    return _items(await request("GET", "/agents/phone-numbers"))


async def import_number(label: str, number: str, provider_id: str) -> dict:
    return await request("POST", "/agents/phone-numbers",
                         json={"label": label, "number": number, "provider": {"id": provider_id}})


# ---- webhooks ------------------------------------------------------------------------

async def list_webhooks() -> list[dict]:
    return _items(await request("GET", "/agents/webhooks"))


async def create_webhook(url: str, secret: str, display_name: str) -> dict:
    return await request("POST", "/agents/webhooks", json={"url": url, "secret": secret, "display_name": display_name})


async def update_webhook(webhook_id: str, url: str) -> dict:
    return await request("PATCH", f"/agents/webhooks/{webhook_id}", json={"url": url})


async def get_webhook(webhook_id: str) -> dict:
    return await request("GET", f"/agents/webhooks/{webhook_id}")


# ---- misc ----------------------------------------------------------------------------

async def list_models() -> list[dict]:
    return _items(await request("GET", "/v1/agents/models"))
