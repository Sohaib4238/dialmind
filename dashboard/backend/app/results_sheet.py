"""Call results for the sales team, in OUR OWN Google Sheet (never the sales sheet): one row per lead we've called.

The row is found by its Lead ID (column A), so sorting or filtering the sheet is fine; a new lead is appended.
Written after each call (leads.on_call_finished marks the lead "dirty"; sheet_sync.watch flushes within seconds), and
when a called lead turns "Handled by sales". Values are written RAW: text from a lead never becomes a formula.
Columns: see HEADER. Times are in the results sheet's time zone (Leads page setting).
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import select

from app import gsheets, leads, outcomes, standby
from app.bus import bus
from app.db import Call, Lead, as_utc, get_session, utcnow

logger = logging.getLogger("dashboard.results_sheet")

HEADER = ["Lead ID", "Name", "Phone", "Source", "Sales sheet tab", "Last call", "Attempts", "Tag", "Callback time",
          "Summary", "Transferred", "Transferred to", "Updated"]
LAST_COL = chr(ord("A") + len(HEADER) - 1)
BATCH = 200
RETRY_AFTER_S = 120
_lock = asyncio.Lock()


def _fmt(dt, tz: str) -> str:
    if not dt:
        return ""
    t = as_utc(dt).astimezone(ZoneInfo(tz))
    return f"{t:%Y-%m-%d} {t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'} {t.tzname()}"


def tag(lead: Lead, last: Call | None) -> str:
    """The sales team's tag (outcomes.py labels): Do not contact and Handled by sales win; else the latest call."""
    if lead.dnc:
        return "Do not contact"
    if lead.sales_handled:
        return "Handled by sales"
    if last is not None and last.status in ("ended", "failed"):
        return outcomes.label(last.outcome or outcomes.from_result(last.result))
    return outcomes.STATUS_LABELS.get(lead.status, lead.status)


def row_for(lead: Lead, calls: list[Call], tz: str) -> list:
    last = next((c for c in calls if c.id == lead.last_call_id), calls[0] if calls else None)
    moved = next((c for c in calls if c.transfer_status == "transferred"), None)
    summary = ""
    if last is not None:
        summary = last.ai_summary or last.outcome_note or last.transfer_summary or last.reason or ""
    return [lead.id, lead.name, leads.format_phone(lead.phone), lead.lead_source or lead.source, lead.sheet_tab,
            _fmt(lead.last_call_at, tz), lead.attempts, tag(lead, last),
            _fmt(lead.callback_at, tz) if not lead.dnc and not lead.sales_handled else "", summary[:500],
            "Yes" if moved else "No", (moved.transfer_to_name or "Supervisor") if moved else "", _fmt(utcnow(), tz)]


def _dirty() -> list[tuple[Lead, list[Call]]]:
    with get_session() as s:
        rows = s.exec(select(Lead).where(Lead.results_dirty == True).limit(BATCH)).all()  # noqa: E712
        out = []
        for lead in rows:
            if not lead.attempts and not lead.last_call_id:  # never called: no row (nothing to report yet)
                lead.results_dirty = False
                s.add(lead)
                continue
            calls = s.exec(select(Call).where(Call.lead_id == lead.id).order_by(Call.id.desc())).all()
            out.append((lead, calls))
        s.commit()
    return out


async def flush() -> dict | None:
    """Write every lead marked dirty. None when the results sheet isn't set up (nothing to do)."""
    from app import sheet_sync

    cfg = sheet_sync.load()
    sid = gsheets.sheet_id_from(cfg["results_sheet"])
    if not sid or standby.on() or not gsheets.key_info()["ok"] or _lock.locked():
        return None
    last = sheet_sync._state().get("results") or {}
    if last.get("ok") is False and (utcnow() - as_utc(datetime.fromisoformat(last["at"]))).total_seconds() < RETRY_AFTER_S:
        return None  # it failed a moment ago (sheet not shared yet?): don't ask Google every 30 s
    async with _lock:
        todo = await asyncio.to_thread(_dirty)
        if not todo:
            return None
        tab, tz = cfg["results_tab"], cfg["results_timezone"]
        try:
            await gsheets.ensure_tab(sid, tab)
            col_a = await gsheets.values_get(sid, gsheets.a1(tab, "A:A"))
            if not col_a or not col_a[0] or str(col_a[0][0]).strip() != HEADER[0]:
                await gsheets.values_update(sid, [{"range": gsheets.a1(tab, f"A1:{LAST_COL}1"), "values": [HEADER]}])
                col_a = [[HEADER[0]], *col_a[1:]] if col_a else [[HEADER[0]]]
            where = {str(r[0]).strip(): i + 1 for i, r in enumerate(col_a) if r and str(r[0]).strip()}
            updates, appends, seen = [], [], {}
            for lead, calls in todo:
                values = row_for(lead, calls, tz)
                seen[lead.id] = lead.updated_at
                if n := where.get(str(lead.id)):
                    updates.append({"range": gsheets.a1(tab, f"A{n}:{LAST_COL}{n}"), "values": [values]})
                else:
                    appends.append(values)
            await gsheets.values_update(sid, updates)
            await gsheets.values_append(sid, gsheets.a1(tab, f"A1:{LAST_COL}1"), appends)
        except gsheets.SheetsError as e:
            logger.warning("results sheet: %s", e)
            res = {"ok": False, "at": utcnow().isoformat(), "error": str(e)}
            sheet_sync._save_state(results=res)
            bus.publish({"type": "sheet_results", "results": res})
            return res
        await asyncio.to_thread(_mark_clean, seen)
        res = {"ok": True, "at": utcnow().isoformat(), "written": len(todo), "updated": len(updates), "added": len(appends)}
        sheet_sync._save_state(results=res)
        bus.publish({"type": "sheet_results", "results": res})
        return res


def _mark_clean(seen: dict) -> None:
    """Clear the flag only if the lead didn't change while we were writing (else it goes out on the next tick)."""
    with get_session() as s:
        for lead_id, updated in seen.items():
            lead = s.get(Lead, lead_id)
            if lead is not None and lead.updated_at == updated:
                lead.results_dirty = False
                s.add(lead)
        s.commit()


def mark_all_called() -> int:
    """'Rewrite the results sheet': every lead we've called goes out again."""
    with get_session() as s:
        rows = s.exec(select(Lead).where(Lead.attempts > 0)).all()
        for lead in rows:
            lead.results_dirty = True
            s.add(lead)
        s.commit()
    return len(rows)
