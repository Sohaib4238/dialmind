"""Phase 5 step 3: service fit (skip clear mismatches, "Call anyway"). Throwaway DB, no Cartesia."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import sys
from datetime import datetime, timezone

DB = f"{TMP}/fit_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["CARTESIA_EMAIL_TOOL_ID"] = ""  # Phase 0-6 behaviour (no email tool); see test_email_tool.py
os.environ["CARTESIA_SUPERVISOR_TOOL_ID"] = ""  # Phase 0-5 behaviour (no hand-off); see test_supervisor.py
os.environ["SMS_SENDING_DISABLED"] = "1"  # Phase 7b: never a real text from a test
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""  # never touch the real messaging profile

import app.dialer as dialer_mod  # noqa: E402
from app import leads  # noqa: E402
from app.db import init_db  # noqa: E402
from settings import SettingsError, load_settings  # noqa: E402

init_db()
cases = {
    "": "empty",
    "Website redesign": "fit",
    "New websites": "fit",  # plural
    "SEO and website": "fit",
    "Plumbing repair": "not_fit",
    "Bookkeeping": "not_fit",
    "Roofing and a new website": "fit",  # anything we offer wins
    "Need more customers": "unclear",
    "Website-builder help": "fit",
    "Mortgages": "not_fit",
    "Wix site": "fit",
    # Part 0 (2026-10-28): we offer more than web design
    "Mobile app development (iOS / Android)": "fit",
    "Need an app for my bakery": "fit",
    "Logo design": "fit",
    "Branding and a new logo": "fit",
    "Graphic design for flyers": "fit",
    "UI/UX design": "fit",
    "SEO": "fit",
    "Digital marketing": "fit",
    "Social media design": "fit",
    "Shopify store": "fit",
    "Landing pages": "fit",
    "Accounting app": "fit",  # an app wins over accounting
    "Commercial cleaning": "not_fit",
    "Tax preparation": "not_fit",
    "Interior painting": "not_fit",
}
for service, want in cases.items():
    got = leads.service_fit(service)
    assert got[0] == want, (service, got)
print(f"{len(cases)} service cases OK")

text = open(f"{REPO}/docs/sample_leads_v2.csv", encoding="utf-8").read()
r = leads.import_csv(text, "sample_leads_v2.csv")
assert r["not_a_fit"] == 2, r  # plumbing + bookkeeping
by = {x["name"]: x for x in leads.list_leads()["leads"]}
assert by["Tom Becker"]["status"] == "not_a_fit" and by["Grace Lee"]["status"] == "not_a_fit"
assert by["Linda Park"]["status"] == "new"  # empty service: called
assert by["Nina Shah"]["status"] == "new"  # unclear: called
print("import:", r)

# The dialer never picks a not_a_fit lead. Tue 15:00 UTC: Eastern/Central leads are inside hours.
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
picked = set()
for _ in range(10):
    lead, info = dialer_mod.pick_next(NOW)
    if not lead:
        break
    picked.add(lead.name)
    with leads.get_session() as s:  # pretend it was called: no retry
        row = s.get(leads.Lead, lead.id)
        row.status = "answered"
        s.add(row)
        s.commit()
assert "Tom Becker" not in picked and "Grace Lee" not in picked, picked
print("dialer picked (in hours):", sorted(picked))

# Call anyway -> back in the queue and stays there when the check runs again.
tom = by["Tom Becker"]["id"]
d = leads.call_anyway(tom)
assert d["status"] == "new" and d["fit_override"] is True
leads.apply_service_fit()
assert leads.get_lead(tom)["lead"]["status"] == "new"
lead, _ = dialer_mod.pick_next(NOW)
assert lead and lead.name == "Tom Becker", lead
print("call anyway: back in queue ->", lead.name)

# Settings change: turning the check off frees Grace; turning it back on marks her again.
real = load_settings()
off = real.model_copy(update={"service_fit": real.service_fit.model_copy(update={"enabled": False})})
leads.load_settings = lambda: off
leads.apply_service_fit()
assert by["Grace Lee"]["id"] and leads.get_lead(by["Grace Lee"]["id"])["lead"]["status"] == "new"
leads.load_settings = lambda: real
assert leads.apply_service_fit() == 1
assert leads.get_lead(by["Grace Lee"]["id"])["lead"]["status"] == "not_a_fit"


def broken():
    raise SettingsError("bad file")


leads.load_settings = broken  # invalid settings never skip anyone
assert leads.service_fit("Plumbing")[0] == "unclear"
leads.load_settings = load_settings
print("settings toggle + invalid settings OK")

# The new list frees old not_a_fit leads that we do serve now (apply_service_fit runs at start and on save).
with leads.get_session() as s:
    for name, svc in (("Old Logo", "Logo and plumbing"), ("Old App", "iOS app"), ("Old Plumber", "Plumbing")):
        s.add(leads.Lead(phone=f"+1212555{len(name):02d}{ord(name[4]) % 100:02d}", name=name, service_required=svc,
                         status="not_a_fit"))
    s.commit()
leads.apply_service_fit()
st = {x["name"]: x["status"] for x in leads.list_leads()["leads"]}
assert st["Old Logo"] == "new" and st["Old App"] == "new" and st["Old Plumber"] == "not_a_fit", st
print("re-check: old not_a_fit leads we now serve are back to new")

# The AI is told what we offer (first word of each line) and to pitch the lead's own service.
from app import agent_config  # noqa: E402
from settings import service_names  # noqa: E402
names = service_names(real.service_fit.offered)
assert names[:2] == ["web design", "website development"] and "mobile app development" in names and "seo" in names, names
ins = agent_config.build_instructions(real)
assert "# What we offer" in ins and "mobile app development" in ins and "talk about apps" in ins
assert "{{service_required}}" in real.fill(real.main_script) and "website" not in ins.split("# What we offer")[0]
print("prompt: offer list + pitch by {service_required}")

# Validator: keywords are normalised, empty lists rejected.
from settings import ServiceFit  # noqa: E402
sf = ServiceFit(offered=" Website ,web DESIGN,,website ", not_offered="plumbing")
assert sf.offered == "website, web design", sf.offered
sf = ServiceFit(offered="Web design, website\n\n  LOGO design,  logo \nwebsite, seo\n", not_offered="plumbing")
assert sf.offered == "web design, website\nlogo design, logo\nseo", repr(sf.offered)  # one service per line kept
try:
    ServiceFit(offered=" , ", not_offered="x")
    raise AssertionError("empty keyword list accepted")
except ValueError:
    pass
print("ALL SERVICE FIT TESTS PASS")
