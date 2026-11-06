"""Load and validate config/agent_settings.yaml.

Strict on purpose (the dashboard will edit this file later): unknown keys, wrong
types, empty text and unknown {placeholders} are all rejected with a clear message.
"""

import os
import re
import string
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# AGENT_SETTINGS_PATH moves the file (tests use a temp copy; a server keeps it on its data disk)
SETTINGS_PATH = Path(os.getenv("AGENT_SETTINGS_PATH") or Path(__file__).resolve().parent.parent / "config" / "agent_settings.yaml")
# Lead details, different on every call: sent to Cartesia as dynamic variables ({{lead_name}} ...).
# specialist_title is worked out from service_required (specialist_titles below): who the lead would talk to.
LEAD_PLACEHOLDERS = ("lead_name", "business_name", "service_required", "business_description", "job_description", "location",
                     "specialist_title", "lead_source")
# Our own details, the same on every call: {callback_number} is company_phone.
COMPANY_PLACEHOLDERS = ("caller_name", "company_name", "callback_number", "company_website", "company_address")
# Only in the email and text (SMS) templates (filled by the backend when it sends one).
EMAIL_PLACEHOLDERS = ("callback_time",)
FOLLOW_UP_SECTIONS = ("emails.", "sms.")
# A text is cut into 160-character pieces (each billed); keep templates to about two.
SMS_MAX_CHARS = 300
# Only in the opening line: the sentence about where the lead came from (lead_sources below), filled per call.
OPENING_PLACEHOLDERS = ("source_line",)
PLACEHOLDERS = {*COMPANY_PLACEHOLDERS, *LEAD_PLACEHOLDERS, *EMAIL_PLACEHOLDERS, *OPENING_PLACEHOLDERS}
# The introduction: our company name only when they ask (2026-11-05). The old default is read as the new one.
OLD_OPENING_LINE = "Hey, this is {caller_name} from {company_name}. Did I catch you at an okay time?"
DEFAULT_OPENING_LINE = "Hi, this is {caller_name}. {source_line} Is now an okay time?"
_OPTIONAL = re.compile(r"\[([^\[\]]*)\]")


def check_brackets(v: str) -> str:
    """[optional parts] must be closed and not nested."""
    depth = 0
    for ch in v:
        depth += {"[": 1, "]": -1}.get(ch, 0)
        if depth < 0 or depth > 1:
            raise ValueError("a [ ] part is not closed properly, or is inside another one")
    if depth:
        raise ValueError("a [ is never closed with ]")
    return v


def drop_empty_parts(text: str, values: dict[str, str]) -> str:
    """Leave out every [part] that uses a placeholder whose value is empty, and keep the others without the brackets.
    "I saw your request on Bark[ for {service_required}]" -> "...on Bark" when service_required is empty."""
    def one(m: re.Match) -> str:
        part = m.group(1)
        names = [n for _, n, _, _ in string.Formatter().parse(part) if n]
        return "" if any(not (values.get(n) or "").strip() for n in names) else part
    return _OPTIONAL.sub(one, text)
# The voicemail message must fit in about 20 seconds (~2.5 spoken words a second).
VOICEMAIL_MAX_WORDS = 50


class SettingsError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Email(_Strict):
    subject: str = Field(min_length=1, max_length=150)
    body: str = Field(min_length=1, max_length=3000)

    @field_validator("subject")
    @classmethod
    def _one_line(cls, v: str) -> str:
        if "\n" in v:
            raise ValueError("must be one line")
        return v


class Emails(_Strict):
    """Follow-up emails after calls (sent by the backend over SMTP; .env has the account)."""

    enabled: bool = True
    daily_limit: int = Field(default=50, ge=1, le=2000)
    after_voicemail: bool = False
    interested: Email
    callback: Email
    details: Email
    voicemail_followup: Email


class Sms(_Strict):
    """Follow-up texts after calls (sent by the backend through Telnyx; .env has the number)."""

    enabled: bool = True
    daily_limit: int = Field(default=50, ge=1, le=2000)
    not_answered: str = Field(min_length=1, max_length=SMS_MAX_CHARS)
    not_interested: str = Field(min_length=1, max_length=SMS_MAX_CHARS)
    not_now: str = Field(min_length=1, max_length=SMS_MAX_CHARS)
    interested: str = Field(min_length=1, max_length=SMS_MAX_CHARS)


class SpecialistTitles(_Strict):
    """What the lead hears / reads for the person we hand them to ({specialist_title}), by their service.
    Never "supervisor" or "manager"."""

    # One title per line: "senior web designer: website, web design, ...". The first line with a matching
    # keyword wins. Said as "our <title>".
    by_service: str = Field(min_length=1)
    # Empty or unclear service: said exactly like this.
    default: str = Field(default="one of our senior specialists", min_length=1, max_length=80)

    @field_validator("by_service")
    @classmethod
    def _lines(cls, v: str) -> str:
        lines = []
        for n, line in enumerate((v or "").splitlines(), 1):
            if not line.strip():
                continue
            title, sep, words = line.partition(":")
            title = re.sub(r"\s+", " ", title).strip()
            if not sep or not title or not keywords(words):
                raise ValueError(f"line {n}: write it as 'senior web designer: website, web design' (title, colon, keywords)")
            if re.search(r"\b(supervisor|manager)\b", title, re.I):
                raise ValueError(f"line {n}: the lead must never hear 'supervisor' or 'manager'")
            lines.append(f"{title}: {', '.join(keywords(words))}")
        if not lines:
            raise ValueError("needs at least one line like 'senior web designer: website, web design'")
        return "\n".join(lines)

    @field_validator("default")
    @classmethod
    def _no_manager(cls, v: str) -> str:
        if re.search(r"\b(supervisor|manager)\b", v, re.I):
            raise ValueError("the lead must never hear 'supervisor' or 'manager'")
        return v

    def title_for(self, service: str) -> str:
        """'a new Shopify store' -> 'our senior web designer'; empty / unclear -> the default."""
        t = (service or "").lower()
        for line in self.by_service.splitlines():
            title, _, words = line.partition(":")
            if any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?:s|es)?(?![a-z0-9])", t) for w in keywords(words)):
                return f"our {title.strip()}"
        return self.default


class SeniorUnavailable(_Strict):
    message: str = Field(min_length=1)
    scheduling_instructions: str = Field(min_length=1)


class Objection(_Strict):
    objection: str = Field(min_length=1)
    response: str = Field(min_length=1)


class Goodbye(_Strict):
    rules: str = Field(min_length=1)
    line: str = Field(min_length=1)


class CallingHours(_Strict):
    start: str
    end: str
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(min_length=1)

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("must be HH:MM (24h), e.g. 09:00")
        return v

    @model_validator(mode="after")
    def _order(self) -> "CallingHours":
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class CallSettings(_Strict):
    calling_hours: CallingHours
    max_call_minutes: int = Field(ge=1, le=60)
    recordings_keep_days: int = Field(default=30, ge=0, le=3650)  # 0 = keep forever


def spoken_words(text: str) -> int:
    """Words in a text as spoken; a {placeholder} counts as two (a name, a service)."""
    return len(re.sub(r"\{\w+\}", "x x", text or "").split())


class Voicemail(_Strict):
    """When a call reaches voicemail: hang up at once (default, saves minutes) or leave a short message."""

    leave_message: bool = False
    message: str = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def _short(cls, v: str) -> str:
        words = spoken_words(v)
        if words > VOICEMAIL_MAX_WORDS:
            raise ValueError(f"too long: about {words} words (~{round(words / 2.5)} seconds). Keep it under "
                             f"{VOICEMAIL_MAX_WORDS} words, about 20 seconds")
        return v


class Ambience(_Strict):
    """Background sound mixed under the agent's voice by Cartesia (Voice page). The list of sounds: app/ambience.py."""

    enabled: bool = False
    sound: str = Field(default="office", pattern=r"^[a-z0-9_]{1,40}$")
    volume_percent: int = Field(default=30, ge=5, le=100)

    @field_validator("sound", mode="before")
    @classmethod
    def _old_name(cls, v):
        return "busy_office" if v == "call_center" else v  # a removed sound, in an older settings file


class Voice(_Strict):
    """The Cartesia voice the agent speaks with (chosen on the dashboard's Voice page)."""

    provider: Literal["cartesia"] = "cartesia"
    voice_id: str
    speed: float = Field(default=1.0, ge=0.6, le=1.5)
    volume: float = Field(default=1.0, ge=0.5, le=2.0)
    ambience: Ambience = Field(default_factory=Ambience)

    @field_validator("voice_id")
    @classmethod
    def _uuid(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", v):
            raise ValueError("must be a Cartesia voice ID (like c961b81c-a935-4c17-bfb3-ba2239de8c2f)")
        return v


OLD_SILENT_GREETING = "Hi, is this {lead_name}? This is {caller_name} from {company_name}."  # until 2026-11-04
SILENT_CHECK_MAX_WORDS = 8


class CallStart(_Strict):
    """Who speaks first: the AI gives the callee a few seconds to say hello. If they stay silent it only checks that
    they can hear ("Hello?"); its introduction comes once, after they've spoken (History #38)."""

    greet_if_silent: bool = True
    wait_seconds: int = Field(default=4, ge=2, le=8)
    silent_greeting: str = Field(default="Hello?", min_length=1, max_length=80)

    @field_validator("silent_greeting", mode="before")
    @classmethod
    def _old_default(cls, v):
        # The old default was a whole introduction: said to a silent callee, then again after their hello.
        return "Hello?" if isinstance(v, str) and v.strip() == OLD_SILENT_GREETING else v

    @field_validator("silent_greeting")
    @classmethod
    def _short_check(cls, v: str) -> str:
        if "\n" in v:
            raise ValueError("must be one line")
        if "{caller_name}" in v or "{company_name}" in v:
            raise ValueError("just a short check like \"Hello?\": the AI introduces itself once, after they've spoken")
        if spoken_words(v) > SILENT_CHECK_MAX_WORDS:
            raise ValueError(f"keep it to a short check like \"Hello, can you hear me?\" (at most {SILENT_CHECK_MAX_WORDS} words)")
        return v


class Inbound(_Strict):
    """Calls TO our number (a lead calling back, a new caller). Off: our number isn't connected to the AI.
    The AI answers with `greeting`, then looks the caller up in our leads (lookup_caller tool) before it says more."""

    enabled: bool = False
    greeting: str = Field(default="Thanks for calling {company_name}, this is {caller_name}. How can I help you?",
                          min_length=1, max_length=300)
    new_caller: str = Field(default="Ask for their name, then their business name, then what they need help with, one "
                                    "question at a time. Then carry on like our normal call.", min_length=1)

    @field_validator("greeting")
    @classmethod
    def _no_lead_details(cls, v: str) -> str:
        if "\n" in v:
            raise ValueError("must be one line")
        if any("{" + k + "}" in v for k in LEAD_PLACEHOLDERS):
            raise ValueError("can't use lead details like {lead_name}: the AI doesn't know who's calling until after "
                             "the greeting. It greets a known lead by name right after.")
        return v


class Silence(_Strict):
    """What happens when the callee goes quiet (they may have hung up: Cartesia doesn't notice)."""

    enabled: bool = True
    silence_check_seconds: int = Field(default=8, ge=5, le=60)
    max_silence_seconds: int = Field(default=25, ge=20, le=240)
    check_message: str = Field(min_length=1)
    close_message: str = Field(min_length=1)

    @model_validator(mode="after")
    def _room_for_two_check_ins(self) -> "Silence":
        need = 2 * self.silence_check_seconds + 4
        if self.max_silence_seconds < need:
            raise ValueError(f"max_silence_seconds must be at least {need} (two check-ins of "
                             f"{self.silence_check_seconds}s plus time to say the closing line)")
        return self


class LeadSources(_Strict):
    """How the introduction says where the lead came from ({source_line} in the opening line). The source is the
    sales sheet's "Source" column ({lead_source}). Parts in [square brackets] are left out when a detail in them is
    empty."""

    # Where we found the business ourselves (one per line or comma-separated); any other source is a request they made
    found_sources: str = "LinkedIn, Facebook, Instagram"
    request_line: str = Field(default="I saw your request on {lead_source}[ for {service_required}][ for your {business_name}].",
                              min_length=1, max_length=300)
    found_line: str = Field(default="I came across your business on {lead_source}.", min_length=1, max_length=300)
    no_source_line: str = Field(default="[I saw you were looking for help with {service_required}.]", max_length=300)

    @field_validator("request_line", "found_line", "no_source_line")
    @classmethod
    def _line(cls, v: str) -> str:
        if "\n" in v:
            raise ValueError("must be one line")
        if "{source_line}" in v:
            raise ValueError("{source_line} only goes in the opening line")
        return check_brackets(v)

    def kind(self, source: str) -> str:
        """"found" / "request" / "none" for a lead source like "Bark" or "LinkedIn Sales Navigator"."""
        src = (source or "").strip().lower()
        if not src:
            return "none"
        return "found" if any(k in src for k in keywords(self.found_sources)) else "request"


class Dialer(_Strict):
    """Auto-dialer rules (Leads page ON/OFF switch)."""

    max_calls_per_session: int = Field(default=3, ge=1, le=1000)
    retry_statuses: list[Literal["no_answer", "bad_line", "failed"]] = Field(default_factory=lambda: ["no_answer", "bad_line"])
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_gap_hours: int = Field(default=2, ge=1, le=168)
    pause_after_failures: int = Field(default=3, ge=1, le=20)
    min_free_minutes: int = Field(default=2, ge=0, le=60)
    voicemail_retries: int = Field(default=2, ge=0, le=5)
    callback_default_days: int = Field(default=3, ge=1, le=30)
    max_callbacks: int = Field(default=3, ge=1, le=10)


class ServiceFit(_Strict):
    """Skip leads whose "service required" is clearly something we don't sell."""

    enabled: bool = True
    offered: str = Field(min_length=1)  # one service per line: its name first, then other words for it
    not_offered: str = Field(min_length=1)

    @field_validator("offered", "not_offered")
    @classmethod
    def _keywords(cls, v: str) -> str:
        """Tidy each line (lowercase, single spaces, ", " between words), drop empty lines and repeats."""
        seen: set[str] = set()
        lines = []
        for line in (v or "").splitlines():
            words = [w for w in keywords(line) if w not in seen]
            seen.update(words)
            if words:
                lines.append(", ".join(words))
        if not lines:
            raise ValueError("needs at least one keyword (comma-separated)")
        return "\n".join(lines)


def service_names(text: str) -> list[str]:
    """The first keyword of each line: what we offer, in plain words (for the AI's instructions)."""
    return [k[0] for k in (keywords(line) for line in (text or "").splitlines()) if k]


def keywords(text: str) -> list[str]:
    """'Website, web design,  SEO' -> ['website', 'web design', 'seo'] (no duplicates)."""
    out: list[str] = []
    for part in re.split(r"[,\n;]+", text or ""):
        w = re.sub(r"\s+", " ", part).strip().lower()
        if w and w not in out:
            out.append(w)
    return out


class AgentSettings(_Strict):
    company_name: str = Field(min_length=1)
    caller_name: str = Field(min_length=1)
    company_phone: str = Field(min_length=1)  # {callback_number}
    company_website: str = ""
    company_address: str = ""  # required by law in every email: emails aren't sent while it's empty
    opening_line: str = Field(min_length=1)
    lead_sources: LeadSources = Field(default_factory=LeadSources)
    call_start: CallStart = Field(default_factory=CallStart)
    inbound: Inbound = Field(default_factory=Inbound)
    speaking_style: str = Field(min_length=1)
    main_script: str = Field(min_length=1)
    ai_receptionist_script: str = Field(min_length=1)
    voicemail: Voicemail
    emails: Emails
    sms: Sms
    specialist_titles: SpecialistTitles
    transfer_intro: str = Field(min_length=1)
    senior_unavailable: SeniorUnavailable
    objection_handling: list[Objection]
    goodbye: Goodbye
    voice: Voice
    silence: Silence
    dialer: Dialer
    service_fit: ServiceFit
    settings: CallSettings

    @field_validator("opening_line", mode="before")
    @classmethod
    def _old_opening(cls, v):
        return DEFAULT_OPENING_LINE if isinstance(v, str) and v.strip() == OLD_OPENING_LINE else v

    @field_validator("opening_line")
    @classmethod
    def _opening(cls, v: str) -> str:
        if "\n" in v:
            raise ValueError("must be one line")
        return check_brackets(v)

    @model_validator(mode="after")
    def _known_placeholders(self) -> "AgentSettings":
        def check(value, path: str) -> None:
            if isinstance(value, str):
                try:
                    parsed = list(string.Formatter().parse(value))
                except ValueError:
                    raise ValueError(f"{path}: a {{ or }} doesn't belong to a placeholder like {{lead_name}}") from None
                for _, name, _, _ in parsed:
                    if name is not None and name not in PLACEHOLDERS:
                        raise ValueError(
                            f"{path}: unknown placeholder {{{name}}} (allowed: "
                            + ", ".join("{" + p + "}" for p in sorted(PLACEHOLDERS)) + ")"
                        )
                    if name in EMAIL_PLACEHOLDERS and not path.startswith(FOLLOW_UP_SECTIONS):
                        raise ValueError(f"{path}: unknown placeholder {{{name}}} (it can only be used in the "
                                         "email and text templates)")
                    if name in OPENING_PLACEHOLDERS and path != "opening_line":
                        raise ValueError(f"{path}: {{{name}}} can only be used in the opening line")
            elif isinstance(value, BaseModel):
                for k in type(value).model_fields:
                    check(getattr(value, k), f"{path}.{k}" if path else k)
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    check(v, f"{path}[{i}]")

        check(self, "")
        return self

    def fill(self, text: str, lead: dict | None = None, extra: dict | None = None) -> str:
        """Replace our own placeholders ({caller_name}, {callback_number}...) in a text. Lead placeholders become
        Cartesia's {{lead_name}} variables, or the given lead's values (a voice sample, an email).
        extra: values for the email/text-only placeholders ({callback_time})."""
        lead_values = {k: (lead or {}).get(k, "") if lead is not None else "{{" + k + "}}" for k in LEAD_PLACEHOLDERS}
        email_values = {k: (extra or {}).get(k, "") for k in EMAIL_PLACEHOLDERS}
        opening_values = {k: (extra or {}).get(k, "{{" + k + "}}") for k in OPENING_PLACEHOLDERS}
        return text.format(caller_name=self.caller_name, company_name=self.company_name,
                           callback_number=self.company_phone, company_website=self.company_website,
                           company_address=self.company_address, **lead_values, **email_values, **opening_values)


def load_settings(path: Path = SETTINGS_PATH) -> AgentSettings:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SettingsError(f"settings file not found: {path}") from None
    except yaml.YAMLError as e:
        raise SettingsError(f"{path.name} is not valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise SettingsError(f"{path.name} must be a mapping of keys at the top level")
    try:
        return AgentSettings.model_validate(raw)
    except ValidationError as e:
        lines = [f"  - {'.'.join(str(p) for p in err['loc']) or '(top)'}: {err['msg']}" for err in e.errors()]
        raise SettingsError(f"{path.name} is invalid:\n" + "\n".join(lines)) from None


def build_instructions(s: AgentSettings) -> str:
    """System prompt for the LLM, built from the settings file."""
    objections = "\n".join(f"- {o.objection}: {s.fill(o.response)}" for o in s.objection_handling)
    return f"""\
You are {s.caller_name}, calling on behalf of {s.company_name}. This is a live phone call.

How you speak:
{s.fill(s.speaking_style)}

Your opening line (already spoken at the start of the call, don't repeat it): "{s.fill(s.opening_line)}"

What to do on this call:
{s.fill(s.main_script)}

Handling objections:
{objections}

Ending the call:
{s.fill(s.goodbye.rules)}
Example goodbye: "{s.fill(s.goodbye.line)}"

Output rules (your words are turned into speech): plain spoken words only. No lists, \
bullet points, markdown, emojis, or special symbols. Write numbers the way you'd say them.
"""
