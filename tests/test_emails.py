"""Phase 7 part B: follow-up emails (templates by outcome, test mode, daily limit, DNC, not configured).
Fake SMTP server on 127.0.0.1 (fake_smtp.py), fake Cartesia, throwaway DB. No calls, no real emails."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_smtp import FakeSMTP  # noqa: E402

DB = f"{TMP}/emails_test.db"
if os.path.exists(DB):
    os.remove(DB)
smtp = FakeSMTP(user="calls@example.test", password="app-password-123")
# Everything email-related is set here BEFORE .env is loaded (load_dotenv never overrides), so the real
# account in .env (if any) is never used. Starts "not configured".
os.environ.update(DATABASE_URL=f"sqlite:///{DB}", CARTESIA_CALLS_DISABLED="1", CARTESIA_SUPERVISOR_TOOL_ID="",
                  CARTESIA_EMAIL_TOOL_ID="", EMAIL_SENDING_DISABLED="", SMTP_HOST="", SMTP_PORT=str(smtp.port),
                  SMTP_USER="", SMTP_PASSWORD="", SMTP_SECURITY="", EMAIL_FROM_NAME="", EMAIL_FROM_ADDRESS="",
                  EMAIL_TEST_MODE="0", EMAIL_TEST_ADDRESS="", IMAP_HOST="")
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile
import env_utils  # noqa: E402,F401

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup, emails, leads  # noqa: E402
from app.db import Call, EmailMessage, Lead, LeadEvent, get_session, init_db  # noqa: E402
from settings import load_settings  # noqa: E402
from sqlmodel import select  # noqa: E402

init_db()
REAL = load_settings()
S = REAL.model_copy(update={"company_address": "123 Example St, Springfield, FL 33139", "company_website": ""})


def with_emails(**kw):
    return S.model_copy(update={"emails": S.emails.model_copy(update=kw)})

emails.load_settings = lambda: S
NOW = datetime.now(timezone.utc)


def configure(**kw):
    os.environ.update(SMTP_HOST="127.0.0.1", SMTP_USER="calls@example.test", SMTP_PASSWORD="app-password-123",
                      EMAIL_FROM_ADDRESS="calls@example.test", EMAIL_FROM_NAME="Taylor at Example", **kw)


n = 0


def lead_and_call(name="Maria Lopez", email="maria@bloom.test", outcome="interested", callback_at=None,
                  email_collected="", email_reason="", dnc=False, service="a new mobile app", business="Bloom Bakery"):
    global n
    n += 1
    with get_session() as s:
        ld = Lead(phone=f"+1305555{7000 + n:04d}", name=name, email=email, service_required=service, business=business,
                  location="Miami, FL 33130", dnc=dnc, status="do_not_contact" if dnc else "new")
        s.add(ld)
        s.flush()
        c = Call(room=f"ac_mail{n}", number=ld.phone, status="ended", result="answered", lead_id=ld.id, outcome=outcome,
                 outcome_source="ai", callback_at=callback_at, email_collected=email_collected, email_reason=email_reason)
        s.add(c)
        s.commit()
        return ld.id, c.id


def last_msg():
    return smtp.messages[-1]["msg"]


# ---- 1. not configured: saved as not sent, nothing breaks ---------------------------------------------------
lid, cid = lead_and_call()
r = emails.send_for_call(cid)
assert r["status"] == "not_sent" and "email not configured" in r["detail"], r
assert not smtp.messages
st = emails.status()
assert st["configured"] is False and "SMTP_HOST" in st["detail"]
print("not configured:", r["detail"])

configure()
# ---- 2. interested -> "Great talking" with next steps; plain text; footer + unsubscribe -------------------------
lid, cid = lead_and_call()
r = emails.send_for_call(cid)
assert r["status"] == "sent" and r["template"] == "interested" and r["to_email"] == "maria@bloom.test", r
m = smtp.messages[-1]
msg = m["msg"]
assert m["to"] == ["maria@bloom.test"] and m["from"] == "calls@example.test" and smtp.logins[-1] == "calls@example.test"
assert msg["Subject"] == "Great talking with you, Maria Lopez" and "Taylor at Example" in msg["From"]
assert msg.get_content_type() == "text/plain" and not msg.is_multipart()
body = msg.get_content()
assert "Hi Maria Lopez," in body and "Bloom Bakery" in body and "a new mobile app" in body and "Next step" in body
for k in ("Example Agency · (945) 555-0142", "123 Example St, Springfield, FL 33139", "Reply STOP or \"unsubscribe\""):
    assert k in body, k
assert msg["List-Unsubscribe"] == "<mailto:calls@example.test?subject=unsubscribe>" and msg["Message-ID"]
assert emails.send_for_call(cid)["id"] == r["id"] and len(smtp.messages) == 1  # one email per call
with get_session() as s:
    ev = [e.text for e in s.exec(select(LeadEvent).where(LeadEvent.lead_id == lid)).all()]
assert any(e.startswith("Email sent (Interested) to maria@bloom.test") for e in ev), ev
print("interested: sent, plain text, footer (company, phone, address, STOP), List-Unsubscribe, once per call")

# ---- 3. not now with a time -> callback email with the time in the lead's zone ----------------------------------
cb = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)  # 10:00 in Miami (EST)
lid, cid = lead_and_call(name="Ken", outcome="not_now", callback_at=cb)
r = emails.send_for_call(cid)
body = last_msg().get_content()
assert r["template"] == "callback" and "give you a call on Thursday, November 5 at 10 AM (your time)." in body, body
flat = body.replace("\r\n", "\n")
assert "\n\n\n" not in flat and flat.count("\n--\n") == 1, repr(body)
lid, cid = lead_and_call(name="Supervisor busy", outcome="transfer_declined", callback_at=None)
assert emails.send_for_call(cid)["template"] == "callback" and "in the next few days" in last_msg().get_content()
lid, cid = lead_and_call(name="Mgr later", outcome="interested", callback_at=cb)
assert emails.send_for_call(cid)["template"] == "callback"  # interested + the senior specialist calls back
print("callback: 'on Thursday, November 5 at 10 AM (your time)'; no time -> 'in the next few days'")

# ---- 4. asked for details (not interested otherwise) -> details email; empty website line dropped --------------
lid, cid = lead_and_call(name="", outcome="not_interested", email_collected="maria@bloom.test", email_reason="details",
                         service="")
r = emails.send_for_call(cid)
body = last_msg().get_content()
assert r["template"] == "details" and "Hi there," in body and "Website:" not in body and "Phone: (945) 555-0142" in body
assert "your project" in body, body
S = S.model_copy(update={"company_website": "https://example.example"})
lid, cid = lead_and_call(outcome="not_interested", email_collected="x@y.test", email_reason="details")
emails.send_for_call(cid)
assert "Website: https://example.example" in last_msg().get_content() and "https://example.example" in last_msg().get_content().split("--")[-1]
lid, cid = lead_and_call(outcome="not_interested")  # no request for details: nothing sent, nothing stored
assert emails.send_for_call(cid) is None
print("details: company info + phone (+ website when set); empty name -> 'Hi there'; plain no -> no email")

# ---- 5. never: Do not contact, wrong number, gatekeeper without an email, no address, voicemail (off) -----------
count = len(smtp.messages)
for kw in ({"outcome": "do_not_contact"}, {"outcome": "wrong_number"}, {"outcome": "gatekeeper"}, {"email": ""},
           {"outcome": "voicemail"}, {"outcome": "needs_review"}):
    lid, cid = lead_and_call(**kw)
    assert emails.send_for_call(cid) is None, kw
lid, cid = lead_and_call(dnc=True)
assert emails.send_for_call(cid) is None
r = emails.send(lid, "interested", call_id=cid)  # even sent by hand: refused for a DNC lead
assert r["status"] == "not_sent" and "Do not contact" in r["detail"]
assert len(smtp.messages) == count
emails.load_settings = lambda: with_emails(after_voicemail=True)
lid, cid = lead_and_call(outcome="voicemail")
assert emails.send_for_call(cid)["template"] == "voicemail_followup" and "Sorry I missed you" in last_msg()["Subject"]
emails.load_settings = lambda: S
print("never emailed: DNC, wrong number, gatekeeper, no address, voicemail (unless switched on), needs review")

# ---- 6. test mode: everything goes to the test address ----------------------------------------------------------
os.environ["EMAIL_TEST_MODE"] = ""  # empty / missing = ON (safe default)
assert emails.test_mode()
lid, cid = lead_and_call()
r = emails.send_for_call(cid)
assert r["status"] == "not_sent" and "EMAIL_TEST_ADDRESS" in r["detail"], r
os.environ["EMAIL_TEST_ADDRESS"] = "me@office.test"
lid, cid = lead_and_call(email="realclient@biz.test")
r = emails.send_for_call(cid)
assert r["status"] == "sent" and r["test_mode"] and r["to_email"] == "me@office.test" and r["lead_email"] == "realclient@biz.test"
assert smtp.messages[-1]["to"] == ["me@office.test"] and last_msg()["Subject"].startswith("[TEST for realclient@biz.test] ")
os.environ["EMAIL_TEST_MODE"] = "0"
print("test mode: on by default, sent to EMAIL_TEST_ADDRESS (subject says who it was for); no address -> not sent")

# ---- 7. problems are saved, never raised ---------------------------------------------------------------------
os.environ["SMTP_PASSWORD"] = "wrong"
lid, cid = lead_and_call()
r = emails.send_for_call(cid)
assert r["status"] == "failed" and "app password" in r["detail"], r
os.environ["SMTP_PASSWORD"] = "app-password-123"
again = emails.retry(r["id"])
assert again["status"] == "sent" and again["call_id"] == cid
try:
    emails.retry(again["id"])
    raise AssertionError("re-sent a sent email")
except emails.EmailError:
    pass
lid, cid = lead_and_call(email="refuse@nowhere.test")
assert emails.send_for_call(cid)["status"] == "failed"
os.environ["SMTP_PORT"] = "1"  # nothing listens there
lid, cid = lead_and_call()
r = emails.send_for_call(cid)
assert r["status"] == "failed" and "couldn't reach the mail server" in r["detail"], r
os.environ["SMTP_PORT"] = str(smtp.port)
os.environ["SMTP_HOST"] = "smtp.example.com"
os.environ["SMTP_SECURITY"] = "none"
r = emails.send(lid, "interested")
assert r["status"] == "failed" and "only allowed" in r["detail"], r  # never a plain-text password over the internet
os.environ.update(SMTP_HOST="127.0.0.1", SMTP_SECURITY="")
S0 = S
S = S.model_copy(update={"company_address": ""})
emails.load_settings = lambda: S
assert "no business address" in emails.send(lid, "interested")["detail"]
S = S0
emails.load_settings = lambda: S
os.environ["EMAIL_SENDING_DISABLED"] = "1"
assert "EMAIL_SENDING_DISABLED" in emails.send(lid, "interested")["detail"]
os.environ["EMAIL_SENDING_DISABLED"] = ""
emails.load_settings = lambda: with_emails(enabled=False)
assert emails.send_for_call(lead_and_call()[1]) is None
emails.load_settings = lambda: S
print("failures saved: wrong password (app password hint) + retry, refused address, server down, no TLS, "
      "no business address, kill switch, switched off")

# ---- 8. daily limit (rolling 24 hours) -------------------------------------------------------------------------
sent = emails.sent_last_24h()
emails.load_settings = lambda: with_emails(daily_limit=sent + 1)
assert emails.send_for_call(lead_and_call()[1])["status"] == "sent"
r = emails.send_for_call(lead_and_call()[1])
assert r["status"] == "not_sent" and "daily limit reached" in r["detail"], r
with get_session() as s:  # yesterday's emails don't count
    for e in s.exec(select(EmailMessage).where(EmailMessage.status == "sent")).all():
        e.sent_at = NOW - timedelta(hours=25)
        s.add(e)
    s.commit()
assert emails.send_for_call(lead_and_call()[1])["status"] == "sent"
emails.load_settings = lambda: S
print(f"daily limit: stops at the limit ({sent + 1}), counts only the last 24 hours")

# ---- 9. end to end: a (fake) call ends interested -> the email goes out by itself ------------------------------
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test")
calls.POLL_EVERY_S = 0.05
calls._silence_settings = lambda: {"enabled": False, "check_s": 8, "max_s": 25}


class Fake:
    calls = {}

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.calls["ac_e2e"] = {"status": "started", "end_reason": None, "transcript": []}
        return "ac_e2e"

    async def get_call(self, cid):
        c = self.calls[cid]
        return {"id": cid, "status": c["status"], "end_reason": c["end_reason"], "transcript": c["transcript"],
                "start_time": "2026-10-28T15:00:00Z", "end_time": "2026-10-28T15:01:00Z"}

    async def cancel_call(self, cid):
        return {}


fake = Fake()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fake.create_call, fake.get_call, fake.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync


async def e2e():
    with get_session() as s:
        ld = Lead(phone="+13055559999", name="Eva End", service_required="logo design", location="Miami, FL 33130")
        s.add(ld)
        s.commit()
        lid = ld.id
    c = await calls.manager.start_call("+17865550100", lead_id=lid)
    await asyncio.sleep(0.3)
    calls.manager.collect_email(c.id, {"email": "eva at end dot test", "confirmed": True, "reason": "interested"}, via="tool")
    fake.calls["ac_e2e"]["transcript"] = [
        {"role": "user", "text": "Yes, I'd love a quote for a logo.", "start_timestamp": 1.0},
        {"role": "assistant", "text": "Great, our specialist will reach out. Have a good one!", "start_timestamp": 3.0,
         "tool_calls": [{"id": "o1", "name": "record_outcome",
                         "arguments": {"outcome": "interested", "note": "wants a logo quote", "callback_time": ""}}]}]
    await asyncio.sleep(0.4)
    fake.calls["ac_e2e"].update(status="completed", end_reason="client_hangup")
    t0 = time.time()
    while time.time() - t0 < 5 and not emails.list_emails(call_id=c.id):
        await asyncio.sleep(0.05)
    return lid, c.id

lid, cid = asyncio.run(e2e())
got = emails.list_emails(call_id=cid)
assert len(got) == 1 and got[0]["status"] == "sent" and got[0]["template"] == "interested", got
assert smtp.messages[-1]["to"] == ["eva@end.test"] and "logo design" in last_msg().get_content()
assert leads.get_lead(lid)["emails"][0]["id"] == got[0]["id"]  # shown on the lead page
print("end to end: email confirmed on the call -> interested -> sent automatically after the call")

# ---- 10. "Send test email" (Health page): only ever to EMAIL_TEST_ADDRESS, no lead, no record ------------------
count, records = len(smtp.messages), len(emails.list_emails(limit=1000))
os.environ["EMAIL_TEST_ADDRESS"] = ""
assert not emails.send_test()["ok"] and len(smtp.messages) == count
os.environ["EMAIL_TEST_ADDRESS"] = "me@office.test"
r = emails.send_test()
assert r["ok"] and smtp.messages[-1]["to"] == ["me@office.test"] and last_msg()["Subject"].startswith("[TEST email"), r
assert "Hi Maria," in last_msg().get_content() and len(emails.list_emails(limit=1000)) == records
os.environ["SMTP_PASSWORD"] = "nope"
assert "app password" in emails.send_test()["detail"]
os.environ["SMTP_PASSWORD"] = "app-password-123"
print("send test email: only to the test address, not stored, clear error on a wrong password")

smtp.stop()
print("ALL EMAIL TESTS PASS")
