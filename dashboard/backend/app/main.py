"""Testing Dashboard API (FastAPI). Runs on 127.0.0.1; only the Cartesia webhook (and a ping)
are reachable from the internet, through the Cloudflare quick tunnel.

    cd dashboard\\backend
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

import asyncio
import hmac
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
# Shared code: shared/settings.py (validator), shared/env_utils.py (.env + logs)
sys.path.insert(0, str(PROJECT_ROOT / "shared"))

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sqlmodel import select  # noqa: E402
from sse_starlette.sse import EventSourceResponse  # noqa: E402
from starlette.responses import FileResponse, JSONResponse  # noqa: E402

from env_utils import file_log  # noqa: E402

# Every call step, webhook and Cartesia error ends up in logs/backend.log.
file_log("backend.log")
from app import agent_config, ambience, auth, cartesia, cartesia_setup, emails, gsheets, health, inbound, leads, phonic, providers, rejections, results_sheet, settings_store, sheet_sync, sms, standby, storage, supervisor, telnyx_events, test_mode, tunnel, unsubscribe, voices  # noqa: E402
from settings import SettingsError  # noqa: E402
from app.dialer import AutoDialer  # noqa: E402
from app.bus import bus  # noqa: E402
from app.calls import CallBusy, CallError, HangupFailed, manager  # noqa: E402
from app.db import Call, CallEvent, call_to_dict, event_to_dict, get_session, init_db  # noqa: E402

dialer = AutoDialer(manager)  # always OFF at start (never auto-starts)

logger = logging.getLogger("dashboard.main")
DEV = os.getenv("DASHBOARD_DEV", "0") == "1"
FRONTEND_ORIGINS = ["http://127.0.0.1:3000", "http://localhost:3000",
                    *filter(None, os.getenv("DASHBOARD_EXTRA_ORIGINS", "").split(","))]  # e.g. a demo frontend on :3001
# Paths that may be reached through a tunnel without a login (each checks its own secret).
PUBLIC_PATHS = {"/api/cartesia/webhook", "/api/cartesia/tools/record_outcome", "/api/cartesia/tools/request_supervisor",
                "/api/cartesia/tools/collect_email", "/api/cartesia/tools/lookup_caller", "/api/cartesia/tools/save_caller",
                "/api/telnyx/webhook", "/api/telnyx/messaging", "/api/public/ping", "/api/auth/login", "/api/auth/me", "/api/auth/logout"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if standby.on():
        logger.warning(standby.DETAIL)
    leads.apply_service_fit()  # leads imported before the service-fit check existed
    manager.mark_stale_calls()
    if n := leads.fix_placeholder_names():  # leads named "na" / "N/A" before names were cleaned
        logger.info("cleared %s placeholder lead names", n)
    if n := rejections.repair_history():  # runs of refused calls before the check existed: attempts given back
        logger.warning("%s refused calls relabelled as trunk_rejected; their leads got the attempt back", n)
    watcher = asyncio.create_task(tunnel.watch())
    inbox = asyncio.create_task(unsubscribe.watch())  # STOP replies -> Do not contact (off unless IMAP is set up)
    texts = asyncio.create_task(sms.watch())  # queued follow-up texts, sent when the lead's calling hours start
    disk = asyncio.create_task(storage.watch())  # deletes recordings past the keep period
    sheets = asyncio.create_task(sheet_sync.watch())  # sales sheet every N minutes (if on) + the results sheet
    yield
    sheets.cancel()
    watcher.cancel()
    inbox.cancel()
    texts.cancel()
    disk.cancel()


app = FastAPI(title="DialMind — Testing Dashboard", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Requests from this computer: no login. Through a proxy (cf-* / x-forwarded-* headers) or on the server
    (DIALER_REQUIRE_LOGIN): only the secret-checked webhooks/tools and the login, unless there's a session
    whose role allows it (auth.py)."""
    path = request.url.path
    if not auth.is_remote(request) or path in PUBLIC_PATHS:
        return await call_next(request)
    if not auth.configured():
        return JSONResponse({"detail": "Not found"}, status_code=404)  # remote access is off
    role = auth.session_role(request)
    if not role:
        return JSONResponse({"detail": "Please log in."}, status_code=401)
    if not auth.allowed(role, request.method, path):
        return JSONResponse({"detail": "Your login can't do that: it needs an admin."}, status_code=403)
    return await call_next(request)


# ---- login (remote use only) --------------------------------------------------------------

class Login(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def auth_login(body: Login, request: Request) -> Response:
    if not auth.configured():
        raise HTTPException(400, "Login isn't set up yet (manage_users.py).")
    ip = auth.client_ip(request)
    if auth.locked(ip, body.username[:100]):
        raise HTTPException(429, "Too many wrong tries. Wait a few minutes.")
    token = auth.login(ip, body.username[:100], body.password[:200])
    if not token:
        raise HTTPException(401, "Wrong username or password.")
    resp = JSONResponse({"ok": True, "user": body.username.strip()})
    resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_HOURS * 3600, httponly=True, samesite="lax",
                    secure=auth.is_remote(request), path="/")
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    if not auth.is_remote(request):
        return {"remote": False, "configured": auth.configured(), "user": "local", "role": "admin"}
    return {"remote": True, "configured": auth.configured(), "user": auth.session_user(request), "role": auth.session_role(request)}


@app.post("/api/auth/logout")
def auth_logout() -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


state = {"checks": None, "checked_at": None}


# ---- config -------------------------------------------------------------------------

@app.get("/api/config")
def get_config() -> dict:
    return {
        "dev": DEV,
        "default_number": os.getenv("TEST_CALL_NUMBER", ""),
        "engine": "cartesia",
        "from_number": os.getenv("TELNYX_PHONE_NUMBER", ""),
    }


@app.get("/api/test-mode")
def get_test_mode() -> dict:
    """For the dashboard banner: {"on", "number"} (every call goes to number while on)."""
    return test_mode.status()


# ---- calls --------------------------------------------------------------------------

class StartCall(BaseModel):
    number: str


@app.post("/api/calls")
async def start_call(body: StartCall) -> dict:
    try:
        call = await manager.start_call(body.number)
    except CallError as e:
        raise HTTPException(400, str(e))
    except CallBusy as e:
        raise HTTPException(409, str(e))
    return call_to_dict(call)


@app.post("/api/calls/{call_id}/hangup")
async def hang_up(call_id: int, force: bool = False) -> dict:
    try:
        return await manager.hang_up(call_id, force=force)
    except CallError as e:
        raise HTTPException(409, str(e))
    except HangupFailed as e:
        raise HTTPException(502, str(e))
    except cartesia.CartesiaError as e:
        raise HTTPException(502, f"Couldn't reach Cartesia to hang up: {e}")


@app.get("/api/calls")
def list_calls(limit: int = 100) -> list[dict]:
    with get_session() as s:
        calls = s.exec(select(Call).order_by(Call.id.desc()).limit(min(limit, 500))).all()
    return [call_to_dict(c) for c in calls]


@app.get("/api/calls/active")
def active_call() -> dict:
    call = manager.active_call()
    return {"call": call_to_dict(call) if call else None}


@app.get("/api/calls/{call_id}")
def get_call(call_id: int) -> dict:
    with get_session() as s:
        call = s.get(Call, call_id)
        if call is None:
            raise HTTPException(404, "Call not found")
        events = s.exec(select(CallEvent).where(CallEvent.call_id == call_id).order_by(CallEvent.id)).all()
    return {"call": call_to_dict(call), "events": [event_to_dict(e) for e in events]}


@app.get("/api/calls/{call_id}/audio")
async def call_audio(call_id: int) -> Response:
    try:
        rec = await manager.recording(call_id)
    except cartesia.CartesiaError as e:
        raise HTTPException(404 if e.status == 404 else 502, f"No recording: {e}")
    if not rec or not rec[0]:
        raise HTTPException(404, "No recording for this call (yet)")
    audio, mime = rec
    return Response(audio, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


# ---- leads ---------------------------------------------------------------------------

class LeadsCsv(BaseModel):
    csv: str
    filename: str = ""


@app.post("/api/leads/import/preview")
def leads_import_preview(body: LeadsCsv) -> dict:
    try:
        return leads.parse_csv(body.csv, body.filename)
    except leads.LeadError as e:
        raise HTTPException(400, str(e))


# async: it publishes to the live bus, which lives on the event loop
@app.post("/api/leads/import")
async def leads_import(body: LeadsCsv) -> dict:
    try:
        return await asyncio.to_thread(leads.import_csv, body.csv, body.filename)
    except leads.LeadError as e:
        raise HTTPException(400, str(e))


@app.get("/api/leads")
def list_leads(q: str = "", status: str = "", limit: int = 500, offset: int = 0, sort: str = "sheet") -> dict:
    return leads.list_leads(q, status, limit, offset, sort)


@app.get("/api/leads/counts")
def lead_counts() -> dict:
    return leads.counts()


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: int) -> dict:
    try:
        return leads.get_lead(lead_id)
    except leads.LeadError as e:
        raise HTTPException(404, str(e))


class DncChange(BaseModel):
    dnc: bool
    reason: str = ""


@app.post("/api/leads/{lead_id}/dnc")
async def set_lead_dnc(lead_id: int, body: DncChange) -> dict:
    try:
        return leads.set_dnc(lead_id, body.dnc, body.reason)
    except leads.LeadError as e:
        raise HTTPException(404, str(e))


class DncNumber(BaseModel):
    phone: str
    reason: str = ""


@app.post("/api/dnc")
async def add_dnc(body: DncNumber) -> dict:
    try:
        return leads.add_dnc_number(body.phone, body.reason)
    except leads.LeadError as e:
        raise HTTPException(400, str(e))


class OutcomeChange(BaseModel):
    outcome: str
    note: str = ""
    callback_time: str = ""  # lead's local time, YYYY-MM-DDTHH:MM


@app.post("/api/leads/{lead_id}/outcome")
async def set_lead_outcome(lead_id: int, body: OutcomeChange) -> dict:
    try:
        return leads.set_outcome(lead_id, body.outcome, body.note, body.callback_time)
    except leads.LeadError as e:
        raise HTTPException(404 if "not found" in str(e) else 400, str(e))


class SalesHandled(BaseModel):
    handled: bool


@app.post("/api/leads/{lead_id}/sales-handled")
async def lead_sales_handled(lead_id: int, body: SalesHandled) -> dict:
    """Clear (or set) "Handled by sales" by hand. The next sheet sync sets it again while the row is still coloured."""
    try:
        return leads.set_sales_handled(lead_id, body.handled)
    except leads.LeadError as e:
        raise HTTPException(404, str(e))


# ---- Google Sheets: the sales team's leads (read only) + our results sheet ----------------------

@app.get("/api/sheets")
def sheets_status() -> dict:
    return sheet_sync.status()


class SheetSettings(BaseModel):
    enabled: bool | None = None
    sales_sheet: str | None = None
    tabs: list[str] | None = None
    sync_minutes: int | None = None
    handled_colors: list[str] | None = None
    handled_action: str | None = None
    results_sheet: str | None = None
    results_tab: str | None = None
    results_timezone: str | None = None
    dialer_order: str | None = None


@app.put("/api/sheets")
async def save_sheets(body: SheetSettings) -> dict:
    try:
        return sheet_sync.save(body.model_dump(exclude_none=True))
    except sheet_sync.SheetSettingsError as e:
        raise HTTPException(400, str(e))


@app.post("/api/sheets/sync")
async def sheets_sync_now() -> dict:
    """Read the sales sheet now: new leads in, colours applied. Never starts the dialer."""
    res = await sheet_sync.sync_now("manual")
    if not res.get("ok") and not res.get("at"):  # not ready / already running: nothing was read
        raise HTTPException(409, res["error"])
    return res


@app.post("/api/sheets/recheck-colors")
async def sheets_recheck_colors() -> dict:
    """Read the sales sheet and apply ONLY the colour rule to leads we already have (green / yellow / red rows ->
    Handled by sales). Nothing is imported, filled in or moved."""
    res = await sheet_sync.sync_now("colours", colors_only=True)
    if not res.get("ok") and not res.get("at"):
        raise HTTPException(409, res["error"])
    return res


@app.post("/api/sheets/results/rewrite")
async def sheets_rewrite_results() -> dict:
    """Write every called lead's row to the results sheet again (e.g. after setting it up)."""
    if not gsheets.sheet_id_from(sheet_sync.load()["results_sheet"]):
        raise HTTPException(400, "Set the results sheet link first.")
    n = await asyncio.to_thread(results_sheet.mark_all_called)
    sheet_sync._save_state(results={})  # try at once, even right after a failure
    sheet_sync.wake()
    return {"ok": True, "queued": n}


@app.post("/api/leads/{lead_id}/call-anyway")
async def lead_call_anyway(lead_id: int) -> dict:
    try:
        return leads.call_anyway(lead_id)
    except leads.LeadError as e:
        raise HTTPException(404, str(e))


@app.post("/api/leads/{lead_id}/call")
async def call_lead(lead_id: int) -> dict:
    try:
        lead = leads.get_lead(lead_id)["lead"]
    except leads.LeadError as e:
        raise HTTPException(404, str(e))
    try:
        call = await manager.start_call(lead["phone"], lead_id=lead_id)
    except CallError as e:
        raise HTTPException(400, str(e))
    except CallBusy as e:
        raise HTTPException(409, str(e))
    return call_to_dict(call)


# ---- follow-up emails (Phase 7) ----------------------------------------------------------

@app.get("/api/emails")
def list_emails(lead_id: int | None = None, call_id: int | None = None, limit: int = 200) -> list[dict]:
    return emails.list_emails(lead_id, call_id, limit)


@app.get("/api/emails/status")
def email_status() -> dict:
    on, detail = unsubscribe.enabled()
    return {**emails.status(), "unsubscribe": {**unsubscribe.state, "enabled": on, "detail": unsubscribe.state.get("detail")
                                               if on and unsubscribe.state.get("last_check") else detail}}


@app.post("/api/emails/test")
async def send_test_email() -> dict:
    """A sample email to EMAIL_TEST_ADDRESS only (checks the account without a call)."""
    return await asyncio.to_thread(emails.send_test)


@app.post("/api/emails/check-unsubscribes")
async def check_unsubscribes() -> dict:
    """Read STOP / unsubscribe replies now (read-only) instead of waiting for the next check."""
    return await asyncio.to_thread(unsubscribe.check_now)


@app.post("/api/emails/{email_id}/retry")
async def retry_email(email_id: int) -> dict:
    try:
        return await asyncio.to_thread(emails.retry, email_id)
    except emails.EmailError as e:
        raise HTTPException(400, str(e))
    except SettingsError as e:
        raise HTTPException(400, f"Fix the settings first: {e}")


# ---- follow-up texts (Phase 7b) ------------------------------------------------------------

@app.get("/api/sms")
def list_texts(lead_id: int | None = None, call_id: int | None = None, limit: int = 200) -> list[dict]:
    return sms.list_texts(lead_id, call_id, limit)


@app.get("/api/sms/status")
def sms_status() -> dict:
    return sms.status()


@app.post("/api/sms/test")
async def send_test_sms() -> dict:
    """ONE sample text to TEST_CALL_NUMBER only (Health page button)."""
    return await asyncio.to_thread(sms.send_test)


@app.post("/api/sms/{sms_id}/retry")
async def retry_sms(sms_id: int) -> dict:
    try:
        return await asyncio.to_thread(sms.retry, sms_id)
    except sms.SmsError as e:
        raise HTTPException(400, str(e))
    except SettingsError as e:
        raise HTTPException(400, f"Fix the settings first: {e}")


@app.post("/api/telnyx/messaging")
async def telnyx_messaging(request: Request) -> dict:
    """Telnyx messaging events: delivery reports for our texts, and replies (STOP -> Do not contact).
    Signed like the call events (Ed25519, TELNYX_PUBLIC_KEY); refused without a valid signature."""
    raw = await request.body()
    ok, why = telnyx_events.verify(raw, request.headers.get("telnyx-signature-ed25519", ""),
                                   request.headers.get("telnyx-timestamp", ""))
    if not ok:
        raise HTTPException(401, why)
    return await asyncio.to_thread(sms.handle_webhook, raw)


# ---- auto-dialer -----------------------------------------------------------------------

@app.get("/api/dialer")
def dialer_state() -> dict:
    return dialer.snapshot()


@app.post("/api/dialer/on")
async def dialer_on() -> dict:
    return dialer.turn_on()


@app.post("/api/dialer/off")
async def dialer_off() -> dict:
    return dialer.turn_off("Turned off from the dashboard")


class TestMode(BaseModel):
    enabled: bool
    confirm: bool = False


class DialerOrder(BaseModel):
    order: str


@app.post("/api/dialer/order")
async def dialer_order(body: DialerOrder) -> dict:
    """New leads: sheet order (top to bottom, first tab first) or newest first."""
    try:
        sheet_sync.set_dialer_order(body.order)
    except sheet_sync.SheetSettingsError as e:
        raise HTTPException(400, str(e))
    dialer.publish()
    return dialer.snapshot()


@app.post("/api/dialer/test-mode")
async def dialer_test_mode(body: TestMode) -> dict:
    if not body.enabled and not body.confirm:
        raise HTTPException(400, "Turning test mode off needs confirmation.")
    try:
        return dialer.set_test_mode(body.enabled)
    except CallError as e:
        raise HTTPException(409, str(e))


# ---- from Cartesia (public through the tunnel) --------------------------------------

@app.get("/api/public/ping")
def public_ping() -> dict:
    return {"ok": True}


@app.post("/api/cartesia/webhook")
async def cartesia_webhook(request: Request) -> dict:
    expected = os.getenv("CARTESIA_WEBHOOK_SECRET", "")
    got = request.headers.get("x-webhook-secret", "")
    if not expected or not hmac.compare_digest(got.encode(), expected.encode()):
        raise HTTPException(401, "Bad webhook secret")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "Body must be JSON")
    return manager.ingest_webhook(payload if isinstance(payload, dict) else {})


@app.post("/api/cartesia/tools/record_outcome")
async def record_outcome_tool(request: Request) -> Response:
    """Webhook tool: the AI reports the call's outcome mid-call. Cartesia sends
    Authorization: Bearer <CARTESIA_TOOL_SECRET>; the reply text goes back to the AI."""
    reply = manager.record_outcome_tool(await _tool_payload(request))
    return Response(reply, media_type="text/plain")


@app.post("/api/cartesia/tools/request_supervisor")
async def request_supervisor_tool(request: Request) -> Response:
    """Webhook tool: ask the supervisor to take this call. Each use waits a few seconds for his
    Accept / Busy, then replies AVAILABLE / WAITING / NOT AVAILABLE (same Bearer secret as record_outcome)."""
    payload = await _tool_payload(request)
    # JSON: Cartesia saves its transfer_number (the supervisor who accepted) into {{transfer_number}}
    return JSONResponse(await manager.request_supervisor_tool(payload))


@app.post("/api/cartesia/tools/collect_email")
async def collect_email_tool(request: Request) -> Response:
    """Webhook tool: the lead's email address, read back and confirmed (same Bearer secret as record_outcome)."""
    reply = manager.collect_email_tool(await _tool_payload(request))
    return Response(reply, media_type="text/plain")


@app.post("/api/cartesia/tools/lookup_caller")
async def lookup_caller_tool(request: Request) -> Response:
    """Webhook tool (inbound calls): who is calling, from our leads (same Bearer secret as record_outcome)."""
    reply = await manager.lookup_caller_tool(await _tool_payload(request))
    return Response(reply, media_type="text/plain")


@app.post("/api/cartesia/tools/save_caller")
async def save_caller_tool(request: Request) -> Response:
    """Webhook tool (inbound calls): a new caller's details -> a new lead (same Bearer secret)."""
    reply = await manager.save_caller_tool(await _tool_payload(request))
    return Response(reply, media_type="text/plain")


async def _tool_payload(request: Request) -> dict:
    expected = os.getenv("CARTESIA_TOOL_SECRET", "")
    auth = request.headers.get("authorization", "")
    got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not expected or not hmac.compare_digest(got.encode(), expected.encode()):
        raise HTTPException(401, "Bad tool secret")
    raw = await request.body()
    if len(raw) > 16_000:
        raise HTTPException(413, "Too big")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "Body must be JSON")
    payload = payload if isinstance(payload, dict) else {}
    info = payload.get("call_info")
    if not payload.get("call_id") and isinstance(info, dict):  # Phonic: the conversation id is in call_info
        payload["call_id"] = str(info.get("conversation_id") or "")
    return payload


# ---- supervisor (Phase 6) -------------------------------------------------------------

class SupervisorWait(BaseModel):
    wait_seconds: int


class SupervisorPerson(BaseModel):
    key: str | None = None  # a new login name (rename)
    name: str | None = None
    phone: str | None = None
    available: bool | None = None
    hours: dict | None = None


class Availability(BaseModel):
    available: bool
    key: str | None = None  # admin / this computer: whose switch. A supervisor always flips their own.


class Answer(BaseModel):
    accept: bool
    supervisor: str | None = None  # admin / this computer, not on the list themselves: answer for this one


def _viewer(request: Request) -> tuple[str | None, bool]:
    """(login name or None on this computer, may act as admin)."""
    if not auth.is_remote(request):
        return None, True
    return auth.session_user(request), auth.session_role(request) == "admin"


@app.get("/api/supervisor")
def supervisor_settings(request: Request) -> dict:
    user, admin = _viewer(request)
    from app import cartesia_setup as cs

    st = cs._state()
    return {**supervisor.public_state(), "dashboard_url": tunnel.state.get("dashboard_url"),
            "login_configured": auth.configured(), "transfer_ready": agent_config.supervisor_enabled(),
            "me": user, "is_admin": admin, "me_supervisor": bool(supervisor.person(user)),
            "logins": sorted(auth.users()) if admin else [],
            "assignments": st.get("supervisor_assignments"), "assignments_error": st.get("supervisor_assignments_error") or ""}


def _after_people_change(old_phones: list[str]) -> None:
    if supervisor.phones() != old_phones and inbound.enabled():  # the inbound agent has the phones in its transfer tool
        asyncio.create_task(inbound.apply("supervisor phones changed"))


@app.put("/api/supervisor")
async def save_supervisor_wait(body: SupervisorWait) -> dict:
    try:
        return supervisor.save_wait(body.wait_seconds)
    except supervisor.SupervisorError as e:
        raise HTTPException(400, str(e))


@app.put("/api/supervisor/people/{key}")
async def save_supervisor_person(key: str, body: SupervisorPerson) -> dict:
    """Add or change one supervisor (admin only: auth.allowed)."""
    old = supervisor.phones()
    try:
        res = supervisor.save_person(key, body.model_dump(exclude_none=True))
    except supervisor.SupervisorError as e:
        raise HTTPException(400, str(e))
    _after_people_change(old)
    return res


@app.delete("/api/supervisor/people/{key}")
async def delete_supervisor_person(key: str) -> dict:
    old = supervisor.phones()
    try:
        res = supervisor.remove_person(key)
    except supervisor.SupervisorError as e:
        raise HTTPException(404, str(e))
    _after_people_change(old)
    return res


@app.post("/api/supervisor/availability")
async def set_supervisor_availability(body: Availability, request: Request) -> dict:
    """A supervisor's own "available" switch (also from their laptop). An admin may flip anyone's (key)."""
    user, admin = _viewer(request)
    key = body.key if admin and body.key else user
    if not key:
        people = supervisor.load()["people"]
        if len(people) != 1:
            raise HTTPException(400, "Say whose switch: there's more than one supervisor.")
        key = people[0]["key"]
    try:
        return supervisor.set_available(key, body.available)
    except supervisor.SupervisorError as e:
        raise HTTPException(403 if not admin else 400, str(e))


@app.get("/api/supervisor/requests")
def supervisor_requests() -> list[dict]:
    return manager.open_requests()


@app.post("/api/supervisor/requests/{call_id}/answer")
async def answer_supervisor_request(call_id: int, body: Answer, request: Request) -> dict:
    """Accept / Busy. async: it wakes the AI's waiting request_supervisor on the same event loop. The first Accept
    wins and the call goes to that supervisor's phone."""
    user, admin = _viewer(request)
    try:
        return manager.answer_supervisor(call_id, body.accept, user=user, as_key=body.supervisor, admin=admin)
    except CallError as e:
        raise HTTPException(409, str(e))


@app.post("/api/telnyx/webhook")
async def telnyx_webhook(request: Request) -> dict:
    """Telnyx call events for our SIP connection (off until TELNYX_PUBLIC_KEY is set and the
    connection's Webhook URL points here). call.hangup closes the Cartesia call right away,
    because Cartesia ignores the BYE Telnyx sends when the callee hangs up."""
    raw = await request.body()
    ok, why = telnyx_events.verify(raw, request.headers.get("telnyx-signature-ed25519", ""),
                                   request.headers.get("telnyx-timestamp", ""))
    if not ok:
        raise HTTPException(401, why)
    return telnyx_events.handle(raw, manager)


@app.get("/api/stream")
async def stream(request: Request):
    q = bus.subscribe()

    async def gen():
        try:
            yield {"event": "hello", "data": "{}"}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), 15)
                    yield {"event": "message", "data": data}
                except asyncio.TimeoutError:
                    continue  # sse-starlette sends pings
        finally:
            bus.unsubscribe(q)

    # no-transform: proxies must not gzip it. Next.js's /api proxy (remote use) did, which held the events
    # back in the gzip buffer, so the remote dashboard never connected. X-Accel-Buffering: same for nginx-likes.
    return EventSourceResponse(gen(), ping=10, headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


LIVE_PING_S = 20  # Cloudflare closes idle WebSockets after ~100 s


@app.websocket("/api/live")
async def live_ws(ws: WebSocket):
    """The same live messages as /api/stream, over a WebSocket: Cloudflare quick tunnels don't support SSE,
    so the remote dashboard (supervisor's laptop) uses this. The http middleware doesn't run for
    WebSockets, so the login is checked here."""
    if auth.is_remote(ws) and not (auth.configured() and auth.session_user(ws)):
        await ws.close(code=1008)  # policy violation: not logged in
        return
    await ws.accept()
    q = bus.subscribe()

    async def drain_client():  # notices the browser going away (and ignores anything it sends)
        while True:
            await ws.receive_text()

    reader = asyncio.create_task(drain_client())
    try:
        await ws.send_text('{"type": "hello"}')
        while not reader.done():
            try:
                data = await asyncio.wait_for(q.get(), LIVE_PING_S)
            except asyncio.TimeoutError:
                data = '{"type": "ping"}'
            await ws.send_text(data)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader.cancel()
        bus.unsubscribe(q)


# ---- health -------------------------------------------------------------------------

@app.get("/api/health")
def get_health() -> dict:
    return {"link": tunnel.link_status(), "checks": state["checks"], "checked_at": state["checked_at"]}


@app.post("/api/health/check")
async def run_checks() -> dict:
    state["checks"] = await health.run_all()
    state["checked_at"] = datetime.now(timezone.utc).isoformat()
    return get_health()


async def _sync_agent(reason: str) -> dict:
    """Push the saved settings to the Cartesia agent; a failure doesn't undo the save."""
    if not os.getenv("CARTESIA_AGENT_ID"):
        return {"ok": False, "detail": "Cartesia agent not set up yet"}
    try:
        r = await cartesia_setup.sync_agent(reason)
        if r.get("ambience_error"):
            return {"ok": False, "detail": f"Saved and synced, but the background sound isn't on: {r['ambience_error']}"}
        return {"ok": True, "action": r["action"]}
    except Exception as e:  # noqa: BLE001 - reported to the UI, retried before the next call
        return {"ok": False, "detail": f"Saved, but couldn't update Cartesia yet ({e}). It retries before the next call."}


# ---- settings -----------------------------------------------------------------------

@app.get("/api/settings")
def get_settings() -> dict:
    return {**settings_store.read(), "sync": cartesia_setup.sync_status(), "inbound": inbound.status()}


class SaveSettings(BaseModel):
    values: dict
    version: str | None = None


@app.put("/api/settings")
async def save_settings(body: SaveSettings) -> dict:
    try:
        res = await asyncio.to_thread(settings_store.save, body.values, body.version)
    except settings_store.SettingsInvalid as e:
        raise HTTPException(422, {"errors": e.errors})
    except settings_store.SettingsConflict as e:
        raise HTTPException(409, str(e))
    leads.apply_service_fit()  # the service-fit keywords may have changed
    return {**res, "cartesia": await _sync_agent("instructions saved"), "inbound": await inbound.apply("instructions saved")}


# ---- voice provider (Cartesia / Phonic) -------------------------------------------------

def _provider_payload() -> dict:
    return {**providers.load(), "features": providers.features(), "noises": list(providers.PHONIC_NOISES),
            "speed_range": list(providers.SPEED_RANGE), "phonic_configured": phonic.configured(),
            "phonic_test_only": phonic.test_only(), "phonic_test_number": phonic.test_only_number()}


@app.get("/api/voice-provider")
async def get_voice_provider() -> dict:
    return _provider_payload()


class SaveVoiceProvider(BaseModel):
    provider: str
    phonic: dict | None = None


@app.put("/api/voice-provider")
async def save_voice_provider(body: SaveVoiceProvider) -> dict:
    if manager.active_call():
        raise HTTPException(409, "A call is in progress. Change the voice provider after it ends.")
    if body.provider == "phonic" and not phonic.configured():
        raise HTTPException(422, "PHONIC_API_KEY is not set in .env")
    try:
        await asyncio.to_thread(providers.save, body.provider, body.phonic)
    except providers.ProviderSettingsError as e:
        raise HTTPException(422, str(e))
    return _provider_payload()


@app.get("/api/phonic/voices")
async def phonic_voices() -> dict:
    try:
        voices_ = await phonic.list_voices()
    except phonic.PhonicError as e:
        raise HTTPException(502, str(e))
    return {"voices": [{"id": v.get("id"), "name": v.get("name") or v.get("id"), "description": v.get("description") or "",
                        "preview_url": v.get("audio_url") or ""} for v in voices_ if v.get("id")]}


# ---- voice (Cartesia) ---------------------------------------------------------------

@app.get("/api/voice")
async def get_voice() -> dict:
    data = settings_store.read()
    voice = data["values"].get("voice")
    info = await voices.voice_info(voice["voice_id"]) if isinstance(voice, dict) and voice.get("voice_id") else None
    return {
        "voice": voice,
        "voice_info": info,
        "version": data["version"],
        "file_error": data["file_error"],
        "help": settings_store.voice_help(),
        "sounds": ambience.catalog(),
        "sync": cartesia_setup.sync_status(),
    }


@app.get("/api/voice/voices")
async def list_voices(refresh: bool = False) -> dict:
    try:
        return await voices.list_voices(refresh)
    except voices.VoiceApiError as e:
        raise HTTPException(502, str(e))


@app.get("/api/voice/preview/{voice_id}")
async def voice_preview(voice_id: str) -> Response:
    """The voice's own preview file, proxied because Cartesia's file URLs need our key (free)."""
    try:
        audio, mime = await voices.preview(voice_id)
    except voices.VoiceApiError as e:
        raise HTTPException(404, str(e))
    return Response(audio, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


# Stands in for the lead's details when the opening line is spoken as a voice sample.
SAMPLE_LEAD = {"lead_name": "Maria", "business_name": "Bloom Boutique", "service_required": "a new website",
               "business_description": "a dress boutique", "job_description": "an online store", "location": "Miami",
               "specialist_title": "our senior web designer", "lead_source": "Bark"}


def _opening_line() -> str:
    try:
        s = settings_store.AgentSettings.model_validate(settings_store.read()["values"])
        return agent_config.introduction(s, SAMPLE_LEAD)
    except Exception:  # noqa: BLE001 - invalid settings: fall back to a neutral sentence
        return "Hi, this is a quick sample of my voice. How does it sound?"


@app.get("/api/voice/sample-text")
def voice_sample_text() -> dict:
    text = _opening_line()
    return {"text": text, "characters": len(text)}


@app.get("/api/voice/ambience/{sound}")
def ambience_preview(sound: str) -> FileResponse:
    """The background sound file, to listen to on the Voice page (free: no Cartesia call)."""
    if not ambience.exists(sound) or not ambience.file_hash(sound):
        raise HTTPException(404, "No such sound")
    return FileResponse(ambience.path_for(sound), media_type="audio/wav")


@app.post("/api/voice/sounds")
async def upload_sound(filename: str, request: Request) -> dict:
    """Upload your own background sound: the raw MP3/WAV is the body. Processed like ours, then given to Cartesia."""
    if int(request.headers.get("content-length") or 0) > ambience.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That file is too big: 20 MB at most")
    try:
        sound = await asyncio.to_thread(ambience.add_upload, await request.body(), filename)
    except ambience.SoundError as e:
        raise HTTPException(422, str(e))
    try:
        await cartesia_setup.upload_sound(sound["id"])
        return {**sound, "cartesia": {"ok": True}}
    except (cartesia.CartesiaError, KeyError) as e:  # it stays in the list; the next sync with it chosen tries again
        return {**sound, "cartesia": {"ok": False, "detail": f"Added, but not on Cartesia yet ({e}). It retries when you use it."}}


class SampleRequest(BaseModel):
    voice_id: str
    speed: float = 1.0
    volume: float = 1.0


@app.post("/api/voice/sample")
async def voice_sample(body: SampleRequest) -> Response:
    """Only for voices without a preview file: speaks the opening line (uses TTS credits)."""
    try:
        audio, mime = await voices.generate_sample(body.voice_id, _opening_line(), body.speed, body.volume)
    except voices.VoiceApiError as e:
        raise HTTPException(502, str(e))
    return Response(audio, media_type=mime)


class SaveVoice(BaseModel):
    voice: dict
    version: str | None = None


@app.put("/api/voice")
async def save_voice(body: SaveVoice) -> dict:
    amb = body.voice.get("ambience") or {}
    sound = amb.get("sound", ambience.DEFAULT_SOUND)
    # The default name may have no file (none ship with the repo): fine while the sound is off.
    if not ambience.exists(sound) and (amb.get("enabled") or sound != ambience.DEFAULT_SOUND):
        raise HTTPException(422, {"errors": [{"path": "voice.ambience.sound", "message": f"No such background sound: {sound}"}]})
    try:
        res = await asyncio.to_thread(settings_store.save_voice, body.voice, body.version)
    except settings_store.SettingsInvalid as e:
        raise HTTPException(422, {"errors": e.errors})
    except settings_store.SettingsConflict as e:
        raise HTTPException(409, str(e))
    if inbound.enabled():  # the inbound agent speaks with the same voice
        await inbound.apply("voice saved")
    return {**res, "cartesia": await _sync_agent("voice saved")}


# ---- dev only -----------------------------------------------------------------------

class Simulate(BaseModel):
    number: str = ""


@app.post("/api/dev/simulate")
async def simulate(body: Simulate) -> dict:
    if not DEV:
        raise HTTPException(404, "Not found")
    try:
        return call_to_dict(manager.start_simulation(body.number))
    except CallBusy as e:
        raise HTTPException(409, str(e))
