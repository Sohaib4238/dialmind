"""STOP / unsubscribe replies to our follow-up emails -> the lead goes on Do-Not-Contact (Phase 7).

The sending account's inbox is checked over IMAP every few minutes, read-only (EXAMINE + BODY.PEEK: nothing is
marked as read or moved). A reply is matched to a lead by In-Reply-To / References (our Message-ID, so it works
in test mode too) or by the sender being the lead's address. Gmail's own "Unsubscribe" button sends an email with
subject "unsubscribe" (our List-Unsubscribe header), which is caught the same way.

Off when IMAP isn't set up (IMAP_HOST, or Gmail's imap.gmail.com by default) and when EMAIL_SENDING_DISABLED=1.
"""

import asyncio
import email
import imaplib
import json
import logging
import os
import re
import ssl
import time
from datetime import date, timedelta
from email import policy
from email.utils import getaddresses, parseaddr

from sqlmodel import select

from app import leads, standby
from app.db import DATA_DIR, EmailMessage, Lead, get_session

logger = logging.getLogger("dashboard.unsubscribe")

CHECK_EVERY_S = 300
LOOKBACK_DAYS = 14
MAX_MESSAGES = 100
SEEN_PATH = DATA_DIR / "unsubscribe_seen.json"
state: dict = {"enabled": False, "detail": "Not checked yet", "last_check": None, "unsubscribed": 0}

_STOP_LINE = re.compile(
    r"^(?:please\s+)?(?:stop|unsubscribe|unsubscribe me|remove|remove me|opt[ -]?out|stop (?:emailing|sending|"
    r"contacting|e-?mailing)(?: me| us)?(?: please)?|take me off(?: your| the| this)?(?: mailing)? list|"
    r"no more emails?)(?: please)?[\s.!]*$", re.I)
_QUOTE_START = re.compile(r"^(?:on .+wrote:|-{2,}\s*original message|from:\s)", re.I)


def imap_settings() -> dict:
    smtp_host = (os.getenv("SMTP_HOST") or "").strip().lower()
    host = (os.getenv("IMAP_HOST") or "").strip() or ("imap.gmail.com" if smtp_host == "smtp.gmail.com" else "")
    port = (os.getenv("IMAP_PORT") or "993").strip()
    return {"host": host, "port": int(port) if port.isdigit() else 993,
            "user": (os.getenv("SMTP_USER") or "").strip(), "password": os.getenv("SMTP_PASSWORD") or ""}


def enabled() -> tuple[bool, str]:
    if standby.on():
        return False, "Off (standby: the live backend reads the inbox)"
    if (os.getenv("EMAIL_SENDING_DISABLED") or "").strip() == "1":
        return False, "Off (EMAIL_SENDING_DISABLED=1)"
    c = imap_settings()
    if not c["host"] or not c["user"] or not c["password"]:
        return False, "Not set up: STOP replies aren't read automatically (set IMAP_HOST, or use Gmail)"
    return True, f"Checking {c['user']} on {c['host']} every {CHECK_EVERY_S // 60} minutes"


# ---- reading a reply -------------------------------------------------------------------------

def reply_text(msg) -> str:
    """The new text of a reply (no quoted original below it)."""
    part = msg.get_body(preferencelist=("plain", "html")) if msg.is_multipart() else msg
    try:
        text = part.get_content() if part is not None else ""
    except (KeyError, LookupError):
        text = ""
    if part is not None and part.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", "\n", text)
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith(">") or _QUOTE_START.match(s):
            break
        out.append(s)
    return "\n".join(out).strip()


def is_stop(msg) -> str:
    """Why this email is an unsubscribe request ("" if it isn't)."""
    subject = re.sub(r"^(?:\s*(?:re|fwd?|aw)\s*:\s*)+", "", str(msg.get("Subject") or ""), flags=re.I).strip()
    if _STOP_LINE.match(subject) or re.search(r"\bunsubscribe\b", subject, re.I):
        return f"subject \"{subject[:40]}\""
    lines = [x for x in reply_text(msg).split("\n") if x][:3]
    for line in lines:
        if _STOP_LINE.match(line):
            return f"replied \"{line[:40]}\""
    return ""


def _find_lead(msg) -> int | None:
    refs = " ".join(str(msg.get(h) or "") for h in ("In-Reply-To", "References"))
    ids = re.findall(r"<[^<>\s]+>", refs)
    with get_session() as s:
        if ids:
            hit = s.exec(select(EmailMessage).where(EmailMessage.message_id.in_(ids))).first()
            if hit:
                return hit.lead_id
        sender = parseaddr(str(msg.get("From") or ""))[1].strip().lower()
        if sender:
            lead = s.exec(select(Lead).where(Lead.email == sender)).first()
            if lead:
                return lead.id
    return None


def _load_seen() -> list[str]:
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def process(messages: list) -> list[dict]:
    """Apply unsubscribe requests from these emails (email.message objects). Each is handled once."""
    seen = _load_seen()
    seen_set, done = set(seen), []
    for msg in messages:
        key = str(msg.get("Message-ID") or "") or f"{msg.get('From')}|{msg.get('Date')}|{msg.get('Subject')}"
        if key in seen_set:
            continue
        seen.append(key)
        seen_set.add(key)
        why = is_stop(msg)
        if not why:
            continue
        lead_id = _find_lead(msg)
        if lead_id is None:
            logger.info("unsubscribe request from %s: no matching lead", getaddresses([str(msg.get("From") or "")]))
            continue
        try:
            lead = leads.set_dnc(lead_id, True, f"Email: {why}")
        except leads.LeadError:
            continue
        done.append({"lead_id": lead_id, "why": why, "name": lead.get("name", "")})
        logger.info("lead %s unsubscribed by email (%s)", lead_id, why)
    SEEN_PATH.write_text(json.dumps(seen[-5000:]), encoding="utf-8")
    return done


# ---- the inbox -------------------------------------------------------------------------------

def fetch() -> list:
    """Recent emails that might be unsubscribe requests (read-only)."""
    c = imap_settings()
    since = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    query = f'(SINCE {since} OR OR SUBJECT "unsubscribe" BODY "unsubscribe" OR BODY "stop" BODY "remove")'
    out = []
    with imaplib.IMAP4_SSL(c["host"], c["port"], ssl_context=ssl.create_default_context(), timeout=30) as m:
        m.login(c["user"], c["password"])
        m.select("INBOX", readonly=True)
        typ, data = m.search(None, query)
        ids = (data[0] or b"").split()[-MAX_MESSAGES:] if typ == "OK" else []
        for i in ids:
            typ, parts = m.fetch(i, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if typ == "OK" and raw:
                out.append(email.message_from_bytes(raw, policy=policy.default))
    return out


def check_now() -> dict:
    on, detail = enabled()
    state.update(enabled=on, detail=detail)
    if not on:
        return state
    try:
        done = process(fetch())
        state["unsubscribed"] += len(done)
        state.update(last_check=time.time(), detail=detail, last_done=done)
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
        msg = str(e)[:150]
        if "AUTHENTICATIONFAILED" in msg.upper() or "invalid credentials" in msg.lower():
            msg = "the inbox refused the login (Gmail: use the app password, and IMAP must be on)"
        state.update(last_check=time.time(), detail=f"Couldn't check the inbox: {msg}")
        logger.warning("unsubscribe check: %s", msg)
    return state


async def watch() -> None:
    while True:
        try:
            await asyncio.to_thread(check_now)
        except Exception:  # noqa: BLE001
            logger.exception("unsubscribe check crashed")
        await asyncio.sleep(CHECK_EVERY_S)
