"""Phase 5 step 10: set a lead's outcome by hand (lead page). Throwaway DB, no Cartesia, tunnel watcher off."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import sys
from datetime import date, timedelta

DB = f"{TMP}/manual_outcome_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile
import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import leads as leads_mod, tunnel  # noqa: E402
from app.db import Call, Lead, get_session  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel
from app.main import app  # noqa: E402

with TestClient(app) as c:
    with get_session() as s:
        lead = Lead(phone="+15125557001", name="Review Rita", location="Austin, TX 78701")
        s.add(lead)
        s.commit()
        call = Call(room="ac_m1", number="+17865550100", status="dialing", lead_id=lead.id)
        s.add(call)
        s.commit()
        lid, cid = lead.id, call.id
    leads_mod.on_call_started(call)
    calls.apply_status(cid, "answered", "connected")
    calls.apply_status(cid, "ended", "hung up", result="answered", transcript=[{"role": "user", "text": "Hmm, maybe.", "start_timestamp": 1}])
    assert c.get(f"/api/leads/{lid}").json()["lead"]["status"] == "needs_review"

    # reviewed: not now, Friday 11:00 their time (Central)
    day = date.today() + timedelta(days=3)
    r = c.post(f"/api/leads/{lid}/outcome", json={"outcome": "not_now", "note": "call Friday late morning",
                                                   "callback_time": f"{day.isoformat()}T11:00"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "callback" and d["callback_at"][:13] in (f"{day.isoformat()}T16", f"{day.isoformat()}T17"), d["callback_at"]  # 11:00 CDT/CST -> 16/17 UTC
    call_d = c.get(f"/api/calls/{cid}").json()["call"]
    assert call_d["outcome"] == "not_now" and call_d["outcome_source"] == "manual" and call_d["outcome_note"] == "call Friday late morning"
    print("needs review -> not now by hand: callback", d["callback_at"])

    # errors: bad outcome, past time
    assert c.post(f"/api/leads/{lid}/outcome", json={"outcome": "voicemail"}).status_code == 400
    assert c.post(f"/api/leads/{lid}/outcome", json={"outcome": "not_now", "callback_time": "2020-01-01T10:00"}).status_code == 400
    assert c.post("/api/leads/999/outcome", json={"outcome": "interested"}).status_code == 404

    # interested clears the callback
    d = c.post(f"/api/leads/{lid}/outcome", json={"outcome": "interested"}).json()
    assert d["status"] == "interested" and d["callback_at"] is None
    # DNC by hand, then other outcomes are refused until it's removed
    d = c.post(f"/api/leads/{lid}/outcome", json={"outcome": "do_not_contact", "note": "asked by email"}).json()
    assert d["dnc"] and d["status"] == "do_not_contact"
    assert c.post(f"/api/leads/{lid}/outcome", json={"outcome": "interested"}).status_code == 400
    ev = [e["text"] for e in c.get(f"/api/leads/{lid}").json()["events"]]
    assert any("Outcome set by hand: Interested" in x for x in ev)
    print("errors rejected; interested clears callback; DNC by hand sticks")
print("ALL MANUAL OUTCOME TESTS PASS")
