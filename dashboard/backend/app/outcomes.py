"""Call outcomes: what happened on a call in business terms, and what that means for the lead.

Sources, strongest first when they disagree:
  1. Do-Not-Contact, from any source (the AI, the transcript safety net, or by hand)
  2. the transfer to the supervisor (outcome_source "transfer"): transferred, or an interested lead the
     supervisor couldn't take (transfer_declined) or a transfer that didn't go through (transfer_failed)
  3. the AI's record_outcome tool during the call (outcome_source "ai")
  4. transcript rules after the call (outcome_source "transcript")
  5. how the call ended: voicemail / no answer / bad line / failed (outcome_source "call");
     answered with nothing else known -> needs_review
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# outcome -> (label, lead status). Labels are what the dashboard and the results sheet show (the sales team's
# tags, 2026-11-05); the internal values never change.
OUTCOMES = {
    "interested": ("Interested – callback", "interested"),
    "not_now": ("Callback", "callback"),
    "gatekeeper": ("Callback (gatekeeper)", "callback"),
    "not_interested": ("Rejected", "not_interested"),
    "do_not_contact": ("Do not contact", "do_not_contact"),
    "wrong_number": ("Wrong number", "bad_number"),
    "voicemail": ("Voicemail", "voicemail"),
    "bad_line": ("No answer (bad line)", "bad_line"),
    "no_answer": ("No answer", "no_answer"),
    "failed": ("Call failed", "failed"),
    "needs_review": ("Needs review", "needs_review"),
    "transferred": ("Connected – transferred", "transferred"),
    "transfer_declined": ("Interested – callback", "callback"),
    "transfer_failed": ("Interested – callback", "callback"),
}
# Lead status -> label (Leads page, results sheet). "handled_by_sales": the row is green/yellow in the sales sheet.
STATUS_LABELS = {
    "new": "New", "calling": "Calling", "interested": "Interested – callback", "callback": "Callback",
    "not_interested": "Rejected", "needs_review": "Needs review", "voicemail": "Voicemail", "no_answer": "No answer",
    "bad_line": "No answer (bad line)", "bad_number": "Wrong number", "do_not_contact": "Do not contact",
    "not_a_fit": "Not a fit", "failed": "Call failed", "answered": "Answered", "transferred": "Connected – transferred",
    "texted": "Texted", "handled_by_sales": "Handled by sales",
}
# What the AI may report during the call (record_outcome)
AI_OUTCOMES = ("interested", "not_now", "not_interested", "do_not_contact", "gatekeeper", "wrong_number")
CALLBACK_OUTCOMES = ("not_now", "gatekeeper", "transfer_declined", "transfer_failed")
# The AI may give a callback time with these (interested: when the supervisor should call them back)
AI_CALLBACK_OUTCOMES = ("not_now", "gatekeeper", "interested")
TRANSFER_OUTCOMES = ("transferred", "transfer_declined", "transfer_failed")
UNREACHED = ("no_answer", "voicemail", "bad_line", "failed")  # nobody to talk to
RESULT_OUTCOME = {"answered": "needs_review", "voicemail": "voicemail", "no_answer": "no_answer", "busy": "no_answer",
                  "bad_line": "bad_line", "failed": "failed", "trunk_rejected": "failed"}

CALLBACK_MIN_AHEAD = timedelta(minutes=10)
CALLBACK_MAX_AHEAD = timedelta(days=90)
DEFAULT_CALLBACK_HOUR = 10  # a date without a time: 10:00 in the lead's time zone


def label(outcome: str | None) -> str:
    return OUTCOMES.get(outcome or "", (outcome or "", ""))[0]


def lead_status(outcome: str) -> str:
    return OUTCOMES.get(outcome, ("", "needs_review"))[1]


def from_result(result: str | None) -> str:
    return RESULT_OUTCOME.get(result or "", "failed")


# Voicemail greetings / carrier recordings (callee side). "Strong" ones close the line at once.
VOICEMAIL_STRONG = (
    r"leave (?:me |us )?(?:a |your )?(?:message|name and number|voicemail)", r"after the (?:tone|beep)",
    r"record your message", r"at the (?:tone|beep)", r"mailbox is full", r"mailbox (?:has not|hasn't) been set up",
    r"voice ?mail(?:box)? (?:is full|has not been set up|hasn't been set up|is not set up)",
    r"(?:is not|isn't) available\W+(?:please )?(?:leave|record|at the tone)",
    r"the (?:person|number|subscriber) you (?:are|have) (?:trying to reach|calling|called|dialed)",
    r"has a voice ?mail box that",
)
VOICEMAIL_FULL = (r"mailbox is full", r"(?:has not|hasn't|is not) (?:been )?set up", r"cannot accept (?:any )?(?:new )?messages")
_VM_RE = re.compile("|".join(f"(?:{p})" for p in VOICEMAIL_STRONG), re.I)
_VM_FULL_RE = re.compile("|".join(f"(?:{p})" for p in VOICEMAIL_FULL), re.I)


def voicemail_phrase(text: str) -> str | None:
    """'voicemail' or 'mailbox full / not set up' if this callee line sounds like a recording."""
    if not text or not _VM_RE.search(text):
        return None
    return "mailbox full / not set up" if _VM_FULL_RE.search(text) else "voicemail"


VOICEMAIL_WITHIN_S = 30  # a greeting starts at the beginning of the call; later it's a person talking


def _callee_lines(transcript: list[dict], within_s: float | None = None) -> list[str]:
    out = []
    for t in transcript:
        text, start = (t.get("text") or "").strip(), t.get("start_timestamp")
        if t.get("role") != "user" or not text:
            continue
        if within_s is not None and not (isinstance(start, (int, float)) and start <= within_s):
            continue
        out.append(text)
    return out


# Callee asks not to be called, or is abusive towards us (directed, not just swearing about something).
DNC_PHRASES = (
    r"\bstop (?:calling|ringing|bothering)\b", r"\bquit (?:calling|bothering)\b",
    r"\b(?:don'?t|do not|never) (?:ever )?(?:call|contact|ring) (?:me|us|here|this number)\b(?! (?:before|until|after|between|in the|on |at |during))",
    r"\b(?:take|remove|delete|get) (?:me|us|my number|this number|my name)\b.{0,20}\b(?:off|from)\b.{0,25}\b(?:list|database|system)\b",
    r"\b(?:put|add) (?:me|us|my number|this number) on (?:the |your )?(?:do not call|dnc|no call)",
    r"\blose (?:my|this|our) number\b", r"\bthis is harass", r"\bi'?ll (?:report|sue) you\b",
    r"\bf+u+c*k+ (?:off|you|yourself|outta)\b", r"\bfuck(?:ing)? (?:robot|scammer|spammer)\b", r"\bscrew you\b",
    r"\bgo to hell\b", r"\bpiss off\b", r"\bget lost\b", r"\bshove it\b",
)
WRONG_NUMBER_PHRASES = (
    r"\bwrong number\b", r"\b(?:you'?ve|you have|you got) (?:got )?the wrong (?:number|person|business|place)\b",
    r"\b(?:no ?one|nobody) (?:here )?(?:by|with|named|called) that name\b", r"\b(?:there'?s|there is) no(?:body)? \w+ here\b",
    r"\b(?:doesn'?t|does not|don'?t|do not) (?:work|live) here\b", r"\bthis (?:isn'?t|is not) (?:a |the )?business\b",
    r"\b(?:never|haven'?t) heard of (?:that|them|it|this) (?:business|company)\b",
    r"\b(?:went|gone) out of business\b", r"\bnot in business any ?more\b",
)
_DNC_RE = re.compile("|".join(f"(?:{p})" for p in DNC_PHRASES), re.I)
_WRONG_RE = re.compile("|".join(f"(?:{p})" for p in WRONG_NUMBER_PHRASES), re.I)


def _norm(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'")


def transcript_rules(transcript: list[dict], result: str | None) -> tuple[str, str] | None:
    """(outcome, why) from the words on the call, or None."""
    if result in ("answered", "bad_line"):  # someone talked
        lines = [_norm(x) for x in _callee_lines(transcript)]
        for line in lines:
            if m := _DNC_RE.search(line):
                return "do_not_contact", f"they said \"{m.group(0)}\""
        for line in lines:
            if m := _WRONG_RE.search(line):
                return "wrong_number", f"they said \"{m.group(0)}\""
    if result == "answered":  # an answered call that was really a recording
        for line in _callee_lines(transcript, VOICEMAIL_WITHIN_S)[:3]:
            if kind := voicemail_phrase(line):
                return "voicemail", f"{kind} (heard: \"{line[:80]}\")"
    return None


def transfer_outcome(transfer_status: str, ai_outcome: str | None) -> str | None:
    """transferred / transfer_declined / transfer_failed, or None when the transfer doesn't decide it.
    A request to the supervisor is only made for an interested lead, so a missing AI outcome counts as interested;
    "unavailable" (supervisor off when we dialed) only counts once the AI says they're interested."""
    if ai_outcome in ("do_not_contact", "wrong_number", "not_interested"):
        return None
    if transfer_status in ("transferring", "transferred"):
        return "transferred"
    if transfer_status in ("declined", "timeout") and ai_outcome in (None, "interested", "not_now", "gatekeeper"):
        return "transfer_declined"
    if transfer_status == "unavailable" and ai_outcome == "interested":
        return "transfer_declined"
    # accepted but no transfer seen: the AI never used the transfer tool
    if transfer_status in ("failed", "accepted") and ai_outcome in (None, "interested", "not_now", "gatekeeper"):
        return "transfer_failed"
    return None


def decide(ai_outcome: str | None, ai_source: str, result: str | None, transcript: list[dict],
           transfer_status: str = "") -> tuple[str, str, str]:
    """The call's final (outcome, source, note-if-any)."""
    rule = transcript_rules(transcript or [], result)
    if rule and rule[0] == "do_not_contact":
        return rule[0], "transcript", rule[1]
    ai = ai_outcome if ai_source == "ai" and ai_outcome in AI_OUTCOMES else None
    if transfer_status and (t := transfer_outcome(transfer_status, ai)):
        return t, "transfer", ""
    if ai_source == "ai" and ai_outcome in AI_OUTCOMES:
        return ai_outcome, "ai", ""
    if rule:
        return rule[0], "transcript", rule[1]
    return from_result(result), "call", ""


def parse_callback(text: str, tz_name: str | None, now_utc: datetime) -> datetime | None:
    """The AI's callback time, "YYYY-MM-DDTHH:MM" (or a date) in the lead's local time -> UTC.
    None if missing, unreadable, in the past, or too far ahead."""
    s = (text or "").strip()
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}):(\d{2})(?::\d{2})?)?", s)
    if not m:
        return None
    try:
        day = datetime.strptime(m[1], "%Y-%m-%d")
        hour, minute = (int(m[2]), int(m[3])) if m[2] else (DEFAULT_CALLBACK_HOUR, 0)
        local = day.replace(hour=hour, minute=minute, tzinfo=ZoneInfo(tz_name or "America/New_York"))
    except ValueError:
        return None
    at = local.astimezone(timezone.utc)
    if not (now_utc + CALLBACK_MIN_AHEAD <= at <= now_utc + CALLBACK_MAX_AHEAD):
        return None
    return at


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY_RE = re.compile(r"\b(day after tomorrow|tomorrow|next week|next month|" + "|".join(_WEEKDAYS) + r")s?\b", re.I)
_PART_HOURS = {"morning": 10, "afternoon": 14, "evening": 17}
_PART_RE = re.compile(r"\b(morning|afternoon|evening)\b", re.I)
_CLAUSE_BREAK_RE = re.compile(r"[,.;:!?]|\b(?:but|so|and|then)\b", re.I)
_NOT_THAT_DAY_RE = re.compile(r"n't\b|\b(?:not|never|busy|away|except|unavailable|closed|last|ago|was|were|had|did)\b"
                              r"|\b(?:i'm|am|be|we're) (?:out|off)\b", re.I)


def _clause(text: str, m: re.Match) -> tuple[str, str]:
    """The words before and after a match, within its part of the sentence."""
    breaks = list(_CLAUSE_BREAK_RE.finditer(text, 0, m.start()))
    after = _CLAUSE_BREAK_RE.search(text, m.end())
    return text[breaks[-1].end() if breaks else 0:m.start()], text[m.end():after.start() if after else len(text)]


def _names_the_day(text: str, m: re.Match) -> bool:
    """False for "busy tomorrow", "not on Monday", "last Friday"; "don't call until Monday" does name it."""
    before, after = _clause(text, m)
    if re.search(r"\b(?:until|till)\s+$", before, re.I):
        return True
    return not _NOT_THAT_DAY_RE.search(" ".join(before.split()[-4:] + after.split()[:2]))


def callback_from_words(text: str, tz_name: str | None, now_utc: datetime, hours=None) -> datetime | None:
    """A day in plain words ("tomorrow", "Monday", "next week") -> that day at a sensible hour, in UTC: 10:00 their
    time (or their morning / afternoon / evening), inside the calling hours (hours.start, .end, .days), on the first
    calling day from then on. The last day named wins. None if no day is named, or it's too far ahead."""
    s = _norm(text or "")
    named = [m for m in _DAY_RE.finditer(s) if _names_the_day(s, m)]
    if not named:
        return None
    m = named[-1]
    word = m[1].lower()
    tz = ZoneInfo(tz_name or "America/New_York")
    today = now_utc.astimezone(tz).date()
    if word == "tomorrow":
        day = today + timedelta(days=1)
    elif word == "day after tomorrow":
        day = today + timedelta(days=2)
    elif word == "next week":
        day = today + timedelta(days=7 - today.weekday())
    elif word == "next month":
        day = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    else:
        day = today + timedelta(days=(_WEEKDAYS.index(word) - today.weekday() - 1) % 7 + 1)
    start, end, days = (hours.start, hours.end, hours.days) if hours else ("09:00", "18:00", ("mon", "tue", "wed", "thu", "fri"))
    while _WEEKDAYS[day.weekday()][:3] not in days:  # the settings require at least one calling day
        day += timedelta(days=1)
    before, after = _clause(s, m)
    part = _PART_RE.search(f"{before} {after}")
    minutes = _PART_HOURS[part[1].lower()] * 60 if part else DEFAULT_CALLBACK_HOUR * 60
    first, last = _minutes(start), _minutes(end) - 60  # the last full hour of the calling day
    minutes = max(first, min(minutes, last))
    at = datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=tz).astimezone(timezone.utc)
    return at if now_utc + CALLBACK_MIN_AHEAD <= at <= now_utc + CALLBACK_MAX_AHEAD else None


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def default_callback(now_utc: datetime, days: int) -> datetime:
    return now_utc + timedelta(days=days)
