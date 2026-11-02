"""Phase 5 step 2: lead time zone from location (zip -> state -> area code). Throwaway DB, no Cartesia."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import sys
from datetime import datetime, timezone

DB = f"{TMP}/tz_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

import app.dialer as dialer_mod  # noqa: E402
from app.db import Lead, get_session, init_db, lead_to_dict  # noqa: E402
from app.timezones import lead_timezone, state_in  # noqa: E402

ET, CT, MT, PT = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"
MIAMI_PHONE = "+13055550100"  # Eastern area code
cases = [
    ("Miami, FL 33130", MIAMI_PHONE, ET, "zip"),
    ("Austin TX 78701", MIAMI_PHONE, CT, "zip"),
    ("98101", MIAMI_PHONE, PT, "zip"),
    ("El Paso, TX 79901", MIAMI_PHONE, MT, "zip"),  # split state: west Texas is Mountain
    ("Pensacola FL 32501", MIAMI_PHONE, CT, "zip"),  # Florida panhandle is Central
    ("Knoxville, TN 37902", MIAMI_PHONE, ET, "zip"),  # East Tennessee
    ("Phoenix AZ 85001", MIAMI_PHONE, "America/Phoenix", "zip"),
    ("12345 Main St, Denver, CO 80202", MIAMI_PHONE, MT, "zip"),  # house number isn't the zip
    ("Denver CO", MIAMI_PHONE, MT, "state"),
    ("austin, tx", MIAMI_PHONE, CT, "state"),
    ("Salt Lake City, Utah", MIAMI_PHONE, MT, "state"),
    ("New York", "+13105550100", ET, "state"),
    ("Washington, DC", MIAMI_PHONE, ET, "state"),
    ("Seattle, Washington", MIAMI_PHONE, PT, "state"),
    ("San Francisco", "+14155550100", PT, "area_code"),  # no state: area code fallback
    ("", "+12065550100", PT, "area_code"),
    ("Come in or call me", "+12065550100", PT, "area_code"),  # "in"/"or" are words, not states
    ("Somewhere", "+15555550100", None, "unknown"),  # 555 isn't a real area code
]
for loc, phone, want_tz, want_src in cases:
    got = lead_timezone(loc, phone)
    assert got == (want_tz, want_src), (loc, phone, got)
print(f"{len(cases)} location cases OK")
assert state_in("Kansas City, MO") == "MO" and state_in("Portland, ME 04101") == "ME"

# Dialer: calling hours follow the location, not the phone. Tue 2026-10-27 15:00 UTC = 11:00 ET, 08:00 PT.
init_db()
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
with get_session() as s:
    s.add(Lead(phone="+13055550101", name="Seattle with Miami phone", location="Seattle, WA 98101"))  # 08:00 -> no
    s.add(Lead(phone="+15555550102", name="No tz", location=""))  # unknown -> skipped
    s.commit()
lead, info = dialer_mod.pick_next(NOW)
assert lead is None and info["outside_hours"] == 1 and info["unknown_tz"] == 1, info
print("idle reason:", dialer_mod._why_idle(info))
with get_session() as s:
    s.add(Lead(phone="+12065550103", name="Miami with Seattle phone", location="Miami FL 33130"))  # 11:00 -> yes
    s.commit()
lead, info = dialer_mod.pick_next(NOW)
assert lead and lead.name == "Miami with Seattle phone" and "New York" in info["local_time"], (lead, info)
print("picked:", lead.name, info["local_time"])

with get_session() as s:
    d = lead_to_dict(s.get(Lead, 1))
assert d["timezone"] == PT and d["tz_source"] == "zip", d
print("ALL TIME ZONE TESTS PASS")
