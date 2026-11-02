"""Where do Cartesia and Telnyx send things right now? Read-only (GETs); prints hostnames, never a key or secret.

    python check_links.py        # on the server: /srv/dialmind/app/switch.sh status
"""

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path[:0] = [str(Path(__file__).resolve().parents[2] / "shared"), str(Path(__file__).resolve().parent)]

import env_utils  # noqa: E402,F401  (loads .env)

from app import cartesia, sms, standby, test_mode, tunnel  # noqa: E402


def host(url: str | None) -> str:
    return urlparse(url or "").hostname or "(empty)"


async def main() -> None:
    print(f"standby: {'ON' if standby.on() else 'off'} | fixed address: {tunnel.fixed_url() or '(none)'}")
    print(f"test mode: {'ON, calls to ' + test_mode.number() if test_mode.status()['on'] else 'OFF'}")
    print(f"kill switches: calls={os.getenv('CARTESIA_CALLS_DISABLED', '')} texts={os.getenv('SMS_SENDING_DISABLED', '')} "
          f"emails={os.getenv('EMAIL_SENDING_DISABLED', '')}")
    try:
        w = await cartesia.get_webhook(os.environ["CARTESIA_WEBHOOK_ID"])
        print(f"Cartesia webhook:     {host(w.get('url'))}")
        for name in ("CARTESIA_OUTCOME_TOOL_ID", "CARTESIA_SUPERVISOR_TOOL_ID", "CARTESIA_EMAIL_TOOL_ID"):
            t = await cartesia.get_tool(os.environ[name])
            print(f"Cartesia tool {t.get('name', '?'):<19} {host((t.get('api_schema') or {}).get('url'))}")
    except (cartesia.CartesiaError, KeyError) as e:
        print(f"Cartesia: couldn't read it ({type(e).__name__}: {e})")
    try:
        prof = sms._get(f"/messaging_profiles/{os.environ['TELNYX_MESSAGING_PROFILE_ID']}").get("data") or {}
        print(f"Telnyx messaging:     {host(prof.get('webhook_url'))}")
    except (sms.SmsError, KeyError) as e:
        print(f"Telnyx: couldn't read it ({type(e).__name__}: {e})")


if __name__ == "__main__":
    asyncio.run(main())
