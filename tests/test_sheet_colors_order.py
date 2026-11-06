"""Sales sheet colours + order (2026-11-05). A FAKE Google modelled on the real sales sheet as scanned that day (whole rows
painted #ea9999 / #e06666 / #ff0000 / #ffff00 / #00ff00 with white A-B, "Addtional info" spelled that way, TAB_B's
"Profile link", "na" / UK numbers); nothing leaves this computer. Covers: green / yellow / red ALWAYS handled (even when
the saved setting says only green), the colour table (hex, rows, family) and the colour rules count, colour painted only
in a later column, "Re-check colours" touching nothing but the colour rule, the skipped rows grouped, long dashes in a
number, sheet order on the Leads page and in the dialer, the dialer order setting, rows moving in the sheet."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEY_FILE = f"{TMP}/google_service_account.json"
os.environ.update(GOOGLE_SERVICE_ACCOUNT_FILE=KEY_FILE, CARTESIA_CALLS_DISABLED="1", CARTESIA_EMAIL_TOOL_ID="",
                  CARTESIA_SUPERVISOR_TOOL_ID="", SMS_SENDING_DISABLED="1", TELNYX_MESSAGING_PROFILE_ID="")
private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
with open(KEY_FILE, "w", encoding="utf-8") as f:
    json.dump({"type": "service_account", "private_key": private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
        "client_email": "sheets@test.iam.gserviceaccount.com", "token_uri": "https://oauth2.googleapis.com/token"}, f)

import env_utils  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import dialer as dialer_mod, gsheets, sheet_sync, tunnel  # noqa: E402
from app.db import Lead, LeadEvent, get_session, utcnow  # noqa: E402


async def no_tunnel():
    return None

tunnel.watch = no_tunnel
from app.main import app, dialer  # noqa: E402

SALES = "1SalesSheetAbcdefghijklmnopqrstuvwxyz0123"
TAB_A, TAB_B = "Tab A", "Tab B"
H_HEAD = ["Date", "Source", "Client Name", "Business Description", "Contact Number", "Email Address", "City, State, Zip",
          "Services Required", "Budget", "Addtional info", "Sam", "Umar"]
Y_HEAD = ["Date", "Source", "Client Name", "Profile link", "Business Description", "Contact Number", "Email Address",
          "City, State, Zip", "Services Required", "Budget", "Addtional info", "Sam", "Umar"]
WHITE, LRED, RED2, RED, YEL, GRN = "#ffffff", "#ea9999", "#e06666", "#ff0000", "#ffff00", "#00ff00"


def rgb(h):
    h = h.lstrip("#")
    return {k: int(h[i:i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4)) if int(h[i:i + 2], 16)}


def row(values, colors=None, width=None):
    """colors: one hex for the whole row, or {column index: hex}; like the sheet, A-B are white on the red rows."""
    width = width or len(values)
    values = values + [""] * (width - len(values))
    out = []
    for i, v in enumerate(values):
        c = {"formattedValue": v} if v else {}
        hx = colors.get(i) if isinstance(colors, dict) else colors
        if hx:
            c["effectiveFormat"] = {"backgroundColor": rgb(hx), "backgroundColorStyle": {"rgbColor": rgb(hx)}}
        out.append(c)
    return out


def tab_a(name, phone, colors=None, info=""):
    return row(["11/01/2026", "Bark", name, "Bakery", phone, "", "Miami, FL 33101", "Website", "", info], colors, 12)


A_B_WHITE_REST_LRED = {i: (WHITE if i < 2 else LRED) for i in range(12)}
SHEET = {
    TAB_A: [
        row(H_HEAD),
        tab_a("Plain Paul", "(305) 555-0101", WHITE, info="Wants a shop"),  # row 2
        tab_a("Light Red Lia", "(305) 555-0102", A_B_WHITE_REST_LRED),  # row 3: white A-B, light red C-L
        tab_a("Yellow Yan", "(305) 555-0103", YEL),  # row 4
        tab_a("Red Rae", "(305) 555-0104", RED2),  # row 5
        tab_a("Only Sam", "(305) 555-0105", {10: RED}),  # row 6: painted only in the salesperson's column K
        tab_a("Green Gus", "(305) 555-0106", GRN),  # row 7
        tab_a("No Number", "na", WHITE),  # row 8
        tab_a("Uk Ulla", "44 7700 900123"),  # row 9
        tab_a("Dash Dan", "540–555–0152"),  # row 10: en dashes
        tab_a("Plain Pia", "(305) 555-0107"),  # row 11
        tab_a("Empty Ed", ""),  # row 12
        tab_a("Bad Bo", "316-153-44091"),  # row 13
    ],
    TAB_B: [
        row(Y_HEAD),
        row(["11/04/2026", "LinkedIn", "Bea Plain", "linkedin.com/in/beaplain", "Dentist", "(305) 555-0201", "", "Miami, FL",
             "SEO", "", "Has a site"], WHITE, 13),  # row 2
        row(["11/04/2026", "Bark", "Bea Red", "", "Cafe", "(305) 555-0202", "", "Miami, FL", "SEO", "", ""], RED, 13),  # row 3
        row(["11/04/2026", "Bark", "Bea NA", "", "Cafe", "N/A", "", "", "", "", ""], {5: "#fafcff"}, 13),  # row 4
    ],
}
RULES = {TAB_B: {"conditionalFormats": [{"ranges": [{}]}], "bandedRanges": [{"range": {}}, {"range": {}}]}}
log = []


async def fake_http(method, url, *, headers=None, json_body=None, form=None):
    if url == "https://oauth2.googleapis.com/token":
        return 200, {"access_token": "tok", "expires_in": 3600}
    u = urlsplit(url)
    q = parse_qs(u.query)
    log.append((method, url))
    assert method == "GET" and unquote(u.path).startswith(f"/v4/spreadsheets/{SALES}"), url  # read only
    if q.get("includeGridData"):
        want = [r.split("!")[0].strip("'").replace("''", "'") for r in q["ranges"]]
        want.reverse()  # Google's order isn't ours to trust: the sync puts the tabs in the setting's order
        return 200, {"sheets": [{"properties": {"title": t}, **RULES.get(t, {}),
                                 "data": [{"rowData": [{"values": r} for r in SHEET[t]]}]} for t in want]}
    return 200, {"sheets": [{"properties": {"title": t}} for t in SHEET]}

gsheets._http = fake_http


def lead(phone):
    with get_session() as s:
        return s.exec(select(Lead).where(Lead.phone == phone)).first()


def count():
    with get_session() as s:
        return len(s.exec(select(Lead)).all())


# ---- 1. cells: long dashes, the skip groups, the sheet's own header spellings ------------------------------------
assert sheet_sync.split_phones("540–555–0152") == (["+15405550152"], "")
assert sheet_sync.split_phones("(305)—555‐0101")[0] == ["+13055550101"]
for cell, want in (("", "empty"), ("na", "text"), ("N/A", "text"), ("whatsapp only", "text"), ("44 1632 960123", "uk"),
                   ("on whatsapp +44 7843 238589", "uk"), ("+91 98765 43210", "foreign"), ("9851089399,", "invalid"),
                   ("316-153-44091", "invalid"), ("443-555-12", "invalid")):
    assert sheet_sync.skip_group(cell) == want, (cell, sheet_sync.skip_group(cell), want)
hm = sheet_sync._header_map([[{"text": h, "rgb": None} for h in Y_HEAD]])
assert hm[1]["additional_info"] == 10 and hm[1]["links"] == 3 and hm[1]["phone"] == 5, hm
print("cells: long dashes in a number are fine; skipped rows grouped (empty / na / UK / other country / invalid);"
      " 'Addtional info' and 'Profile link' read")

# ---- 2. the saved setting said only green: green, yellow and red count anyway --------------------------------------
sheet_sync.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
sheet_sync.SETTINGS_PATH.write_text(json.dumps({"sales_sheet": SALES, "tabs": [TAB_A, TAB_B], "handled_colors": ["green"]}))
assert sheet_sync.load()["handled_colors"] == ["green", "yellow", "red"]
assert sheet_sync.load()["dialer_order"] == "sheet"
print("setting: a saved ['green'] still reads as green + yellow + red; dialer order defaults to sheet order")

with TestClient(app) as c:
    assert dialer.state == "off"
    st = c.get("/api/sheets").json()
    assert st["always_handled"] == ["green", "yellow", "red"] and st["settings"]["handled_colors"] == ["green", "yellow", "red"]

    # ---- 3. leads imported earlier, when only green counted (like the real import) --------------------------------
    now = utcnow()
    with get_session() as s:
        for name, phone, r in (("Plain Paul", "+13055550101", 2), ("Light Red Lia", "+13055550102", 3),
                               ("Yellow Yan", "+13055550103", 4), ("Red Rae", "+13055550104", 5),
                               ("Only Sam", "+13055550105", 6)):
            s.add(Lead(phone=phone, name=name, location="Miami, FL 33101", sheet_tab=TAB_A, sheet_row=r, status="new",
                       source=f"Google Sheet: {TAB_A}", created_at=now, updated_at=now))
        s.add(Lead(phone="+13055550202", name="Bea Red", sheet_tab=TAB_B, sheet_row=3, status="callback", attempts=1,
                   callback_at=now + timedelta(days=1), location="Miami, FL", created_at=now, updated_at=now))
        s.add(Lead(phone="+13055550201", name="Bea Plain", sheet_tab=TAB_B, sheet_row=2, dnc=True,
                   status="do_not_contact", created_at=now, updated_at=now))
        s.commit()
    n0 = count()

    # ---- 4. Re-check colours: only the colour rule, on leads we already have ---------------------------------------
    r = c.post("/api/sheets/recheck-colors")
    assert r.status_code == 200, r.text
    sm = r.json()
    assert sm["ok"] and sm["colors_only"] and sm["new"] == 0 and sm["filled"] == 0 and sm["moved"] == 0, sm
    assert count() == n0  # nothing imported
    assert sm["handled"]["existing"] == 5 and sm["not_imported"] == 3, sm  # Lia, Yan, Rae, Sam, Bea Red / Gus, Dan, Pia
    marked = {x["name"]: x["color"] for x in sm["marked_handled"]}
    assert marked == {"Light Red Lia": "red", "Yellow Yan": "yellow", "Red Rae": "red", "Only Sam": "red",
                      "Bea Red": "red"}, marked
    for p in ("+13055550102", "+13055550103", "+13055550104", "+13055550105"):
        x = lead(p)
        assert x.status == "handled_by_sales" and x.sales_handled, (p, x.status)
    p = lead("+13055550101")
    assert p.status == "new" and not p.sales_handled and p.additional_info == ""  # white: untouched, nothing filled
    y = lead("+13055550202")
    assert y.status == "handled_by_sales" and y.callback_at is None and y.attempts == 1  # red row: callback cleared
    d = lead("+13055550201")
    assert d.dnc and d.status == "do_not_contact" and not d.sales_handled  # white row: DNC untouched
    # the colour table: every distinct colour, rows with it, its family, handled or not; per tab, TAB_A first
    tbl = [(x["tab"], x["hex"], x["rows"], x["family"], x["handled"]) for x in sm["colors"]]
    assert tbl[0][0] == TAB_A and tbl[-1][0] == TAB_B, tbl
    for want in ((TAB_A, WHITE, 3, "none", False), (TAB_A, LRED, 1, "red", True), (TAB_A, YEL, 1, "yellow", True),
                 (TAB_A, RED2, 1, "red", True), (TAB_A, RED, 1, "red", True), (TAB_A, GRN, 1, "green", True),
                 (TAB_A, "", 6, "none", False), (TAB_B, RED, 1, "red", True), (TAB_B, "#fafcff", 1, "none", False)):
        assert want in tbl, (want, tbl)
    fams = {x["family"]: x["rows"] for x in sm["row_colors"][TAB_A]}
    assert fams == {"none": 7, "red": 3, "yellow": 1, "green": 1}, fams
    assert sm["color_rules"] == {TAB_B: {"conditional": 1, "banded": 2}}, sm["color_rules"]
    groups = {g["group"]: g["count"] for g in sm["skipped_groups"]}
    assert groups == {"empty": 1, "text": 2, "uk": 1, "invalid": 1}, groups
    assert sm["skipped_count"] == 5 and all(x.get("group") for x in sm["skipped"])
    with get_session() as s:
        ev = [e.text for e in s.exec(select(LeadEvent).where(LeadEvent.lead_id == lead("+13055550105").id)).all()]
    assert any("row 6" in e and "red" in e for e in ev), ev
    assert dialer.state == "off" and all(m == "GET" for m, _ in log)
    print("re-check: 5 red/yellow leads (incl. one painted only in column K and a callback) -> Handled by sales;"
          " nothing imported, filled or moved; DNC + white untouched; colour table by hex; rules counted; skips grouped")

    # ---- 5. a full sync: the rest comes in at its sheet position; empty fields filled ------------------------------
    sm = c.post("/api/sheets/sync").json()
    assert sm["ok"] and not sm["colors_only"] and sm["new"] == 2 and sm["handled"]["new"] == 1, sm  # Dan, Pia; Gus
    dan = lead("+15405550152")
    assert dan and (dan.sheet_tab, dan.sheet_row, dan.status) == (TAB_A, 10, "new")
    assert lead("+13055550106").status == "handled_by_sales"
    assert lead("+13055550101").additional_info == "Wants a shop" and sm["filled"] >= 1  # "Addtional info"
    assert lead("+13055550201").links == "linkedin.com/in/beaplain"  # Profile link (DNC lead: only an empty field)
    assert c.get("/api/sheets").json()["last_sync"]["new"] == 2
    print("sync: the rest imported with its tab + row (incl. the en-dash number); 'Addtional info' and 'Profile link' filled")

    # ---- 6. the Leads page: sheet order by default (TAB_A top to bottom, then TAB_B, then leads from elsewhere) -------
    with get_session() as s:
        s.add(Lead(phone="+13055550999", name="Csv Cora", location="Miami, FL", status="new",
                   created_at=now - timedelta(days=9), updated_at=utcnow() + timedelta(seconds=5)))
        s.commit()
    rows = c.get("/api/leads").json()["leads"]
    order = [(x["sheet_tab"], x["sheet_row"]) for x in rows]
    assert order == [(TAB_A, 2), (TAB_A, 3), (TAB_A, 4), (TAB_A, 5), (TAB_A, 6), (TAB_A, 7), (TAB_A, 10), (TAB_A, 11),
                     (TAB_B, 2), (TAB_B, 3), ("", None)], order
    assert c.get("/api/leads?sort=recent").json()["leads"][0]["name"] == "Csv Cora"  # last change first
    print("leads page: sheet order by default (TAB_A rows, then TAB_B, then a CSV lead); 'recent' still available")

    # ---- 7. the dialer: sheet order, or newest first ------------------------------------------------------------
    wed_noon_miami = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)
    picked, _ = dialer_mod.pick_next(wed_noon_miami)
    assert picked.name == "Plain Paul", picked.name  # TAB_A row 2 (rows 3-7 are handled)
    callable_ = [x.name for x in sheet_sync.queue_order(
        [lead(p) for p in ("+13055550999", "+13055550107", "+15405550152", "+13055550101")])]
    assert callable_ == ["Plain Paul", "Dash Dan", "Plain Pia", "Csv Cora"], callable_
    snap = c.get("/api/dialer").json()
    assert snap["order"] == "sheet" and set(snap["orders"]) == {"sheet", "newest"}
    assert c.post("/api/dialer/order", json={"order": "random"}).status_code == 400
    snap = c.post("/api/dialer/order", json={"order": "newest"}).json()
    assert snap["order"] == "newest" and sheet_sync.load()["dialer_order"] == "newest"
    assert sheet_sync.load()["handled_colors"] == ["green", "yellow", "red"]  # nothing else changed
    picked, _ = dialer_mod.pick_next(wed_noon_miami)
    assert picked.name in ("Dash Dan", "Plain Pia"), picked.name  # imported in step 5: the newest
    assert picked.name == "Plain Pia"  # same import: the lower row is the newer one
    c.post("/api/dialer/order", json={"order": "sheet"})
    assert dialer_mod.pick_next(wed_noon_miami)[0].name == "Plain Paul"
    assert dialer.state == "off"
    print("dialer: sheet order (TAB_A top first, handled rows skipped, CSV leads last); 'newest first' setting; never turned on")

    # ---- 8. rows inserted above: the leads keep their place; a new top row comes first -----------------------------
    SHEET[TAB_A].insert(1, tab_a("Top Tia", "(305) 555-0108"))
    sm = c.post("/api/sheets/sync").json()
    assert sm["new"] == 1 and sm["moved"] >= 8, sm
    assert (lead("+13055550101").sheet_row, lead("+15405550152").sheet_row) == (3, 11)
    assert lead("+13055550202").sheet_row == 3  # TAB_B unchanged
    assert c.get("/api/leads").json()["leads"][0]["name"] == "Top Tia"
    assert dialer_mod.pick_next(wed_noon_miami)[0].name == "Top Tia"
    with get_session() as s:
        ev = s.exec(select(LeadEvent).where(LeadEvent.lead_id == lead("+13055550101").id)).all()
    assert not any("row 3" in e.text for e in ev)  # a move isn't an event
    print("moves: a row inserted at the top shifts the others' row numbers; the new row is first in the list and the queue")

print("ALL SHEET COLOUR + ORDER TESTS PASS")
