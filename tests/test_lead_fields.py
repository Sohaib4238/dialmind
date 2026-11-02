"""Phase 5 step 1: lead fields (CSV import, storage, migration). Throwaway DB, no Cartesia."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import sqlite3
import sys

DB = f"{TMP}/lead_fields_test.db"
if os.path.exists(DB):
    os.remove(DB)
# A lead table as older versions created it (no location / service / description columns).
con = sqlite3.connect(DB)
con.execute("""CREATE TABLE lead (id INTEGER PRIMARY KEY, phone VARCHAR NOT NULL UNIQUE, name VARCHAR NOT NULL,
    business VARCHAR NOT NULL, city VARCHAR NOT NULL, notes VARCHAR NOT NULL, extra JSON, status VARCHAR NOT NULL,
    dnc BOOLEAN NOT NULL, dnc_reason VARCHAR NOT NULL, dnc_at DATETIME, attempts INTEGER NOT NULL,
    last_call_at DATETIME, last_call_id INTEGER, source VARCHAR NOT NULL, created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL)""")
con.execute("""INSERT INTO lead (phone, name, business, city, notes, extra, status, dnc, dnc_reason, attempts, source,
    created_at, updated_at) VALUES ('+13055550121', 'Maria (old)', 'Bloom', 'Miami', '', '{}', 'new', 0, '', 0, 'old.csv',
    '2026-10-01 10:00:00', '2026-10-01 10:00:00')""")
con.commit()
con.close()

os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

from app import leads  # noqa: E402
from app.db import Lead, get_session, init_db  # noqa: E402

init_db()
init_db()  # running the migration twice is harmless
with get_session() as s:
    old = s.get(Lead, 1)
assert old.location == "Miami" and old.service_required == "", old  # city copied into location
print("migration: old lead gets location from city:", old.location)

text = open(f"{REPO}/docs/sample_leads_v2.csv", encoding="utf-8").read()
p = leads.parse_csv(text, "sample_leads_v2.csv")
print("columns:", p["columns"])
assert p["columns"] == {"name": "Name", "phone": "Phone Number", "service_required": "Service Required",
                        "business_description": "Business Description", "job_description": "Job Description",
                        "location": "Location"}, p["columns"]
assert p["counts"] == {"valid": 8, "invalid": 0, "duplicates": 1, "updates": 1}, p["counts"]
assert p["updates"][0]["fields"]["service_required"] == "Website redesign"
assert "name" not in p["updates"][0]["fields"]  # existing values are never overwritten

r = leads.import_csv(text, "sample_leads_v2.csv")
assert r["imported"] == 8 and r["updated"] == 1, r
by_phone = {x["phone"]: x for x in leads.list_leads()["leads"]}
maria = by_phone["+13055550121"]
assert maria["name"] == "Maria (old)" and maria["service_required"] == "Website redesign"
assert maria["location"] == "Miami"  # kept (it wasn't empty)
aisha = by_phone["+17865550123"]
assert aisha["location"] == "" and aisha["service_required"] == "Landing page"  # empty location is fine
linda = by_phone["+19545550125"]
assert linda["service_required"] == "" and linda["job_description"] == "" and linda["location"] == "Austin TX 78701"
print("import:", r, "| empty fields stored as ''")

# Separate city / state / zip columns are joined into location; long text is cut; whitespace collapsed.
csv2 = "phone,first name,last name,city,state,zip,job,services needed\n" \
       f"(305) 555-0140,Ann,Lee,Miami,FL,33101,\"line one\nline two\",{'x' * 1500}\n" \
       "(305) 555-0141,,,,,,,\n"
p2 = leads.parse_csv(csv2)
v = p2["valid"]
assert v[0]["location"] == "Miami, FL 33101" and v[0]["name"] == "Ann Lee", v[0]
assert v[0]["job_description"] == "line one line two"
assert len(v[0]["service_required"]) == leads.MAX_FIELD_CHARS
assert v[1]["location"] == "" and v[1]["name"] == "", v[1]
print("city/state/zip ->", v[0]["location"], "| long text cut to", len(v[0]["service_required"]))

# Search covers the new fields.
assert {x["name"] for x in leads.list_leads(q="shopify")["leads"]} == {"Sam Ortiz"}
assert {x["name"] for x in leads.list_leads(q="78701")["leads"]} == {"Linda Park"}
print("search: job description and zip found")

# Phase 7: an email column (any common name) is imported, cleaned, and fills an empty email on a known lead.
csv3 = ("Phone,Name,E-mail Address\n"
        "(305) 555-0150,Eve,  Eve.Stone@Example.COM \n"
        "(305) 555-0151,Bad,not-an-email\n"
        "(305) 555-0152,None,\n")
r3 = leads.import_csv(csv3, "emails.csv")
by_phone = {x["phone"]: x for x in leads.list_leads()["leads"]}
assert by_phone["+13055550150"]["email"] == "eve.stone@example.com", by_phone["+13055550150"]
assert by_phone["+13055550151"]["email"] == "" and "not-an-email" in by_phone["+13055550151"]["notes"]
assert by_phone["+13055550152"]["email"] == ""
r4 = leads.import_csv("phone,email\n(305) 555-0152,none@example.com\n", "fill.csv")
assert r4["updated"] == 1 and leads.find_by_phone("+13055550152").email == "none@example.com", r4
assert {x["name"] for x in leads.list_leads(q="eve.stone")["leads"]} == {"Eve"}
print("email column: imported, cleaned, invalid kept in notes, fills empty emails")
print("ALL LEAD FIELD TESTS PASS")
