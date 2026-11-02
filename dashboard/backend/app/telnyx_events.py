"""Telnyx call events for our SIP connection (optional; off until set up in the Telnyx portal).

Why: Cartesia ignores the BYE Telnyx sends when the callee hangs up, so the call stays open on
Cartesia until a timeout. Telnyx can POST call events (Call Initiated / Answered / Bridged /
Hangup) for a SIP connection to a webhook URL. On call.hangup we cancel the Cartesia call.

Security: Telnyx signs every webhook (Ed25519 over "{telnyx-timestamp}|{raw body}"). We verify it
with TELNYX_PUBLIC_KEY (Mission Control Portal -> Keys & Credentials -> Public Key) and reject
anything older than 5 minutes. Without the key, the endpoint refuses everything.
"""

import base64
import json
import logging
import os
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.leads import normalize_us_phone

logger = logging.getLogger("dashboard.telnyx")

MAX_AGE_S = 300
_seen: dict[str, float] = {}


def verify(raw: bytes, signature_b64: str, timestamp: str) -> tuple[bool, str]:
    key_b64 = (os.getenv("TELNYX_PUBLIC_KEY") or "").strip()
    if not key_b64:
        return False, "Telnyx webhooks are off (TELNYX_PUBLIC_KEY not set)"
    try:
        ts = int(timestamp)
    except ValueError:
        return False, "Missing telnyx-timestamp"
    if abs(time.time() - ts) > MAX_AGE_S:
        return False, "Webhook too old"
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64))
        key.verify(base64.b64decode(signature_b64), f"{timestamp}|".encode() + raw)
    except (InvalidSignature, ValueError):
        return False, "Bad Telnyx signature"
    return True, ""


def handle(raw: bytes, manager) -> dict:
    """Process a verified Telnyx event. Only call.hangup triggers anything."""
    try:
        data = json.loads(raw).get("data") or {}
    except ValueError:
        return {"ok": True, "ignored": "not JSON"}
    event_id = data.get("id") or ""
    now = time.time()
    for k in [k for k, t in _seen.items() if now - t > 3600]:
        _seen.pop(k, None)
    if event_id and event_id in _seen:
        return {"ok": True, "duplicate": True}
    if event_id:
        _seen[event_id] = now
    kind = data.get("event_type") or ""
    p = data.get("payload") or {}
    logger.info("telnyx %s to=%s from=%s cause=%s source=%s", kind, p.get("to"), p.get("from"),
                p.get("hangup_cause"), p.get("hangup_source"))
    if kind != "call.hangup":
        return {"ok": True, "type": kind}
    # Our calls go out from our Telnyx number to the lead: the lead is "to".
    to = str(p.get("to") or "")
    if to.lower().startswith(("sip:", "sips:")):  # sip:+17865550100@sip.telnyx.com -> +17865550100
        to = to.split(":", 1)[1].split("@", 1)[0]
    number, _ = normalize_us_phone(to)
    if not number:
        return {"ok": True, "ignored": f"unrecognised number {to!r}", "type": kind}
    detail = (f"Telnyx call.hangup: cause {p.get('hangup_cause')}, source {p.get('hangup_source')}, "
              f"SIP {p.get('sip_hangup_cause')}")
    return {**manager.callee_hung_up(number, detail), "type": kind}
