"""Offline tests for follow-up texts (Phase 7b): fake Telnyx API, fake key, throwaway DB + test-mode file. No real SMS.

Rules: not answered -> text; polite (not interested / not now / interested) -> text; rude (do_not_contact),
transferred, wrong number -> no text; DNC lead never; ONE text per lead, ever; daily total; outside calling hours -> queued.
Plus: test mode (every text to the test number + the hard guard), templates, STOP replies and delivery reports through
the signed webhook, the messaging-profile webhook guard, the Health "Send test SMS".
"""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

DIR = TMP
DB, STATE = f"{DIR}/sms_test.db", f"{DIR}/sms_state.json"
for f in (DB, STATE):
    if os.path.exists(f):
        os.remove(f)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["DIALER_STATE_PATH"] = STATE  # missing file = test mode ON; never the real switch
os.environ["SMS_SENDING_DISABLED"] = "1"  # lifted below, in-process, once the fake Telnyx is in place
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""
os.environ["TELNYX_API_KEY"] = "KEY_fake_for_tests"  # even a real request would be refused by Telnyx
os.environ["TELNYX_SMS_FROM"] = "+13025550199"
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""
os.environ["DASHBOARD_PASSWORD_HASH"] = ""
os.environ["EMAIL_SENDING_DISABLED"] = "1"
TEST = "+17865550100"
os.environ["TEST_CALL_NUMBER"] = TEST

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup, sms, test_mode, tunnel  # noqa: E402
from app.db import Call, Lead, LeadEvent, SmsMessage, get_session  # noqa: E402
from app.main import app  # noqa: E402
from settings import load_settings  # noqa: E402

calls.POLL_EVERY_S = 0.05
tunnel.watch = lambda *a, **k: __import__("asyncio").sleep(0)
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_WEBHOOK_SECRET="s3cret")

# ---- fake Telnyx ----
posts, gets, profile = [], [], {"name": "DialMind", "webhook_url": ""}


def fake_post(path, body, method="POST"):
    posts.append((method, path, body))
    if path == "/messages":
        return {"data": {"id": f"msg_{len(posts)}", "to": [{"status": "queued"}]}}
    profile.update(body)
    return {"data": profile}


def fake_get(path):
    gets.append(path)
    return {"data": dict(profile)}

sms._post, sms._get = fake_post, fake_get
os.environ["SMS_SENDING_DISABLED"] = "0"  # in-process only: every request goes to the fake above
texts = lambda: [b for m, p, b in posts if p == "/messages"]  # noqa: E731

# Tue 2026-10-27 15:00 UTC = 11:00 in Miami (inside 09:00-18:00 Mon-Fri)
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
clock = {"now": NOW}
sms.utcnow = lambda: clock["now"]

S = load_settings()
sms.load_settings = lambda: S  # tests change limits on this copy only


# ---- fake Cartesia (only for the end-to-end check) ----
created = []


async def fake_create(agent_id, from_id, to, variables, ring, max_min):
    test_mode.guard(to)
    created.append(to)
    return f"ac_sms{time.monotonic_ns()}"


async def fake_get_call(cid):
    return {"id": cid, "status": "completed", "end_reason": "dial_no_answer", "transcript": []}


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia.create_call, cartesia.get_call, cartesia_setup.sync_agent = fake_create, fake_get_call, fake_sync

n_calls = [0]


def lead(name):
    with get_session() as s:
        return s.exec(select(Lead).where(Lead.name == name)).one()


def finished_call(name, outcome, **kw):
    """A finished call with this outcome for the lead (as calls.py leaves it), then the follow-up text."""
    n_calls[0] += 1
    with get_session() as s:
        c = Call(room=f"ac_t{n_calls[0]}", number=TEST, status="ended", result="answered", lead_id=lead(name).id,
                 outcome=outcome, **kw)
        s.add(c)
        s.commit()
        s.refresh(c)
    return sms.for_call(c.id)


def set_lead(name, **kw):
    with get_session() as s:
        row = s.exec(select(Lead).where(Lead.name == name)).one()
        for k, v in kw.items():
            setattr(row, k, v)
        s.add(row)
        s.commit()


def signed(client, body: dict, key=None):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = base64.b64encode((key or PRIV).sign(f"{ts}|".encode() + raw)).decode()
    return client.post("/api/telnyx/messaging", content=raw,
                       headers={"telnyx-signature-ed25519": sig, "telnyx-timestamp": ts, "content-type": "application/json"})


PRIV = Ed25519PrivateKey.generate()
os.environ["TELNYX_PUBLIC_KEY"] = base64.b64encode(PRIV.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

# 0) unit checks: STOP words, template text, calling-hours math
for t, want in [("STOP", True), ("stop", True), ("Stop.", True), ("unsubscribe", True), ("QUIT", True), ("End", True),
                ("cancel", True), ("Stop texting me", True), ("please stop", True), ("opt out", True), ("STOPALL", True),
                ("stop by tomorrow?", False), ("end of the day works", False), ("Yes call me", False), ("", False)]:
    assert sms.is_stop(t) is want, (t, want)
x = Lead(phone="+13055550999", name="", service_required="", location="Miami FL")
body = sms.build(S, "not_answered", x)
assert S.company_name in body and "Reply STOP to opt out" in body and "Hi there" in body, body
S.sms.interested = "Hello {lead_name}!"  # a template without company name / STOP: both get added
assert sms.build(S, "interested", x) == f"{S.company_name}: Hello there! Reply STOP to opt out."
S.sms.interested = load_settings().sms.interested
cb = datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)  # 10:00 in Miami
assert "on Friday, October 30 at 10 AM (your time)" in sms.build(S, "not_now", x, cb)
hours = S.settings.calling_hours
assert sms.next_send_time("America/New_York", hours, NOW) == NOW
assert sms.next_send_time("America/New_York", hours, datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)) \
    == datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc)  # Mon 23:00 -> Tue 09:00 Miami
assert sms.next_send_time("America/New_York", hours, datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)) \
    == datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)  # Saturday -> Monday 09:00 (EST after Nov 1)
print("0. STOP words, templates (company + STOP always), calling-hours math")

with TestClient(app) as c:
    names = ["No Answer", "Voicemail", "Polite No", "Not Now", "Keen", "Rude", "Transferred", "Wrong", "Dnc Lead",
             "Night Owl", "Repeat", "Live Lead", "Unknown Zone", "Details"]
    csv = "phone,name,location\n" + "".join(f"(305) 555-02{10 + i},{n},Miami FL 33130\n" for i, n in enumerate(names))
    c.post("/api/leads/import", json={"csv": csv, "filename": "t.csv"})
    set_lead("Unknown Zone", phone="+15555550299", location="")

    # 1) not answered -> ALWAYS a text (template not_answered); voicemail too; and through the real call flow
    r = finished_call("No Answer", "no_answer")
    assert r["status"] == "sent" and r["template"] == "not_answered" and r["to_phone"] == TEST and r["test_mode"], r
    assert texts()[-1] == {"from": "+13025550199", "to": TEST, "text": r["body"]}
    assert finished_call("Voicemail", "voicemail")["template"] == "not_answered"
    print("1. not answered / voicemail -> text (to the test number):", r["body"][:70], "…")

    # 2) answered + polite -> text, one template each
    assert finished_call("Polite No", "not_interested")["template"] == "not_interested"
    r = finished_call("Not Now", "not_now", callback_at=cb)
    assert r["template"] == "not_now" and "October 30 at 10 AM" in r["body"], r["body"]
    assert finished_call("Keen", "interested")["template"] == "interested"
    assert finished_call("Details", "needs_review", email_collected="d@x.test")["template"] == "interested"
    print("2. polite: not_interested / not_now (with the callback time) / interested / asked for details -> text")

    # 3) rude (do_not_contact), transferred, wrong number, failed, unknown -> NO text
    before = len(texts())
    for name, o in (("Rude", "do_not_contact"), ("Transferred", "transferred"), ("Wrong", "wrong_number"),
                    ("Live Lead", "failed"), ("Live Lead", "needs_review")):
        assert finished_call(name, o) is None, o
    assert len(texts()) == before
    print("3. rude / transferred / wrong number / failed / needs review -> no text")

    # 4) a DNC lead is never texted (not even recorded), also when it went DNC after the text was queued
    c.post(f"/api/leads/{lead('Dnc Lead').id}/dnc", json={"dnc": True, "reason": "test"})
    assert finished_call("Dnc Lead", "no_answer") is None and len(texts()) == before
    with get_session() as s:
        assert not s.exec(select(SmsMessage).where(SmsMessage.lead_id == lead("Dnc Lead").id)).all()
    print("4. DNC lead -> never texted")

    # 5) ONE text per lead, ever: not the next day, not a month later, not with another outcome
    r = finished_call("No Answer", "no_answer")
    assert r["status"] == "not_sent" and "already got a text (one per lead, ever)" in r["detail"] and len(texts()) == before, r
    for later, o in ((timedelta(hours=25), "no_answer"), (timedelta(days=40), "interested"), (timedelta(days=400), "not_now")):
        clock["now"] = NOW + later
        r2 = finished_call("No Answer", o)
        assert r2["status"] == "not_sent" and "one per lead, ever" in r2["detail"] and len(texts()) == before, (o, r2)
    clock["now"] = NOW
    assert not hasattr(load_settings().sms, "per_lead_daily_limit")
    print("5. one text per lead, ever (25 h / 40 days / 400 days, other outcomes):", r["detail"])

    # 6) daily total limit
    S.sms.daily_limit = sms.sent_last_24h()
    r = finished_call("Repeat", "no_answer")
    assert r["status"] == "not_sent" and "daily limit reached" in r["detail"], r
    S.sms.daily_limit = 50
    with get_session() as s:  # let Repeat be texted again later in this test
        for m in s.exec(select(SmsMessage).where(SmsMessage.lead_id == lead("Repeat").id)).all():
            s.delete(m)
        s.commit()
    print("6. daily total limit:", r["detail"])

    # 7) outside calling hours -> queued, sent when the lead's hours start; DNC meanwhile -> not sent
    clock["now"] = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)  # Mon 23:00 in Miami
    before = len(texts())
    q = finished_call("Night Owl", "no_answer")
    assert q["status"] == "queued" and q["send_after"].startswith("2026-10-27T13:00") and len(texts()) == before, q
    assert "Tue Oct 27, 09:00 their time" in q["detail"], q["detail"]
    assert finished_call("Night Owl", "no_answer")["status"] == "not_sent"  # the queued one counts
    assert sms.process_queue(datetime(2026, 10, 27, 12, 59, tzinfo=timezone.utc)) == []
    clock["now"] = datetime(2026, 10, 27, 13, 0, 30, tzinfo=timezone.utc)
    done = sms.process_queue(clock["now"])
    assert [d["status"] for d in done] == ["sent"] and texts()[-1]["to"] == TEST
    clock["now"] = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)
    q2 = finished_call("Repeat", "no_answer")
    c.post(f"/api/leads/{lead('Repeat').id}/dnc", json={"dnc": True, "reason": "asked"})
    done = sms.process_queue(datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc))
    assert q2["status"] == "queued" and done[0]["status"] == "not_sent" and "Do not contact" in done[0]["detail"]
    assert finished_call("Unknown Zone", "no_answer")["detail"].startswith("not sent: the lead's time zone is unknown")
    clock["now"] = NOW
    print("7. outside calling hours -> queued, sent at 09:00 their time; DNC before then -> not sent")

    # 8) test mode: hard guard; off -> the lead's own number; a text made in test mode stays a test text
    n = len(posts)
    try:
        sms.send_raw("+13055550221", "hi")
        raise AssertionError("guard didn't block")
    except sms.SmsError as e:
        assert "Blocked: TEST MODE is on" in str(e) and "No text sent" in str(e) and len(posts) == n
    clock["now"] = datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc)
    q3 = finished_call("Wrong", "interested")  # queued in test mode (a lead with no text yet)
    assert q3["status"] == "queued"
    test_mode.set_on(False)
    clock["now"] = NOW + timedelta(days=1)
    r = finished_call("Live Lead", "not_interested")
    assert r["to_phone"] == lead("Live Lead").phone and not r["test_mode"] and texts()[-1]["to"] == lead("Live Lead").phone
    done = sms.process_queue(datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc))
    assert done[0]["to_phone"] == TEST and done[0]["test_mode"], done  # still the test phone
    # a test-mode text (to the test phone) doesn't use up the lead's one real text; a real one blocks everything
    assert finished_call("Live Lead", "interested")["detail"].endswith("one per lead, ever)")  # live: already texted
    test_mode.set_on(True)
    assert finished_call("Live Lead", "interested")["status"] == "sent"  # its first test-mode text
    assert "one per lead, ever" in finished_call("Live Lead", "interested")["detail"]  # and only one of those too
    clock["now"] = NOW
    print("8. test mode: guard blocks other numbers; off -> lead's number; queued test text stays on the test phone")

    # 9) the real call flow sends the text after the call ends (clock: later than every text above)
    clock["now"] = NOW + timedelta(days=10)
    before = len(texts())
    set_lead("Transferred", status="new")
    cid = c.post(f"/api/leads/{lead('Transferred').id}/call").json()["id"]
    end = time.time() + 5
    while time.time() < end and len(texts()) == before:
        time.sleep(0.05)
    lst = c.get("/api/sms", params={"call_id": cid}).json()
    assert lst and lst[0]["template"] == "not_answered" and lst[0]["status"] == "sent", lst
    print("9. real call flow (no answer) -> text after the call; /api/sms lists it")

    # 10) webhook: signature required; delivery reports; STOP from the test phone -> that lead DNC; other replies logged
    bad = signed(c, {"data": {"id": "evx", "event_type": "message.received"}}, key=Ed25519PrivateKey.generate())
    assert bad.status_code == 401
    msg_id = next(m for m in sms.list_texts(call_id=cid))
    with get_session() as s:
        tid = s.get(SmsMessage, msg_id["id"]).telnyx_id
    r = signed(c, {"data": {"id": "ev1", "event_type": "message.finalized",
                            "payload": {"id": tid, "to": [{"phone_number": TEST, "status": "delivered"}]}}}).json()
    assert r["status"] == "delivered" and c.get("/api/sms", params={"call_id": cid}).json()[0]["status"] == "delivered"
    assert signed(c, {"data": {"id": "ev1", "event_type": "message.finalized", "payload": {}}}).json().get("duplicate")
    last_lead = lead("Transferred")
    r = signed(c, {"data": {"id": "ev2", "event_type": "message.received",
                            "payload": {"from": {"phone_number": TEST}, "text": "Sounds good, thanks"}}}).json()
    assert r["lead_id"] == last_lead.id and not r.get("stop") and not lead("Transferred").dnc
    r = signed(c, {"data": {"id": "ev3", "event_type": "message.received",
                            "payload": {"from": {"phone_number": TEST}, "text": "STOP"}}}).json()
    assert r["stop"] and r["lead_id"] == last_lead.id and lead("Transferred").dnc
    with get_session() as s:  # the test phone itself never goes on DNC
        assert not any(x.dnc for x in s.exec(select(Lead).where(Lead.phone == TEST)).all())
    r = signed(c, {"data": {"id": "ev4", "event_type": "message.received",
                            "payload": {"from": {"phone_number": lead("Polite No").phone}, "text": "unsubscribe"}}}).json()
    assert r["stop"] and lead("Polite No").dnc
    n_calls = len(created)
    blocked = c.post(f"/api/leads/{lead('Polite No').id}/call")  # after STOP the lead is never called again either
    assert blocked.status_code >= 400 and "Do-Not-Contact" in blocked.text and len(created) == n_calls, blocked.text
    r = signed(c, {"data": {"id": "ev5", "event_type": "message.received",
                            "payload": {"from": {"phone_number": "+13055550277"}, "text": "Stop"}}}).json()
    assert r["stop"] and r["lead_id"] is None
    with get_session() as s:
        assert s.exec(select(Lead).where(Lead.phone == "+13055550277")).one().dnc  # a stranger's STOP: DNC number
        ev = s.exec(select(LeadEvent).where(LeadEvent.lead_id == last_lead.id, LeadEvent.kind == "sms")).all()
    assert any("Text reply" in e.text for e in ev) and any("delivered" in e.text for e in ev)
    clock["now"] = NOW
    print("10. webhook: signature checked, delivered status, replies logged, STOP -> Do not contact")

    # 11) lead page + status + retry
    d = c.get(f"/api/leads/{lead('No Answer').id}").json()
    assert d["texts"] and {t["status"] for t in d["texts"]} >= {"sent", "not_sent"}
    st = c.get("/api/sms/status").json()
    assert st["configured"] and st["test_mode"] and st["test_number"] == TEST and st["from_number"] == "+13025550199"
    failed = next(t for t in d["texts"] if t["status"] == "not_sent")
    clock["now"] = NOW + timedelta(days=3)
    r = c.post(f"/api/sms/{failed['id']}/retry").json()  # the lead already got its one text: never a second
    assert r["status"] == "not_sent" and "one per lead, ever" in r["detail"], r
    assert c.post(f"/api/sms/{d['texts'][-1]['id']}/retry").status_code in (200, 400)
    os.environ["SMS_SENDING_DISABLED"] = "1"  # a lead whose only text was never sent can still get one
    first = sms.create(lead("Rude").id, "not_answered")
    os.environ["SMS_SENDING_DISABLED"] = "0"
    assert first["status"] == "not_sent" and c.post(f"/api/sms/{first['id']}/retry").json()["status"] == "sent"
    clock["now"] = NOW
    print("11. lead page lists texts; /api/sms/status; retry")

    # 12) Health "Send test SMS": exactly one text, to the test number only (also with test mode off)
    n = len(texts())
    r = c.post("/api/sms/test").json()
    assert r["ok"] and len(texts()) == n + 1 and texts()[-1]["to"] == TEST and texts()[-1]["text"].startswith("[TEST]")
    test_mode.set_on(False)
    assert c.post("/api/sms/test").json()["ok"] and texts()[-1]["to"] == TEST
    test_mode.set_on(True)
    os.environ["SMS_SENDING_DISABLED"] = "1"
    r = c.post("/api/sms/test").json()
    assert not r["ok"] and "SMS_SENDING_DISABLED" in r["detail"] and len(texts()) == n + 2
    os.environ["SMS_SENDING_DISABLED"] = "0"
    print("12. Send test SMS -> one text to the test number; kill switch respected")

# 13) messaging-profile webhook: set on our own profile, never changed when another app owns it
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = "prof_test"
r = sms.ensure_webhook("https://abc.trycloudflare.com")
assert r["ok"] and r["action"] == "updated" and profile["webhook_url"] == "https://abc.trycloudflare.com/api/telnyx/messaging"
assert sms.ensure_webhook("https://abc.trycloudflare.com")["action"] == "unchanged"
assert sms.ensure_webhook("https://new.trycloudflare.com")["action"] == "updated"  # our old tunnel URL -> re-pointed
profile.update(webhook_url="https://dialpad.example.com/api/webhooks/telnyx/messaging?token=secret")
n = len(posts)
r = sms.ensure_webhook("https://abc.trycloudflare.com")
assert not r["ok"] and "another app (dialpad.example.com)" in r["detail"] and "secret" not in r["detail"] and len(posts) == n
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""
print("13. messaging webhook: our profile re-pointed; another app's profile never touched")

os.path.exists(STATE) and os.remove(STATE)
print("ALL SMS TESTS PASS")
