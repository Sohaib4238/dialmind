"""Offline tests for leads: CSV preview/import, counters, DNC, lead calls (fake Cartesia, no call)."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import sys
import time

DB = f"{TMP}/leads_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

from fastapi.testclient import TestClient  # noqa: E402

import app.calls as calls  # noqa: E402
from app import cartesia, cartesia_setup, leads  # noqa: E402
from app.main import app  # noqa: E402

calls.POLL_EVERY_S = 0.05
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_WEBHOOK_SECRET="s3cret")
dialed = []
state = {"script": "answered"}


async def fake_create(agent_id, from_id, to, variables, ring, max_min):
    dialed.append(to)
    return f"ac_lead{len(dialed)}"


async def fake_get(cid):
    if state["script"] == "answered":
        return {"id": cid, "status": "completed", "end_reason": "client_hangup",
                "transcript": [{"role": "user", "text": "Hello?", "start_timestamp": 0.5}],
                "start_time": "2026-10-27T10:00:00Z", "end_time": "2026-10-27T10:00:30Z"}
    return {"id": cid, "status": "completed", "end_reason": "voicemail_detected", "transcript": []}


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia.create_call, cartesia.get_call, cartesia_setup.sync_agent = fake_create, fake_get, fake_sync

# phone cleanup unit checks
for raw, want in [("(305) 555-0101", "+13055550101"), ("1-954-555-0104", "+19545550104"), ("+1 786 555 0103", "+17865550103"),
                  ("(212) 555-0106 ext. 12", "+12125550106"), ("555-0107", None), ("+44 20 7946 0018", None),
                  ("(123) 555-0108", None), ("call me", None), ("", None), ("(911) 555-0100", None), ("(305) 155-0100", None)]:
    got, why = leads.normalize_us_phone(raw)
    assert got == want, (raw, got, why)
print("phone cleanup: 11 cases ok")

csv_text = open(f"{REPO}/docs/sample_leads.csv", encoding="utf-8").read()
with TestClient(app) as c:
    pv = c.post("/api/leads/import/preview", json={"csv": csv_text, "filename": "sample_leads.csv"}).json()
    print("preview:", pv["counts"], "| columns:", pv["columns"], "| extra:", pv["extra_columns"])
    for x in pv["invalid"]:
        print(f"   invalid row {x['row']}: {x['phone']!r} -> {x['reason']}")
    for x in pv["duplicates"]:
        print(f"   duplicate row {x['row']}: {x['phone']} -> {x['reason']}")
    assert pv["counts"] == {"valid": 7, "invalid": 5, "duplicates": 1, "updates": 0}, pv["counts"]
    maria = next(v for v in pv["valid"] if v["name"] == "Maria Lopez")
    assert maria["business"] == "Bloom Boutique" and "Instagram: @example_bloom" in maria["notes"] and maria["extra"]["Lead Source"] == "Instagram post"
    assert c.get("/api/leads").json()["total"] == 0  # preview saves nothing

    imp = c.post("/api/leads/import", json={"csv": csv_text, "filename": "sample_leads.csv"}).json()
    assert imp["imported"] == 7
    again = c.post("/api/leads/import/preview", json={"csv": csv_text, "filename": "sample_leads.csv"}).json()
    assert again["counts"]["valid"] == 0 and again["counts"]["duplicates"] == 8  # all already leads now
    print("import: 7 imported; importing again -> 0 valid, 8 duplicates")
    bad = c.post("/api/leads/import/preview", json={"csv": "name,city\nBob,Miami\n"})
    assert bad.status_code == 400 and "No phone column" in bad.json()["detail"]

    data = c.get("/api/leads").json()
    print("counts:", data["counts"]["total"], "total,", data["counts"]["called"], "called |", {k: v for k, v in data["counts"]["by_status"].items() if v})
    assert c.get("/api/leads", params={"q": "pilates"}).json()["total"] == 1
    assert c.get("/api/leads", params={"q": "555-0103"}).json()["total"] == 1
    assert c.get("/api/leads", params={"status": "new"}).json()["total"] == 7

    lead = next(x for x in data["leads"] if x["name"] == "Linda Park")
    # DNC: mark -> call refused (never dialed) -> unmark -> call works
    c.post(f"/api/leads/{lead['id']}/dnc", json={"dnc": True, "reason": "Asked us not to call"})
    r = c.post(f"/api/leads/{lead['id']}/call")
    print("call DNC lead:", r.status_code, r.json()["detail"])
    assert r.status_code == 400 and "Do-Not-Contact" in r.json()["detail"] and dialed == []
    r = c.post("/api/calls", json={"number": lead["phone"]})  # the Call page is blocked too
    assert r.status_code == 400 and dialed == []
    assert c.get("/api/leads", params={"status": "do_not_contact"}).json()["total"] == 1
    c.post(f"/api/leads/{lead['id']}/dnc", json={"dnc": False})
    assert c.get(f"/api/leads/{lead['id']}").json()["lead"]["status"] == "new"

    r = c.post(f"/api/leads/{lead['id']}/call")
    # TEST MODE (default ON): "Call this lead" dials the test number, but the call still counts for the lead
    assert r.status_code == 200 and dialed == [os.environ["TEST_CALL_NUMBER"]] and r.json()["number"] == os.environ["TEST_CALL_NUMBER"]
    assert r.json()["lead_id"] == lead["id"]
    time.sleep(0.4)
    d = c.get(f"/api/leads/{lead['id']}").json()
    print("lead after call:", d["lead"]["status"], "attempts", d["lead"]["attempts"], "| calls:", [(x["id"], x["result"]) for x in d["calls"]],
          "| activity:", [e["kind"] for e in d["events"]])
    assert d["lead"]["status"] == "needs_review" and d["lead"]["attempts"] == 1 and d["calls"][0]["lead_id"] == lead["id"]

    # a manual call from the Call page to a lead's number is linked to that lead
    other = next(x for x in data["leads"] if x["name"] == "Grace Lee")
    state["script"] = "voicemail"
    assert c.post("/api/calls", json={"number": other["phone"]}).status_code == 200
    time.sleep(0.4)
    d = c.get(f"/api/leads/{other['id']}").json()
    assert d["lead"]["status"] == "voicemail" and d["lead"]["attempts"] == 1
    print("manual call to a lead's number -> lead status", d["lead"]["status"])

    # DNC a number that isn't a lead
    r = c.post("/api/dnc", json={"phone": "(305) 555-0199", "reason": "Called and asked to be removed"}).json()
    assert r["dnc"] and r["status"] == "do_not_contact"
    assert c.post("/api/calls", json={"number": "+13055550199"}).status_code == 400
    assert c.post("/api/dnc", json={"phone": "12345"}).status_code == 400
    print("DNC by number: blocked; bad number rejected")
    print("final counts:", c.get("/api/leads").json()["counts"])
    print("numbers dialed (all fake 555-01xx, fake Cartesia):", dialed)

print("ALL LEADS TESTS PASS")
