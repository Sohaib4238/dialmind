"""DIALER_STANDBY: the server before the switch-over must not touch the shared Cartesia / Telnyx setup, and must not
call, text, email or read the inbox. Fakes only; each guard is also checked with standby OFF, so it can't pass by accident."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import smtplib
import urllib.request
from email.message import EmailMessage

os.environ["DIALER_STANDBY"] = "1"
os.environ["DASHBOARD_USERNAME"] = "boss"
os.environ["DASHBOARD_SESSION_SECRET"] = "test-session-secret"

import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, cartesia, cartesia_setup, emails, health, sms, standby, tunnel, unsubscribe  # noqa: E402

os.environ["DASHBOARD_PASSWORD_HASH"] = auth.hash_password("a-long-password")


async def no_tunnel():
    return None

tunnel.watch = no_tunnel
from app.main import app  # noqa: E402

REMOTE = {"x-forwarded-for": "203.0.113.9", "x-forwarded-host": "dialer.example"}  # what Caddy adds


def refused(coro) -> str:
    try:
        asyncio.run(coro)
    except cartesia.CartesiaError as e:
        return str(e)
    raise AssertionError("expected a CartesiaError")


# the switch itself
for value, want in (("", False), ("0", False), ("1", True), ("yes", True)):
    os.environ["DIALER_STANDBY"] = value
    assert standby.on() is want
del os.environ["DIALER_STANDBY"]
assert standby.on() is False
os.environ["DIALER_STANDBY"] = "1"
print("switch: on for anything but empty / missing / 0")

# Cartesia: every kind of write is stopped before the network; a GET still goes out
os.environ["CARTESIA_CALLS_DISABLED"] = "0"
writes = [cartesia.create_call("a", "f", os.environ["TEST_CALL_NUMBER"], {}, 30, 2), cartesia.cancel_call("ac_x"),
          cartesia.update_agent_config("a", {}, "x"), cartesia.create_agent("n", "d", {}),
          cartesia.attach_webhook("a", "w"), cartesia.create_webhook("https://x", "s", "n"),
          cartesia.update_webhook("w", "https://x"), cartesia.create_tool({}), cartesia.update_tool("t", {}),
          cartesia.import_number("l", "+1", "p"), cartesia.request("DELETE", "/agents/phone-numbers/x"),
          cartesia_setup.sync_agent("test")]
for w in writes:
    assert "Standby" in refused(w)
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
assert "Standby" not in refused(cartesia.get_agent("a"))  # stopped only by the tests' network guard
print(f"cartesia: {len(writes)} kinds of write refused, GET passes the guard")

# The tunnel watcher re-points nothing, even with a tunnel running
seen = []


async def fake_url(http, metrics_url=""):
    return "https://stolen.trycloudflare.com"


def fake_ensure(name):
    async def ensure(url):
        seen.append(name)
        return {"url": url, "action": "unchanged", "tool_id": "t", "attached": True}
    return ensure


async def fake_ping(http, base):
    return True

tunnel._tunnel_url, tunnel._public_ping = fake_url, fake_ping
cartesia_setup.ensure_webhook = fake_ensure("webhook")
cartesia_setup.ensure_outcome_tool = fake_ensure("record_outcome")
cartesia_setup.ensure_supervisor_tool = fake_ensure("request_supervisor")
cartesia_setup.ensure_email_tool = fake_ensure("collect_email")
sms.ensure_webhook = lambda url: seen.append("messaging") or {"detail": "fake"}

asyncio.run(tunnel.check_once(None))
assert seen == [] and tunnel.state["public_url"] is None and tunnel.state["checked_at"]
link = tunnel.link_status()
assert link["standby"] is True and link["online"] is False and "laptop" in link["detail"]
check = asyncio.run(health.check_link())
assert check["ok"] is True and check["warn"] is True
print("tunnel: nothing re-pointed in standby; Health shows it as a warning, not a failure")

del os.environ["DIALER_STANDBY"]
asyncio.run(tunnel.check_once(None))
assert seen == ["webhook", "record_outcome", "request_supervisor", "collect_email", "messaging"], seen
assert "standby" not in tunnel.link_status()
os.environ["DIALER_STANDBY"] = "1"
tunnel.state.update(public_url=None, webhook=None)
print("tunnel: the same check with standby off re-points all five (so the guard is what stopped it)")

# Telnyx: no write leaves (texts, messaging profile)
opened = []
real_urlopen = urllib.request.urlopen
urllib.request.urlopen = lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(OSError("fake"))
for path, method in (("/messages", "POST"), ("/messaging_profiles/p", "PATCH")):
    try:
        sms._post(path, {}, method=method)
    except sms.SmsError as e:
        assert "standby" in str(e)
    else:
        raise AssertionError("expected an SmsError")
assert opened == []
del os.environ["DIALER_STANDBY"]
try:
    sms._post("/messages", {})
except sms.SmsError:
    pass
assert len(opened) == 1
os.environ["DIALER_STANDBY"] = "1"
urllib.request.urlopen = real_urlopen
print("telnyx: no write leaves in standby; with standby off it does")

# Email: SMTP is never opened
connected = []


class FakeSMTP:
    def __init__(self, *a, **k):
        connected.append(a)
        raise OSError("fake")

smtplib.SMTP = smtplib.SMTP_SSL = FakeSMTP
c = {"host": "smtp.example", "port": 587, "security": "starttls", "user": "u", "password": "p"}
try:
    emails._smtp_send(EmailMessage(), c)
except emails.EmailError as e:
    assert "standby" in str(e)
else:
    raise AssertionError("expected an EmailError")
assert connected == []
print("email: SMTP never opened")

# Inbox: not read, even when IMAP is set up
os.environ.update(IMAP_HOST="imap.example", SMTP_USER="u@example.com", SMTP_PASSWORD="p", EMAIL_SENDING_DISABLED="0")
on, detail = unsubscribe.enabled()
assert on is False and "standby" in detail
del os.environ["DIALER_STANDBY"]
assert unsubscribe.enabled()[0] is True
os.environ["DIALER_STANDBY"] = "1"
os.environ["EMAIL_SENDING_DISABLED"] = "1"
print("inbox: not read in standby")

# Through the app: the login still guards everything, the checks may run, changes stay refused
async def fake_checks():
    return [{"name": "fake", "ok": True, "detail": "", "ms": 0}]

health.run_all = fake_checks
with TestClient(app) as cl:
    assert cl.get("/api/health", headers=REMOTE).status_code == 401
    assert cl.post("/api/health/check", headers=REMOTE).status_code == 401
    assert cl.post("/api/auth/login", headers=REMOTE, json={"username": "boss", "password": "a-long-password"}).status_code == 200
    cookies = {auth.COOKIE: auth.make_session("boss")}
    h = {**REMOTE, "cookie": f"{auth.COOKIE}={cookies[auth.COOKIE]}"}
    assert cl.get("/api/health", headers=h).json()["link"]["standby"] is True
    assert cl.post("/api/health/check", headers=h).json()["checks"][0]["name"] == "fake"
    assert cl.get("/api/test-mode", headers=h).json()["on"] is True
    assert cl.get("/api/dialer", headers=h).json()["state"] == "off"
    for method, path, body in (("POST", "/api/calls", {"number": "+15551234567"}), ("POST", "/api/dialer/on", None),
                               ("POST", "/api/dialer/test-mode", {"enabled": False, "confirm": True}),
                               ("PUT", "/api/settings", {"values": {}}), ("POST", "/api/sms/test", None),
                               ("POST", "/api/emails/test", None), ("POST", "/api/leads/import", {"csv": ""})):
        assert cl.request(method, path, headers=h, json=body).status_code == 403, path
    # Even a request from inside (no proxy headers) can't place a call or change the agent
    r = cl.post("/api/calls", json={"number": os.environ["TEST_CALL_NUMBER"]})
    assert r.status_code == 400, r.text
    assert cl.get("/api/settings").json()["sync"]["detail"].startswith("Standby")
print("app: login required, Health works, calls / dialer / settings / test sends refused")

print("ALL OK")
