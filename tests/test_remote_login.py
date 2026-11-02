"""Phase 6: login for remote use (supervisor's laptop). Throwaway DB, no calls, the real .env is never written."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import sys

DB = f"{TMP}/remote_login_test.db"
SUP = f"{TMP}/remote_login_sup.json"
for f in (DB, SUP):
    if os.path.exists(f):
        os.remove(f)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["SUPERVISOR_SETTINGS_PATH"] = SUP
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, tunnel  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel
from app.main import app  # noqa: E402

os.environ.update(CARTESIA_TOOL_SECRET="t-secret", DASHBOARD_SESSION_SECRET="test-session-secret",
                  DASHBOARD_USERNAME="", DASHBOARD_PASSWORD_HASH="")
REMOTE = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "abc"}  # what cloudflared adds
PROXIED = {"x-forwarded-for": "203.0.113.8", "x-forwarded-host": "abc.trycloudflare.com"}  # Next.js /api proxy

# password hashing
h = auth.hash_password("correct horse battery")
assert h.startswith("pbkdf2_sha256$") and "correct" not in h
assert auth.check_password("correct horse battery", h) and not auth.check_password("wrong", h)
assert not auth.check_password("x", "") and not auth.check_password("x", "md5$1$a$b")
assert auth.hash_password("same", b"0" * 16) == auth.hash_password("same", b"0" * 16) != auth.hash_password("same")
print("hash: salted PBKDF2, verify OK, garbage refused")

with TestClient(app) as c:
    # local: no login, everything works
    assert c.get("/api/leads").status_code == 200 and c.get("/api/auth/me").json()["remote"] is False
    # remote with no login set up: dashboard hidden (404), tools still work with their secret
    assert c.get("/api/leads", headers=REMOTE).status_code == 404
    assert c.post("/api/cartesia/tools/request_supervisor", headers={**REMOTE, "Authorization": "Bearer t-secret"},
                  json={"call_id": "ac_x"}).status_code == 200
    assert c.post("/api/auth/login", headers=REMOTE, json={"username": "a", "password": "b"}).status_code == 400
    print("not set up: remote dashboard 404; local unchanged; tool endpoint works with its secret")

    os.environ.update(DASHBOARD_USERNAME="supervisor", DASHBOARD_PASSWORD_HASH=auth.hash_password("correct horse battery"))
    for hdr in (REMOTE, PROXIED):
        r = c.get("/api/leads", headers=hdr)
        assert r.status_code == 401 and r.json()["detail"] == "Please log in.", r.text
        assert c.get("/api/stream", headers=hdr).status_code == 401
    me = c.get("/api/auth/me", headers=REMOTE).json()
    assert me == {"remote": True, "configured": True, "user": None, "role": None}, me
    # wrong password, then lock-out after 5 tries (per IP)
    bad = {**REMOTE, "cf-connecting-ip": "198.51.100.1"}
    for _ in range(5):
        assert c.post("/api/auth/login", headers=bad, json={"username": "supervisor", "password": "nope"}).status_code == 401
    r = c.post("/api/auth/login", headers=bad, json={"username": "supervisor", "password": "correct horse battery"})
    assert r.status_code == 429, r.text
    assert c.post("/api/auth/login", headers=REMOTE, json={"username": "boss", "password": "correct horse battery"}).status_code == 401
    print("remote: 401 until logged in (tunnel and Next proxy); wrong login 401; 5 failures -> locked 5 min")

    r = c.post("/api/auth/login", headers=REMOTE, json={"username": "supervisor", "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    sc = r.headers["set-cookie"]
    assert "HttpOnly" in sc and "Secure" in sc and "samesite=lax" in sc.lower(), sc
    cookie = {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}
    c.cookies.clear()
    hdr = {**REMOTE, **cookie}
    assert c.get("/api/auth/me", headers=hdr).json()["user"] == "supervisor"
    assert c.get("/api/leads", headers=hdr).status_code == 200
    assert c.get("/api/calls", headers=hdr).status_code == 200
    assert c.get("/api/supervisor", headers=hdr).status_code == 200
    assert c.get("/api/supervisor/requests", headers=hdr).status_code == 200
    # allowed writes: availability + answering the pop-up
    assert c.post("/api/supervisor/availability", headers=hdr, json={"available": True}).json()["available"] is True
    assert c.post("/api/supervisor/requests/999/answer", headers=hdr, json={"accept": True}).status_code == 409  # no such open request
    # everything else that changes something stays on the main computer
    for method, path, body in (("post", "/api/dialer/on", None), ("post", "/api/calls", {"number": "+15125550100"}),
                               ("put", "/api/supervisor", {"phone": "+15125550100"}), ("put", "/api/settings", {"values": {}, "version": ""}),
                               ("post", "/api/leads/1/dnc", {"dnc": True}), ("post", "/api/dev/simulate", {"number": "+1"}),
                               ("post", "/api/voice/sample", {"voice_id": "x"})):
        r = getattr(c, method)(path, headers=hdr, json=body)
        assert r.status_code == 403, (path, r.status_code)
    assert c.get("/api/dev/anything", headers=hdr).status_code == 403
    print("logged in: reads OK, availability + answer OK; dialer/calls/settings/leads/dev -> 403")

    # live updates over a WebSocket (Cloudflare quick tunnels don't carry SSE)
    from starlette.websockets import WebSocketDisconnect
    from app.bus import bus
    with c.websocket_connect("/api/live") as ws:  # this computer: no login
        assert ws.receive_json() == {"type": "hello"}
    for hdr_bad in (REMOTE, {**REMOTE, "Cookie": f"{auth.COOKIE}=garbage.sig"}):
        try:
            with c.websocket_connect("/api/live", headers=hdr_bad) as ws:
                ws.receive_json()
            raise AssertionError("remote WebSocket without a login was accepted")
        except WebSocketDisconnect as e:
            assert e.code == 1008, e.code
    with c.websocket_connect("/api/live", headers=hdr) as ws:
        assert ws.receive_json() == {"type": "hello"}
        bus.publish({"type": "supervisor_request", "request": {"call_id": 7}})
        assert ws.receive_json() == {"type": "supervisor_request", "request": {"call_id": 7}}
    # SSE must not be gzipped by a proxy (it buffered the stream)
    with c.stream("GET", "/api/stream") as r:
        assert "no-transform" in r.headers["cache-control"], r.headers
    print("live WebSocket: local OK; remote refused (1008) without / with a bad login; logged in gets hello + messages;"
          " SSE marked no-transform")

    # tampered / expired / renamed-user sessions
    body, sig = r_cookie = cookie["Cookie"].split("=", 1)[1].split(".")
    assert c.get("/api/leads", headers={**REMOTE, "Cookie": f"{auth.COOKIE}={body}.x{sig[1:]}"}).status_code == 401
    old = auth.make_session("supervisor", now=1_000_000)
    assert c.get("/api/leads", headers={**REMOTE, "Cookie": f"{auth.COOKIE}={old}"}).status_code == 401
    os.environ["DASHBOARD_USERNAME"] = "someone_else"
    assert c.get("/api/leads", headers=hdr).status_code == 401
    os.environ["DASHBOARD_USERNAME"] = "supervisor"
    assert c.post("/api/auth/logout", headers=hdr).status_code == 200
    print("tampered, expired or renamed sessions refused; logout OK")

print("ALL REMOTE LOGIN TESTS PASS")
