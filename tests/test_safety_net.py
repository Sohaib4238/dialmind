"""Phase 5 step 9: transcript safety net (DNC / wrong number phrases) + needs_review. No Cartesia, throwaway DB."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import sys

DB = f"{TMP}/safety_test.db"
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
import app.leads as leads_mod  # noqa: E402
from app import outcomes  # noqa: E402
from app.db import Call, Lead, get_session, init_db  # noqa: E402

init_db()


def T(*callee, agent="Sure, thanks."):
    turns, t = [], 0.5
    for line in callee:
        turns.append({"role": "user", "text": line, "start_timestamp": t})
        turns.append({"role": "assistant", "text": agent, "start_timestamp": t + 2})
        t += 5
    return turns


dnc = ["Stop calling me.", "Please take me off your list", "Don't call this number again", "Remove my number from your database",
       "Put me on the do not call list", "Fuck off", "Go to hell, you scammer", "This is harassment", "Lose my number",
       "Never call here again!", "I’m telling you, don’t call me"]
wrong = ["Sorry, wrong number", "You've got the wrong business", "There's no Maria here", "Nobody by that name",
         "She doesn't work here anymore", "They went out of business last year"]
neither = ["Hello?", "Our website is fucking slow honestly", "Don't call me before 10, I'm busy in the mornings",
           "Call me back next week", "I'm not interested, thanks", "Who gave you this number?", "Sure, I'm interested",
           "Can you take me through the price list?"]
for x in dnc:
    assert outcomes.transcript_rules(T(x), "answered")[0] == "do_not_contact", x
for x in wrong:
    assert outcomes.transcript_rules(T(x), "answered")[0] == "wrong_number", x
for x in neither:
    assert outcomes.transcript_rules(T(x), "answered") is None, (x, outcomes.transcript_rules(T(x), "answered"))
assert outcomes.transcript_rules(T("Stop calling"), "no_answer") is None  # nobody talked
print(f"phrases: {len(dnc)} DNC, {len(wrong)} wrong number, {len(neither)} neither - OK")

# Priority: DNC phrase beats the AI; the AI beats a wrong-number phrase; nothing -> needs_review
assert outcomes.decide("interested", "ai", "answered", T("Fine, but stop calling me after this")) [:2] == ("do_not_contact", "transcript")
assert outcomes.decide("not_now", "ai", "answered", T("Wrong number? no, it's me, call later"))[:2] == ("not_now", "ai")
assert outcomes.decide(None, "", "answered", T("wrong number buddy"))[:2] == ("wrong_number", "transcript")
assert outcomes.decide(None, "", "answered", T("Hello?", "Uh huh."))[:2] == ("needs_review", "call")
assert outcomes.decide(None, "", "bad_line", T("Who is this? Stop calling me"))[:2] == ("do_not_contact", "transcript")
print("priority: DNC phrase > AI > other phrases > call end")

# End to end: an answered call where the AI forgot the tool -> lead DNC from the transcript.
with get_session() as s:
    lead = Lead(phone="+13055556001", name="Forgot Tool", location="Miami FL 33130")
    s.add(lead)
    s.commit()
    call = Call(room="ac_sn1", number="+17865550100", status="dialing", lead_id=lead.id)
    s.add(call)
    s.commit()
    lid, cid = lead.id, call.id
leads_mod.on_call_started(call)
calls.apply_status(cid, "answered", "connected")
c = calls.apply_status(cid, "ended", "Callee hung up", result="answered",
                       transcript=T("Hello?", "Take me off your list, seriously."))
with get_session() as s:
    lead = s.get(Lead, lid)
assert c.outcome == "do_not_contact" and c.outcome_source == "transcript" and lead.dnc and lead.status == "do_not_contact", \
    (c.outcome, c.outcome_source, lead.dnc, lead.status)
assert "take me off your list" in c.outcome_note.lower(), c.outcome_note
print("end to end: transcript DNC ->", lead.dnc_reason)
print("ALL SAFETY NET TESTS PASS")
