"""Phonic behind the provider interface (app/phonic.py is the HTTP client).

Same agent as Cartesia: the instructions come from agent_config.build_instructions (one prompt for both), and the
per-call lead details are the same variables, passed as Phonic template_variables ({{lead_name}} etc.).

Our webhook tools (record_outcome, request_supervisor, collect_email) are the same backend endpoints Cartesia uses.
Phonic needs them as named tools in its account: ensure_tools() creates or re-points them before a call, at the
public URL (PUBLIC_BASE_URL / PHONIC_PUBLIC_URL, else the laptop's quick tunnel). Without a public URL the call
still works, without those tools (the outcome then comes from the transcript safety net).

What Phonic doesn't have, and isn't faked:
  - voicemail detection for the person we call (Phonic only detects voicemail on a transfer). The agent is told to
    hang up on a recording, and our own transcript check (outcomes.voicemail_phrase) still closes the line.
  - a call summary (Call.ai_summary stays empty for Phonic calls).
  - a ringing timeout (the Telnyx connection's own timeout applies).
"""

import json
import logging
import os
from datetime import datetime

import aiohttp

from app import phonic, tunnel
from app.agent_config import (EMAIL_REASONS, EMAIL_TOOL_NAME, EMAIL_TOOL_PATH, OUTCOME_TOOL_NAME, OUTCOME_TOOL_PATH,
                              SUPERVISOR_TOOL_NAME, SUPERVISOR_TOOL_PATH, build_instructions)
from app.providers.base import ProviderError, VoiceProvider

logger = logging.getLogger("dashboard.phonic_provider")

END_TOOL = "natural_conversation_ending"  # Phonic's built-in hang-up
TRANSFER_TOOL_NAME = "transfer_to_specialist"
# Phonic ended_by -> our end_reason (the Cartesia names calls.py already knows)
ENDED_BY = {
    "user": "client_hangup",
    "user_canceled": "api_cancelled",
    "user_validation_failed": "error",
    "assistant": "agent_hangup",
    "assistant_silence_limit_reached": "call_inactivity",
    "assistant_time_limit_reached": "max_duration",
    "configuration_endpoint_timed_out": "config_error",
    "configuration_endpoint_error": "config_error",
    "configuration_endpoint_invalid_response": "config_error",
    "error": "error",
}
FAILED_ENDINGS = {"error", "config_error"}
TOOL_NAMES = {END_TOOL: "end_call"}  # Phonic's tool name -> the name calls.py looks for


def _parse(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


def _param(name: str, kind: str, description: str, enum: tuple | list | None = None) -> dict:
    p = {"name": name, "type": "enum" if enum else kind, "description": description, "is_required": True,
         "location": "request_body"}
    if enum:
        p["enum_values"] = list(enum)
    return p


def tool_bodies(base_url: str, secret: str, supervisor_phone: str | None) -> dict[str, dict]:
    """Our tools as Phonic tools, by name. Same endpoints, Bearer secret and wording as the Cartesia ones."""
    from app.outcomes import AI_OUTCOMES

    auth = {"Authorization": f"Bearer {secret}"}

    def webhook(name: str, path: str, description: str, mode: str, timeout_ms: int, params: list[dict]) -> dict:
        return {"name": name, "type": "custom_webhook", "description": description, "execution_mode": mode,
                "endpoint_method": "POST", "endpoint_url": base_url + path, "endpoint_headers": auth,
                "endpoint_timeout_ms": timeout_ms, "parameters": params}

    transfer_desc = f"Transfer the call to our senior specialist. Only right after {SUPERVISOR_TOOL_NAME} answered AVAILABLE."
    if not supervisor_phone:
        transfer_desc += f" Dial the transfer_number from that {SUPERVISOR_TOOL_NAME} answer, and no other number."
    return {
        OUTCOME_TOOL_NAME: webhook(
            OUTCOME_TOOL_NAME, OUTCOME_TOOL_PATH,
            "Save how this call went. Use it once you know the outcome, before you say goodbye. Use it again only if "
            "the outcome changes. Never mention it to the person.", "async", 10_000,
            [_param("outcome", "string", "See the Call outcome section of your instructions.", AI_OUTCOMES),
             _param("note", "string", "One short line in plain words: what they said, the best time, who to ask for."),
             _param("callback_time", "string", "Only for not_now or gatekeeper when a day or time was given: the "
                    "lead's local time as YYYY-MM-DDTHH:MM (24-hour). Otherwise an empty string.")]),
        SUPERVISOR_TOOL_NAME: webhook(
            SUPERVISOR_TOOL_NAME, SUPERVISOR_TOOL_PATH,
            "Ask our senior specialist whether they can take this call now. The answer is AVAILABLE, WAITING or NOT "
            "AVAILABLE, with what to do. On WAITING, say a short line and use it again. Never mention it, or any "
            "number in its answer, to the person.", "sync", 12_000,
            [_param("summary", "string", "For the senior specialist, two or three short sentences: who they are, "
                    "what they need, and what they said. Plain words.")]),
        EMAIL_TOOL_NAME: webhook(
            EMAIL_TOOL_NAME, EMAIL_TOOL_PATH,
            "Save the lead's email address for our follow-up email. Use it only after you read the address back to "
            "them and they said it's right. Never mention it to the person.", "sync", 8_000,
            [_param("email", "string", "The address they confirmed, written normally, like sarah@gmail.com."),
             _param("confirmed", "boolean", "true only if you read it back to them and they said yes."),
             _param("reason", "string", "details: they asked for details or an email. interested: they're "
                    "interested. not_now: they want a call another time. other: anything else.", EMAIL_REASONS)]),
        # Hand-off (SIP REFER through our Telnyx trunk), like Cartesia's transfer_to_number: Phonic leaves the call.
        TRANSFER_TOOL_NAME: {
            "name": TRANSFER_TOOL_NAME, "type": "built_in_transfer_to_phone_number", "description": transfer_desc,
            "execution_mode": "sync", "phone_number": supervisor_phone or None, "keep_listening": False,
            "use_agent_phone_number": False, "detect_voicemail": False},
    }


def _single_supervisor_phone() -> str | None:
    """The transfer number when only one supervisor can take calls (a fixed number is safer than one the agent
    picks); None with several (the agent dials the number request_supervisor gave it)."""
    from app import supervisor

    phones = {p["phone"] for p in supervisor.load()["people"] if p.get("phone")}
    return phones.pop() if len(phones) == 1 else None


async def public_url() -> str:
    url = (os.getenv("PHONIC_PUBLIC_URL") or "").strip().rstrip("/") or tunnel.fixed_url()
    if url:
        return url
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            return (await tunnel._tunnel_url(http) or "").rstrip("/")
    except (aiohttp.ClientError, TimeoutError):
        return ""


async def ensure_tools() -> tuple[set[str], str]:
    """Create / re-point our tools in Phonic. Returns (names ready, a note for the call log)."""
    from app.agent_config import supervisor_enabled

    secret = os.getenv("CARTESIA_TOOL_SECRET", "").strip()
    base = await public_url()
    if not secret or not base:
        return set(), "no public URL or tool secret: Phonic call without our tools (outcome from the transcript)"
    fixed_phone = _single_supervisor_phone()
    wanted = tool_bodies(base, secret, fixed_phone)
    if not supervisor_enabled():
        wanted.pop(SUPERVISOR_TOOL_NAME)
        wanted.pop(TRANSFER_TOOL_NAME)
    existing = {t.get("name"): t for t in await phonic.list_tools()}
    done, actions = set(), []
    for name, body in wanted.items():
        have = existing.get(name)
        try:
            if have is None:
                await phonic.create_tool(body)
                actions.append(f"{name} created")
            elif any(have.get(k) != v for k, v in body.items() if k not in ("endpoint_headers", "parameters")):
                await phonic.update_tool(name, {k: v for k, v in body.items() if k not in ("name", "type")})
                actions.append(f"{name} updated")
            else:
                # Headers aren't returned in full; re-send them in case the secret changed.
                if "endpoint_headers" in body:
                    await phonic.update_tool(name, {"endpoint_headers": body["endpoint_headers"]})
            done.add(name)
        except phonic.PhonicError as e:
            logger.warning("Phonic tool %s: %s", name, e)
            actions.append(f"{name} failed ({e})")
    # PHONIC_TEST_ONLY guards the number we dial, not a transfer target the agent picks itself: in test-only mode
    # the transfer (and so the hand-off) is only offered with one fixed supervisor number.
    if phonic.test_only() and not fixed_phone and TRANSFER_TOOL_NAME in done:
        done.discard(TRANSFER_TOOL_NAME)
        actions.append("transfer not offered (test only: no single fixed supervisor number)")
    return done, ", ".join(actions) or "tools up to date"


def build_call_config(s, variables: dict, max_minutes: int, tools: set[str], options: dict) -> dict:
    """The per-call Phonic config (POST .../sip/outbound_call `config`)."""
    supervisor_on = SUPERVISOR_TOOL_NAME in tools and TRANSFER_TOOL_NAME in tools
    outcome_on = OUTCOME_TOOL_NAME in tools
    email_on = outcome_on and EMAIL_TOOL_NAME in tools and s.emails.enabled
    prompt = build_instructions(s, outcome=outcome_on, supervisor=supervisor_on, email=email_on,
                                end_tool=END_TOOL, voicemail_tool=False)
    q, start = s.silence, s.call_start
    if start.greet_if_silent:  # Phonic's "poke" = our silent greeting ("Hello?") to a callee who stays quiet
        poke_s, poke_text = start.wait_seconds, variables.get("silent_greeting") or "Hello?"
    elif q.enabled:
        poke_s, poke_text = q.silence_check_seconds, s.fill(q.check_message)
    else:
        poke_s, poke_text = None, ""
    custom = [n for n in (OUTCOME_TOOL_NAME if outcome_on else "", EMAIL_TOOL_NAME if email_on else "",
                          SUPERVISOR_TOOL_NAME if supervisor_on else "", TRANSFER_TOOL_NAME if supervisor_on else "") if n]
    config = {
        "system_prompt": prompt,
        "template_variables": {k: "" if v is None else str(v) for k, v in variables.items()},
        "welcome_message": None,  # the callee speaks first; the poke covers a silent line
        "generate_welcome_message": False,
        "voice_id": options["voice_id"],
        "audio_speed": options["audio_speed"],
        "background_noise": options["background_noise"] or None,
        "background_noise_level": options["background_noise_level"] if options["background_noise"] else 0,
        "default_language": "en",
        "boosted_keywords": [k for k in (s.company_name, s.caller_name) if k],
        "no_input_poke_sec": poke_s,
        "no_input_end_conversation_sec": q.max_silence_seconds + 5 if q.enabled else 180,
        "max_conversation_duration_sec": max(60, int(max_minutes) * 60),
        "tools": [END_TOOL, *custom],
    }
    if poke_s:
        config["no_input_poke_text"] = poke_text
    return config


def normalize(conv: dict) -> dict:
    """A Phonic conversation -> the call shape calls.ingest_call reads (providers/base.py).
    The newest turn may still be growing while the call is live, so it's held back until a later one exists."""
    started, ended = _parse(conv.get("started_at")), _parse(conv.get("ended_at"))
    ended_by = conv.get("ended_by") or ""
    reason = ENDED_BY.get(ended_by, ended_by) if ended else None
    status = ("failed" if reason in FAILED_ENDINGS else "completed") if ended else "started" if started else None
    items = sorted((i for i in conv.get("items") or [] if isinstance(i, dict)), key=lambda i: i.get("item_idx") or 0)
    if not ended:
        items = items[:-1]
    turns = []
    for it in items:
        at = _parse(it.get("started_at"))
        start = round((at - started).total_seconds(), 2) if at and started else None
        dur = it.get("duration_ms")
        tools = []
        for tc in it.get("tool_calls") or []:
            name = (tc.get("tool") or {}).get("name") or ""
            result = tc.get("response_body")
            tools.append({"id": tc.get("id"), "name": TOOL_NAMES.get(name, name), "arguments": tc.get("request_body"),
                          "result": result if isinstance(result, str) or result is None else json.dumps(result)[:2000]})
        turns.append({"role": it.get("role"), "text": it.get("live_transcript") or it.get("post_call_transcript") or "",
                      "start_timestamp": start,
                      "end_timestamp": round(start + dur / 1000, 2) if start is not None and isinstance(dur, (int, float)) else None,
                      "tool_calls": tools})
    return {"id": conv.get("id"), "status": status, "end_reason": reason, "error_message": None,
            "start_time": conv.get("started_at"), "end_time": conv.get("ended_at"), "summary": "",
            "ended_by": ended_by, "transcript": turns}


class PhonicProvider(VoiceProvider):
    name = "phonic"
    label = "Phonic"
    features = {
        "voicemail_detection": False,  # not offered for the called person (only on transfers); see the module notes
        "voice_speed": True,  # audio_speed
        "background_sound": True,  # Phonic's own: office / call-center / coffee-shop, with a level
        "transfer": "SIP REFER hand-off to the supervisor's number (Phonic leaves the call)",
        "summary": False,
        "webhooks": False,  # we poll the conversation (Phonic's webhooks are set in its dashboard; not needed)
    }

    def __init__(self) -> None:
        self.tools: set[str] = set()

    def owns(self, call_ref: str) -> bool:
        return (call_ref or "").startswith("conv_")

    async def prepare(self) -> dict:
        if not phonic.configured():
            raise ProviderError("PHONIC_API_KEY is not set in .env")
        try:
            self.tools, note = await ensure_tools()
        except phonic.PhonicError as e:
            self.tools, note = set(), f"couldn't set up the Phonic tools ({e}): calling without them"
        return {"action": f"Phonic: {note}", "agent_id": "phonic (per-call config)", "hash": ""}

    async def start_call(self, to_number: str, variables: dict, ringing_timeout_s: int, max_minutes: int) -> str:
        from settings import load_settings

        from app import providers

        config = build_call_config(load_settings(), variables, max_minutes, self.tools, providers.load()["phonic"])
        reply = await phonic.outbound_call(to_number, config)
        conv_id = reply.get("conversation_id")
        if not conv_id:
            raise ProviderError("Phonic accepted the request but returned no conversation", 200, reply)
        return conv_id

    async def get_call(self, call_ref: str) -> dict:
        return normalize(await phonic.get_conversation(call_ref))

    async def end_call(self, call_ref: str) -> dict:
        return await phonic.cancel(call_ref)

    async def recording(self, call_ref: str) -> tuple[bytes, str]:
        url = (await phonic.get_conversation(call_ref)).get("audio_url")
        if not url:
            return b"", "audio/wav"
        return await phonic.download(url)
