"""{specialist_title}: who the lead is handed to, by their service ("our senior web designer"). The lead never hears or
reads "supervisor" / "manager"; the dashboard keeps calling it the Supervisor. Offline: no calls, no texts, no emails."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import os
import re
import sys

DB = f"{TMP}/specialist_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CARTESIA_CALLS_DISABLED"] = "1"
os.environ["SMS_SENDING_DISABLED"] = "1"
os.environ["EMAIL_SENDING_DISABLED"] = "1"
os.environ["TELNYX_MESSAGING_PROFILE_ID"] = ""
# the hand-off, outcome and email rules all in the prompt (fake IDs: nothing is called)
os.environ.update(CARTESIA_SUPERVISOR_TOOL_ID="agent_tool_sup", CARTESIA_OUTCOME_TOOL_ID="agent_tool_out",
                  CARTESIA_EMAIL_TOOL_ID="agent_tool_email")

import env_utils  # noqa: E402,F401

from app import emails, settings_store, sms, supervisor  # noqa: E402
from app.agent_config import build_config, lead_variables, supervisor_tool_body  # noqa: E402
from app.db import Lead, init_db  # noqa: E402
from settings import AgentSettings, load_settings  # noqa: E402

init_db()
s = load_settings()
t = s.specialist_titles

# 1. The default mapping (the user's list)
cases = {
    "web design": "our senior web designer", "a new website": "our senior web designer",
    "Website redesign": "our senior web designer", "landing page": "our senior web designer",
    "ecommerce store": "our senior web designer", "Shopify": "our senior web designer", "WordPress site": "our senior web designer",
    "graphic design": "our senior graphic designer", "Logo": "our senior graphic designer",
    "branding": "our senior graphic designer", "social media design": "our senior graphic designer",
    "mobile app": "our senior app developer", "iOS app": "our senior app developer", "Android": "our senior app developer",
    "UI/UX": "our senior UI/UX designer", "UX design for our app": "our senior UI/UX designer",
    "SEO": "our senior marketing specialist", "digital marketing": "our senior marketing specialist",
    "": "one of our senior specialists", "   ": "one of our senior specialists", "something else": "one of our senior specialists",
}
for service, want in cases.items():
    assert t.title_for(service) == want, (service, t.title_for(service), want)
print(f"1. mapping: {len(cases)} services -> the right title; empty / unclear -> {t.default!r}")

# 2. Editable (Instructions page): a form group with both fields; edits are tidied and used; bad lines rejected
spec = next(f for f in settings_store.read()["fields"] if f["key"] == "specialist_titles")
assert spec["kind"] == "group" and [f["key"] for f in spec["fields"]] == ["by_service", "default"], spec
vals = s.model_dump()
vals["specialist_titles"] = {"by_service": "Senior  Video Editor:  Video, YouTube\n\nsenior web designer: website",
                             "default": "one of our experts"}
s2 = AgentSettings.model_validate(vals)
assert s2.specialist_titles.by_service == "Senior Video Editor: video, youtube\nsenior web designer: website"
assert s2.specialist_titles.title_for("YouTube videos") == "our Senior Video Editor"
assert s2.specialist_titles.title_for("logo") == "one of our experts"
for bad, why in (({"by_service": "senior web designer website"}, "title, colon, keywords"),
                 ({"by_service": "senior manager: website"}, "never hear"),
                 ({"by_service": "senior web designer: website", "default": "our supervisor"}, "never hear")):
    try:
        AgentSettings.model_validate({**vals, "specialist_titles": bad})
        raise AssertionError(f"accepted {bad}")
    except ValueError as e:
        assert why in str(e), str(e)
print("2. editable on the Instructions page (group: Title by service + Unclear / empty service); bad lines rejected")

# 3. Per-call variable, always sent (also without a lead)
lead = Lead(phone="+13055550101", name="Maria", business="Bloom", service_required="a new logo", location="Miami, FL 33130")
assert lead_variables(lead, s)["specialist_title"] == "our senior graphic designer"
assert lead_variables(lead)["specialist_title"] == "our senior graphic designer"  # loads the settings itself
assert lead_variables(None)["specialist_title"] == "one of our senior specialists"
print("3. {specialist_title} in every call's variables")

# 4. What the AI says: the transfer lines use it; nothing the lead hears says supervisor / manager
ins = build_config(s)["instructions"]
for line in ("Let me check if {{specialist_title}} is free right now, one moment.",
             "Connecting you with {{specialist_title}} now",
             "Sorry, {{specialist_title}} is with a client right now. When's a good time for them to call you back?"):
    assert line in ins, line
assert 'always call them "{{specialist_title}}". Never say supervisor, manager or boss.' in ins
quoted = re.findall(r'"([^"]+)"', ins)  # every exact line the AI is told to say
assert quoted and not [q for q in quoted if re.search(r"supervisor|manager", q, re.I)], quoted
rest = ins.replace("Never say supervisor, manager or boss.", "").replace("request_supervisor", "").replace("supervisor_status", "")
assert not re.search(r"supervisor|manager|\bhe\b|\bhim\b|\bhis\b", rest, re.I), re.findall(r".{40}(?:supervisor|manager).{40}", rest)
tool = supervisor_tool_body("https://x.trycloudflare.com/t", "secret")
assert "manager" not in str(tool["description"]) + str(tool["api_schema"]["request_body_schema"])
for reply in (supervisor.REPLY_AVAILABLE, supervisor.REPLY_WAITING, supervisor.REPLY_NOT_AVAILABLE):
    assert "manager" not in reply.lower(), reply
print("4. transfer lines say {specialist_title}; no supervisor / manager in anything the AI says")

# 5. Every template in the settings: no supervisor / manager; texts and emails fill it in
def strings(v, path=""):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from strings(x, f"{path}.{k}")
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from strings(x, f"{path}[{i}]")
    elif isinstance(v, str):
        yield path, v

hits = [p for p, v in strings(s.model_dump()) if re.search(r"supervisor|manager", v, re.I)]
assert not hits, hits
text = sms.build(s, "interested", lead)
assert "our senior graphic designer" in text and "{" not in text and "STOP" in text, text
assert len(text) <= 300
_, body = emails.build(s, "interested", lead)
assert "Next step: our senior graphic designer will reach out" in body, body
_, body = emails.build(s, "details", Lead(phone="+13055550102", name="Sam", service_required="", location="Miami, FL"))
assert "a free 15-minute call with one of our senior specialists" in body, body
print("5. templates: no supervisor / manager; SMS + emails filled:", text[:90], "…")

# 6. The dashboard still says Supervisor (only what the lead hears changed)
for f in (f"{REPO}/dashboard/frontend/components/supervisor-popup.tsx",
          f"{REPO}/dashboard/frontend/app/supervisor/page.tsx"):
    ui = open(f, encoding="utf-8").read()
    assert "Supervisor" in ui and "specialist_title" not in ui, f
print("6. dashboard wording unchanged (Supervisor page / pop-up)")
print("ALL SPECIALIST TITLE TESTS PASS")
