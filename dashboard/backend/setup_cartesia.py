"""One-time (idempotent) Cartesia setup for the dialer. Safe to run again.

    . .\\setup_env.ps1
    python dashboard\\backend\\setup_cartesia.py

Creates (or finds) in your Cartesia account, and saves the IDs to .env:
  1. SIP trunk provider -> sip.telnyx.com (CARTESIA_SIP_TRANSPORT, default TLS) with the Telnyx SIP credentials
     from .env. Nothing is created in Telnyx.
  2. The Telnyx number (TELNYX_PHONE_NUMBER) imported on that trunk, used as caller ID.
  3. The managed agent, configured from config/agent_settings.yaml.
  4. The webhook, if the tunnel is running (otherwise the backend does it when it starts).
No call is placed. Secrets are never printed.

    python dashboard/backend/setup_cartesia.py --recreate-trunk   (from the project root)
deletes and recreates the SIP trunk + number import with CARTESIA_SIP_TRANSPORT (tls / tcp);
Cartesia can't change the transport of an existing trunk.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "shared"), str(Path(__file__).resolve().parent)]

import aiohttp  # noqa: E402
from env_utils import file_log  # noqa: E402  (loads .env)

from app import cartesia, cartesia_setup, tunnel  # noqa: E402


async def main() -> int:
    try:
        if "--recreate-trunk" in sys.argv:
            r = await cartesia_setup.recreate_sip_trunk()
            print(f"0. SIP trunk recreated: {r['old_transport']} -> {r['new_transport']} ({r['address']})")
            print(f"   provider {r['old_provider_id']} -> {r['new_provider_id']}")
            print(f"   number   {r['old_number_id']} -> {r['new_number_id']}")
        provider_id, created = await cartesia_setup.ensure_sip_provider()
        print(f"1. SIP trunk provider  {provider_id}  ({'created' if created else 'already there'})")
        number_id, created = await cartesia_setup.ensure_number(provider_id)
        print(f"2. Phone number        {number_id}  ({'imported' if created else 'already imported'})")
        agent = await cartesia_setup.sync_agent("setup_cartesia.py")
        print(f"3. Agent               {agent['agent_id']}  (config {agent['action']})")
        async with aiohttp.ClientSession() as http:
            url = await tunnel._tunnel_url(http)
        if url:
            w = await cartesia_setup.ensure_webhook(url)
            print(f"4. Webhook             {w['webhook_id']}  -> {w['url']}  ({w['action']}, attached={w['attached']})")
            t = await cartesia_setup.ensure_outcome_tool(url)
            agent = await cartesia_setup.sync_agent("record_outcome tool")
            print(f"5. record_outcome tool {t['tool_id']}  -> {t['url']}  ({t['action']}, agent config {agent['action']})")
        else:
            print("4. Webhook             skipped: tunnel not running (the backend registers it on start)")
            print("5. record_outcome tool skipped: tunnel not running (the backend registers it on start)")
    except cartesia.CartesiaError as e:
        print(f"FAILED: {e}")
        return 1
    print("Done. IDs are in .env.")
    return 0


if __name__ == "__main__":
    file_log("setup_cartesia.log")
    raise SystemExit(asyncio.run(main()))
