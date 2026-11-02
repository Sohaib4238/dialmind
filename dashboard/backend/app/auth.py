"""Logins and roles for the dashboard.

On the laptop (http://127.0.0.1) nothing changes: no login, full control. A request that came through a proxy
(Caddy, a Cloudflare tunnel, Next.js's /api proxy) is "remote" and needs a session cookie from POST /api/auth/login.
DIALER_REQUIRE_LOGIN=1 (the server): every request counts as remote, proxy headers or not.

Users live in dashboard_users.json next to .env (DIALER_USERS_FILE moves it; permission 600), read on every
request, so a change works at once. Create or reset one with dashboard/backend/manage_users.py. Only a salted PBKDF2
hash is stored. Roles:
  admin       everything: calls, dialer, leads, settings
  supervisor  reads, Accept / Busy on the pop-up, the "available" switch, the (read-only) Health checks

The old single login still works, as a supervisor:
  DASHBOARD_USERNAME        login name
  DASHBOARD_PASSWORD_HASH   pbkdf2_sha256$<iterations>$<salt>$<hash>
  DASHBOARD_SESSION_SECRET  signs the session cookie; generated on first use
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

from starlette.requests import HTTPConnection, Request

from env_utils import ENV_PATH, save_env  # shared/env_utils.py

COOKIE = "dialer_session"
SESSION_HOURS = 12
ITERATIONS = 310_000
MAX_FAILURES, LOCK_S = 5, 300  # per IP
USER_MAX_FAILURES, USER_LOCK_S = 20, 3600  # per user, against attacks from many IPs (a restart clears it)
ROLES = ("admin", "supervisor")
# A supervisor may also POST to these (everything else that writes is admin-only)
SUPERVISOR_WRITE_PATHS = (re.compile(r"^/api/supervisor/requests/\d+/answer$"), re.compile(r"^/api/supervisor/availability$"),
                          re.compile(r"^/api/auth/(login|logout)$"), re.compile(r"^/api/health/check$"))  # the checks only read
_failures: dict[str, list[float]] = {}


def hash_password(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(base64.b64encode(dk).decode(), want)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # same timing for a name that doesn't exist


# ---- users ---------------------------------------------------------------------------------

def users_path() -> Path:
    return Path(os.getenv("DIALER_USERS_FILE") or ENV_PATH.with_name("dashboard_users.json"))


def read_users_file() -> dict[str, dict]:
    """{name: {"role", "hash"}}. Unreadable or broken = nobody (fails closed)."""
    try:
        data = json.loads(users_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("role") in ROLES and v.get("hash")}


def write_users_file(users: dict[str, dict]) -> None:
    path = users_path()
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
    os.replace(tmp, path)


def users() -> dict[str, dict]:
    """Everyone who can log in. The file wins over the old single login of the same name."""
    found = read_users_file()
    name, stored = os.getenv("DASHBOARD_USERNAME", "").strip(), os.getenv("DASHBOARD_PASSWORD_HASH", "").strip()
    if name and stored and name not in found:
        found[name] = {"role": "supervisor", "hash": stored, "legacy": True}
    return found


def configured() -> bool:
    return bool(users())


def is_remote(request: HTTPConnection) -> bool:
    """Came in through a proxy: cloudflared adds cf-* headers; Caddy and Next.js's /api proxy add x-forwarded-*."""
    if os.getenv("DIALER_REQUIRE_LOGIN", "").strip() not in ("", "0"):
        return True
    h = request.headers
    return any(k in h for k in ("cf-ray", "cf-connecting-ip", "x-forwarded-for", "x-forwarded-host"))


# ---- sessions ------------------------------------------------------------------------------

def _secret() -> bytes:
    s = os.getenv("DASHBOARD_SESSION_SECRET", "").strip()
    if not s:
        s = secrets.token_urlsafe(32)
        save_env("DASHBOARD_SESSION_SECRET", s)
        os.environ["DASHBOARD_SESSION_SECRET"] = s
    return s.encode()


def _sign(data: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(_secret(), data, hashlib.sha256).digest()).decode().rstrip("=")


def _fingerprint(stored: str) -> str:
    """Part of each session: a new password logs out that user's old sessions."""
    return hashlib.sha256(stored.encode()).hexdigest()[:16]


def make_session(user: str, now: float | None = None) -> str:
    stored = users().get(user, {}).get("hash", "")
    body = base64.urlsafe_b64encode(json.dumps({"u": user, "exp": int((now or time.time()) + SESSION_HOURS * 3600),
                                                "h": _fingerprint(stored)}).encode())
    return f"{body.decode()}.{_sign(body)}"


def session_user(request: HTTPConnection) -> str | None:
    token = request.cookies.get(COOKIE, "")
    body, _, sig = token.partition(".")
    if not body or not sig or not hmac.compare_digest(sig, _sign(body.encode())):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    user = users().get(data.get("u"))
    if data.get("exp", 0) < time.time() or not user or data.get("h") != _fingerprint(user["hash"]):
        return None  # expired, user removed, or the password changed since
    return data["u"]


def session_role(request: HTTPConnection) -> str | None:
    """Read from the users, not the cookie: a role change works at once."""
    name = session_user(request)
    return users()[name]["role"] if name else None


def client_ip(request: Request) -> str:
    h = request.headers
    return h.get("cf-connecting-ip") or (h.get("x-forwarded-for") or "").split(",")[0].strip() or \
        (request.client.host if request.client else "?")


def locked(ip: str, username: str = "", now: float | None = None) -> bool:
    now = now or time.time()
    _failures[ip] = [t for t in _failures.get(ip, []) if now - t < LOCK_S]
    if len(_failures[ip]) >= MAX_FAILURES:
        return True
    key = f"user:{username.strip()}"
    _failures[key] = [t for t in _failures.get(key, []) if now - t < USER_LOCK_S]
    return len(_failures[key]) >= USER_MAX_FAILURES


def login(ip: str, username: str, password: str) -> str | None:
    """A session token, or None (wrong login). Callers check locked() first."""
    name = username.strip()
    user = users().get(name)
    ok = check_password(password, user["hash"] if user else _DUMMY_HASH) and bool(user)  # always hash: same timing
    if not ok:
        _failures.setdefault(ip, []).append(time.time())
        if user:  # only real names: made-up ones must not fill the memory
            _failures.setdefault(f"user:{name}", []).append(time.time())
        return None
    _failures.pop(ip, None)
    _failures.pop(f"user:{name}", None)
    return make_session(name)


def allowed(role: str | None, method: str, path: str) -> bool:
    if path.startswith("/api/dev/"):
        return False
    if role == "admin":
        return True
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    return any(p.match(path) for p in SUPERVISOR_WRITE_PATHS)
