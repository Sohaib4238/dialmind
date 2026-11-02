"""Phase 5 step 5: outcome model -> lead status, callbacks, voicemail retries, DNC. Throwaway DB, no Cartesia."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import sys
from datetime import datetime, timedelta, timezone

DB = f"{TMP}/outcomes_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

import env_utils  # noqa: E402,F401

import app.calls as calls  # noqa: E402
import app.dialer as dialer_mod  # noqa: E402
import app.leads as leads_mod  # noqa: E402
from app import outcomes  # noqa: E402
from app.db import Call, Lead, as_utc, get_session, init_db  # noqa: E402

init_db()
TEST = os.environ["TEST_CALL_NUMBER"]
# Tue 2026-10-27 14:00 UTC = 10:00 New York (morning). Calling hours 09:00-18:00 Mon-Fri.
T0 = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)
clock = {"now": T0}
calls.utcnow = leads_mod.utcnow = lambda: clock["now"]
seq = {"n": 0}


def lead(name: str, phone: str) -> int:
    with get_session() as s:
        x = Lead(phone=phone, name=name, location="Miami, FL 33130", created_at=T0, updated_at=T0)
        s.add(x)
        s.commit()
        return x.id


def call(lead_id: int | None, result: str, ai: str | None = None, note: str = "", callback_at=None,
         number: str = "+13055550199", transcript=None) -> Call:
    """One finished call: started -> (AI outcome recorded) -> final with the given result."""
    seq["n"] += 1
    with get_session() as s:
        c = Call(room=f"ac_o{seq['n']}", number=number, status="dialing", lead_id=lead_id, created_at=clock["now"])
        s.add(c)
        s.commit()
        s.refresh(c)
    leads_mod.on_call_started(c)
    if result in ("answered", "bad_line"):
        calls.apply_status(c.id, "answered", "connected")
    if ai:
        with get_session() as s:
            row = s.get(Call, c.id)
            row.outcome, row.outcome_source, row.outcome_note, row.callback_at = ai, "ai", note, callback_at
            s.add(row)
            s.commit()
    status = "failed" if result == "failed" else "ended"
    return calls.apply_status(c.id, status, "done", result=result, transcript=transcript or [])


def L(lead_id: int) -> Lead:
    with get_session() as s:
        return s.get(Lead, lead_id)


def picks(at: datetime) -> str | None:
    x, _ = dialer_mod.pick_next(at)
    return x.name if x else None


# --- each outcome -> lead status ---------------------------------------------------------
cases = [
    ("Answered, nothing known", "answered", None, "needs_review"),
    ("Interested", "answered", "interested", "interested"),
    ("Not interested", "answered", "not_interested", "not_interested"),
    ("Wrong number", "answered", "wrong_number", "bad_number"),
    ("Abusive", "answered", "do_not_contact", "do_not_contact"),
    ("Voicemail", "voicemail", None, "voicemail"),
    ("Bad line", "bad_line", None, "bad_line"),
    ("No answer", "no_answer", None, "no_answer"),
    ("AI outcome beats bad line", "bad_line", "interested", "interested"),
]
ids = {}
for i, (name, result, ai, want) in enumerate(cases):
    ids[name] = lead(name, f"+1305555{1000 + i}")
    c = call(ids[name], result, ai)
    got = L(ids[name])
    assert got.status == want, (name, got.status, c.outcome, c.outcome_source)
    assert c.outcome == (ai or outcomes.from_result(result)), (name, c.outcome)
assert L(ids["Abusive"]).dnc is True
print(f"{len(cases)} outcome -> status cases OK")

# Final statuses are never picked by the dialer (move everything else out of the way first).
for name in ("Voicemail", "Bad line", "No answer"):
    with get_session() as s:
        x = s.get(Lead, ids[name])
        x.status = "answered"  # parked
        s.add(x)
        s.commit()
assert picks(T0 + timedelta(days=1)) is None
print("interested / not interested / bad number / needs review / DNC: never called again")

# --- not now with a time the lead gave ----------------------------------------------------
nn = lead("Not now 3pm", "+13055552001")
at = datetime(2026, 10, 28, 19, 0, tzinfo=timezone.utc)  # Wed 15:00 New York
c = call(nn, "answered", "not_now", note="busy, call Wed 3pm", callback_at=at)
x = L(nn)
assert x.status == "callback" and as_utc(x.callback_at) == at and x.callback_note == "busy, call Wed 3pm", x
assert picks(at - timedelta(minutes=1)) is None and picks(at) == "Not now 3pm"
print("not now + time: callback at", at.isoformat())

# --- not now without a time -> +3 days; gatekeeper too ---------------------------------------
nn2 = lead("Not now, no time", "+13055552002")
call(nn2, "answered", "not_now")
assert as_utc(L(nn2).callback_at) == T0 + timedelta(days=3), L(nn2).callback_at
gk = lead("Gatekeeper", "+13055552003")
call(gk, "answered", "gatekeeper", note="owner in after 2pm")
assert L(gk).status == "callback" and L(gk).callback_note == "owner in after 2pm"
print("not now / gatekeeper without a time: callback in 3 days")

# --- missed callback -> stays callback (after the retry gap); max_callbacks -> needs review -------
clock["now"] = at
call(nn, "no_answer")
x = L(nn)
assert x.status == "callback" and as_utc(x.callback_at) == at + timedelta(hours=2) and x.callback_count == 2, (x.status, x.callback_at, x.callback_count)
call(nn, "answered", "not_now")  # 3rd
call(nn, "answered", "not_now")  # 4th > max_callbacks (3)
assert L(nn).status == "needs_review" and L(nn).callback_at is None
print("missed callback retried after the gap; 4th callback in a row -> needs review")

# --- callback reached, other outcome: callback cleared --------------------------------------
clock["now"] = T0
call(gk, "answered", "interested")
x = L(gk)
assert x.status == "interested" and x.callback_at is None and x.callback_count == 0

# --- voicemail retries: max 2, each at another time of day ---------------------------------
for i in list(ids.values()) + [nn2]:  # park everything else
    with get_session() as s:
        y = s.get(Lead, i)
        y.status = "answered"
        s.add(y)
        s.commit()
vm = lead("VM lead", "+13055553001")
clock["now"] = T0  # 10:00 morning
call(vm, "voicemail")
assert L(vm).vm_count == 1
assert picks(T0 + timedelta(hours=1)) is None  # same morning, gap not met
assert picks(T0 + timedelta(hours=1, minutes=59)) is None  # still morning (11:59)
assert picks(T0 + timedelta(hours=2)) == "VM lead"  # 12:00 = afternoon, gap met
clock["now"] = T0 + timedelta(hours=4)  # 14:00 afternoon
call(vm, "voicemail")
assert picks(T0 + timedelta(hours=7)) is None  # 17:00 afternoon again: not yet
nxt_morning = T0 + timedelta(days=1)  # Wed 10:00
assert picks(nxt_morning) == "VM lead"
clock["now"] = nxt_morning
call(vm, "voicemail")  # 3rd voicemail = 2 retries used
assert L(vm).vm_count == 3 and picks(T0 + timedelta(days=1, hours=5)) is None and picks(T0 + timedelta(days=2)) is None
print("voicemail: retried afternoon, then morning; stops after 2 retries")

# --- AI voicemail end + no_answer counts; test number never DNC'd ------------------------------
c = call(None, "answered", "do_not_contact", number=TEST)
assert leads_mod.dnc_block_reason(TEST) is None
c = call(None, "answered", "do_not_contact", number="+13125550100")
assert leads_mod.dnc_block_reason("+13125550100")
print("manual call DNC: the number is listed, but never the test phone")

# --- parse_callback ------------------------------------------------------------------------
NY = "America/New_York"
assert outcomes.parse_callback("2026-10-28T15:00", NY, T0) == datetime(2026, 10, 28, 19, 0, tzinfo=timezone.utc)
assert outcomes.parse_callback("2026-10-28 9:30", "America/Chicago", T0) == datetime(2026, 10, 28, 14, 30, tzinfo=timezone.utc)
assert outcomes.parse_callback("2026-10-29", NY, T0) == datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc)
for bad in ("", "tomorrow", "2026-10-27T09:00", "2027-06-01T10:00", "2026-13-01T10:00", "2026-10-28T25:00"):
    assert outcomes.parse_callback(bad, NY, T0) is None, bad
print("callback time parsing OK (past / too far / garbage -> default)")

# --- a day in plain words -> that day, 10:00 their time, in calling hours (T0 = Tue 10:00 New York) ---
HOURS = dialer_mod.load_settings().settings.calling_hours
assert (HOURS.start, HOURS.end, HOURS.days) == ("09:00", "18:00", ["mon", "tue", "wed", "thu", "fri"])


def utc(month, day, hour):
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)


# New York: 10:00 = 14:00 UTC until Nov 1, then 15:00 UTC
for words, want in (("tomorrow", utc(10, 28, 14)), ("I can't decide it, so we will talk tomorrow.", utc(10, 28, 14)),
                    ("Said to call back tomorrow, no specific time given.", utc(10, 28, 14)),
                    ("Any time tomorrow", utc(10, 28, 14)), ("tomorrow afternoon", utc(10, 28, 18)), ("Tomorrow morning works", utc(10, 28, 14)),
                    ("tomorrow evening", utc(10, 28, 21)), ("the day after tomorrow", utc(10, 29, 14)),
                    ("Friday", utc(10, 30, 14)), ("call me on Monday", utc(11, 2, 15)), ("next Monday", utc(11, 2, 15)),
                    ("Tuesday", utc(11, 3, 15)),  # today is Tuesday: the next one
                    ("next week", utc(11, 2, 15)), ("Try me next month", utc(11, 2, 15)),  # Nov 1 is a Sunday: on to Monday
                    ("Saturday", utc(11, 2, 15)),  # not a calling day: on to Monday
                    ("not tomorrow, next week", utc(11, 2, 15)), ("I'm busy tomorrow, try Thursday", utc(10, 29, 14)),
                    ("Don't call until Monday", utc(11, 2, 15)), ("Can't talk now call me tomorrow", utc(10, 28, 14)),
                    ("Have him reach out tomorrow", utc(10, 28, 14))):
    assert outcomes.callback_from_words(words, NY, T0, HOURS) == want, (words, outcomes.callback_from_words(words, NY, T0, HOURS))
for words in ("", None, "At any time you can call me.", "later", "I'm busy tomorrow", "Tomorrow I'm busy", "not on Monday",
              "We launched last Monday", "Monday was a bad day"):
    assert outcomes.callback_from_words(words, NY, T0, HOURS) is None, words
# their own date, not ours: 23:00 Tuesday in New York is already Wednesday in UTC
assert outcomes.callback_from_words("tomorrow", NY, datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc), HOURS) == utc(10, 28, 14)
assert outcomes.callback_from_words("tomorrow", "America/Los_Angeles", T0, HOURS) == utc(10, 28, 17)
# Friday's "tomorrow" goes to Monday; other calling hours are respected


class Hours:
    start, end, days = "11:30", "13:00", ["mon", "wed"]


assert outcomes.callback_from_words("tomorrow", NY, datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc), HOURS) == utc(11, 2, 15)
assert outcomes.callback_from_words("tomorrow", NY, T0, Hours) == datetime(2026, 10, 28, 15, 30, tzinfo=timezone.utc)  # 11:30 NY
assert outcomes.callback_from_words("tomorrow evening", NY, T0, Hours) == datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)  # 12:00 NY
print("callback from plain words OK (day named, 10:00 their time, calling hours and days)")

# --- old statuses are renamed by the migration -----------------------------------------------
from sqlalchemy import text  # noqa: E402
from app.db import engine  # noqa: E402
with engine.begin() as conn:
    conn.execute(text("ALTER TABLE lead DROP COLUMN vm_count"))
    conn.execute(text("UPDATE lead SET status = 'not_now' WHERE name = 'Interested'"))
init_db()
assert L(ids["Interested"]).status == "callback"
print("ALL OUTCOME TESTS PASS")
