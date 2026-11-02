"""PUBLIC_BASE_URL: one fixed address for the Cartesia webhook, the tools and the Telnyx messaging webhook.
Fakes only. Also: the SMS webhook accepts our domain, and rollback to the laptop's tunnel works."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os

DOMAIN = "https://dialer.example.com"
os.environ["PUBLIC_BASE_URL"] = DOMAIN + "/"
os.environ["DIALER_STANDBY"] = "1"
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = "profile-1"
os.environ["TELNYX_API_KEY"] = "fake"

import env_utils  # noqa: E402,F401

from app import cartesia, cartesia_setup, sms, tunnel  # noqa: E402

pointed = []
fail_next = {}
metrics_reads = []


async def fake_url(http, metrics_url=""):
    metrics_reads.append(metrics_url)
    return "https://random.trycloudflare.com"


def fake_ensure(name):
    async def ensure(url):
        if fail_next.pop(name, False):
            raise cartesia.CartesiaError("fake failure")
        pointed.append((name, url))
        return {"url": url, "action": "updated", "tool_id": "t", "attached": True}
    return ensure


async def fake_ping(http, base):
    assert base == DOMAIN
    return True

tunnel._tunnel_url, tunnel._public_ping = fake_url, fake_ping
cartesia_setup.ensure_webhook = fake_ensure("webhook")
cartesia_setup.ensure_outcome_tool = fake_ensure("record_outcome")
cartesia_setup.ensure_supervisor_tool = fake_ensure("request_supervisor")
cartesia_setup.ensure_email_tool = fake_ensure("collect_email")
real_ensure_webhook = sms.ensure_webhook
sms.ensure_webhook = lambda url: pointed.append(("messaging", url)) or {"detail": "fake", "ok": True}

# Standby: the address is known, nothing moves
asyncio.run(tunnel.check_once(None))
assert pointed == [] and metrics_reads == []
assert tunnel.state["dashboard_url"] == DOMAIN and tunnel.state["public_url"] is None
assert tunnel.link_status()["standby"] is True
print("standby: nothing moves, even with the address set")

# THE switch: standby off -> all five at the domain, once
del os.environ["DIALER_STANDBY"]
asyncio.run(tunnel.check_once(None))
assert [p[0] for p in pointed] == ["webhook", "record_outcome", "request_supervisor", "collect_email", "messaging"], pointed
assert all(p[1] == DOMAIN for p in pointed), pointed
assert metrics_reads == []  # no quick tunnel involved
s = tunnel.link_status()
assert s["online"] is True and s["public_url"] == DOMAIN and "standby" not in s
print("switch: standby off points all five at the domain, no tunnel read")

asyncio.run(tunnel.check_once(None))
asyncio.run(tunnel.check_once(None))
assert len(pointed) == 5
print("then: exactly once, not on every check")

# A failure is retried (a fixed URL never changes, so nothing else would)
pointed.clear()
tunnel.state["public_url"] = None
fail_next["record_outcome"] = True
asyncio.run(tunnel.check_once(None))
assert "record_outcome" not in [p[0] for p in pointed] and tunnel.state["tool"] is None and "record_outcome" in tunnel.state["error"]
pointed.clear()
asyncio.run(tunnel.check_once(None))
assert "record_outcome" in [p[0] for p in pointed]
pointed.clear()
asyncio.run(tunnel.check_once(None))
assert pointed == []
print("a failed re-point is retried on the next check, then stops")

# Laptop (no PUBLIC_BASE_URL): the quick tunnel as before
del os.environ["PUBLIC_BASE_URL"]
tunnel.state.update(public_url=None, webhook=None)
tunnel._public_ping = lambda http, base: asyncio.sleep(0, True)
asyncio.run(tunnel.check_once(None))
assert tunnel.state["public_url"] == "https://random.trycloudflare.com" and len(metrics_reads) == 2
assert pointed and all(p[1] == "https://random.trycloudflare.com" for p in pointed)
print("laptop: quick tunnel unchanged when no fixed address is set")

# ---- the Telnyx messaging webhook ----------------------------------------------------------
sms.ensure_webhook = real_ensure_webhook
current = {"url": ""}
patches = []
sms._get = lambda path: {"data": {"name": "DialMind", "webhook_url": current["url"]}}


def fake_post(path, body, method="POST"):
    patches.append(body["webhook_url"])
    current["url"] = body["webhook_url"]
    return {}

sms._post = fake_post
TUNNEL = "https://abc.trycloudflare.com"
HOOK = "/api/telnyx/messaging"

os.environ["PUBLIC_BASE_URL"] = DOMAIN
for start, target, why in (("", DOMAIN, "empty"), (TUNNEL + HOOK, DOMAIN, "laptop tunnel -> domain (the switch)"),
                           ("https://203-0-113-10.sslip.io" + HOOK, DOMAIN, "temporary host -> domain"),
                           (DOMAIN + HOOK, TUNNEL, "domain -> laptop tunnel (rollback)")):
    current["url"], patches[:] = start, []
    r = sms.ensure_webhook(target)
    assert r["ok"] is True and patches == [target + HOOK], (why, r, patches)
print("messaging webhook: accepts the domain, and the laptop can take it back")

del os.environ["PUBLIC_BASE_URL"]  # the laptop has no PUBLIC_BASE_URL, and rollback must still work
current["url"], patches[:] = DOMAIN + HOOK, []
assert sms.ensure_webhook(TUNNEL)["ok"] is True and patches == [TUNNEL + HOOK]

current["url"], patches[:] = DOMAIN + HOOK, []
assert sms.ensure_webhook(DOMAIN)["action"] == "unchanged" and patches == []

for other in ("https://other-app.example.com/hook", "https://evil.example/x.trycloudflare.com/hook",
              "https://dialer.example.com.evil.example/hook", "https://trycloudflare.com.evil.example/hook"):
    current["url"], patches[:] = other, []
    r = sms.ensure_webhook(DOMAIN)
    assert r["ok"] is False and "another app" in r["detail"] and patches == [], other
print("messaging webhook: another app's URL (and look-alikes) is never touched")

print("ALL FIXED URL TESTS PASS")
