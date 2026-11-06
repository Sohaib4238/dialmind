"""Leads from the sales team's Google Sheet (READ ONLY), every N minutes and on "Sync now" (Leads page).

- Only the configured tabs (default "Tab A" and "Tab B"), all sources.
- Only NEW leads are added (status "new"). A lead we already have keeps its status, calls, history and DNC; only its
  EMPTY sheet fields are filled in (source, budget, additional info...). Importing never starts the dialer.
- Row colours: a row painted in a "handled" colour belongs to the sales team. Green, yellow and red (any shade; red =
  wrong number etc.) ALWAYS count (ALWAYS_HANDLED, whatever is saved); others can be added. A new one comes in as
  "Handled by sales" (or is skipped: setting); an existing lead whose row turns coloured is never called again (status
  "Handled by sales", callback cleared). DNC still wins. Other colours are called normally. Every sync reports each
  distinct colour found (hex, rows, family); "Re-check colours" applies only the colour rule to leads we already have.
- Messy cells: 2-3 numbers in one cell (first valid US number = main, the rest = alternates), several emails in one
  cell, website / LinkedIn links inside the business description, a missing city, long dashes in a number. Dedupe by
  every normalized number, across both tabs and our existing leads.
- Sheet order: each lead keeps its tab + row (updated when rows move). The Leads page and the dialer go top to bottom,
  first tab first (the tabs setting's order); the dialer can use "newest first" instead (dialer_order).

Settings: data/sheets.json (Leads page). Last result: data/sheets_state.json. The Google key: gsheets.py.
"""

import asyncio
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from sqlmodel import select

from app import gsheets, leads, standby
from app.bus import bus
from app.db import DATA_DIR, Lead, LeadEvent, as_utc, get_session, utcnow

logger = logging.getLogger("dashboard.sheet_sync")

SETTINGS_PATH = Path(os.getenv("SHEETS_SETTINGS_PATH") or DATA_DIR / "sheets.json")
STATE_PATH = Path(os.getenv("SHEETS_STATE_PATH") or DATA_DIR / "sheets_state.json")
TICK_S = 30
SYNC_MIN, SYNC_MAX = 5, 1440
MAX_LISTED = 300  # rows listed per kind in the summary (counts are always complete)

DEFAULTS = {
    "enabled": False,  # the automatic sync; "Sync now" works whenever the sheet and the key are set
    "sales_sheet": "",  # link or ID of the sales team's sheet (read only)
    "tabs": ["Tab A", "Tab B"],
    "sync_minutes": 30,
    "handled_colors": ["green", "yellow", "red"],
    "handled_action": "import",  # import (as "Handled by sales", never called) / skip
    "results_sheet": "",  # OUR sheet for call results (Editor), never the sales sheet
    "results_tab": "Call results",
    "results_timezone": "America/New_York",
    "dialer_order": "sheet",  # the auto-dialer's order for new leads: sheet (top to bottom, first tab first) / newest
}
# Rows in these colours are never called, whatever is saved (the sales team's rule: green / yellow = theirs, red = wrong
# number etc.). 2026-11-05: the saved setting was ["green"] only, so 104 yellow / red rows were imported as callable.
ALWAYS_HANDLED = ("green", "yellow", "red")
DIALER_ORDERS = {"sheet": "Sheet order (top to bottom)", "newest": "Newest first"}
RESULT_ZONES = {"America/New_York": "US Eastern", "America/Chicago": "US Central", "America/Denver": "US Mountain",
                "America/Los_Angeles": "US Pacific", "UTC": "UTC"}


class SheetSettingsError(Exception):
    pass


# ---- settings + state ----------------------------------------------------------------------

def load() -> dict:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    s = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    s["handled_colors"] = _handled(s["handled_colors"])
    if s["dialer_order"] not in DIALER_ORDERS:
        s["dialer_order"] = DEFAULTS["dialer_order"]
    return s


def _handled(colors) -> list[str]:
    """The saved colours plus the ones that always count, in the usual order."""
    saved = colors if isinstance(colors, list) else []
    return [c for c in gsheets.COLOR_NAMES if c in ALWAYS_HANDLED or c in saved]


def _write(s: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)


def set_dialer_order(order: str) -> dict:
    """The dialer's order (Leads page, dialer panel). Changes only that setting."""
    if order not in DIALER_ORDERS:
        raise SheetSettingsError(f"Order: one of {', '.join(DIALER_ORDERS)}.")
    s = load()
    s["dialer_order"] = order
    _write(s)
    return s


def save(values: dict) -> dict:
    s = load()
    before = (s["results_sheet"], s["results_tab"])
    s.update({k: v for k, v in values.items() if k in DEFAULTS and v is not None})
    s["enabled"] = bool(s["enabled"])
    for k in ("sales_sheet", "results_sheet"):
        link = str(s[k] or "").strip()
        if link and not gsheets.sheet_id_from(link):
            raise SheetSettingsError(f"That doesn't look like a Google Sheet link: {link[:80]}")
        s[k] = link
    sales, results = gsheets.sheet_id_from(s["sales_sheet"]), gsheets.sheet_id_from(s["results_sheet"])
    if results and results == sales:
        raise SheetSettingsError("The results sheet must be a different sheet: we never write to the sales team's sheet.")
    tabs = s["tabs"] if isinstance(s["tabs"], list) else str(s["tabs"]).split("\n")
    s["tabs"] = list(dict.fromkeys(t.strip() for t in tabs if str(t).strip()))[:10]
    if not s["tabs"]:
        raise SheetSettingsError("Name at least one tab to read.")
    try:
        s["sync_minutes"] = int(s["sync_minutes"])
    except (TypeError, ValueError):
        raise SheetSettingsError("Sync every: a number of minutes.") from None
    if not SYNC_MIN <= s["sync_minutes"] <= SYNC_MAX:
        raise SheetSettingsError(f"Sync every {SYNC_MIN}-{SYNC_MAX} minutes.")
    s["handled_colors"] = _handled(s["handled_colors"])
    if s["handled_action"] not in ("import", "skip"):
        raise SheetSettingsError("Handled rows: import or skip.")
    if s["dialer_order"] not in DIALER_ORDERS:
        raise SheetSettingsError(f"Dialer order: one of {', '.join(DIALER_ORDERS)}.")
    s["results_tab"] = re.sub(r"\s+", " ", str(s["results_tab"] or "")).strip()[:90] or DEFAULTS["results_tab"]
    if s["results_timezone"] not in RESULT_ZONES:
        raise SheetSettingsError("Pick a time zone for the results sheet.")
    _write(s)
    if (s["results_sheet"], s["results_tab"]) != before:
        _save_state(results={})  # a new link may fix the last error: the results sheet is tried again at once
    wake()
    return status()


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(**kv) -> None:
    st = {**_state(), **kv}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(st, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def status() -> dict:
    cfg, st = load(), _state()
    last = st.get("last_sync") or {}
    nxt = None
    if cfg["enabled"] and last.get("at"):
        nxt = datetime.fromisoformat(last["at"]).timestamp() + cfg["sync_minutes"] * 60
    return {"settings": cfg, "key": gsheets.key_info(), "last_sync": last or None, "results": st.get("results") or None,
            "next_sync_at": datetime.fromtimestamp(nxt).astimezone().isoformat() if nxt else None,
            "running": _lock.locked(), "standby": standby.on(), "colors": list(gsheets.COLOR_NAMES),
            "always_handled": list(ALWAYS_HANDLED), "result_zones": RESULT_ZONES, "dialer_orders": DIALER_ORDERS}


# ---- reading one row ---------------------------------------------------------------------------

def _k(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


COLUMNS = {  # our field -> sheet headers (normalized: lowercase letters and digits only)
    "phone": {"contactnumber", "phone", "phonenumber", "number", "mobile", "cell", "contact", "phoneno", "mobilenumber"},
    "name": {"clientname", "name", "client", "contactname", "fullname", "leadname"},
    "business": {"businessname", "company", "companyname"},
    "business_description": {"businessdescription", "business", "description", "aboutbusiness", "businessdetails"},
    "email": {"emailaddress", "email", "emails", "mail", "emailid"},
    "location": {"citystatezip", "location", "address", "citystate", "city"},
    "service_required": {"servicesrequired", "servicerequired", "services", "service", "serviceneeded"},
    "lead_source": {"source", "leadsource", "platform", "channel"},
    "budget": {"budget"},
    # "Addtional info": the sales sheet's own spelling (both tabs, 2026-11-05)
    "additional_info": {"additionalinfo", "addtionalinfo", "additonalinfo", "additionalinformation", "additionalnotes",
                        "notes", "info", "comments"},
    "date": {"date", "dateadded", "leaddate"},
    "links": {"profilelink", "profile", "profileurl", "website", "websiteurl", "link", "links", "linkedin",
              "linkedinprofile"},  # TAB_B's "Profile link"
}
DASHES = re.compile("[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]")  # long / unicode dashes typed in a number
PHONE_RE = re.compile(r"(?:\+?1[\s.\-]*)?\(?\d{3}\)?[\s.\-]*\d{3}[\s.\-]*\d{4}(?:\s*(?:x|ext\.?)\s*\d{1,5})?", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b(?:[a-z0-9-]+\.)*linkedin\.com/\S*", re.I)
STATE_ZIP_RE = re.compile(r"^\s*(?:[A-Za-z]{2})?\s*\d{5}(?:-\d{4})?\s*$|^\s*[A-Za-z]{2}\s*$")


def _header_map(rows: list[list[dict]]) -> tuple[int, dict[str, int]] | None:
    """(index of the header row, {field: column}) from the first 10 rows; None without a phone column."""
    for i, r in enumerate(rows[:10]):
        keys = [_k(c["text"]) for c in r]
        m: dict[str, int] = {}
        for field, names in COLUMNS.items():
            for j, k in enumerate(keys):
                if k in names and j not in m.values():
                    m[field] = j
                    break
        if "phone" in m:
            return i, m
    return None


def _clean(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").replace("\x00", "")).strip()[: leads.MAX_FIELD_CHARS]


def split_phones(cell: str) -> tuple[list[str], str]:
    """Every valid US number in a cell, in order, without repeats; or ([], why)."""
    found, bad = [], []
    cell = DASHES.sub("-", cell or "")  # 540–599–6752 (en dashes) is a fine number
    for m in PHONE_RE.finditer(cell):
        p, why = leads.normalize_us_phone(m.group(0))
        if p and p not in found:
            found.append(p)
        elif not p:
            bad.append(why)
    if found:
        return found, ""
    if not (cell or "").strip():
        return [], "no phone number"
    return [], f"no valid US number in {cell.strip()[:40]!r}" + (f" ({bad[0]})" if bad else "")


SKIP_GROUPS = {  # key -> label, in the order the summary lists them
    "empty": "No phone number: the cell is empty",
    "text": "No phone number: the cell says \"na\", \"N/A\" or other text",
    "uk": "UK number (+44): we only call US numbers",
    "foreign": "Another country's number: we only call US numbers",
    "invalid": "Not a valid US number (wrong number of digits, or an area / exchange code that can't exist)",
    "handled": "Handled by sales: not imported (setting)",
    "other": "Other",
}


def skip_group(cell: str) -> str:
    """Why a row has no number we can call, as a group key (SKIP_GROUPS)."""
    c = (cell or "").strip()
    digits = re.sub(r"\D", "", c)
    if not c:
        return "empty"
    if not digits:
        return "text"
    if "+44" in c or (digits.startswith("44") and len(digits) in (12, 13)):
        return "uk"
    if re.match(r"^\s*(?:\+|00)(?!1)", DASHES.sub("-", c)):
        return "foreign"
    return "invalid"


def split_emails(*cells: str) -> list[str]:
    out = []
    for cell in cells:
        for m in EMAIL_RE.finditer(cell or ""):
            e, _ = leads.normalize_email(m.group(0))
            if e and e not in out:
                out.append(e)
    return out


def split_links(text: str) -> tuple[str, list[str]]:
    """(the text without its URLs, the URLs: website / LinkedIn)."""
    links = [m.group(0).rstrip(".,;)") for m in URL_RE.finditer(text or "")]
    rest = URL_RE.sub(" ", text or "")
    rest = re.sub(r"\s*[|,;:-]\s*(?=[|,;:-]|$)", " ", rest)  # separators the links left behind
    return re.sub(r"\s+", " ", rest).strip(" ,;|-"), list(dict.fromkeys(links))


def row_color(cells: list[dict], handled_colors: list[str] = ()) -> str:
    """The row's colour family. Any cell in a "handled" colour makes it that colour (a salesperson may paint only the
    name); otherwise the first painted cell; "none" when nothing is painted."""
    names = [gsheets.color_name(c.get("rgb")) for c in cells]
    return next((n for n in names if n in handled_colors), next((n for n in names if n != "none"), "none"))


def parse_row(cells: list[dict], m: dict[str, int], handled_colors: list[str], width: int | None = None) -> dict:
    """One sheet row. width: how many cells count for the colour (all the header's columns; default the mapped ones)."""
    def col(field: str) -> str:
        j = m.get(field)
        return cells[j]["text"] if j is not None and j < len(cells) else ""

    phones, why = split_phones(col("phone"))
    emails = split_emails(col("email"), col("phone"))
    desc, links = split_links(_clean(col("business_description")))
    link_cell = _clean(col("links"))
    if link_cell:
        _, more = split_links(link_cell)
        if not more and " " not in link_cell and "." in link_cell:
            more = [link_cell]  # a bare "facebook.com/bakery"
        links = list(dict.fromkeys(links + more))
    location = _clean(col("location"))
    parts = [p.strip() for p in location.split(",")]
    city = parts[0] if len(parts) > 1 and not STATE_ZIP_RE.match(parts[0]) else ""
    name = _clean(col("name"))
    if EMAIL_RE.search(name) or PHONE_RE.search(name):
        name = ""
    width = width or ((max(m.values()) + 1) if m else len(cells))
    color = row_color(cells[:width], handled_colors)
    return {
        "phone_cell": col("phone"), "hexes": sorted({gsheets.hex_of(c.get("rgb")) for c in cells[:width]}),
        "phones": phones, "why": why, "name": name, "business": _clean(col("business")),
        "business_description": desc[: leads.MAX_FIELD_CHARS], "links": links,
        "email": emails[0] if emails else "", "alt_emails": emails[1:],
        "location": location, "city": city, "service_required": _clean(col("service_required")),
        "lead_source": _clean(col("lead_source"))[:100], "budget": _clean(col("budget"))[:200],
        "additional_info": _clean(col("additional_info")), "date": _clean(col("date"))[:40],
        "color": color, "handled": color in handled_colors,
        "empty": not any(c["text"].strip() for c in cells),
    }


# ---- the sync ---------------------------------------------------------------------------------

_lock = asyncio.Lock()
_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def wake() -> None:
    """Run the loop now (after a call, a settings change). Safe from any thread (some callers run in to_thread)."""
    if _wake is None or _loop is None or _loop.is_closed():
        return
    try:
        if asyncio.get_running_loop() is _loop:
            _wake.set()
            return
    except RuntimeError:
        pass
    _loop.call_soon_threadsafe(_wake.set)


def ready() -> tuple[bool, str]:
    cfg = load()
    if standby.on():
        return False, "standby: this copy doesn't read or write Google Sheets"
    if not gsheets.sheet_id_from(cfg["sales_sheet"]):
        return False, "no sales sheet link set"
    k = gsheets.key_info()
    if not k["ok"]:
        return False, k["detail"]
    return True, ""


async def sync_now(trigger: str = "manual", colors_only: bool = False) -> dict:
    """Read the tabs, add new leads, apply the colour rule. Returns the summary (also saved for the Leads page).
    colors_only ("Re-check colours"): only the colour rule on leads we already have; nothing imported or filled in."""
    ok, why = ready()
    if not ok:
        return {"ok": False, "error": why}
    if _lock.locked():
        return {"ok": False, "error": "A sync is already running."}
    async with _lock:
        cfg = load()
        started = utcnow()
        try:
            sid = gsheets.sheet_id_from(cfg["sales_sheet"])
            titles = await gsheets.tab_titles(sid)
            want = [t for t in cfg["tabs"] if t in titles]
            missing = [t for t in cfg["tabs"] if t not in titles]
            if not want:
                raise gsheets.SheetsError(f"None of the tabs were found. Tabs in the sheet: {', '.join(titles)[:300]}")
            meta: dict = {}
            grid = await gsheets.read_grid(sid, want, meta)
            grid = {t: grid[t] for t in want if t in grid}  # the tabs setting's order (TAB_A first)
            summary = await asyncio.to_thread(apply_rows, grid, cfg, colors_only, meta)
            summary.update(ok=True, missing_tabs=missing, trigger=trigger)
        except gsheets.SheetsError as e:
            logger.warning("sheet sync failed: %s", e)
            summary = {"ok": False, "error": str(e), "trigger": trigger}
        summary["at"] = started.isoformat()
        summary["colors_only"] = colors_only
        if colors_only:
            _save_state(last_sync=summary)
        else:
            _save_state(last_sync=summary, last_full_at=summary["at"])
        bus.publish({"type": "sheet_sync", "summary": summary})
        if summary.get("ok") and (summary["new"] or summary["handled"]["new"] or summary["handled"]["existing"]):
            bus.publish({"type": "leads", "action": "imported", "count": summary["new"]})
        if summary.get("ok") and summary["handled"]["existing"]:
            wake()  # a called lead turned "Handled by sales": its row in the results sheet changes
        return summary


def _family_label(family: str) -> str:
    return "No colour" if family == "none" else family.capitalize()


def apply_rows(grid: dict[str, list[list[dict]]], cfg: dict, colors_only: bool = False, meta: dict | None = None) -> dict:
    """Group the rows by phone number, then add the new ones and apply the colour rule (one DB transaction).
    colors_only: only mark leads we already have as handled; no new leads, no fields filled, no positions moved."""
    colors = cfg["handled_colors"]
    skipped, dup_rows, groups, by_phone = [], [], [], {}
    tab_rows: dict[str, int] = {}
    hex_rows: Counter = Counter()  # (tab, "#rrggbb" or "" = no fill) -> rows with that colour in any cell
    row_families: dict[str, Counter] = {}  # tab -> {colour family the row was read as: rows}
    for tab, rows in grid.items():
        hm = _header_map(rows)
        if hm is None:
            skipped.append({"tab": tab, "row": None, "reason": "no \"Contact Number\" (phone) column found",
                            "group": "other"})
            continue
        head, m = hm
        # the colour counts over every column with a header (incl. "Addtional info", the salespeople's own columns)
        width = max(max(m.values()) + 1, max((j + 1 for j, c in enumerate(rows[head]) if c["text"].strip()), default=0))
        n = 0
        fams = row_families.setdefault(tab, Counter())
        for i, cells in enumerate(rows[head + 1:], start=head + 2):  # i = the row number in the sheet
            r = parse_row(cells, m, colors, width)
            if r["empty"]:
                continue
            n += 1
            r.update(tab=tab, row=i)
            fams[r["color"]] += 1
            for h in r["hexes"] or [""]:
                hex_rows[(tab, h)] += 1
            if not r["phones"]:
                skipped.append({"tab": tab, "row": i, "name": r["name"], "reason": r["why"],
                                "handled": r["handled"], "group": skip_group(r["phone_cell"])})
                continue
            g = next((groups[by_phone[p]] for p in r["phones"] if p in by_phone), None)
            if g is not None:  # the same lead again (either tab): one lead; handled if any of its rows is
                dup_rows.append({"tab": tab, "row": i, "name": r["name"], "phone": r["phones"][0],
                                 "reason": f"same number as {g['first']['tab']} row {g['first']['row']}"})
                g["handled"] = g["handled"] or r["handled"]
                g["colors"].add(r["color"])
                for p in r["phones"]:
                    by_phone.setdefault(p, g["i"])
                continue
            g = {"i": len(groups), "first": r, "handled": r["handled"], "colors": {r["color"]}}
            groups.append(g)
            for p in r["phones"]:
                by_phone[p] = g["i"]
        tab_rows[tab] = n

    new, existing_handled, new_handled, skipped_handled, filled, moved, not_imported = 0, 0, 0, 0, 0, 0, 0
    marked = []
    now = utcnow()
    with get_session() as s:
        known: dict[str, Lead] = {}
        for lead in s.exec(select(Lead)).all():
            for p in [lead.phone, *[x for x in (lead.alt_phones or "").split(",") if x]]:
                known.setdefault(p, lead)
        for g in groups:
            r = g["first"]
            color = next((c for c in g["colors"] if c in colors), r["color"])
            lead = next((known[p] for p in r["phones"] if p in known), None)
            if lead is not None:
                dup_rows.append({"tab": r["tab"], "row": r["row"], "name": r["name"], "phone": r["phones"][0],
                                 "reason": "already a lead" + (" (Do not contact)" if lead.dnc else "")
                                 + (f": {lead.name}" if lead.name else ""), "lead_id": lead.id})
                if not colors_only:
                    if _fill_empty(s, lead, r):
                        filled += 1
                    if lead.sheet_tab and (lead.sheet_tab, lead.sheet_row) != (r["tab"], r["row"]):
                        # rows inserted / deleted above it: keep its place in the sheet order (no event, no "updated")
                        lead.sheet_tab, lead.sheet_row = r["tab"], r["row"]
                        s.add(lead)
                        moved += 1
                if g["handled"] and leads.mark_sales_handled(
                        s, lead, True, f"its row in {r['tab']} (row {r['row']}) is {color} in the sales sheet"):
                    existing_handled += 1
                    marked.append({"tab": r["tab"], "row": r["row"], "name": lead.name, "lead_id": lead.id, "color": color})
                continue
            if colors_only:
                not_imported += 1
                continue
            if g["handled"] and cfg["handled_action"] == "skip":
                skipped_handled += 1
                skipped.append({"tab": r["tab"], "row": r["row"], "name": r["name"],
                                "reason": f"handled by sales ({color} row): not imported", "group": "handled"})
                continue
            lead = Lead(phone=r["phones"][0], name=r["name"], business=r["business"],
                        business_description=r["business_description"], location=r["location"], city=r["city"],
                        service_required=r["service_required"], email=r["email"],
                        alt_phones=",".join(r["phones"][1:]), alt_emails=",".join(r["alt_emails"]),
                        links=" ".join(r["links"]), lead_source=r["lead_source"], budget=r["budget"],
                        additional_info=r["additional_info"], sheet_tab=r["tab"], sheet_row=r["row"],
                        source=f"Google Sheet: {r['tab']}", extra={"Date": r["date"]} if r["date"] else {},
                        status="handled_by_sales" if g["handled"] else "new", sales_handled=g["handled"],
                        sales_handled_at=now if g["handled"] else None, created_at=now, updated_at=now)
            s.add(lead)
            s.flush()
            for p in r["phones"]:
                known.setdefault(p, lead)
            s.add(LeadEvent(lead_id=lead.id, kind="imported",
                            text=f"Imported from the sales sheet: {r['tab']}, row {r['row']}"
                                 + (f" (Handled by sales: {color} row)" if g["handled"] else "")))
            new += 1
            new_handled += g["handled"]
        s.commit()
    if not colors_only:
        leads.apply_service_fit()  # a "new" lead may be something we don't sell
    for x in marked:
        if row := leads.get_lead_row(x["lead_id"]):
            leads._publish(row)
    groups_count = Counter(x.get("group") or "other" for x in skipped)
    tab_order = {t: i for i, t in enumerate(grid)}
    color_table = [
        {"tab": tab, "hex": h, "family": (fam := gsheets.color_name(_rgb_of(h))), "label": _family_label(fam),
         "rows": n, "handled": fam in colors}
        for (tab, h), n in sorted(hex_rows.items(), key=lambda kv: (tab_order.get(kv[0][0], 99), -kv[1], kv[0][1]))
    ]
    return {
        "tabs": tab_rows, "rows": sum(tab_rows.values()), "new": new - new_handled,
        "duplicates": len(dup_rows), "filled": filled, "moved": moved, "not_imported": not_imported,
        "handled": {"new": new_handled, "existing": existing_handled, "skipped": skipped_handled},
        "handled_colors": colors,
        "skipped_count": len(skipped), "skipped": skipped[:MAX_LISTED], "duplicate_rows": dup_rows[:MAX_LISTED],
        "skipped_groups": [{"group": k, "reason": SKIP_GROUPS[k], "count": groups_count[k]}
                           for k in SKIP_GROUPS if groups_count[k]],
        "marked_handled": marked[:MAX_LISTED],
        "colors": color_table,
        "row_colors": {tab: [{"family": f, "label": _family_label(f), "rows": n, "handled": f in colors}
                             for f, n in fams.most_common()] for tab, fams in row_families.items()},
        "color_rules": {t: v for t, v in (meta or {}).items() if v.get("conditional") or v.get("banded")},
    }


def _rgb_of(h: str) -> tuple | None:
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)) if h else None


SHEET_FIELDS = ("lead_source", "budget", "additional_info", "email", "service_required", "business_description",
                "location", "name")


def _fill_empty(s, lead: Lead, r: dict) -> bool:
    """A lead we already have: fill only EMPTY fields from the sheet (never overwrite, never touch status/calls/DNC)."""
    fill = {k: r[k] for k in SHEET_FIELDS if r.get(k) and not getattr(lead, k)}
    if not lead.sheet_tab:
        fill.update(sheet_tab=r["tab"], sheet_row=r["row"])
    if r["links"] and not lead.links:
        fill["links"] = " ".join(r["links"])
    if not fill:
        return False
    for k, v in fill.items():
        setattr(lead, k, v)
    lead.updated_at = utcnow()
    s.add(lead)
    names = [k.replace("_", " ") for k in fill if k not in ("sheet_tab", "sheet_row")]
    if names:
        s.add(LeadEvent(lead_id=lead.id, kind="note",
                        text=f"Filled empty fields from the sales sheet ({r['tab']}, row {r['row']}): {', '.join(names)}"))
    return bool(names)


# ---- sheet order (Leads page, auto-dialer) --------------------------------------------------------

def tab_ranks(cfg: dict | None = None) -> dict[str, int]:
    """{tab: position in the tabs setting}: TAB_A before TAB_B."""
    return {t: i for i, t in enumerate((cfg or load())["tabs"])}


def sheet_order_key(lead: Lead, ranks: dict[str, int]) -> tuple:
    """Sheet leads top to bottom, first tab first (a tab no longer in the setting after them); then leads from
    elsewhere (CSV, inbound), oldest first."""
    if lead.sheet_tab and lead.sheet_row:
        return (0, ranks.get(lead.sheet_tab, len(ranks)), lead.sheet_row, lead.id or 0)
    return (1, 0, 0, lead.id or 0)


def queue_order(items: list[Lead], order: str | None = None) -> list[Lead]:
    """New leads in the dialer's order (setting dialer_order): sheet order, or newest first."""
    cfg = load()
    if (order or cfg["dialer_order"]) == "newest":
        return sorted(items, key=lambda x: (as_utc(x.created_at), x.id or 0), reverse=True)
    ranks = tab_ranks(cfg)
    return sorted(items, key=lambda x: sheet_order_key(x, ranks))


def sql_sheet_order() -> list:
    """sheet_order_key as ORDER BY terms (the Leads page)."""
    from sqlalchemy import and_, case, literal

    ranks = tab_ranks()
    from_sheet = case((and_(Lead.sheet_tab != "", Lead.sheet_row.is_not(None)), 0), else_=1)
    rank = case(ranks, value=Lead.sheet_tab, else_=len(ranks)) if ranks else literal(0)
    return [from_sheet, rank, Lead.sheet_row, Lead.id]


# ---- every N minutes; the results sheet after each call ------------------------------------------

def _due(cfg: dict) -> bool:
    st = _state()
    last = st.get("last_full_at") or (st.get("last_sync") or {}).get("at")  # a colour re-check doesn't count
    if not last:
        return True
    return (utcnow() - as_utc(datetime.fromisoformat(last))).total_seconds() >= cfg["sync_minutes"] * 60


async def watch() -> None:
    """Background loop (main.py lifespan): the sales sheet when due, and dirty rows of the results sheet."""
    global _wake, _loop
    _wake, _loop = asyncio.Event(), asyncio.get_running_loop()
    from app import results_sheet

    while True:
        try:
            cfg = load()
            if cfg["enabled"] and ready()[0] and _due(cfg):
                await sync_now("auto")
            await results_sheet.flush()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad tick must never stop the loop
            logger.exception("sheet sync loop")
        try:
            await asyncio.wait_for(_wake.wait(), TICK_S)
        except TimeoutError:
            pass
        _wake.clear()
