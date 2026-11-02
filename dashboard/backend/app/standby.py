"""Standby: a second copy of the backend that must leave the shared accounts alone (the server until the switch-over).

Cartesia and Telnyx hold ONE webhook URL per agent / messaging profile, so a second running backend that re-points
them steals the call events from the live one. DIALER_STANDBY=1 makes this copy read-only towards the outside:

  tunnel.check_once    re-points nothing (Cartesia webhook, the 3 tools, Telnyx messaging webhook)
  cartesia.request     GET only (no calls, no cancels, no agent / tool / webhook changes)
  sms._post            nothing (no texts, no messaging profile changes)
  emails._smtp_send    nothing
  unsubscribe.enabled  the inbox isn't read

The dashboard, login, database and read-only checks work as usual. Empty, missing or "0" = off (the laptop).
"""

import os

DETAIL = "Standby: Cartesia and Telnyx still point at the live backend (the laptop). This copy changes nothing there."


def on() -> bool:
    return (os.getenv("DIALER_STANDBY") or "").strip() not in ("", "0")
