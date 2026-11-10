"""Phonic dry run: validates the outbound-call setup (API key, Telnyx trunk, caller ID, the agent config) WITHOUT
placing a call. Phonic's dry_run "validates setup and returns without placing a call" (it doesn't check that the
phone is reachable or test audio).

    python dashboard/backend/phonic_dry_run.py

Safe by construction:
  - its own data folder (.cache/phonic_test: database, test-mode switch, provider + supervisor settings), so the
    real data/, the leads and the Google Sheets are never read or written; one fake lead with the test phone;
  - PHONIC_TEST_ONLY=1 (only TEST_CALL_NUMBER), call kill switches on, texts and emails off;
  - our webhook tools are NOT created in Phonic (that happens before the first real Phonic call).
Prints Phonic's reply. Never prints the API key or the SIP password.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_DIR = Path(os.getenv("PHONIC_TEST_DATA_DIR") or ROOT / ".cache" / "phonic_test")
TEST_DIR.mkdir(parents=True, exist_ok=True)
# Before anything loads .env (dotenv never overrides what's already set)
os.environ.update({
    "DIALER_DATA_DIR": str(TEST_DIR),
    "DATABASE_URL": f"sqlite:///{(TEST_DIR / 'dialer_test.db').as_posix()}",
    "DIALER_STATE_PATH": str(TEST_DIR / "dialer_state.json"),  # missing = TEST MODE on
    "VOICE_PROVIDER_PATH": str(TEST_DIR / "voice_provider.json"),
    "SUPERVISOR_SETTINGS_PATH": str(TEST_DIR / "supervisor.json"),
    "PHONIC_TEST_ONLY": "1",
    "PHONIC_CALLS_DISABLED": "1",
    "CARTESIA_CALLS_DISABLED": "1",
    "SMS_SENDING_DISABLED": "1",
    "EMAIL_SENDING_DISABLED": "1",
})
sys.path[:0] = [str(ROOT / "shared"), str(Path(__file__).resolve().parent)]

import env_utils  # noqa: E402,F401  (loads .env)
from settings import load_settings  # noqa: E402

from app import phonic, providers  # noqa: E402
from app.calls import _max_minutes, call_variables  # noqa: E402
from app.db import Lead, get_session, init_db  # noqa: E402
from app.providers.phonic_provider import build_call_config  # noqa: E402
from sqlmodel import select  # noqa: E402


def fake_lead() -> Lead:
    with get_session() as s:
        lead = s.exec(select(Lead).where(Lead.phone == phonic.test_only_number())).first()
        if lead is None:
            lead = Lead(phone=phonic.test_only_number(), name="Test Lead", business="Test Plumbing Co",
                        location="Miami, FL 33101", service_required="Website design",
                        business_description="Small plumbing business (test lead)", lead_source="Bark")
            s.add(lead)
            s.commit()
            s.refresh(lead)
        return lead


async def main() -> int:
    init_db()
    lead = fake_lead()
    s = load_settings()
    variables, _ = call_variables(s, lead, lead.phone)
    config = build_call_config(s, variables, _max_minutes(), set(), providers.load()["phonic"])
    headers, sip = phonic.sip_target()
    print(f"Test data folder: {TEST_DIR}")
    print(f"Lead #{lead.id}: {lead.name} {lead.phone}")
    print(f"From {phonic.from_number()} via {headers['X-Sip-Address']} ({sip['transport']}, SRTP {sip['media_encryption']}), "
          f"SIP username {'set' if 'X-Sip-Auth-Username' in headers else 'NOT set'}, "
          f"password {'set' if 'X-Sip-Auth-Password' in headers else 'NOT set'}")
    print(f"Config: voice {config['voice_id']}, speed {config['audio_speed']}, background {config['background_noise']}, "
          f"tools {config['tools']}, prompt {len(config['system_prompt'])} chars, "
          f"{len(config['template_variables'])} variables")
    try:
        voices = await phonic.list_voices()
        print(f"Voices available: {len(voices)} ({', '.join(v.get('id', '?') for v in voices[:12])}...)")
    except phonic.PhonicError as e:
        print(f"Voices: {e}")
    try:
        reply = await phonic.outbound_call(phonic.test_only_number(), config, dry_run=True)
    except phonic.PhonicError as e:
        print(f"DRY RUN FAILED: {e}")
        if e.body:
            print(json.dumps(e.body, indent=2)[:3000])
        return 1
    print("DRY RUN reply (HTTP 200):")
    print(json.dumps(reply, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
