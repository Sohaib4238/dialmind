"""Read/write config/agent_settings.yaml for the dashboard.

- Help text for each field comes from the YAML comments above it.
- The form spec is built from the agent's own pydantic model (agent/settings.py), so the
  dashboard and the agent always agree on what's valid.
- Saving: validate with the same validator -> write to a temp file -> re-load it with the
  agent's loader -> back up the old file -> atomic replace. A broken file is never saved.
- Comments and layout are kept on save (ruamel.yaml round-trip).
"""

import io
import os
import re
import shutil
import typing
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args, get_origin

from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString

from settings import SETTINGS_PATH, AgentSettings, SettingsError, load_settings  # agent/settings.py

BACKUP_DIR = SETTINGS_PATH.parent / "backups"
MAX_BACKUPS = 50

# Sections saved and validated but not used by the agent yet.
COMING_SOON: set[str] = set()

# Sections with their own dashboard page (not shown on the Instructions form, but still
# sent back unchanged with every Instructions save).
OWN_PAGE = {"voice"}

LABELS = {
    "company_name": "Company name",
    "caller_name": "Caller name",
    "company_phone": "Call-back number",
    "company_website": "Website",
    "company_address": "Business address",
    "opening_line": "Opening line",
    "lead_sources": "Where the lead came from (introduction)",
    "found_sources": "Sources where we found them",
    "request_line": "They asked on a site (Bark...)",
    "found_line": "We found their business",
    "no_source_line": "No source known",
    "call_start": "Call start (who speaks first)",
    "greet_if_silent": "Greet first if they stay silent",
    "wait_seconds": "Wait for their hello (seconds)",
    "silent_greeting": "If they stay silent, say",
    "inbound": "Inbound calls",
    "inbound.enabled": "Answer calls to our number",
    "inbound.greeting": "Inbound greeting",
    "inbound.new_caller": "New caller (not in our leads)",
    "speaking_style": "Speaking style",
    "main_script": "Main script",
    "ai_receptionist_script": "AI receptionist script",
    "voicemail": "Voicemail",
    "leave_message": "Leave voicemail message",
    "message": "Message",
    "emails": "Follow-up emails",
    "daily_limit": "Daily email limit",
    "after_voicemail": "Email after voicemail too",
    "interested": "Email: interested",
    "callback": "Email: not now / callback",
    "details": "Email: asked for details",
    "voicemail_followup": "Email: after voicemail",
    "specialist_titles": "Who the lead is handed to (title)",
    "by_service": "Title by service",
    "default": "Unclear / empty service",
    "transfer_intro": "Connecting line (transfer)",
    "senior_unavailable": "Manager can't take the call",
    "scheduling_instructions": "Scheduling instructions",
    "objection_handling": "Objection handling",
    "goodbye": "Goodbye",
    "rules": "Rules",
    "line": "Goodbye line",
    "settings": "Call settings",
    "calling_hours": "Calling hours",
    "max_call_minutes": "Max call length (minutes)",
    "recordings_keep_days": "Keep recordings (days)",
    "silence": "Silence",
    "enabled": "On",
    "silence_check_seconds": "Check in after (seconds)",
    "max_silence_seconds": "Hang up after (seconds)",
    "check_message": "First check-in",
    "close_message": "Second check-in (before hanging up)",
    "dialer": "Auto-dialer",
    "max_calls_per_session": "Max calls per session",
    "retry_statuses": "Retry these statuses",
    "max_retries": "Extra tries",
    "retry_gap_hours": "Hours between tries",
    "pause_after_failures": "Pause after failed calls in a row",
    "min_free_minutes": "Stop below minutes left",
    "voicemail_retries": "Voicemail: extra tries",
    "callback_default_days": "Callback after (days)",
    "max_callbacks": "Max callbacks in a row",
    "service_fit": "Service fit",
    "offered": "Services we offer",
    # by full path (wins over the plain key: the text templates share names with the email ones)
    "sms": "Follow-up texts (SMS)",
    "sms.daily_limit": "Daily text limit",
    "sms.not_answered": "Text: not answered / voicemail",
    "sms.not_interested": "Text: not interested (polite)",
    "sms.not_now": "Text: not now / callback",
    "sms.interested": "Text: interested / asked for details",
    "not_offered": "Services we don't offer",
}

OPTION_LABELS = {
    "no_answer": "No answer / busy",
    "bad_line": "Bad line",
    "failed": "Failed",
}

# Help for sections newer than a server's own settings file (its comments come from before they existed).
DEFAULT_HELP = {
    "opening_line": ("The AI's introduction, said once, right after the person says hello. {source_line} is where the "
                     "lead came from (see below). Parts in [square brackets] are left out when a detail in them is empty. "
                     "Our company name isn't in it on purpose: the AI says it only when they ask who's calling."),
    "lead_sources": "How the introduction says where the lead came from (the sales sheet's \"Source\" column, {lead_source}).",
    "lead_sources.found_sources": ("Sources where we found the business ourselves. Any other source is a request the "
                                   "lead made (Bark, Thumbtack...)."),
    "lead_sources.request_line": ("A lead that asked for something. {business_name} falls back to a short business "
                                  "description (\"roofing company\")."),
    "lead_sources.found_line": "A business we found ourselves.",
    "lead_sources.no_source_line": "No source known.",
    "call_start": "When the person picks up and says nothing.",
    "call_start.greet_if_silent": ("The AI gives them a moment to say hello. If they don't, it checks with a short "
                                   "\"Hello?\". Off: it waits until they speak."),
    "call_start.wait_seconds": "How long it waits for their hello (2-8 seconds). If they speak sooner, it answers them right away.",
    "call_start.silent_greeting": ("A short check if they stay silent, like \"Hello?\". Not the introduction: that "
                                   "comes once, after they speak."),
    "voice.ambience.volume_percent": "Keep it low so the voice stays clear.",
    "inbound": "Calls to our number: a lead calling back, or someone new. Off: nobody answers.",
    "inbound.enabled": "The AI answers calls to our number. Needs a one-time Telnyx step (README: Running it for real).",
    "inbound.greeting": ("The first thing a caller hears. No lead details here: right after it, the AI looks up their "
                         "number and greets a known lead by name."),
    "inbound.new_caller": "What the AI finds out from a caller who isn't in our leads. It saves them as a new lead.",
}

# Help that wins over an older settings file's own comment (its meaning changed).
FIXED_HELP = {k: DEFAULT_HELP[k] for k in ("call_start.greet_if_silent", "call_start.silent_greeting", "opening_line")}

_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):")


class SettingsConflict(Exception):
    pass


class SettingsInvalid(Exception):
    def __init__(self, errors: list[dict]) -> None:
        super().__init__("invalid settings")
        self.errors = errors


def _yaml() -> YAML:
    y = YAML()  # round-trip: keeps comments and order
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def file_version() -> str:
    st = SETTINGS_PATH.stat()
    return f"{st.st_mtime_ns}-{st.st_size}"


def parse_help(text: str) -> dict[str, str]:
    """Map dotted key paths to the comment block directly above them."""
    help_: dict[str, str] = {}
    pending: list[str] = []
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            pending.append(stripped.lstrip("#").strip())
            continue
        if not stripped:
            pending = []
            continue
        m = _KEY_RE.match(line)
        if m and not stripped.startswith("-"):
            indent, key = len(m.group(1)), m.group(2)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            path = ".".join([k for _, k in stack] + [key])
            if pending:
                text = " ".join(p for p in pending if p)
                # The UI shows a "Coming soon" tag instead of the file's "LATER STEP:" marker.
                text = re.sub(r"^LATER STEP:\s*", "", text)
                help_[path] = text[:1].upper() + text[1:]
            stack.append((indent, key))
        pending = []
    return help_


def _label(name: str, path: str = "") -> str:
    return LABELS.get(path) or LABELS.get(name, name.replace("_", " ").capitalize())


def _field_spec(model: type[BaseModel], prefix: str, help_: dict[str, str]) -> list[dict]:
    fields = []
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        ann = info.annotation
        spec: dict = {
            "key": name,
            "path": path,
            "label": _label(name, path),
            "help": help_.get(path, ""),
            "coming_soon": path in COMING_SOON or any(path.startswith(c + ".") for c in COMING_SOON),
        }
        origin = get_origin(ann)
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            spec.update(kind="group", fields=_field_spec(ann, path + ".", help_))
        elif origin in (list, typing.List):
            (item,) = get_args(ann)
            if isinstance(item, type) and issubclass(item, BaseModel):
                spec.update(kind="list", item_fields=_field_spec(item, "", {}))
            elif get_origin(item) is Literal:
                spec.update(kind="multi", options=[{"value": v, "label": OPTION_LABELS.get(v, v.capitalize())} for v in get_args(item)])
        elif origin is Literal:
            spec.update(kind="select", options=[{"value": v, "label": OPTION_LABELS.get(v, v)} for v in get_args(ann)])
        elif ann is bool:
            spec["kind"] = "switch"
        elif ann is int:
            meta = {type(m).__name__: m for m in info.metadata}
            spec.update(
                kind="number",
                min=getattr(meta.get("Ge"), "ge", None),
                max=getattr(meta.get("Le"), "le", None),
            )
        elif ann is str:
            spec["kind"] = "time" if prefix.endswith("calling_hours.") else "text"
        else:
            spec["kind"] = "text"
        fields.append(spec)
    return fields


def read() -> dict:
    text = SETTINGS_PATH.read_text(encoding="utf-8")
    try:
        values = load_settings().model_dump()
        error = None
    except SettingsError as e:
        import yaml

        values = yaml.safe_load(text) or {}
        error = str(e)
    return {
        "fields": [f for f in _field_spec(AgentSettings, "", {**DEFAULT_HELP, **parse_help(text), **FIXED_HELP})
                   if f["key"] not in OWN_PAGE],
        "values": values,
        "version": file_version(),
        "file_error": error,
        "path": str(SETTINGS_PATH),
    }


def _validation_errors(e: ValidationError) -> list[dict]:
    out = []
    for err in e.errors():
        loc = [str(p) for p in err["loc"]]
        msg = err["msg"].removeprefix("Value error, ")
        # The placeholder check reports its own path inside the message.
        m = re.match(r"^([\w.\[\]]+): (unknown placeholder.*|a \{ or \} doesn't.*|\{\w+\} can only be used.*)", msg)
        if m:
            loc, msg = [m.group(1)], m.group(2)
        out.append({"path": ".".join(loc), "message": msg})
    return out


def _apply(node, value):
    """Copy plain values into the ruamel tree, keeping comments where the shape is unchanged."""
    if isinstance(value, dict) and hasattr(node, "keys"):
        for k, v in value.items():
            if k in node and isinstance(v, (dict, list)) and not isinstance(v, str):
                node[k] = _apply(node[k], v)
            else:
                node[k] = _scalar(v)
        for k in [k for k in node if k not in value]:
            del node[k]
        return node
    if isinstance(value, list):
        if isinstance(node, CommentedSeq):
            return _apply_seq(node, value)
        return [_plain(v) for v in value]
    return _scalar(value)


def _apply_seq(seq: CommentedSeq, value: list) -> CommentedSeq:
    """Update a list in place. The comment block that follows a list in the file (e.g. the
    one above the next top-level key) is stored on its last item, so move it along when
    items are added or removed."""
    if all(not isinstance(v, (dict, list)) for v in value):
        seq[:] = [_scalar(v) for v in value]  # keeps flow style like [mon, tue]
        return seq

    trailing = _pop_trailing_comment(seq)
    for i, v in enumerate(value):
        if i < len(seq) and isinstance(v, dict) and hasattr(seq[i], "keys"):
            _apply(seq[i], v)
        elif i < len(seq):
            seq[i] = _plain(v)
        else:
            seq.append(CommentedMap(_plain(v)) if isinstance(v, dict) else _plain(v))
    del seq[len(value):]
    if trailing is not None and seq and isinstance(seq[-1], CommentedMap) and seq[-1]:
        last_key = list(seq[-1].keys())[-1]
        seq[-1].ca.items.setdefault(last_key, [None, None, None, None])[2] = trailing
    return seq


def _pop_trailing_comment(seq: CommentedSeq):
    if not seq or not isinstance(seq[-1], CommentedMap) or not seq[-1]:
        return None
    last_key = list(seq[-1].keys())[-1]
    entry = seq[-1].ca.items.get(last_key)
    if entry and len(entry) > 2 and entry[2] is not None:
        token, entry[2] = entry[2], None
        return token
    return None


def _plain(v):
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return _scalar(v)


def _scalar(v):
    if isinstance(v, str) and "\n" in v.strip():
        return LiteralScalarString(v.rstrip() + "\n")
    return v


def save_voice(voice: dict, version: str | None) -> dict:
    """Replace only the voice section (Voice page). Same validator, backup and conflict check."""
    import yaml

    values = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    values["voice"] = voice
    return save(values, version)


def voice_help() -> dict[str, str]:
    """Help text for the voice fields, from the YAML comments."""
    help_ = {**DEFAULT_HELP, **parse_help(SETTINGS_PATH.read_text(encoding="utf-8"))}
    return {k.removeprefix("voice."): v for k, v in help_.items() if k.startswith("voice.")}


def save(values: dict, version: str | None) -> dict:
    if version and version != file_version():
        raise SettingsConflict("The settings file changed on disk since you opened it. Reload to see the latest version.")

    try:
        validated = AgentSettings.model_validate(values)
    except ValidationError as e:
        raise SettingsInvalid(_validation_errors(e)) from None

    y = _yaml()
    tree = y.load(SETTINGS_PATH.read_text(encoding="utf-8"))
    _apply(tree, validated.model_dump())
    buf = io.StringIO()
    y.dump(tree, buf)

    tmp = SETTINGS_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    try:
        load_settings(tmp)  # the agent's own loader must accept the exact file we write
    except SettingsError as e:
        tmp.unlink(missing_ok=True)
        raise SettingsInvalid([{"path": "", "message": f"Not saved: generated file failed validation. {e}"}]) from None

    BACKUP_DIR.mkdir(exist_ok=True)
    now = datetime.now()
    backup = BACKUP_DIR / f"agent_settings.{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}.yaml"
    shutil.copy2(SETTINGS_PATH, backup)
    os.replace(tmp, SETTINGS_PATH)
    for old in sorted(BACKUP_DIR.glob("agent_settings.*.yaml"))[:-MAX_BACKUPS]:
        old.unlink(missing_ok=True)
    return {"version": file_version(), "backup": backup.name}
