"""Logins with roles: admin (everything) and supervisor (view, pop-up, available). Fakes only; no call is placed."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import json
import os
import stat
import subprocess
import sys

os.environ.update(DASHBOARD_USERNAME="", DASHBOARD_PASSWORD_HASH="", DASHBOARD_SESSION_SECRET="test-session-secret",
                  CARTESIA_CALLS_DISABLED="1", CARTESIA_EMAIL_TOOL_ID="", TELNYX_MESSAGING_PROFILE_ID="")

import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, tunnel  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel
from app.main import app  # noqa: E402

PROXY = {"x-forwarded-for": "203.0.113.20", "x-forwarded-host": "dialer.example"}  # what Caddy adds
MANAGE = [sys.executable, str(REPO / "dashboard" / "backend" / "manage_users.py")]
PASSWORD = {"boss": "admin-password-123", "sup": "supervisor-pass-456"}


def manage(*args, password=None):
    return subprocess.run(MANAGE + list(args), input=password, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})


# ---- the command --------------------------------------------------------------------------
r = manage("set", "boss", "--role", "admin", "--password-stdin", password=PASSWORD["boss"] + "\n")
assert r.returncode == 0 and "Created 'boss' (admin)" in r.stdout, r.stdout + r.stderr
r = manage("set", "sup", "--role", "supervisor", "--password-stdin", password=PASSWORD["sup"] + "\n")
assert r.returncode == 0, r.stderr
raw = auth.users_path().read_text(encoding="utf-8")
for pw in PASSWORD.values():
    assert pw not in raw
data = json.loads(raw)
assert data["boss"]["role"] == "admin" and data["boss"]["hash"].startswith("pbkdf2_sha256$")
if os.name == "posix":
    assert stat.S_IMODE(auth.users_path().stat().st_mode) == 0o600
assert auth.users_path().parent == env_utils.ENV_PATH.parent
out = manage("list").stdout
assert "boss" in out and "admin" in out and "pbkdf2" not in out and "sup" in out, out
assert PASSWORD["boss"] not in r.stdout + r.stderr
for bad in (("set", "x", "--role", "admin", "--password-stdin"), ("set", "bad name", "--role", "admin", "--password-stdin")):
    r = manage(*bad, password="short\n" if bad[1] == "x" else "long-enough-password\n")
    assert r.returncode != 0 and "x" not in json.loads(auth.users_path().read_text(encoding="utf-8")), r.stdout
assert manage("set", "boss", "--role", "owner", "--password-stdin", password="long-enough-password\n").returncode != 0
assert manage("remove", "nobody").returncode != 0
print("command: creates users, hash only, too-short / bad name / bad role refused, password never printed")


def login(c, name, pw=None):
    r = c.post("/api/auth/login", headers=PROXY, json={"username": name, "password": pw or PASSWORD[name]})
    assert r.status_code == 200, r.text
    token = r.cookies[auth.COOKIE]
    c.cookies.clear()
    return {**PROXY, "Cookie": f"{auth.COOKIE}={token}"}


ADMIN_ONLY = [("post", "/api/dialer/on", None), ("post", "/api/dialer/off", None), ("post", "/api/calls", {"number": "+15125550100"}),
              ("post", "/api/calls/1/hangup", None), ("put", "/api/supervisor", {"phone": "+15125550100"}),
              ("put", "/api/settings", {"values": {}, "version": ""}), ("put", "/api/voice", {"voice": {}, "version": ""}),
              ("post", "/api/voice/sounds?filename=x.mp3", None),
              ("post", "/api/leads/1/dnc", {"dnc": True}), ("post", "/api/dnc", {"phone": "+15125550100"}),
              ("post", "/api/leads/1/call", None), ("post", "/api/leads/1/outcome", {"outcome": "x"}),
              ("post", "/api/leads/import", {"csv": ""}), ("post", "/api/dialer/test-mode", {"enabled": True}),
              ("post", "/api/sms/test", None), ("post", "/api/emails/test", None)]

with TestClient(app) as c:
    # ---- no login
    assert c.get("/api/leads", headers=PROXY).status_code == 401
    me = c.get("/api/auth/me", headers=PROXY).json()
    assert me["user"] is None and me["role"] is None
    # Local is still the admin on the laptop
    assert c.get("/api/auth/me").json() == {"remote": False, "configured": True, "user": "local", "role": "admin"}

    # ---- supervisor
    sup = login(c, "sup")
    assert c.get("/api/auth/me", headers=sup).json()["role"] == "supervisor"
    for path in ("/api/leads", "/api/calls", "/api/health", "/api/supervisor", "/api/supervisor/requests", "/api/dialer", "/api/settings"):
        assert c.get(path, headers=sup).status_code == 200, path
    assert c.post("/api/supervisor/availability", headers=sup, json={"available": True}).json()["available"] is True
    assert c.post("/api/supervisor/requests/999/answer", headers=sup, json={"accept": True}).status_code == 409  # allowed, no such request
    for method, path, body in ADMIN_ONLY:
        r = getattr(c, method)(path, headers=sup, json=body)
        assert r.status_code == 403 and "admin" in r.json()["detail"], (path, r.status_code)
    print("supervisor: can view, answer, flip available; all admin actions 403")

    # ---- admin: past the role check everywhere (each gets the route's own answer, never 401/403)
    boss = login(c, "boss")
    assert c.get("/api/auth/me", headers=boss).json()["role"] == "admin"
    for method, path, body in ADMIN_ONLY:
        if path in ("/api/sms/test", "/api/emails/test"):
            continue  # reach their kill switches; checked in test_sms / test_emails
        r = getattr(c, method)(path, headers=boss, json=body)
        assert r.status_code not in (401, 403), (path, r.status_code, r.text)
    assert c.post("/api/dialer/off", headers=boss).json()["state"] == "off"
    assert c.post("/api/dev/simulate", headers=boss, json={"number": "+1"}).status_code == 403  # dev routes: nobody remote
    print("admin: every action passes the role check; dev routes stay closed")

    # ---- changes work at once
    data = json.loads(auth.users_path().read_text(encoding="utf-8"))
    data["sup"]["role"] = "admin"
    auth.write_users_file(data)
    assert c.get("/api/auth/me", headers=sup).json()["role"] == "admin"
    data["sup"]["role"] = "supervisor"
    auth.write_users_file(data)
    assert c.post("/api/dialer/off", headers=sup).status_code == 403
    # Reset: old sessions and the old password stop working
    old_boss = boss
    assert manage("set", "boss", "--role", "admin", "--password-stdin", password="brand-new-password\n").returncode == 0
    assert c.get("/api/leads", headers=old_boss).status_code == 401
    assert c.post("/api/auth/login", headers=PROXY, json={"username": "boss", "password": PASSWORD["boss"]}).status_code == 401
    login(c, "boss", "brand-new-password")
    # Remove: the session dies
    assert manage("remove", "sup").returncode == 0
    assert c.get("/api/leads", headers=sup).status_code == 401
    r = manage("set", "sup", "--role", "supervisor", "--password-stdin", password=PASSWORD["sup"] + "\n")
    print("role change works at once; a reset logs out old sessions; removing a user ends their session")

    # ---- the old single login: still works, never more than a supervisor
    os.environ.update(DASHBOARD_USERNAME="oldlogin", DASHBOARD_PASSWORD_HASH=auth.hash_password("old-login-password"))
    old = login(c, "oldlogin", "old-login-password")
    assert c.get("/api/auth/me", headers=old).json()["role"] == "supervisor"
    assert c.post("/api/dialer/on", headers=old).status_code == 403
    assert "old single login" in manage("list").stdout
    # The file wins over it for the same name
    assert manage("set", "oldlogin", "--role", "admin", "--password-stdin", password="file-password-789\n").returncode == 0
    assert c.post("/api/auth/login", headers=PROXY, json={"username": "oldlogin", "password": "old-login-password"}).status_code == 401
    os.environ.update(DASHBOARD_USERNAME="", DASHBOARD_PASSWORD_HASH="")
    assert manage("remove", "oldlogin").returncode == 0
    print("old single login: works as a supervisor, the users file wins")

    # ---- broken users file = nobody
    good = auth.users_path().read_text(encoding="utf-8")
    auth.users_path().write_text("{not json", encoding="utf-8")
    assert c.get("/api/leads", headers=sup).status_code == 404 and auth.users() == {}
    auth.users_path().write_text(json.dumps({"x": {"role": "root", "hash": "h"}}), encoding="utf-8")
    assert auth.users() == {}
    auth.users_path().write_text(good, encoding="utf-8")
    print("broken file or unknown role: nobody gets in")

    # ---- lockouts: per IP, and per user across many IPs
    auth._failures.clear()
    for i in range(5):
        hdr = {**PROXY, "x-forwarded-for": "198.51.100.77"}
        assert c.post("/api/auth/login", headers=hdr, json={"username": "boss", "password": "nope"}).status_code == 401
    assert c.post("/api/auth/login", headers=hdr, json={"username": "boss", "password": "brand-new-password"}).status_code == 429
    auth._failures.clear()
    for i in range(auth.USER_MAX_FAILURES):
        hdr = {**PROXY, "x-forwarded-for": f"198.51.{i}.1"}
        assert c.post("/api/auth/login", headers=hdr, json={"username": "boss", "password": "nope"}).status_code == 401
    hdr = {**PROXY, "x-forwarded-for": "192.0.2.200"}
    assert c.post("/api/auth/login", headers=hdr, json={"username": "boss", "password": "brand-new-password"}).status_code == 429
    assert c.post("/api/auth/login", headers=hdr, json={"username": "sup", "password": PASSWORD["sup"]}).status_code == 200  # others unaffected
    # Made-up names aren't remembered
    auth._failures.clear()
    c.post("/api/auth/login", headers=hdr, json={"username": "ghost", "password": "nope"})
    assert not any(k.startswith("user:ghost") and v for k, v in auth._failures.items())
    auth._failures.clear()
    c.cookies.clear()
    print("lockout: 5 per IP, 20 per user from many IPs; made-up names aren't stored")

    # ---- the server: no request counts as "this computer"
    os.environ["DIALER_REQUIRE_LOGIN"] = "1"
    assert c.get("/api/leads").status_code == 401
    assert c.post("/api/calls", json={"number": "+15125550100"}).status_code == 401
    assert c.post("/api/dialer/on").status_code == 401
    assert c.get("/api/public/ping").status_code == 200
    assert c.post("/api/cartesia/tools/record_outcome", json={}).status_code == 401  # its own secret
    assert c.get("/api/auth/me").json()["remote"] is True
    from starlette.websockets import WebSocketDisconnect
    try:
        with c.websocket_connect("/api/live") as ws:
            ws.receive_json()
        raise AssertionError("WebSocket accepted without a login")
    except WebSocketDisconnect as e:
        assert e.code == 1008
    os.environ["DIALER_REQUIRE_LOGIN"] = "0"
    assert c.get("/api/leads").status_code == 200
    print("DIALER_REQUIRE_LOGIN: no proxy headers no longer means trusted")

print("ALL ROLE TESTS PASS")
