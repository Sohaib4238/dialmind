"""Phase 7 part B: STOP / unsubscribe replies -> Do not contact. Fake SMTP + fake IMAP inbox, throwaway DB.
No real email account is touched."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import email
import os
import sys
from email import policy
from email.message import EmailMessage as Mime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_smtp import FakeSMTP  # noqa: E402

DB = f"{TMP}/unsubscribe_test.db"
SEEN = f"{TMP}/unsubscribe_seen.json"
for f in (DB, SEEN):
    if os.path.exists(f):
        os.remove(f)
smtp = FakeSMTP(user="calls@example.test", password="pw")
os.environ.update(DATABASE_URL=f"sqlite:///{DB}", CARTESIA_CALLS_DISABLED="1", CARTESIA_SUPERVISOR_TOOL_ID="",
                  CARTESIA_EMAIL_TOOL_ID="", EMAIL_SENDING_DISABLED="", SMTP_HOST="127.0.0.1", SMTP_PORT=str(smtp.port),
                  SMTP_USER="calls@example.test", SMTP_PASSWORD="pw", SMTP_SECURITY="", EMAIL_FROM_NAME="",
                  EMAIL_FROM_ADDRESS="calls@example.test", EMAIL_TEST_MODE="1", EMAIL_TEST_ADDRESS="me@office.test",
                  IMAP_HOST="imap.fake.test", IMAP_PORT="993")
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile
import env_utils  # noqa: E402,F401

from app import emails, leads, unsubscribe  # noqa: E402
from app.db import Lead, get_session, init_db  # noqa: E402
from pathlib import Path  # noqa: E402
from settings import load_settings  # noqa: E402

init_db()
unsubscribe.SEEN_PATH = Path(SEEN)
S = load_settings().model_copy(update={"company_address": "123 Example St, Springfield, FL 33139"})
emails.load_settings = lambda: S


def new_lead(name, addr, phone):
    with get_session() as s:
        x = Lead(phone=phone, name=name, email=addr, service_required="website", location="Miami, FL 33130")
        s.add(x)
        s.commit()
        return x.id


def reply(frm, subject, text, in_reply_to="", mid=""):
    m = Mime()
    m["From"], m["To"], m["Subject"] = frm, "calls@example.test", subject
    m["Message-ID"] = mid or f"<r{abs(hash((frm, subject, text)))}@mail.test>"
    if in_reply_to:
        m["In-Reply-To"] = m["References"] = in_reply_to
    m.set_content(text)
    return email.message_from_bytes(m.as_bytes(), policy=policy.default)


# Emails went out in test mode (to me@office.test), so replies come from the test address: matched by Message-ID.
ann = new_lead("Ann", "ann@client.test", "+13055558001")
bob = new_lead("Bob", "bob@client.test", "+13055558002")
cat = new_lead("Cat", "cat@client.test", "+13055558003")
sent = {lid: emails.send(lid, "interested") for lid in (ann, bob, cat)}
assert all(r["status"] == "sent" for r in sent.values()), sent
mid = {lid: next(m["msg"]["Message-ID"] for m in smtp.messages if f"[TEST for {e}]" in m["msg"]["Subject"])
       for lid, e in ((ann, "ann@client.test"), (bob, "bob@client.test"), (cat, "cat@client.test"))}

inbox = [
    reply("Me <me@office.test>", "Re: [TEST for ann@client.test] Great talking with you, Ann", "STOP\n\nOn Tue, Taylor wrote:\n> Hi Ann",
          in_reply_to=mid[ann]),
    reply("me@office.test", "Re: Great talking with you, Bob", "Thanks, sounds good. Talk soon!\n> Reply STOP or unsubscribe",
          in_reply_to=mid[bob]),  # the quoted footer says STOP: not a request
    reply("Cat <cat@client.test>", "unsubscribe", ""),  # Gmail's Unsubscribe button (List-Unsubscribe mailto)
    reply("me@office.test", "Re: hello", "Please don't stop calling, I'm interested", in_reply_to=mid[bob]),
    reply("stranger@else.test", "STOP", "stop"),  # no lead: ignored
]
done = unsubscribe.process(inbox)
assert {d["lead_id"] for d in done} == {ann, cat}, done
assert leads.get_lead(ann)["lead"]["dnc"] and leads.get_lead(cat)["lead"]["dnc"] and not leads.get_lead(bob)["lead"]["dnc"]
assert "Email: replied \"STOP\"" in leads.get_lead(ann)["lead"]["dnc_reason"]
print("STOP reply (matched by Message-ID in test mode) and Gmail's unsubscribe -> Do not contact; "
      "quoted footer / 'don't stop' / strangers ignored")

# Each email is handled once: un-marking by hand sticks on the next check.
leads.set_dnc(ann, False)
assert unsubscribe.process(inbox) == [] and not leads.get_lead(ann)["lead"]["dnc"]
leads.set_dnc(ann, True, "back on")

# Never emailed after unsubscribing.
r = emails.send(cat, "details")
assert r["status"] == "not_sent" and "Do not contact" in r["detail"]
print("each reply handled once; no more emails after unsubscribing")

# Phrases
for yes in ("STOP", "stop.", "Unsubscribe", "Please remove me", "remove", "opt out", "Stop emailing me please",
            "take me off your list", "No more emails!"):
    assert unsubscribe.is_stop(reply("a@b.test", "Re: hi", yes)), yes
for no in ("Sounds good", "Can we talk tomorrow?", "Don't stop", "I stopped by your office", "Stop by anytime!"):
    assert not unsubscribe.is_stop(reply("a@b.test", "Re: hi", no)), no
assert unsubscribe.is_stop(reply("a@b.test", "Re: STOP", "")) and unsubscribe.is_stop(reply("a@b.test", "Fwd: Unsubscribe me", ""))
print("phrases: STOP / unsubscribe / remove me / opt out yes; 'stop by' / 'don't stop' no")


# The inbox is read over IMAP, read-only (fake imaplib).
class FakeIMAP:
    log = []

    def __init__(self, host, port, ssl_context=None, timeout=None):
        FakeIMAP.log.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        FakeIMAP.log.append(("logout",))

    def login(self, user, pw):
        FakeIMAP.log.append(("login", user))
        if pw != "pw":
            raise unsubscribe.imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b""]

    def select(self, box, readonly=False):
        FakeIMAP.log.append(("select", box, readonly))
        return "OK", [b"2"]

    def search(self, charset, query):
        FakeIMAP.log.append(("search", query))
        return "OK", [b"1 2"]

    def fetch(self, i, what):
        FakeIMAP.log.append(("fetch", what))
        m = reply("Bob <bob@client.test>", "Re: Great talking", "Unsubscribe" if i == b"2" else "Hi!", mid=f"<imap{i}@x>")
        return "OK", [(b"1 (BODY[] {100}", m.as_bytes()), b")"]


unsubscribe.imaplib.IMAP4_SSL = FakeIMAP
st = unsubscribe.check_now()
assert st["enabled"] and leads.get_lead(bob)["lead"]["dnc"], st
assert ("select", "INBOX", True) in FakeIMAP.log and all(x[1] == "(BODY.PEEK[])" for x in FakeIMAP.log if x[0] == "fetch")
assert ("connect", "imap.fake.test", 993) in FakeIMAP.log
print("IMAP: read-only (EXAMINE + BODY.PEEK), Bob unsubscribed by his own address")

os.environ["SMTP_PASSWORD"] = "bad"
assert "refused the login" in unsubscribe.check_now()["detail"]
os.environ["SMTP_PASSWORD"] = "pw"
os.environ["EMAIL_SENDING_DISABLED"] = "1"
assert not unsubscribe.check_now()["enabled"]
os.environ.update(EMAIL_SENDING_DISABLED="", IMAP_HOST="", SMTP_HOST="smtp.gmail.com")
assert unsubscribe.imap_settings()["host"] == "imap.gmail.com"  # Gmail: no extra setting needed
os.environ["SMTP_HOST"] = "mail.company.test"
assert not unsubscribe.enabled()[0] and "Not set up" in unsubscribe.enabled()[1]
print("login refused -> clear message; off with the kill switch; Gmail IMAP by default; other servers need IMAP_HOST")
smtp.stop()
print("ALL UNSUBSCRIBE TESTS PASS")
