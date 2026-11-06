"""Leads: CSV import (with preview), the leads dashboard queries, Do-Not-Contact, and keeping
each lead's status in step with its calls.

Phones are stored as US E.164 (+1NXXNXXXXXX). DNC wins over everything: a DNC lead's status
is always "do_not_contact", and the call manager refuses to dial a DNC number.
"""

import csv
import io
import os
import re
from collections import Counter
from datetime import timedelta

from sqlmodel import func, or_, select

from app import outcomes
from app.bus import bus
from app.timezones import lead_timezone
from app.db import LEAD_STATUSES, Call, Lead, LeadEvent, get_session, lead_event_to_dict, lead_to_dict, utcnow
from settings import SettingsError, keywords, load_settings  # shared/settings.py

MAX_CSV_BYTES = 5_000_000
MAX_ROWS = 20_000

# CSV header aliases (lowercased, spaces/underscores/dashes removed) -> our field
ALIASES = {
    "phone": {"phone", "phonenumber", "number", "mobile", "cell", "cellphone", "telephone", "tel", "phoneno", "contactnumber", "mobilenumber"},
    "name": {"name", "fullname", "contact", "contactname", "person", "lead", "leadname"},
    "first_name": {"firstname", "first", "givenname"},
    "last_name": {"lastname", "last", "surname", "familyname"},
    "business": {"business", "businessname", "company", "companyname", "organization", "organisation", "org", "brand", "shop", "store"},
    "location": {"location", "address", "fulladdress", "streetaddress", "businessaddress", "area", "region", "servicearea"},
    "city": {"city", "town", "locality"},
    "state": {"state", "province", "st"},
    "zip": {"zip", "zipcode", "postal", "postalcode", "postcode"},
    "service_required": {"servicerequired", "service", "services", "serviceneeded", "servicesneeded", "servicesrequired",
                         "requiredservice", "servicerequested", "need", "needs", "requirement", "requirements"},
    "business_description": {"businessdescription", "aboutbusiness", "aboutthebusiness", "businessinfo", "companydescription",
                             "businessdetails", "aboutcompany"},
    "job_description": {"jobdescription", "job", "jobdetails", "project", "projectdescription", "projectdetails",
                        "workdescription", "scope", "task"},
    "email": {"email", "emailaddress", "mail", "contactemail", "businessemail", "workemail", "emailid"},
    "notes": {"notes", "note", "comments", "comment", "remarks", "description"},
}
# Lead text fields from the CSV; each is cut to this many characters (empty is fine).
TEXT_FIELDS = ("name", "business", "city", "location", "service_required", "business_description", "job_description",
               "email")
MAX_FIELD_CHARS = 1000


class LeadError(Exception):
    pass


# ---- phone numbers -------------------------------------------------------------------

def normalize_us_phone(raw: str) -> tuple[str | None, str]:
    """(E.164 or None, reason if invalid). Accepts (555) 123-4567, 555.123.4567, +1 555..., 1-555-...;
    a trailing extension (x12, ext. 12) is ignored."""
    s = (raw or "").strip()
    if not s:
        return None, "empty phone"
    s = re.split(r"(?i)\s*(?:ext\.?|extension|x|#)\s*\d+\s*$", s)[0]
    if re.search(r"[A-Za-z]", s):
        return None, "contains letters"
    digits = re.sub(r"\D", "", s)
    if s.startswith("+") and not digits.startswith("1"):
        return None, "not a US number (+1)"
    if s.startswith("00"):
        return None, "international prefix, not a US number"
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None, f"{len(digits)} digits (a US number has 10)"
    area, exchange = digits[:3], digits[3:6]
    if area[0] in "01":
        return None, f"area code {area} can't start with {area[0]}"
    if area[1:] == "11":
        return None, f"{area} is a service code, not an area code"
    if exchange[0] in "01":
        return None, f"exchange {exchange} can't start with {exchange[0]}"
    return f"+1{digits}", ""


def format_phone(e164: str) -> str:
    m = re.fullmatch(r"\+1(\d{3})(\d{3})(\d{4})", e164 or "")
    return f"({m[1]}) {m[2]}-{m[3]}" if m else e164


# ---- email addresses -----------------------------------------------------------------

_EMAIL_RE = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")
# Common slips when an address is typed or heard: the AI is asked to check these with the lead.
DOMAIN_TYPOS = {"gmial.com": "gmail.com", "gmai.com": "gmail.com", "gamil.com": "gmail.com", "gmail.co": "gmail.com",
                "gmail.con": "gmail.com", "gnail.com": "gmail.com", "hotmial.com": "hotmail.com", "hotmail.co": "hotmail.com",
                "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com", "outlok.com": "outlook.com", "iclod.com": "icloud.com"}


def normalize_email(raw: str) -> tuple[str, str]:
    """(address or "", reason if invalid). Also takes the spoken form: "sarah at example dot com"."""
    s = (raw or "").strip().lower().removeprefix("mailto:")
    if "@" not in s:  # spoken: the words "at" / "dot" stand for the symbols
        s = re.sub(r"\s+at\s+", "@", f" {s} ").strip()
    s = re.sub(r"\s+dot\s+", ".", f" {s} ").strip()  # also "example dot com" after a written @
    s = re.sub(r"\s+", "", s).strip(".,;:<>()[]\"'")
    if not s:
        return "", "empty"
    if len(s) > 254 or not _EMAIL_RE.fullmatch(s):
        return "", f"{raw.strip()[:60]!r} isn't a full email address"
    return s, ""


def email_typo(address: str) -> str:
    """The likely meant domain ("gmail.com") if the address has a common typo, else ""."""
    return DOMAIN_TYPOS.get(address.rsplit("@", 1)[-1], "")


# ---- CSV -----------------------------------------------------------------------------

def _key(header: str) -> str:
    return re.sub(r"[\s_\-.]+", "", (header or "").strip().lower())


def _map_columns(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for i, h in enumerate(headers):
        k = _key(h)
        for field, names in ALIASES.items():
            if k in names and field not in mapping:
                mapping[field] = i
                break
    return mapping


def parse_csv(text: str, filename: str = "") -> dict:
    """Parse and check a leads CSV without saving anything. Returns the preview."""
    if len(text.encode("utf-8", "ignore")) > MAX_CSV_BYTES:
        raise LeadError("The file is too big (max 5 MB).")
    text = text.lstrip("﻿")
    if not text.strip():
        raise LeadError("The file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise LeadError("No data rows found (the first row must be the column names).")
    headers = [h.strip() for h in rows[0]]
    mapping = _map_columns(headers)
    if "phone" not in mapping:
        raise LeadError(f"No phone column found. Columns in the file: {', '.join(headers) or '(none)'}. "
                        "Name one of them phone, phone number, mobile or number.")
    if len(rows) - 1 > MAX_ROWS:
        raise LeadError(f"Too many rows ({len(rows) - 1}); the limit is {MAX_ROWS} per file.")
    used = set(mapping.values())
    extra_cols = [(i, h) for i, h in enumerate(headers) if i not in used and h]

    with get_session() as s:
        existing = {lead.phone: lead for lead in s.exec(select(Lead)).all()}

    valid, invalid, duplicates, updates, seen = [], [], [], [], {}
    for n, r in enumerate(rows[1:], start=2):  # n = line number in the file (header is line 1)
        def col(field: str) -> str:
            i = mapping.get(field)
            return _clean(r[i]) if i is not None and i < len(r) else ""

        raw_phone = col("phone")
        phone, why = normalize_us_phone(raw_phone)
        name = col("name") or " ".join(x for x in (col("first_name"), col("last_name")) if x)
        fields = {
            "name": name,
            "business": col("business"),
            "city": col("city"),
            # one free-text location; separate city / state / zip columns are joined into it
            "location": col("location") or _join_location(col("city"), col("state"), col("zip")),
            "service_required": col("service_required"),
            "business_description": col("business_description"),
            "job_description": col("job_description"),
            "email": normalize_email(col("email"))[0],
        }
        fields = {k: v[:MAX_FIELD_CHARS] for k, v in fields.items()}
        bad_email = col("email") if col("email") and not fields["email"] else ""
        if not phone:
            invalid.append({"row": n, "phone": raw_phone, "name": name, "reason": why})
            continue
        if phone in seen:
            duplicates.append({"row": n, "phone": phone, "name": name, "reason": f"same number as row {seen[phone]}"})
            continue
        if phone in existing:
            lead = existing[phone]
            why = "already a lead" + (" (Do not contact)" if lead.dnc else "") + (f": {lead.name}" if lead.name else "")
            fill = {k: v for k, v in fields.items() if v and not getattr(lead, k)}
            if fill:  # an older import without these columns: fill the empty fields, keep the rest
                why += f" (fills empty: {', '.join(k.replace('_', ' ') for k in fill)})"
                updates.append({"lead_id": lead.id, "row": n, "fields": fill})
            duplicates.append({"row": n, "phone": phone, "name": name, "reason": why})
            seen[phone] = n
            continue
        seen[phone] = n
        extra = {h: r[i].strip() for i, h in extra_cols if i < len(r) and r[i].strip()}
        notes = col("notes")
        if bad_email:
            notes = "\n".join(x for x in (notes, f"email (not valid, not used): {bad_email}") if x)
        if extra:  # extra columns are kept: in notes (readable) and in `extra` (as-is)
            lines = [notes] if notes else []
            lines += [f"{k}: {v}" for k, v in extra.items()]
            notes = "\n".join(lines)
        valid.append({"row": n, "phone": phone, "phone_display": format_phone(phone), **fields,
                      "notes": notes, "extra": extra})

    return {
        "filename": filename,
        "columns": {f: headers[i] for f, i in mapping.items()},
        "extra_columns": [h for _, h in extra_cols],
        "total_rows": len(rows) - 1,
        "counts": {"valid": len(valid), "invalid": len(invalid), "duplicates": len(duplicates), "updates": len(updates)},
        "valid": valid,
        "invalid": invalid,
        "duplicates": duplicates,
        "updates": updates,
    }


def _clean(v: str) -> str:
    """CSV cell -> single-spaced text (keeps it safe to show and to pass to the agent)."""
    return re.sub(r"\s+", " ", (v or "").replace("\x00", "")).strip()


def _join_location(city: str, state: str, zip_: str) -> str:
    tail = " ".join(x for x in (state, zip_) if x)
    return ", ".join(x for x in (city, tail) if x)


def import_csv(text: str, filename: str = "") -> dict:
    preview = parse_csv(text, filename)
    now = utcnow()
    with get_session() as s:
        for v in preview["valid"]:
            lead = Lead(phone=v["phone"], **{k: v[k] for k in TEXT_FIELDS},
                        notes=v["notes"], extra=v["extra"], source=filename or "csv", created_at=now, updated_at=now)
            s.add(lead)
            s.flush()
            s.add(LeadEvent(lead_id=lead.id, kind="imported", text=f"Imported from {filename or 'CSV'} (row {v['row']})"))
        for u in preview["updates"]:
            lead = s.get(Lead, u["lead_id"])
            for k, val in u["fields"].items():
                setattr(lead, k, val)
            lead.updated_at = now
            s.add(lead)
            _log(s, lead.id, "note", f"Filled empty fields from {filename or 'CSV'} (row {u['row']}): "
                 + ", ".join(k.replace("_", " ") for k in u["fields"]))
        s.commit()
    not_fit = apply_service_fit()
    bus.publish({"type": "leads", "action": "imported", "count": preview["counts"]["valid"]})
    return {"imported": preview["counts"]["valid"], "updated": preview["counts"]["updates"], "not_a_fit": not_fit,
            "counts": preview["counts"]}


# ---- queries -------------------------------------------------------------------------

def list_leads(q: str = "", status: str = "", limit: int = 500, offset: int = 0, sort: str = "sheet") -> dict:
    """sort: sheet (the sales sheet's order, first tab first; other leads after) / recent (last change first)."""
    with get_session() as s:
        stmt = select(Lead)
        if status:
            stmt = stmt.where(Lead.status == status)
        if q.strip():
            like = f"%{q.strip()}%"
            digits = re.sub(r"\D", "", q)
            conds = [Lead.name.ilike(like), Lead.business.ilike(like), Lead.city.ilike(like), Lead.location.ilike(like),
                     Lead.service_required.ilike(like), Lead.business_description.ilike(like),
                     Lead.job_description.ilike(like), Lead.notes.ilike(like), Lead.email.ilike(like),
                     Lead.lead_source.ilike(like), Lead.additional_info.ilike(like), Lead.sheet_tab.ilike(like)]
            if len(digits) >= 3:
                conds.append(Lead.phone.like(f"%{digits}%"))
            stmt = stmt.where(or_(*conds))
        total = s.exec(select(func.count()).select_from(stmt.subquery())).one()
        if sort == "recent":
            order = [Lead.updated_at.desc(), Lead.id.desc()]
        else:
            from app.sheet_sync import sql_sheet_order  # sheet_sync imports this module

            order = sql_sheet_order()
        rows = s.exec(stmt.order_by(*order).offset(offset).limit(min(limit, 2000))).all()
    return {"leads": [lead_to_dict(x) for x in rows], "total": total, "counts": counts()}


def counts() -> dict:
    with get_session() as s:
        by_status = Counter(dict(s.exec(select(Lead.status, func.count()).group_by(Lead.status)).all()))
        total = s.exec(select(func.count()).select_from(Lead)).one()
        called = s.exec(select(func.count()).select_from(Lead).where(Lead.attempts > 0)).one()
    return {"total": total, "called": called, "by_status": {k: by_status.get(k, 0) for k in LEAD_STATUSES}}


def get_lead(lead_id: int) -> dict:
    with get_session() as s:
        lead = s.get(Lead, lead_id)
        if lead is None:
            raise LeadError("Lead not found")
        calls = s.exec(select(Call).where(Call.lead_id == lead_id).order_by(Call.id.desc())).all()
        events = s.exec(select(LeadEvent).where(LeadEvent.lead_id == lead_id).order_by(LeadEvent.id.desc())).all()
    from app.db import call_to_dict

    from app.emails import list_emails
    from app.sms import list_texts

    return {"lead": lead_to_dict(lead), "calls": [call_to_dict(c) for c in calls],
            "events": [lead_event_to_dict(e) for e in events], "emails": list_emails(lead_id=lead_id),
            "texts": list_texts(lead_id=lead_id)}


def get_lead_row(lead_id: int) -> Lead | None:
    with get_session() as s:
        return s.get(Lead, lead_id)


def find_by_phone(phone: str) -> Lead | None:
    with get_session() as s:
        return s.exec(select(Lead).where(Lead.phone == phone)).first()


def _publish(lead: Lead) -> None:
    bus.publish({"type": "lead", "lead": lead_to_dict(lead)})


def _log(s, lead_id: int, kind: str, text_: str, **data) -> None:
    s.add(LeadEvent(lead_id=lead_id, kind=kind, text=text_, data=data))


def _status_from_calls(s, lead: Lead) -> str:
    if lead.sales_handled:
        return "handled_by_sales"
    last = s.get(Call, lead.last_call_id) if lead.last_call_id else None
    if last is None:
        return "not_a_fit" if _not_a_fit(lead) else "new"
    if last.status in ("dialing", "ringing", "answered"):
        return "calling"
    return outcomes.lead_status(last.outcome or outcomes.from_result(last.result))


# ---- service fit ---------------------------------------------------------------------

def _matches(text_: str, words: list[str]) -> list[str]:
    t = text_.lower()
    return [w for w in words if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?:s|es)?(?![a-z0-9])", t)]


def service_fit(service: str) -> tuple[str, str]:
    """("empty" | "fit" | "not_fit" | "unclear", why). Only "not_fit" skips the lead."""
    try:
        cfg = load_settings().service_fit
    except SettingsError:
        return "unclear", "settings file invalid: not checked"
    if not cfg.enabled:
        return "unclear", "service-fit check is off"
    if not (service or "").strip():
        return "empty", "no service given"
    if hit := _matches(service, keywords(cfg.offered)):
        return "fit", f"matches {hit[0]!r}"
    if hit := _matches(service, keywords(cfg.not_offered)):
        return "not_fit", f"{hit[0]!r} isn't something we offer"
    return "unclear", "not on either list"


def _not_a_fit(lead: Lead) -> bool:
    return not lead.fit_override and service_fit(lead.service_required)[0] == "not_fit"


def apply_service_fit() -> int:
    """Re-check leads that haven't been called yet (status new / not_a_fit), e.g. after an import
    or a settings change. Returns how many were newly marked not_a_fit."""
    changed = []
    with get_session() as s:
        for lead in s.exec(select(Lead).where(Lead.status.in_(("new", "not_a_fit")), Lead.dnc == False)).all():  # noqa: E712
            want = "not_a_fit" if _not_a_fit(lead) else "new"
            if lead.status != want:
                why = service_fit(lead.service_required)[1]
                _log(s, lead.id, "status", f"Not a fit: {why}" if want == "not_a_fit" else "Service fit OK again: back in the queue",
                     old_status=lead.status, new_status=want)
                lead.status, lead.updated_at = want, utcnow()
                s.add(lead)
                changed.append(lead)
        s.commit()
    for lead in changed:
        _publish(lead)
    return sum(lead.status == "not_a_fit" for lead in changed)


def call_anyway(lead_id: int) -> dict:
    """Override the service-fit check for one lead (it goes back into the dialer's queue)."""
    with get_session() as s:
        lead = s.get(Lead, lead_id)
        if lead is None:
            raise LeadError("Lead not found")
        lead.fit_override = True
        if lead.status == "not_a_fit":
            lead.status = "new"
            _log(s, lead.id, "status", "Call anyway: service-fit check overridden", old_status="not_a_fit", new_status="new")
        lead.updated_at = utcnow()
        s.add(lead)
        s.commit()
        s.refresh(lead)
    _publish(lead)
    return lead_to_dict(lead)


# ---- Do-Not-Contact ------------------------------------------------------------------

def set_dnc(lead_id: int, dnc: bool, reason: str = "") -> dict:
    with get_session() as s:
        lead = s.get(Lead, lead_id)
        if lead is None:
            raise LeadError("Lead not found")
        if lead.dnc == dnc:
            return lead_to_dict(lead)
        lead.dnc = dnc
        lead.dnc_reason = (reason or "Marked from the dashboard").strip()[:300] if dnc else ""
        lead.dnc_at = utcnow() if dnc else None
        old = lead.status
        lead.status = "do_not_contact" if dnc else _status_from_calls(s, lead)
        lead.updated_at = utcnow()
        lead.results_dirty = lead.attempts > 0  # rows exist only for leads we've called
        s.add(lead)
        _log(s, lead.id, "dnc", f"Marked Do not contact: {lead.dnc_reason}" if dnc else "Removed from Do not contact",
             old_status=old, new_status=lead.status)
        s.commit()
        s.refresh(lead)
    _publish(lead)
    return lead_to_dict(lead)


def add_dnc_number(raw_phone: str, reason: str = "") -> dict:
    """Put a number on the DNC list even if it isn't a lead yet (creates a DNC lead for it)."""
    phone, why = normalize_us_phone(raw_phone)
    if not phone:
        raise LeadError(f"Not a valid US number: {why}")
    lead = find_by_phone(phone)
    if lead is None:
        with get_session() as s:
            lead = Lead(phone=phone, source="manual (DNC)", status="new")
            s.add(lead)
            s.flush()
            _log(s, lead.id, "imported", "Added to the Do-Not-Contact list by hand")
            s.commit()
            s.refresh(lead)
    return set_dnc(lead.id, True, reason)


def dnc_block_reason(phone: str) -> str | None:
    """Why this number must not be called, or None."""
    lead = find_by_phone(phone)
    if lead and lead.dnc:
        who = f" ({lead.name})" if lead.name else ""
        return f"{format_phone(phone)}{who} is on the Do-Not-Contact list" + (f": {lead.dnc_reason}" if lead.dnc_reason else "") + ". Not calling."
    return None


def dnc_block_reason_for_lead(lead_id: int) -> str | None:
    with get_session() as s:
        lead = s.get(Lead, lead_id)
    if lead is None:
        return "Lead not found"
    return dnc_block_reason(lead.phone) or sales_block_reason(lead)


def sales_block_reason(lead: Lead | None) -> str | None:
    """The sales team coloured this lead's row green/yellow: we never call it (sheet_sync.py)."""
    if lead is not None and lead.sales_handled:
        who = f" ({lead.name})" if lead.name else ""
        return (f"{format_phone(lead.phone)}{who} is handled by the sales team (its row is coloured in their sheet). "
                "Not calling.")
    return None


def set_sales_handled(lead_id: int, handled: bool, reason: str = "") -> dict:
    """Mark / clear "Handled by sales" (the sheet sync marks it; an admin may clear it on the lead page; the next sync
    marks it again while the row is still coloured). DNC still wins over it."""
    with get_session() as s:
        lead = s.get(Lead, lead_id)
        if lead is None:
            raise LeadError("Lead not found")
        changed = mark_sales_handled(s, lead, handled, reason or ("Marked by hand" if handled else "Cleared by hand"))
        s.commit()
        s.refresh(lead)
    if changed:
        _publish(lead)
    return lead_to_dict(lead)


def mark_sales_handled(s, lead: Lead, handled: bool, reason: str) -> bool:
    """In an open session; True if it changed. Clears any callback (the sales team has the lead now)."""
    if bool(lead.sales_handled) == handled:
        return False
    old = lead.status
    lead.sales_handled = handled
    lead.sales_handled_at = utcnow() if handled else None
    if handled:
        lead.callback_at, lead.callback_note = None, ""
    if not lead.dnc:
        lead.status = "handled_by_sales" if handled else _status_from_calls(s, lead)
    lead.updated_at = utcnow()
    lead.results_dirty = lead.attempts > 0
    s.add(lead)
    _log(s, lead.id, "status", ("Handled by sales: " if handled else "No longer handled by sales: ") + reason,
         old_status=old, new_status=lead.status)
    return True


# ---- call hooks (called by the call manager) -----------------------------------------

def on_call_started(call: Call) -> None:
    if not call.lead_id:
        return
    with get_session() as s:
        lead = s.get(Lead, call.lead_id)
        if lead is None:
            return
        lead.attempts += 1
        lead.last_call_at = utcnow()
        lead.last_call_id = call.id
        if not lead.dnc:
            lead.status = "calling"
        lead.updated_at = utcnow()
        s.add(lead)
        _log(s, lead.id, "call", f"Call #{call.id} started (attempt {lead.attempts})", call_id=call.id)
        s.commit()
        s.refresh(lead)
    _publish(lead)


def on_inbound_call(call: Call) -> None:
    """The lead called us: the call counts for its status, but isn't one of our dialing attempts."""
    if not call.lead_id:
        return
    with get_session() as s:
        lead = s.get(Lead, call.lead_id)
        if lead is None:
            return
        lead.last_call_at = utcnow()
        lead.last_call_id = call.id
        if not lead.dnc:
            lead.status = "calling"
        lead.updated_at = utcnow()
        s.add(lead)
        _log(s, lead.id, "call", f"Inbound call #{call.id}: they called us", call_id=call.id, direction="inbound")
        s.commit()
        s.refresh(lead)
    _publish(lead)


INBOUND_SOURCE = "inbound call"
CALLER_FIELDS = {"name": "name", "business_name": "business", "service": "service_required", "location": "location"}


def create_inbound_lead(phone: str, fields: dict, call_id: int) -> Lead:
    """A new caller (save_caller tool): a new lead from what they said on the call."""
    now = utcnow()
    with get_session() as s:
        lead = Lead(phone=phone, source=INBOUND_SOURCE, status="new", created_at=now, updated_at=now,
                    notes=fields.get("note", ""), **{col: fields.get(k, "") for k, col in CALLER_FIELDS.items()})
        s.add(lead)
        s.flush()
        _log(s, lead.id, "imported", f"New lead from inbound call #{call_id}", call_id=call_id)
        s.commit()
        s.refresh(lead)
    _publish(lead)
    return lead


def fill_from_caller(lead_id: int, fields: dict, call_id: int) -> int:
    """What a caller said about themselves: fills empty fields (a lead made by an inbound call: any field)."""
    with get_session() as s:
        lead = s.get(Lead, lead_id)
        if lead is None:
            return lead_id
        ours = lead.source == INBOUND_SOURCE
        changed = []
        for k, col in CALLER_FIELDS.items():
            v = fields.get(k, "")
            if v and getattr(lead, col) != v and (ours or not getattr(lead, col)):
                setattr(lead, col, v)
                changed.append(col.replace("_", " "))
        note = fields.get("note", "")
        if note and note not in (lead.notes or ""):
            lead.notes = (f"{lead.notes}\n" if lead.notes else "") + f"Call #{call_id}: {note}"
            changed.append("notes")
        if changed:
            lead.updated_at = utcnow()
            s.add(lead)
            _log(s, lead.id, "note", f"From inbound call #{call_id}: " + ", ".join(changed), call_id=call_id)
            s.commit()
            s.refresh(lead)
    if changed:
        _publish(lead)
    return lead_id


def on_call_finished(call: Call) -> None:
    """Apply a finished call's outcome to its lead (status, callback, voicemail count, DNC)."""
    if not call.lead_id:
        if call.outcome == "do_not_contact":
            _dnc_unknown_number(call)
        return
    try:
        d = load_settings().dialer
        max_callbacks, gap_hours = d.max_callbacks, d.retry_gap_hours
    except SettingsError:
        max_callbacks, gap_hours = 3, 2
    now = utcnow()
    with get_session() as s:
        lead = s.get(Lead, call.lead_id)
        if lead is None:
            return
        outcome = call.outcome or outcomes.from_result(call.result)
        new = outcomes.lead_status(outcome)
        old = lead.status
        note = ""
        if outcome == "voicemail":
            lead.vm_count += 1
        if lead.last_call_id == call.id:  # only the latest call decides the status
            if outcome in outcomes.CALLBACK_OUTCOMES:
                lead.callback_count += 1
                if lead.callback_count > max_callbacks:
                    new, note = "needs_review", f"asked for a callback {lead.callback_count} times in a row"
                    lead.callback_at, lead.callback_note = None, ""
                else:
                    lead.callback_at, lead.callback_note = call.callback_at, call.outcome_note
            elif lead.callback_at is not None and outcome in outcomes.UNREACHED:
                lead.callback_count += 1  # a callback that didn't reach anyone: try again after the retry gap
                if lead.callback_count > max_callbacks:
                    lead.callback_at, lead.callback_note = None, ""
                else:
                    new, note = "callback", "callback missed, trying again later"
                    lead.callback_at = now + timedelta(hours=gap_hours)
            else:
                lead.callback_at, lead.callback_note, lead.callback_count = None, "", 0
            if not lead.dnc and not lead.sales_handled:
                lead.status = new
        if outcome == "do_not_contact" and not lead.dnc:
            _mark_dnc(s, lead, f"Call #{call.id}: " + (call.outcome_note or "asked not to be contacted"))
        if lead.dnc or lead.sales_handled:  # never a callback for Do not contact / a lead the sales team has
            lead.callback_at, lead.callback_note = None, ""
        lead.updated_at = now
        lead.results_dirty = True  # its row in the results sheet (results_sheet.py)
        s.add(lead)
        what = outcomes.label(outcome) + (f" ({call.outcome_note})" if call.outcome_note else "") + (f"; {note}" if note else "")
        if outcome == "voicemail":
            what += ", message left" if call.vm_message_left else ", no message"
        _log(s, lead.id, "status", f"Call #{call.id}: {what}", call_id=call.id, result=call.result, outcome=outcome,
             source=call.outcome_source, old_status=old, new_status=lead.status,
             callback_at=lead.callback_at.isoformat() if lead.callback_at else None)
        s.commit()
        s.refresh(lead)
    _publish(lead)


MANUAL_OUTCOMES = (*outcomes.AI_OUTCOMES, "transferred", "needs_review")


def set_outcome(lead_id: int, outcome: str, note: str = "", callback_time: str = "") -> dict:
    """Set a lead's outcome by hand (e.g. after reviewing a call). Also stored on its latest call.
    callback_time: the lead's local time, YYYY-MM-DDTHH:MM (not_now / gatekeeper); empty -> default days."""
    if outcome not in MANUAL_OUTCOMES:
        raise LeadError(f"Outcome must be one of: {', '.join(MANUAL_OUTCOMES)}")
    note = re.sub(r"\s+", " ", note or "").strip()[:300]
    now = utcnow()
    with get_session() as s:
        lead = s.get(Lead, lead_id)
        if lead is None:
            raise LeadError("Lead not found")
        if lead.dnc and outcome != "do_not_contact":
            raise LeadError("This lead is on Do not contact. Remove it from the list first.")
        if lead.sales_handled:
            raise LeadError("The sales team is handling this lead (green/yellow in their sheet). Clear "
                            "\"Handled by sales\" first.")
        callback_at = None
        if outcome in outcomes.CALLBACK_OUTCOMES:
            tz_name, _ = lead_timezone(lead.location, lead.phone)
            if callback_time.strip():
                callback_at = outcomes.parse_callback(callback_time, tz_name, now)
                if callback_at is None:
                    raise LeadError("The callback time must be in the future (within 90 days).")
            else:
                try:
                    days = load_settings().dialer.callback_default_days
                except SettingsError:
                    days = 3
                callback_at = outcomes.default_callback(now, days)
        call = s.get(Call, lead.last_call_id) if lead.last_call_id else None
        if call is not None and call.status in ("ended", "failed"):
            call.outcome, call.outcome_source, call.outcome_note, call.callback_at = outcome, "manual", note, callback_at
            s.add(call)
        old = lead.status
        lead.status = outcomes.lead_status(outcome)
        lead.callback_at, lead.callback_note = callback_at, (note if callback_at else "")
        lead.callback_count = 1 if callback_at else 0
        lead.results_dirty = True
        if outcome == "do_not_contact" and not lead.dnc:
            _mark_dnc(s, lead, "Set by hand" + (f": {note}" if note else ""))
        lead.updated_at = now
        s.add(lead)
        _log(s, lead.id, "status", f"Outcome set by hand: {outcomes.label(outcome)}" + (f" ({note})" if note else ""),
             outcome=outcome, old_status=old, new_status=lead.status,
             callback_at=callback_at.isoformat() if callback_at else None)
        s.commit()
        s.refresh(lead)
    _publish(lead)
    if call is not None:
        from app.db import call_to_dict

        bus.publish({"type": "call", "call": call_to_dict(call)})
    return lead_to_dict(lead)


def _mark_dnc(s, lead: Lead, reason: str) -> None:
    old = lead.status
    lead.dnc, lead.dnc_reason, lead.dnc_at = True, reason.strip()[:300], utcnow()
    lead.status = "do_not_contact"
    lead.callback_at, lead.callback_note = None, ""
    _log(s, lead.id, "dnc", f"Marked Do not contact: {lead.dnc_reason}", old_status=old, new_status=lead.status)


def _dnc_unknown_number(call: Call) -> None:
    """A manual call to a number that isn't a lead ended with Do-Not-Contact: list the number.
    Never the test phone: that would block every test call."""
    if call.number == os.getenv("TEST_CALL_NUMBER", "") or not call.number:
        return
    try:
        add_dnc_number(call.number, f"Call #{call.id}: " + (call.outcome_note or "asked not to be contacted"))
    except LeadError:
        pass
