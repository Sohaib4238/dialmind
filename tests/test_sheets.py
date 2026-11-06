"""The sales team's Google Sheet (read only) -> leads; our results sheet <- call results. A FAKE Google (token endpoint
that checks the signed key login, Sheets API with coloured rows, a results sheet in memory); nothing leaves this
computer. Covers: the 2 tabs only, messy cells, dedupe across tabs and existing leads, green/yellow = Handled by sales
(new and existing), the colour setting, skip mode, never dialing on import, the 30-minute sync, read-only on the sales
sheet, results rows written RAW and updated in place, the opening line for a Bark lead."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import base64
import json
import os
import time
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

KEY_FILE = f"{TMP}/google_service_account.json"
os.environ.update(GOOGLE_SERVICE_ACCOUNT_FILE=KEY_FILE, CARTESIA_CALLS_DISABLED="1", CARTESIA_EMAIL_TOOL_ID="",
                  CARTESIA_SUPERVISOR_TOOL_ID="", SMS_SENDING_DISABLED="1", TELNYX_MESSAGING_PROFILE_ID="")

private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
SA_EMAIL = "dialmind-sheets@test-project.iam.gserviceaccount.com"
with open(KEY_FILE, "w", encoding="utf-8") as f:
    json.dump({"type": "service_account", "project_id": "test-project", "private_key_id": "kid1", "private_key": PEM,
               "client_email": SA_EMAIL, "token_uri": "https://oauth2.googleapis.com/token"}, f)

import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import app.calls as calls  # noqa: E402
from app import agent_config, cartesia, cartesia_setup, dialer as dialer_mod, gsheets, results_sheet, sheet_sync, tunnel  # noqa: E402
from app.db import Call, Lead, LeadEvent, get_session, utcnow  # noqa: E402
from settings import load_settings  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel
from app.main import app, dialer  # noqa: E402

SALES = "1SalesSheetAbcdefghijklmnopqrstuvwxyz0123"
RESULTS = "1ResultsSheetAbcdefghijklmnopqrstuvwxyz012"
TABS = ["Tab A", "Tab B"]
HEAD = ["Date", "Source", "Client Name", "Business Description", "Contact Number", "Email Address", "City, State, Zip",
        "Services Required", "Budget", "Additional info"]
GREEN, YELLOW, RED, PINK = "#d9ead3", "#fff2cc", "#f4cccc", "#ff00ff"


def rgb(h):
    h = h.lstrip("#")
    return {k: int(h[i:i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4)) if int(h[i:i + 2], 16)}


def cell(text, color=None, theme=None):
    c = {"formattedValue": text} if text != "" else {}
    if color:
        c["effectiveFormat"] = {"backgroundColor": rgb(color), "backgroundColorStyle": {"rgbColor": rgb(color)}}
    if theme:
        c["effectiveFormat"] = {"backgroundColorStyle": {"themeColor": theme}}
    return c


def line(values, color=None, only=None, theme=None):
    """A sheet row; color paints every cell, or only the cell at index `only`."""
    return [cell(v, color if only is None or i == only else None, theme if i == 2 else None) for i, v in enumerate(values)]


SHEET = {
    TABS[0]: [
        line(HEAD, "#cccccc"),  # a grey header row must not count
        line(["11/01/2026", "Bark", "Maria Lopez", "Bloom Boutique", "(305) 555-0121", "maria@bloom.test",
              "Miami, FL 33101", "a new website", "$500-$1,000", "Wants an online store"]),
        line(["11/01/2026", "Upwork", "John Smith", "Roofing company https://johnroof.example | linkedin.com/in/johnsmith",
              "512-555-0131 / 512-555-0132, (555) 012-3456", "john@roof.test; John.Smith@Example.com", "TX 78701", "SEO",
              "", ""]),
        line(["11/02/2026", "Bark", "Green Gary", "Plumber", "2125550141", "", "New York, NY 10001", "Logo", "", ""], GREEN),
        line(["11/02/2026", "Thumbtack", "Yellow Yara", "Bakery", "+1 718 555 0151", "", "", "Website", "", ""], YELLOW, only=2),
        line(["11/02/2026", "Bark", "Red Rob", "Cafe", "(415) 555-0161", "", "San Francisco, CA", "SEO", "", ""], RED),
        line(["11/03/2026", "Facebook", "Pink Pam", "Salon", "4155550171", "", "CA 94110", "Branding", "", ""], PINK),
        line(["11/03/2026", "Bark", "Bad Phone", "Shop", "12345", "", "", "", "", ""]),
        line(["", "", "", "", "", "", "", "", "", ""]),  # empty: ignored
        line(["11/03/2026", "Bark", "Old Olga", "Gym", "(737) 555-0181", "olga@gym.test", "Austin, TX", "Website", "$2k",
              "Old lead"], GREEN),  # already a lead (callback): turns handled
        line(["11/03/2026", "Bark", "Known Kim", "Florist", "(737) 555-0182", "", "", "", "$300", "Call after 5"]),  # already a lead
        line(["11/04/2026", "Thumbtack", "Theme Tom", "Painter", "(737) 555-0191", "", "Austin, TX 78702", "Website", "", ""],
             theme="ACCENT6"),  # painted with a theme colour (green in this theme)
    ],
    TABS[1]: [
        line(HEAD),
        line(["11/04/2026", "Bark", "Maria L.", "Boutique", "305.555.0121", "", "", "", "", ""]),  # same as TAB_A row 2
        line(["11/04/2026", "Upwork", "J. Smith", "", "(512) 555-0132", "", "", "", "", ""], YELLOW),  # John's 2nd number
        line(["11/04/2026", "LinkedIn", "Lina Link", "Dentist", "(469) 555-0201", "lina@smile.test", "Dallas, TX 75201",
              "Website redesign", "", ""]),
        line(["11/04/2026", "Bark", "=HYPERLINK(\"http://x\")", "Bar", "(469) 555-0202", "", "Dallas, TX", "SEO", "", ""]),
    ],
    "Other tab": [line(HEAD), line(["", "Bark", "Never Imported", "", "(469) 555-0299", "", "", "", "", ""])],
}
THEME = {"themeColors": [{"colorType": "ACCENT6", "color": {"rgbColor": rgb("#b7e1cd")}}]}


class FakeGoogle:
    def __init__(self):
        self.log = []  # (method, sheet id, scope, url)
        self.results = {}  # tab -> rows
        self.tokens = {}

    async def http(self, method, url, *, headers=None, json_body=None, form=None):
        if url == "https://oauth2.googleapis.com/token":
            assert method == "POST" and form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
            h, c, sig = form["assertion"].split(".")
            pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
            private.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{h}.{c}".encode(), padding.PKCS1v15(),
                                        hashes.SHA256())  # raises if the JWT isn't signed with the key
            claims = json.loads(base64.urlsafe_b64decode(pad(c)))
            assert claims["iss"] == SA_EMAIL and claims["aud"] == url and claims["exp"] - claims["iat"] == 3600
            tok = f"tok-{len(self.tokens)}"
            self.tokens[tok] = claims["scope"]
            return 200, {"access_token": tok, "expires_in": 3600, "token_type": "Bearer"}
        scope = self.tokens[headers["Authorization"].removeprefix("Bearer ")]
        u = urlsplit(url)
        assert u.netloc == "sheets.googleapis.com", url
        path = unquote(u.path).removeprefix("/v4/spreadsheets/")
        sid = path.split("/")[0].split(":")[0]
        self.log.append((method, sid, scope, url))
        q = parse_qs(u.query)
        if sid == SALES:
            if method != "GET":
                return 403, {"error": {"message": "read only"}}
            if q.get("includeGridData"):
                want = [r.split("!")[0].strip("'").replace("''", "'") for r in q["ranges"]]
                return 200, {"properties": {"spreadsheetTheme": THEME}, "sheets": [
                    {"properties": {"title": t}, "data": [{"rowData": [{"values": r} for r in SHEET[t]]}]} for t in want]}
            return 200, {"sheets": [{"properties": {"title": t}} for t in SHEET]}
        if sid != RESULTS:
            return 404, {"error": {"message": "not found"}}
        assert scope == gsheets.WRITE_SCOPE
        if path.endswith(":batchUpdate") and "/values" not in path:
            for req in json_body["requests"]:
                self.results.setdefault(req["addSheet"]["properties"]["title"], [])
            return 200, {}
        if "/values:batchUpdate" in path:
            assert json_body["valueInputOption"] == "RAW"
            for d in json_body["data"]:
                tab, rng = d["range"].rsplit("!", 1)
                n = int(rng.split(":")[0][1:])
                rows = self.results[tab.strip("'")]
                while len(rows) < n:
                    rows.append([])
                rows[n - 1] = list(d["values"][0])
            return 200, {}
        if path.endswith(":append"):
            assert q["valueInputOption"] == ["RAW"]
            tab = path.split("/values/")[1].rsplit("!", 1)[0].strip("'")
            self.results[tab].extend(list(r) for r in json_body["values"])
            return 200, {}
        if "/values/" in path:
            tab = path.split("/values/")[1].rsplit("!", 1)[0].strip("'")
            return 200, {"values": [r[:1] for r in self.results.get(tab, [])]}
        if q.get("fields") == ["sheets.properties.title"]:
            return 200, {"sheets": [{"properties": {"title": t}} for t in self.results]}
        raise AssertionError(url)


g = FakeGoogle()
gsheets._http = g.http


class FakeCartesia:
    def __init__(self):
        self.calls, self.n = {}, 0

    async def create_call(self, agent_id, from_id, to, variables, ring, max_min):
        self.n += 1
        cid = f"ac_g{self.n}"
        self.calls[cid] = {"status": "started", "end_reason": None, "transcript": [], "vars": variables}
        return cid

    async def get_call(self, cid):
        c = self.calls[cid]
        return {"id": cid, "status": c["status"], "end_reason": c["end_reason"], "transcript": list(c["transcript"]),
                "summary": c.get("summary", ""), "start_time": "2026-11-05T15:00:00Z", "end_time": "2026-11-05T15:01:00Z"}

    async def cancel_call(self, cid):
        return {}


fc = FakeCartesia()
cartesia.create_call, cartesia.get_call, cartesia.cancel_call = fc.create_call, fc.get_call, fc.cancel_call


async def fake_sync(reason=""):
    return {"agent_id": "agent_test", "action": "unchanged", "hash": "x"}

cartesia_setup.sync_agent = fake_sync
calls.POLL_EVERY_S = 0.05
os.environ.update(CARTESIA_AGENT_ID="agent_test", CARTESIA_FROM_NUMBER_ID="ap_test", CARTESIA_OUTCOME_TOOL_ID="out_tool",
                  CARTESIA_TOOL_SECRET="tool-secret")
AUTH = {"Authorization": "Bearer tool-secret"}


def wait(pred, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError("timeout")


def lead(phone):
    with get_session() as s:
        return s.exec(select(Lead).where(Lead.phone == phone)).first()


# ---- 1. colours: any shade of green / yellow; the header and white don't count -------------------------------
cn = gsheets.color_name
for h, want in ((GREEN, "green"), ("#00ff00", "green"), ("#b7e1cd", "green"), ("#6aa84f", "green"), ("#274e13", "green"),
                (YELLOW, "yellow"), ("#ffff00", "yellow"), ("#ffe599", "yellow"), ("#bf9000", "yellow"), ("#f1c232", "yellow"),
                (RED, "red"), ("#ff0000", "red"), (PINK, "pink"), ("#ff9900", "orange"), ("#fce5cd", "orange"),
                ("#c9daf8", "blue"), ("#ffffff", "none"), ("#cccccc", "gray")):
    got = cn(tuple(rgb(h).get(k, 0) for k in ("red", "green", "blue")))
    assert got == want, (h, got, want)
assert cn(None) == "none"
print("colours: every green / yellow shade found (incl. Sheets' light 3 and dark 2); white, grey, red, pink, orange apart")

# ---- 2. the messy cells -------------------------------------------------------------------------------------
assert sheet_sync.split_phones("512-555-0131 / 512-555-0132, (555) 012-3456")[0] == ["+15125550131", "+15125550132"]
assert sheet_sync.split_phones("call 305.555.0121 or +1 (305) 555-0121 ext 4")[0] == ["+13055550121"]
assert sheet_sync.split_phones("12345") == ([], "no valid US number in '12345'")
assert sheet_sync.split_emails("a@x.test; B@Example.com, a@x.test") == ["a@x.test", "b@example.com"]
assert sheet_sync.split_links("Roofing company https://johnroof.example | linkedin.com/in/johnsmith") == \
    ("Roofing company", ["https://johnroof.example", "linkedin.com/in/johnsmith"])
assert gsheets.sheet_id_from(f"https://docs.google.com/spreadsheets/d/{SALES}/edit#gid=0") == SALES
assert gsheets.sheet_id_from("not a link") == ""
print("cells: several numbers (first valid = main), emails, links out of the business text, sheet links")

with TestClient(app) as c:
    # ---- 3. settings ----------------------------------------------------------------------------------------
    st = c.get("/api/sheets").json()
    assert st["key"] == {"ok": True, "client_email": SA_EMAIL, "detail": ""}, st["key"]  # the email only, never the key
    assert st["settings"]["tabs"] == TABS and st["settings"]["sync_minutes"] == 30
    assert st["settings"]["handled_colors"] == ["green", "yellow", "red"] and not st["settings"]["enabled"]
    assert "PRIVATE KEY" not in json.dumps(st)
    assert c.post("/api/sheets/sync").status_code == 409  # no sheet yet
    for bad in ({"sales_sheet": "nope"}, {"sync_minutes": 2}, {"tabs": []}, {"handled_action": "maybe"},
                {"sales_sheet": SALES, "results_sheet": f"https://docs.google.com/spreadsheets/d/{SALES}/edit"}):
        assert c.put("/api/sheets", json=bad).status_code == 400, bad
    r = c.put("/api/sheets", json={"sales_sheet": f"https://docs.google.com/spreadsheets/d/{SALES}/edit#gid=0",
                                   "results_sheet": RESULTS})
    assert r.status_code == 200, r.text
    print("settings: defaults (2 tabs, 30 min, green+yellow+red, off); results sheet can't be the sales sheet")

    # ---- 4. existing leads the sheet also has --------------------------------------------------------------
    cb = utcnow() + timedelta(days=2)
    with get_session() as s:
        s.add(Lead(phone="+17375550181", name="Olga", status="callback", attempts=2, callback_at=cb, last_call_at=utcnow()))
        s.add(Lead(phone="+17375550182", name="Kim", status="not_interested", attempts=1, notes="keep me"))
        s.commit()
    assert dialer.state == "off"

    # ---- 5. Sync now ---------------------------------------------------------------------------------------
    r = c.post("/api/sheets/sync")
    assert r.status_code == 200, r.text
    sm = r.json()
    assert sm["ok"] and sm["tabs"] == {TABS[0]: 10, TABS[1]: 4}, sm
    # new: Maria, Pink Pam, Lina, the "=HYPERLINK" one; handled new: Gary (green), Yara (yellow name), Tom (theme),
    # Red Rob (red) and John: his 2nd number is on a yellow row in the other tab (same person: any coloured row counts)
    assert sm["new"] == 4 and sm["handled"] == {"new": 5, "existing": 1, "skipped": 0}, sm
    assert sm["duplicates"] == 4 and sm["skipped_count"] == 1 and sm["skipped"][0]["reason"].startswith("no valid US number")
    reasons = {(d["tab"], d["row"]): d["reason"] for d in sm["duplicate_rows"]}
    assert reasons[(TABS[1], 2)] == f"same number as {TABS[0]} row 2" and reasons[(TABS[1], 3)] == f"same number as {TABS[0]} row 3"
    assert reasons[(TABS[0], 10)].startswith("already a lead") and reasons[(TABS[0], 11)].startswith("already a lead")
    assert sm["marked_handled"][0]["name"] == "Olga" and sm["filled"] == 2
    assert lead("+14695550299") is None  # "Other tab" is never read
    print("sync: 2 tabs only; 4 new, 5 new handled (green / yellow name cell / theme green / red / yellow duplicate), 1 existing turned handled,"
          " 4 duplicates (across tabs, 2nd number, existing), 1 skipped (bad number)")

    m = lead("+13055550121")
    assert (m.status, m.lead_source, m.budget, m.additional_info, m.sheet_tab, m.sheet_row) == \
        ("new", "Bark", "$500-$1,000", "Wants an online store", TABS[0], 2), m
    assert m.location == "Miami, FL 33101" and m.city == "Miami" and m.email == "maria@bloom.test"
    assert m.source == f"Google Sheet: {TABS[0]}" and m.extra == {"Date": "11/01/2026"}
    j = lead("+15125550131")
    assert j.alt_phones == "+15125550132" and j.email == "john@roof.test" and j.alt_emails == "john.smith@example.com"
    assert j.business_description == "Roofing company" and j.links == "https://johnroof.example linkedin.com/in/johnsmith"
    assert j.location == "TX 78701" and j.city == "" and j.status == "handled_by_sales"
    for p in ("+12125550141", "+17185550151", "+17375550191", "+14155550161"):
        x = lead(p)
        assert x.status == "handled_by_sales" and x.sales_handled, (p, x.status)
    assert lead("+14155550171").status == "new"  # pink: called normally
    assert lead("+14695550202").name == '=HYPERLINK("http://x")'  # kept as text (written RAW later)
    o = lead("+17375550181")
    assert o.status == "handled_by_sales" and o.sales_handled and o.callback_at is None and o.attempts == 2
    k = lead("+17375550182")
    assert k.status == "not_interested" and k.notes == "keep me" and k.budget == "$300" and k.additional_info == "Call after 5"
    assert k.name == "Kim" and k.lead_source == "Bark"  # only EMPTY fields filled; name kept
    with get_session() as s:
        ev = [e.text for e in s.exec(select(LeadEvent).where(LeadEvent.lead_id == o.id)).all()]
    assert any("Handled by sales" in e and "green" in e for e in ev), ev
    print("leads: source/budget/info/tab/row kept; 2nd number + 2nd email as alternates; links out; no city OK;"
          " existing: status/notes/DNC untouched, only empty fields filled, green -> Handled by sales + callback cleared")

    # ---- 6. never dialed: dialer off, the queue and every call path skip handled leads -------------------------
    assert dialer.state == "off" and c.get("/api/dialer").json()["state"] == "off"
    picked, _ = dialer_mod.pick_next()
    assert picked is not None and not picked.sales_handled
    r = c.post(f"/api/leads/{lead('+12125550141').id}/call")
    assert r.status_code == 400 and "sales team" in r.json()["detail"], r.text
    r = c.post("/api/calls", json={"number": "+12125550141"})
    assert r.status_code == 400 and "sales team" in r.json()["detail"], r.text
    assert c.post(f"/api/leads/{o.id}/outcome", json={"outcome": "not_now"}).status_code == 400
    print("never dialed: the dialer stays off; handled leads refused by Call this lead, the Call page and manual outcome")

    # ---- 7. a second sync changes nothing; a row turning yellow later stops the calls ---------------------------
    before = {x.id: (x.status, x.attempts) for x in [lead(p) for p in ("+13055550121", "+15125550131", "+14155550171")]}
    sm = c.post("/api/sheets/sync").json()
    assert sm["new"] == 0 and sm["handled"] == {"new": 0, "existing": 0, "skipped": 0} and sm["filled"] == 0, sm
    assert before == {x.id: (x.status, x.attempts) for x in [lead(p) for p in ("+13055550121", "+15125550131", "+14155550171")]}
    with get_session() as s:
        x = s.get(Lead, lead("+14155550171").id)
        x.status, x.attempts, x.callback_at = "callback", 1, utcnow() + timedelta(hours=3)
        s.add(x)
        s.commit()
    SHEET[TABS[0]][6] = line(["11/03/2026", "Facebook", "Pink Pam", "Salon", "4155550171", "", "CA 94110", "Branding", "", ""],
                             "#ffd966")  # the sales team painted it yellow
    sm = c.post("/api/sheets/sync").json()
    assert sm["handled"]["existing"] == 1 and sm["marked_handled"][0]["name"] == "Pink Pam", sm
    x = lead("+14155550171")
    assert x.status == "handled_by_sales" and x.callback_at is None and x.attempts == 1
    print("again: nothing new, nothing reset; a row turned yellow later -> Handled by sales, callback cleared")

    # ---- 8. the colour rule is a setting; skip mode ----------------------------------------------------------
    SHEET[TABS[1]].append(line(["", "Bark", "Gold Gina", "Shop", "(469) 555-0211", "", "", "", "", ""], "#f9cb9c"))  # orange
    SHEET[TABS[1]].append(line(["", "Bark", "Lime Lou", "Shop", "(469) 555-0212", "", "", "", "", ""], GREEN))
    st = c.put("/api/sheets", json={"handled_colors": ["green", "orange"], "handled_action": "skip"}).json()
    assert st["settings"]["handled_colors"] == ["green", "yellow", "orange", "red"], st["settings"]  # yellow + red always
    st = c.put("/api/sheets", json={"handled_colors": ["green"]}).json()
    assert st["settings"]["handled_colors"] == ["green", "yellow", "red"], st["settings"]  # orange off again
    sm = c.post("/api/sheets/sync").json()
    assert lead("+14695550211").status == "new"  # orange isn't "handled"
    assert lead("+14695550212") is None and sm["handled"]["skipped"] == 1
    assert any(s_["reason"] == "handled by sales (green row): not imported" for s_ in sm["skipped"]), sm["skipped"]
    c.put("/api/sheets", json={"handled_colors": ["green", "yellow"], "handled_action": "import"})
    print("setting: green/yellow/red always handled, others optional (orange off -> called normally); skip mode -> not imported, listed")

    # ---- 9. read-only on the sales sheet: GET only, with the read-only scope; writes to it refused ---------------
    sales = [x for x in g.log if x[1] == SALES]
    assert sales and all(m_ == "GET" and sc == gsheets.READ_SCOPE for m_, _, sc, _ in sales), sales
    for coro in (gsheets.values_update(SALES, [{"range": "A1", "values": [["x"]]}]), gsheets.ensure_tab(SALES, "x"),
                 gsheets.values_append(SALES, "A1", [["x"]]), gsheets.values_update("1SomeOtherSheetAbcdefghijklmnop", [])):
        try:
            asyncio.run(coro)
            raise AssertionError("a write outside the results sheet went through")
        except gsheets.SheetsError as e:
            assert str(e).startswith("Refused"), e
    assert all(x[1] == SALES for x in g.log if x[0] != "GET" and x[1] != RESULTS)  # nothing else was ever written
    print("read only: every sales-sheet request is a GET with the read-only scope; writes to it are refused in code")

    # ---- 10. results sheet: one row per lead, written after each call, updated in place, RAW --------------------
    lid = lead("+13055550121").id
    call = c.post(f"/api/leads/{lid}/call").json()
    room = call["room"]
    intro = fc.calls[room]["vars"]["intro_line"]
    assert intro == f"Hi, this is {load_settings().caller_name}. I saw your request on Bark for a new website for your " \
                    "Bloom Boutique. Is now an okay time?", intro
    assert fc.calls[room]["vars"]["lead_source"] == "Bark"
    c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "not_interested",
                                                                      "note": "Has a site already", "callback_time": ""})
    fc.calls[room].update(status="completed", end_reason="client_hangup", summary="Maria already has a website. Not interested.")
    wait(lambda: any(r_ and r_[0] == lid for r_ in g.results.get("Call results", [])))
    rows = g.results["Call results"]
    assert rows[0] == results_sheet.HEADER
    r1 = next(r_ for r_ in rows if r_[0] == lid)
    assert r1[1:5] == ["Maria Lopez", "(305) 555-0121", "Bark", TABS[0]] and r1[6] == 1 and r1[7] == "Rejected", r1
    assert r1[9] == "Maria already has a website. Not interested." and r1[10:12] == ["No", ""] and r1[5].endswith(("EST", "EDT"))
    # the sales team sorts the sheet: the row moves; the next call updates it where it is now
    g.results["Call results"] = [rows[0], ["999", "someone else"], r1]
    call = c.post(f"/api/leads/{lid}/call").json()
    room = call["room"]
    c.post("/api/cartesia/tools/record_outcome", headers=AUTH, json={"call_id": room, "outcome": "not_now",
                                                                      "note": "Call tomorrow", "callback_time": ""})
    fc.calls[room].update(status="completed", end_reason="client_hangup")
    wait(lambda: g.results["Call results"][2][6] == 2)
    r2 = g.results["Call results"][2]
    assert len(g.results["Call results"]) == 3 and r2[0] == lid and r2[7] == "Callback" and r2[8], r2
    assert r2[9] == "Call tomorrow"
    # a called lead turns handled -> its tag changes; a formula-looking name stays text (RAW)
    with get_session() as s:
        x = s.get(Lead, lead("+14695550202").id)
        x.attempts, x.last_call_at, x.results_dirty = 1, utcnow(), True
        s.add(x)
        s.commit()
    SHEET[TABS[0]][1] = line(["11/01/2026", "Bark", "Maria Lopez", "Bloom Boutique", "(305) 555-0121", "", "", "", "", ""], GREEN)
    c.post("/api/sheets/sync")
    wait(lambda: g.results["Call results"][2][7] == "Handled by sales")
    assert g.results["Call results"][2][8] == ""  # no callback for a lead the sales team has
    wait(lambda: any(r_[0] == lead("+14695550202").id for r_ in g.results["Call results"] if r_))
    assert any(r_[1] == '=HYPERLINK("http://x")' for r_ in g.results["Call results"])
    print("results: header + one row per called lead (tag Rejected -> Callback -> Handled by sales), updated where it is"
          " after a sort, Cartesia's summary, RAW values; the intro said Bark + the need + the business")

    # ---- 11. every 30 minutes (when on); standby reads nothing ------------------------------------------------
    n = len([x for x in g.log if x[1] == SALES])
    c.put("/api/sheets", json={"enabled": True})
    time.sleep(1)
    assert len([x for x in g.log if x[1] == SALES]) == n  # synced a moment ago: not due yet
    ago = (utcnow() - timedelta(minutes=31)).isoformat()
    sheet_sync._save_state(last_sync={**sheet_sync._state()["last_sync"], "at": ago}, last_full_at=ago)
    sheet_sync.wake()
    wait(lambda: len([x for x in g.log if x[1] == SALES]) > n)
    wait(lambda: sheet_sync._state()["last_sync"]["trigger"] == "auto")
    assert c.get("/api/sheets").json()["next_sync_at"]
    os.environ["DIALER_STANDBY"] = "1"
    r = c.post("/api/sheets/sync")
    assert r.status_code == 409 and "standby" in r.json()["detail"]
    os.environ["DIALER_STANDBY"] = "0"
    assert dialer.state == "off"
    print("auto: synced when due (30 min), not before; standby refuses; the dialer never turned on")

# ---- 12. the key never reaches the logs -------------------------------------------------------------------
logs = "".join(open(os.path.join(os.environ["DIALER_LOG_DIR"], f), encoding="utf-8", errors="ignore").read()
               for f in os.listdir(os.environ["DIALER_LOG_DIR"]))
assert "PRIVATE KEY" not in logs and PEM.splitlines()[1] not in logs and "tok-" not in logs
print("logs: no key, no token")
print("ALL SHEETS TESTS PASS")
