"""Create/update the Cartesia side of the dialer (idempotent). Used by setup_cartesia.py and
the backend (agent config sync before calls, webhook URL whenever the tunnel URL changes).

Resources (IDs saved in .env, secrets never printed):
  CARTESIA_SIP_PROVIDER_ID  SIP trunk -> sip.telnyx.com with our existing Telnyx SIP credentials
  CARTESIA_FROM_NUMBER_ID   TELNYX_PHONE_NUMBER imported on that trunk (caller ID for calls)
  CARTESIA_AGENT_ID         the managed agent, config built from agent_settings.yaml
  CARTESIA_WEBHOOK_ID       call events -> https://<tunnel>/api/cartesia/webhook
  CARTESIA_WEBHOOK_SECRET   generated here; Cartesia sends it back in X-Webhook-Secret
  CARTESIA_OUTCOME_TOOL_ID  record_outcome webhook tool -> https://<tunnel>/api/cartesia/tools/record_outcome
  CARTESIA_SUPERVISOR_TOOL_ID  request_supervisor webhook tool -> .../api/cartesia/tools/request_supervisor
  CARTESIA_EMAIL_TOOL_ID    collect_email webhook tool -> .../api/cartesia/tools/collect_email
  CARTESIA_TOOL_SECRET      generated here; Cartesia sends it as Authorization: Bearer (all tools)
Inbound calls (app/inbound.py), created the first time "Inbound calls" is switched on:
  CARTESIA_INBOUND_AGENT_ID     the agent that answers our number (same settings, its own start)
  CARTESIA_LOOKUP_TOOL_ID       lookup_caller -> .../api/cartesia/tools/lookup_caller
  CARTESIA_SAVE_CALLER_TOOL_ID  save_caller -> .../api/cartesia/tools/save_caller
The background sound files are uploaded when first switched on; their IDs are in data/cartesia_state.json.
"""

import json
import logging
import os
import secrets
import time

from env_utils import save_env  # shared/env_utils.py
from settings import AgentSettings, load_settings

from app import ambience, cartesia, standby
from app.agent_config import (EMAIL_TOOL_PATH, LOOKUP_TOOL_PATH, OUTCOME_TOOL_PATH, SAVE_CALLER_TOOL_PATH,
                              SUPERVISOR_TOOL_PATH, build_config, build_inbound_config, config_hash, email_tool_body,
                              lookup_tool_body, outcome_tool_body, save_caller_tool_body, supervisor_tool_body)
from app.db import DATA_DIR

# Label of our trunk in Cartesia; a different label per Telnyx account.
PROVIDER_LABEL = os.getenv("CARTESIA_SIP_PROVIDER_LABEL", "dialmind-telnyx")
# Signaling transport to Telnyx: "tls" (sip.telnyx.com:5061) or "tcp" (:5060). Cartesia can't change it
# on an existing trunk, so changing it means recreate_sip_trunk() (setup_cartesia.py --recreate-trunk).
SIP_TRANSPORT = os.getenv("CARTESIA_SIP_TRANSPORT", "tls")
# SRTP: "disabled" / "allowed" / "required". Must match the Telnyx connection (Encrypted media = SRTP -> required).
SIP_MEDIA_ENCRYPTION = os.getenv("CARTESIA_SIP_MEDIA_ENCRYPTION", "disabled")
AGENT_NAME = "DialMind - outbound"
WEBHOOK_NAME = "dialmind-dashboard"
WEBHOOK_PATH = "/api/cartesia/webhook"
STATE_PATH = DATA_DIR / "cartesia_state.json"

logger = logging.getLogger("cartesia_setup")


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(**kv) -> None:
    s = {**_state(), **kv}
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _uploaded_file_id(sound: str) -> str | None:
    """Our uploaded copy of a sound, if the file hasn't changed since."""
    entry = (_state().get("ambience_files") or {}).get(sound) or {}
    return entry.get("file_id") if entry.get("sha256") and entry["sha256"] == ambience.file_hash(sound) else None


def _ambience_file_id(s: AgentSettings) -> str | None:
    return _uploaded_file_id(s.voice.ambience.sound) if s.voice.ambience.enabled else None


def _config(s: AgentSettings) -> dict:
    return build_config(s, ambience_file_id=_ambience_file_id(s))


async def upload_sound(sound: str) -> str:
    """Give Cartesia this sound, once. Returns its file ID."""
    if file_id := _uploaded_file_id(sound):
        return file_id
    digest = ambience.file_hash(sound)
    if not digest:
        raise ambience.SoundError(f"the sound file {sound} isn't there")
    f = await cartesia.upload_file(f"{sound}.wav", ambience.path_for(sound).read_bytes(), "agent_background_sound")
    _save_state(ambience_files={**(_state().get("ambience_files") or {}), sound: {"file_id": f["id"], "sha256": digest}})
    return f["id"]


async def _ensure_ambience_file(s: AgentSettings) -> None:
    if s.voice.ambience.enabled:
        await upload_sound(s.voice.ambience.sound)


def sync_status() -> dict:
    """Is the Cartesia agent running the current agent_settings.yaml?"""
    try:
        s = load_settings()
        current = config_hash(_config(s))
    except Exception as e:  # noqa: BLE001 - invalid settings file
        return {"in_sync": False, "detail": f"Settings file invalid: {e}"}
    st = _state()
    if not _env("CARTESIA_AGENT_ID"):
        return {"in_sync": False, "detail": "Cartesia agent not set up yet"}
    in_sync = st.get("agent_config_hash") == current
    if standby.on():
        return {"in_sync": in_sync, "synced_at": st.get("synced_at"), "detail": "Standby: this copy doesn't update the Cartesia agent"}
    if s.voice.ambience.enabled and st.get("ambience_error"):
        return {"in_sync": False, "synced_at": st.get("synced_at"), "detail": f"Background sound isn't on: {st['ambience_error']}"}
    return {"in_sync": in_sync, "synced_at": st.get("synced_at"),
            "detail": "Synced to Cartesia" if in_sync else "Not synced yet: it syncs on save and before each call"}


async def ensure_sip_provider() -> tuple[str, bool]:
    """(provider_id, created)."""
    if pid := _env("CARTESIA_SIP_PROVIDER_ID"):
        if any(p.get("id") == pid for p in await cartesia.list_providers()):
            return pid, False
    for p in await cartesia.list_providers():
        if p.get("type") == "sip_trunk" and p.get("label") == PROVIDER_LABEL:
            save_env("CARTESIA_SIP_PROVIDER_ID", p["id"])
            return p["id"], False
    user, password = _env("TELNYX_SIP_USERNAME"), _env("TELNYX_SIP_PASSWORD")
    if not user or not password:
        raise cartesia.CartesiaError("TELNYX_SIP_USERNAME / TELNYX_SIP_PASSWORD missing in .env")
    p = await cartesia.create_sip_provider(PROVIDER_LABEL, "sip.telnyx.com", SIP_TRANSPORT, user, password,
                                           SIP_MEDIA_ENCRYPTION)
    save_env("CARTESIA_SIP_PROVIDER_ID", p["id"])
    return p["id"], True


async def recreate_sip_trunk() -> dict:
    """Delete the number import + SIP trunk provider and create them again with SIP_TRANSPORT.
    Nothing changes in Telnyx. Returns the old and new IDs (for the change log)."""
    old_pid, old_nid = _env("CARTESIA_SIP_PROVIDER_ID"), _env("CARTESIA_FROM_NUMBER_ID")
    old = await cartesia.get_provider(old_pid) if old_pid else {}
    number = _env("TELNYX_PHONE_NUMBER")
    for n in await cartesia.list_numbers():
        if n.get("number") == number and (n.get("provider") or {}).get("id") == old_pid:
            await cartesia.delete_number(n["id"])
    if old_pid:
        await cartesia.delete_provider(old_pid)
    save_env("CARTESIA_SIP_PROVIDER_ID", "")
    save_env("CARTESIA_FROM_NUMBER_ID", "")
    pid, _ = await ensure_sip_provider()
    nid, _ = await ensure_number(pid)
    new = await cartesia.get_provider(pid)
    return {"old_provider_id": old_pid, "old_number_id": old_nid, "old_transport": (old.get("outbound") or {}).get("transport"),
            "new_provider_id": pid, "new_number_id": nid, "new_transport": (new.get("outbound") or {}).get("transport"),
            "address": (new.get("outbound") or {}).get("address")}


async def ensure_number(provider_id: str) -> tuple[str, bool]:
    number = _env("TELNYX_PHONE_NUMBER")
    if not number:
        raise cartesia.CartesiaError("TELNYX_PHONE_NUMBER missing in .env")
    for n in await cartesia.list_numbers():
        if n.get("number") == number:
            save_env("CARTESIA_FROM_NUMBER_ID", n["id"])
            return n["id"], False
    n = await cartesia.import_number("Telnyx main line", number, provider_id)
    save_env("CARTESIA_FROM_NUMBER_ID", n["id"])
    return n["id"], True


async def sync_agent(reason: str = "settings sync") -> dict:
    """Create the agent or push the current agent_settings.yaml to it when it changed.
    Returns {"agent_id", "action": created|updated|unchanged, "hash", "ambience_error"}.
    The background sound never stops a call: if Cartesia refuses it, the agent goes without it and the error is reported."""
    s = load_settings()
    ambience_error = ""
    try:
        await _ensure_ambience_file(s)
    except (cartesia.CartesiaError, ambience.SoundError, KeyError) as e:
        ambience_error = f"couldn't upload the sound ({e})"
        logger.warning("background sound: %s", ambience_error)
    config = _config(s)
    h = config_hash(config)
    agent_id = _env("CARTESIA_AGENT_ID")
    if agent_id:
        if _state().get("agent_config_hash") == h:
            _save_state(ambience_error=ambience_error)
            return {"agent_id": agent_id, "action": "unchanged", "hash": h, "ambience_error": ambience_error}
        try:
            try:
                await cartesia.update_agent_config(agent_id, config, f"Dashboard: {reason}")
            except cartesia.CartesiaError as e:
                if e.status == 404 or not config["audio"]["output"]["background_sound"]:
                    raise
                ambience_error = f"Cartesia refused it ({e})"
                logger.warning("background sound: %s", ambience_error)
                config = build_config(s)  # no sound; a hash that doesn't match, so the next call tries again
                await cartesia.update_agent_config(agent_id, config, f"Dashboard: {reason}, without the background sound")
                h = config_hash(config)
            _save_state(agent_config_hash=h, synced_at=time.time(), ambience_error=ambience_error)
            return {"agent_id": agent_id, "action": "updated", "hash": h, "ambience_error": ambience_error}
        except cartesia.CartesiaError as e:
            if e.status != 404:
                raise
    agent = await cartesia.create_agent(AGENT_NAME, "Outbound cold-calling agent (config from agent_settings.yaml)", config)
    save_env("CARTESIA_AGENT_ID", agent["id"])
    _save_state(agent_config_hash=h, synced_at=time.time(), ambience_error=ambience_error)
    return {"agent_id": agent["id"], "action": "created", "hash": h, "ambience_error": ambience_error}


INBOUND_AGENT_NAME = "DialMind - inbound"


def inbound_config(s: AgentSettings | None = None) -> dict:
    from app import supervisor

    s = s or load_settings()
    return build_inbound_config(s, supervisor.phones(), _ambience_file_id(s))


async def sync_inbound_agent(reason: str = "settings sync") -> dict:
    """Create the inbound agent (CARTESIA_INBOUND_AGENT_ID) or push the current settings to it when they changed.
    Like sync_agent; the background sound is dropped if Cartesia refuses it."""
    s = load_settings()
    try:
        await _ensure_ambience_file(s)
    except (cartesia.CartesiaError, ambience.SoundError, KeyError) as e:
        logger.warning("background sound (inbound): %s", e)
    config = inbound_config(s)
    h = config_hash(config)
    agent_id = _env("CARTESIA_INBOUND_AGENT_ID")
    if agent_id:
        if _state().get("inbound_agent_config_hash") == h:
            return {"agent_id": agent_id, "action": "unchanged", "hash": h}
        try:
            try:
                await cartesia.update_agent_config(agent_id, config, f"Dashboard (inbound): {reason}")
            except cartesia.CartesiaError as e:
                if e.status == 404 or not config["audio"]["output"]["background_sound"]:
                    raise
                config = {**config, "audio": {**config["audio"], "output": {**config["audio"]["output"], "background_sound": None}}}
                await cartesia.update_agent_config(agent_id, config, f"Dashboard (inbound): {reason}, without the background sound")
                h = config_hash(config)
            _save_state(inbound_agent_config_hash=h, inbound_synced_at=time.time())
            return {"agent_id": agent_id, "action": "updated", "hash": h}
        except cartesia.CartesiaError as e:
            if e.status != 404:
                raise
    agent = await cartesia.create_agent(INBOUND_AGENT_NAME, "Answers calls to our number (config from agent_settings.yaml)", config)
    save_env("CARTESIA_INBOUND_AGENT_ID", agent["id"])
    _save_state(inbound_agent_config_hash=h, inbound_synced_at=time.time())
    return {"agent_id": agent["id"], "action": "created", "hash": h}


async def ensure_lookup_tool(public_base_url: str) -> dict:
    """lookup_caller (inbound calls): who is calling. Created the first time inbound calls are switched on."""
    url = public_base_url.rstrip("/") + LOOKUP_TOOL_PATH
    return await _ensure_tool(lookup_tool_body(url, _tool_secret()), "CARTESIA_LOOKUP_TOOL_ID", "lookup_tool_body_hash")


async def ensure_save_caller_tool(public_base_url: str) -> dict:
    """save_caller (inbound calls): a new caller -> a new lead."""
    url = public_base_url.rstrip("/") + SAVE_CALLER_TOOL_PATH
    return await _ensure_tool(save_caller_tool_body(url, _tool_secret()), "CARTESIA_SAVE_CALLER_TOOL_ID",
                              "save_caller_tool_body_hash")


async def attach_webhook_to(agent_id: str) -> bool:
    """Our call-events webhook on another agent too (the inbound one). False when there's no webhook yet."""
    webhook_id = _env("CARTESIA_WEBHOOK_ID")
    if not webhook_id:
        return False
    record = await cartesia.get_agent_record(agent_id)
    if record.get("webhook_id") != webhook_id:
        await cartesia.attach_webhook(agent_id, webhook_id)
    return True


def _tool_secret() -> str:
    secret = _env("CARTESIA_TOOL_SECRET")
    if not secret:
        secret = secrets.token_urlsafe(32)
        save_env("CARTESIA_TOOL_SECRET", secret)
    return secret


async def ensure_outcome_tool(public_base_url: str) -> dict:
    """Create the record_outcome webhook tool, or point it at the current public URL. A new tool
    is attached to the agent by the next sync_agent() (its ID is part of the agent config)."""
    url = public_base_url.rstrip("/") + OUTCOME_TOOL_PATH
    return await _ensure_tool(outcome_tool_body(url, _tool_secret()), "CARTESIA_OUTCOME_TOOL_ID", "tool_body_hash")


async def ensure_supervisor_tool(public_base_url: str) -> dict:
    """The request_supervisor webhook tool (Phase 6), like ensure_outcome_tool. Once it exists, the agent
    config also gets the transfer_to_number system tool and the hand-off instructions."""
    url = public_base_url.rstrip("/") + SUPERVISOR_TOOL_PATH
    body = supervisor_tool_body(url, _tool_secret())
    if _state().get("supervisor_assignments") is False and _state().get("supervisor_assignments_tried") == _tool_hash(body):
        body.pop("assignments", None)  # refused before for this same definition: don't ask again on every start
    try:
        res = await _ensure_tool(body, "CARTESIA_SUPERVISOR_TOOL_ID", "supervisor_tool_body_hash")
    except cartesia.CartesiaError as e:
        if "assignments" not in body or e.status not in (400, 422):
            raise
        # Cartesia refused the assignment: the transfer then always rings the first available supervisor.
        logger.warning("request_supervisor: Cartesia refused the transfer_number assignment (%s)", e)
        _save_state(supervisor_assignments=False, supervisor_assignments_tried=_tool_hash(body),
                    supervisor_assignments_error=str(e)[:300])
        body.pop("assignments")
        return await _ensure_tool(body, "CARTESIA_SUPERVISOR_TOOL_ID", "supervisor_tool_body_hash")
    if "assignments" in body:
        _save_state(supervisor_assignments=True, supervisor_assignments_error="")
    return res


async def ensure_email_tool(public_base_url: str) -> dict:
    """The collect_email webhook tool (Phase 7), like ensure_outcome_tool. Once it exists, the agent config
    gets it and the "ask for their email" instructions."""
    url = public_base_url.rstrip("/") + EMAIL_TOOL_PATH
    return await _ensure_tool(email_tool_body(url, _tool_secret()), "CARTESIA_EMAIL_TOOL_ID", "email_tool_body_hash")


async def _ensure_tool(body: dict, env_key: str, hash_key: str) -> dict:
    url = body["api_schema"]["url"]
    tool_id, action = _env(env_key), "unchanged"
    if tool_id:
        try:
            current = await cartesia.get_tool(tool_id)
            if (current.get("api_schema") or {}).get("url") != url or _state().get(hash_key) != _tool_hash(body):
                await cartesia.update_tool(tool_id, {k: v for k, v in body.items() if k != "type"})
                action = "updated"
        except cartesia.CartesiaError as e:
            if e.status != 404:
                raise
            tool_id = ""
    if not tool_id:
        tool = await cartesia.create_tool(body)
        tool_id = tool["id"]
        save_env(env_key, tool_id)
        action = "created"
    _save_state(**{hash_key: _tool_hash(body)})
    return {"tool_id": tool_id, "url": url, "action": action}


def _tool_hash(body: dict) -> str:
    """Hash of the tool definition without its URL (the URL is compared directly)."""
    api = {k: v for k, v in body["api_schema"].items() if k != "url"}
    return config_hash({**body, "api_schema": api})


async def ensure_webhook(public_base_url: str) -> dict:
    """Point the webhook at the current public URL and attach it to the agent."""
    url = public_base_url.rstrip("/") + WEBHOOK_PATH
    agent_id = _env("CARTESIA_AGENT_ID")
    webhook_id, secret = _env("CARTESIA_WEBHOOK_ID"), _env("CARTESIA_WEBHOOK_SECRET")
    action = "unchanged"
    if webhook_id and secret:
        try:
            current = await cartesia.get_webhook(webhook_id)
            if current.get("url") != url:
                await cartesia.update_webhook(webhook_id, url)
                action = "updated"
        except cartesia.CartesiaError as e:
            if e.status != 404:
                raise
            webhook_id = ""
    if not webhook_id or not secret:
        secret = secrets.token_urlsafe(32)
        w = await cartesia.create_webhook(url, secret, WEBHOOK_NAME)
        webhook_id = w["id"]
        save_env("CARTESIA_WEBHOOK_ID", webhook_id)
        save_env("CARTESIA_WEBHOOK_SECRET", secret)
        action = "created"
    attached = False
    if agent_id:
        record = await cartesia.get_agent_record(agent_id)
        if record.get("webhook_id") != webhook_id:
            await cartesia.attach_webhook(agent_id, webhook_id)
        attached = True
    return {"webhook_id": webhook_id, "url": url, "action": action, "attached": attached}
