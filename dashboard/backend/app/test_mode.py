"""TEST MODE: while it's ON, every call dials TEST_CALL_NUMBER, whoever asked for it (auto-dialer,
retries, callbacks, "Call this lead", the Call page), and every follow-up text (sms.py) goes there too.
Default ON; remembered in data/dialer_state.json.

Enforced in two places, so no call path can skip it:
  route()  in calls.CallManager.start_call (the only way we start a call): swaps in the test number
  guard()  in cartesia.create_call (right before the request to Cartesia): refuses anything else;
           sms.send_raw does the same right before the request to Telnyx (guard(to, "text"))
The state is read from the file on every call (not cached), and an unreadable file counts as ON.
DIALER_STATE_PATH moves the file (tests use a temp one, so they never touch the real switch).
"""

import json
import logging
import os
from pathlib import Path

from app.db import DATA_DIR

logger = logging.getLogger("dashboard.test_mode")


class TestModeError(Exception):
    pass


def state_path() -> Path:
    return Path(os.getenv("DIALER_STATE_PATH") or DATA_DIR / "dialer_state.json")


def is_on() -> bool:
    try:
        return json.loads(state_path().read_text(encoding="utf-8")).get("test_mode", True) is not False
    except (OSError, ValueError, AttributeError):
        return True  # missing / broken file: stay safe


def set_on(on: bool) -> None:
    state_path().write_text(json.dumps({"test_mode": bool(on)}), encoding="utf-8")


def number() -> str:
    return (os.getenv("TEST_CALL_NUMBER") or "").strip()


def status() -> dict:
    return {"on": is_on(), "number": number()}


def route(to_number: str) -> str:
    """The number to really dial for a call to to_number."""
    if not is_on():
        return to_number
    if not number():
        raise TestModeError("TEST MODE is on but TEST_CALL_NUMBER is not set in .env: no call placed")
    return number()


def guard(to_number: str, what: str = "call") -> None:
    """Last check before Cartesia dials (or Telnyx texts): in test mode only the test number."""
    if is_on() and (not number() or to_number != number()):
        verb, nothing = ("texted", "No text sent") if what == "text" else ("called", "No call placed")
        logger.error("BLOCKED: test mode is on but a %s to %s was requested (test number %s). %s.",
                     what, to_number, number() or "not set", nothing)
        raise TestModeError(f"Blocked: TEST MODE is on, so only the test number {number() or '(not set)'} may be "
                            f"{verb}, not {to_number}. {nothing}.")
