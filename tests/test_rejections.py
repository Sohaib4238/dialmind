"""Calls refused before they ring (rejections.py), the Telnyx balance stop + Health row, "na" names.

2026-11-06: the Telnyx balance hit $0, Telnyx switched the account off, and 12 leads in a row came back "busy"
(Cartesia's dial_busy) within 3 s. Offline: fake Cartesia, fake Telnyx texts, throwaway DB; no call placed."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

DB = f"{TMP}/rejections_test.db"
STATE = f"{TMP}/dialer_state_rejections.json"
for f in (DB, STATE):
    if os.path.exists(f):
        os.remove(f)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["DIALER_STATE_PATH"] = STATE  # missing file = test mode ON; never the real switch
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""
os.environ["SMS_SENDING_DISABLED"] = "1"  # lifted below, in-process, once the fake Telnyx is in place
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""
os.environ["TELNYX_API_KEY"] = "KEY_fake_for_tests"
os.environ["TELNYX_SMS_FROM"] = "+13025550199"
os.environ["DASHBOARD_PASSWORD_HASH"] = ""
os.environ["EMAIL_SENDING_DISABLED"] = "1"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import app.calls as calls  # noqa: E402
import app.dialer as dialer_mod  # noqa: E402
from app import cartesia, cartesia_setup, health, leads, rejections, sheet_sync, sms, tunnel  # noqa: E402
from app.db import Call, Lead, LeadEvent, SmsMessage, as_utc, get_session  # noqa: E402
from app.main import app, dialer  # noqa: E402
from settings import load_settings  # noqa: E402


async def no_tunnel(*a, **k):
    return None

tunnel.watch = no_tunnel
calls.POLL_EVERY_S = 0.05
dialer_mod.GAP_BETWEEN_CALLS_S = 0
dialer_mod.IDLE_RECHECK_S = 1
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)  # Tuesday, 11:00 in Miami (inside calling hours)
dialer_mod.utcnow = lambda: NOW
sms.utcnow = lambda: NOW
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_WEBHOOK_SECRET="s3cret")

S = load_settings().model_copy(deep=True)
S.sms.enabled = True  # texts on, so we can see that a refused call never sends one
sms.load_settings = lambda: S
sent = []


def fake_post(path, body, method="POST"):
    sent.append(body)
    return {"data": {"id": f"msg_{len(sent)}", "to": [{"status": "queued"}]}}

sms._post = fake_post
os.environ["SMS_SENDING_DISABLED"] = "0"  # in-process only: every request goes to the fake above
BUSY = "Call was busy or declined (SIP 486/603; trunk rejections such as auth failures or carrier blocks also report this reason)"


class Fake:
    def __init__(self):
        self.dialed, self.outcomes, self.calls, self.n = [], [], {}, 0

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.dialed.append(to)
        self.n += 1
        cid = f"ac_r{self.n}"
        self.calls[cid] = {"t0": time.monotonic(), "reason": self.outcomes.pop(0) if self.outcomes else "client_hangup"}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        if time.monotonic() - c["t0"] < 0.15:
            return {"id": cid, "status": "started", "transcript": []}
        if c["reason"] == "dial_busy":
            return {"id": cid, "status": "failed", "end_reason": "dial_busy", "error_message": BUSY}
        t = [{"role": "user", "text": "Hi", "start_timestamp": 0.5}] if c["reason"] == "client_hangup" else []
        return {"id": cid, "status": "completed", "end_reason": c["reason"], "transcript": t,
                "start_time": "2026-10-27T15:00:00Z", "end_time": "2026-10-27T15:00:20Z"}

    async def cancel_call(self, cid):
        return {"cancelled": True}


fake = Fake()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fake.create_call, fake.get_call, fake.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync


def wait_state(c, pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get("/api/dialer").json()
        if pred(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"timeout; dialer = {s}")


def lead_by(name):
    with get_session() as s:
        return s.exec(select(Lead).where(Lead.name == name)).first()


def reset_leads(c, csv):
    """New leads. Nothing is deleted (ids must not be reused): older leads go on Do not contact so the dialer skips
    them, and a good call ends any streak."""
    with get_session() as s:
        for x in s.exec(select(Lead)).all():
            x.dnc = True
            s.add(x)
        for x in s.exec(select(SmsMessage)).all():
            s.delete(x)
        s.add(Call(room=f"ac_good_{time.time_ns()}", number="+13055550298", status="ended", result="voicemail",
                   created_at=NOW, ended_at=NOW, answered_at=NOW))
        s.commit()
    r = c.post("/api/leads/import", json={"csv": csv, "filename": "t.csv"}).json()
    assert r.get("imported") or r.get("counts"), r


def drain_texts():
    time.sleep(0.6)  # the follow-up text runs in the background
    sms.process_queue(NOW + timedelta(minutes=10))  # anything still waiting would go out now


with TestClient(app) as c:
    assert c.post("/api/dialer/test-mode", json={"enabled": True}).json()["test_mode"] is True

    # 1) a single real busy line stays "busy": the attempt counts; its text waits 5 min, then goes out
    reset_leads(c, "phone,name\n(305) 555-0201,One Busy\n")
    fake.outcomes = ["dial_busy"]
    c.post("/api/dialer/on")
    wait_state(c, lambda s: s["session_calls"] == 1 and not s["current"])
    c.post("/api/dialer/off")
    one = lead_by("One Busy")
    with get_session() as s:
        call = s.get(Call, one.last_call_id)
        txt = s.exec(select(SmsMessage).where(SmsMessage.call_id == call.id)).first()
    assert call.result == "busy" and one.attempts == 1 and one.status == "no_answer", (call.result, one.attempts, one.status)
    time.sleep(0.6)
    with get_session() as s:
        txt = s.exec(select(SmsMessage).where(SmsMessage.call_id == call.id)).first()
    assert txt and txt.status == "queued" and "waiting 5 min after a busy line" in txt.detail, txt
    assert as_utc(txt.send_after) == NOW + timedelta(seconds=sms.BUSY_TEXT_DELAY_S), txt.send_after
    sms.process_queue(NOW + timedelta(minutes=6))
    assert len(sent) == 1
    print("1. one busy line: stays busy, attempt counted, text waits 5 min then goes out")

    # 2) 3 refused calls in a row: dialer OFF with a red alert, calls relabelled, attempts given back, no texts
    sent.clear()
    reset_leads(c, "phone,name\n" + "".join(f"(305) 555-02{10 + i},Rej {i}\n" for i in range(5)))
    fake.dialed.clear()
    fake.outcomes = ["dial_busy"] * 5
    c.post("/api/dialer/on")
    s = wait_state(c, lambda s: s["state"] == "off" and s.get("alert"))
    print("2.", s["state"], "|", s["reason"], "|", s["alert"][:90], "…")
    assert len(fake.dialed) == 3, fake.dialed
    assert "3 calls in a row were refused before they rang" in s["alert"] and "Telnyx balance" in s["alert"]
    assert any(e["level"] == "error" and "refused" in e["text"] for e in s["log"])
    for i in range(3):
        lead = lead_by(f"Rej {i}")
        assert lead.attempts == 0 and lead.status == "new" and lead.last_call_id is None, (i, lead)
        with get_session() as db:
            call = db.exec(select(Call).where(Call.lead_id == lead.id)).first()
            ev = db.exec(select(LeadEvent).where(LeadEvent.lead_id == lead.id).order_by(LeadEvent.id.desc())).first()
        assert call.result == "trunk_rejected" and call.status == "failed" and call.outcome == "failed", call
        assert "not counted as an attempt" in ev.text, ev.text
    assert lead_by("Rej 3").attempts == 0
    drain_texts()
    with get_session() as db:
        texts = db.exec(select(SmsMessage)).all()  # (reset_leads cleared the earlier ones)
    assert not sent and all(t.status == "not_sent" for t in texts), [(t.status, t.detail) for t in texts]
    hist = c.get("/api/calls").json()
    assert sum(1 for x in hist if x["result"] == "trunk_rejected") == 3
    # the leads come up again for the dialer as new leads
    lead, _ = dialer_mod.pick_next()
    assert lead is not None and lead.name.startswith("Rej"), lead
    s = c.post("/api/dialer/on").json()
    assert s["alert"] == ""  # turning it on again clears the warning
    c.post("/api/dialer/off")
    print("   relabelled 'trunk_rejected', attempts 0, status new, no texts, leads due again; alert cleared on ON")

    # 3) a callback that was refused: the callback time and count come back
    reset_leads(c, "phone,name\n(305) 555-0230,Cb Lead\n(305) 555-0231,Rej A\n(305) 555-0232,Rej B\n")
    due = (NOW - timedelta(minutes=5))
    cb = lead_by("Cb Lead")
    with get_session() as db:
        first = Call(room="ac_old1", number=cb.phone, status="ended", result="answered", outcome="not_now",
                     outcome_source="ai", outcome_note="call me tomorrow", callback_at=due, lead_id=cb.id,
                     created_at=(NOW - timedelta(days=1)), ended_at=(NOW - timedelta(days=1)),
                     answered_at=(NOW - timedelta(days=1)))
        db.add(first)
        db.commit()
        db.refresh(first)
        row = db.get(Lead, cb.id)
        row.attempts, row.last_call_id, row.last_call_at, row.status = 1, first.id, first.created_at, "callback"
        row.callback_at, row.callback_note, row.callback_count = due, "call me tomorrow", 1
        db.add(row)
        db.commit()
    fake.outcomes = ["dial_busy"] * 3
    c.post("/api/dialer/on")
    wait_state(c, lambda s: s["state"] == "off" and s.get("alert"))
    cb = lead_by("Cb Lead")
    assert cb.attempts == 1 and cb.status == "callback" and cb.last_call_id == first.id, (cb.attempts, cb.status, cb.last_call_id)
    assert as_utc(cb.callback_at) == due and cb.callback_count == 1 and cb.callback_note == "call me tomorrow", (cb.callback_at, cb.callback_count)
    print("3. refused callback: callback time, count and status back as before")

    # 4) a manual call (Call page) that continues the streak: also relabelled; the dialer warning stays
    reset_leads(c, "phone,name\n(305) 555-0240,M1\n(305) 555-0241,M2\n(305) 555-0242,M3\n")
    fake.outcomes = ["dial_busy"] * 3
    for name in ("M1", "M2", "M3"):
        r = c.post(f"/api/leads/{lead_by(name).id}/call")
        assert r.status_code == 200, r.text
        end = time.time() + 5
        while time.time() < end and lead_by(name).status == "calling":
            time.sleep(0.05)
        time.sleep(0.2)
    assert [lead_by(n).attempts for n in ("M1", "M2", "M3")] == [0, 0, 0]
    assert c.get("/api/dialer").json()["alert"].startswith("3 calls in a row")
    print("4. manual calls: same rule, dialer warning shown")

    # 5) history repair (calls from before the check, old log rows): relabelled once, callback back
    reset_leads(c, "phone,name\n(305) 555-0250,H0\n(305) 555-0251,H1\n(305) 555-0252,H2\n(305) 555-0253,H3\n")
    t0 = (NOW - timedelta(hours=3))
    with get_session() as db:
        ids = {}
        h0 = db.exec(select(Lead).where(Lead.name == "H0")).first()
        prev = Call(room="ac_h_prev", number=h0.phone, status="ended", result="answered", outcome="gatekeeper",
                    outcome_note="ask for the manager", callback_at=t0, lead_id=h0.id, created_at=t0 - timedelta(days=1),
                    answered_at=t0 - timedelta(days=1), ended_at=t0 - timedelta(days=1))
        db.add(prev)
        good = Call(room="ac_h_good", number="+13055550299", status="ended", result="voicemail", created_at=t0 - timedelta(minutes=5),
                    ended_at=t0 - timedelta(minutes=5), answered_at=t0 - timedelta(minutes=5))
        db.add(good)
        db.commit()
        for i in range(4):
            lead = db.exec(select(Lead).where(Lead.name == f"H{i}")).first()
            call = Call(room=f"ac_h{i}", number=lead.phone, status="ended", result="busy", outcome="no_answer",
                        reason=f"Line busy: {BUSY}", lead_id=lead.id, created_at=t0 + timedelta(seconds=10 * i),
                        ended_at=t0 + timedelta(seconds=10 * i + 3))
            db.add(call)
            db.commit()
            db.refresh(call)
            ids[i] = call.id
            lead.attempts += 1
            lead.last_call_id, lead.last_call_at, lead.status = call.id, call.created_at, "no_answer"
            if i == 0:  # what the old code did to a callback it missed: count +1, moved 2 h on
                lead.attempts = 2  # the earlier (answered) call + this one
                lead.status, lead.callback_count, lead.callback_at = "callback", 2, t0 + timedelta(hours=2)
                db.add(LeadEvent(lead_id=lead.id, kind="status", text=f"Call #{call.id}: No answer; callback missed, trying again later",
                                 data={"call_id": call.id, "result": "busy"}))
            db.add(lead)
            db.commit()
    assert rejections.repair_history() == 4 and rejections.repair_history() == 0
    h0 = lead_by("H0")
    assert h0.attempts == 1 and h0.status == "callback" and as_utc(h0.callback_at) == t0 and h0.callback_count == 1, (h0.attempts, h0.status, h0.callback_at, h0.callback_count)
    assert all(lead_by(f"H{i}").attempts == 0 and lead_by(f"H{i}").status == "new" for i in (1, 2, 3))
    with get_session() as db:
        assert db.get(Call, good.id).result == "voicemail"
    print("5. history repair: 4 old refused calls relabelled once; the missed callback is back; idempotent")

    # 6) Telnyx balance: the dialer stops before calling below $1, red alert
    reset_leads(c, "phone,name\n(305) 555-0260,Bal Lead\n")
    fake.dialed.clear()
    health._telnyx.update(at=time.time(), balance=0.5, currency="USD", error=None)
    c.post("/api/dialer/on")
    s = wait_state(c, lambda s: s["state"] == "off")
    assert fake.dialed == [] and "Telnyx balance is $0.50" in s["alert"], s
    health._telnyx.update(at=0.0, balance=None)
    print("6. Telnyx $0.50: dialer stopped before calling:", s["alert"][:60], "…")

# 7) Health row: fine, a warning below $5, red at $0
for bal, ok, warn, words in ((9.94, True, False, "$9.94 left"), (3.0, True, True, "low"), (0.0, False, False, "switched the account off")):
    health._telnyx.update(at=time.time(), balance=bal, currency="USD", error=None)
    r = asyncio.run(health.check_telnyx_balance())
    assert r["name"] == "Telnyx balance" and r["ok"] == ok and bool(r.get("warn")) == warn and words in r["detail"], r
health._telnyx.update(balance=None)
assert asyncio.run(health.check_telnyx_balance()) is None  # unknown: no row (the Telnyx row says why)
print("7. Health: Telnyx balance row ok / warning below $5 / red at $0")

# 8) "na" names: CSV import, sheet rows, existing leads
assert leads.clean_name(" N/A ") == "" and leads.clean_name("na") == "" and leads.clean_name("NA") == ""
assert leads.clean_name("Nadia") == "Nadia" and leads.clean_name("Na Li") == "Na Li"
p = leads.parse_csv("phone,name\n(305) 555-0270,na\n(305) 555-0271,N/A\n(305) 555-0272,Ana\n")
assert [v["name"] for v in p["valid"]] == ["", "", "Ana"], p["valid"]
row = sheet_sync.parse_row([{"text": "N/A", "rgb": None}, {"text": "(305) 555-0273", "rgb": None}], {"name": 0, "phone": 1}, [])
assert row["name"] == "" and row["phones"] == ["+13055550273"], row
with get_session() as db:
    db.add(Lead(phone="+13055550274", name="na", business="", city="", notes="", status="new", dnc=False, dnc_reason="",
                attempts=1, source="sheet", created_at=NOW, updated_at=NOW))
    db.add(Lead(phone="+13055550275", name="N/A", business="", city="", notes="", status="new", dnc=False, dnc_reason="",
                attempts=0, source="sheet", created_at=NOW, updated_at=NOW))
    db.commit()
assert leads.fix_placeholder_names() == 2 and leads.fix_placeholder_names() == 0
with get_session() as db:
    fixed = db.exec(select(Lead).where(Lead.phone == "+13055550274")).first()
assert fixed.name == "" and fixed.results_dirty
print("8. names 'na' / 'N/A' -> empty (import + existing leads, results row refreshed); 'Nadia' kept")
print("ALL REJECTION TESTS PASSED")
