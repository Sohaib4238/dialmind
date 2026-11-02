"""Imported FIRST by every test suite: an isolated, offline world for one suite.

- A temp folder per suite (under TEST_TMP_DIR if the runner set it): fake .env, data/, logs/, a temp COPY of
  config/agent_settings.yaml, the test-mode switch, supervisor settings. The real .env, data/, logs/ and config/
  are never read or written.
- Fake keys and IDs only (tests/test.env). No real key is needed, so this runs in CI.
- Kill switches on (calls, emails, texts); suites that test sending lift them in-process for their fakes.
- Network guard: only 127.0.0.1 / localhost can be reached (fake SMTP/IMAP servers). Any real host
  (Cartesia, Telnyx, Gmail...) raises at DNS lookup, so a missing fake can never reach a real service.
"""

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
_suite = Path(sys.argv[0]).stem or "suite"
_root = os.environ.get("TEST_TMP_DIR") or None
if _root:
    Path(_root).mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix=f"{_suite}-", dir=_root)).as_posix()

(Path(TMP) / "config").mkdir()
shutil.copy2(REPO / "config" / "agent_settings.yaml", Path(TMP) / "config" / "agent_settings.yaml")
shutil.copy2(TESTS / "test.env", Path(TMP) / ".env")

os.environ.update({
    "DIALER_ENV_FILE": f"{TMP}/.env",
    "DIALER_DATA_DIR": f"{TMP}/data",
    "DIALER_LOG_DIR": f"{TMP}/logs",
    "AGENT_SETTINGS_PATH": f"{TMP}/config/agent_settings.yaml",
    "DATABASE_URL": f"sqlite:///{TMP}/dialer.db",
    "DIALER_STATE_PATH": f"{TMP}/dialer_state.json",  # missing = test mode ON
    "SUPERVISOR_SETTINGS_PATH": f"{TMP}/supervisor.json",
    "CARTESIA_CALLS_DISABLED": "1",
    "SMS_SENDING_DISABLED": "1",
    "EMAIL_SENDING_DISABLED": "1",
})
sys.path[:0] = [str(TESTS), str(REPO / "shared"), str(REPO / "dashboard" / "backend")]

_LOCAL = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}
_real_getaddrinfo = socket.getaddrinfo


def _local_only(host, *args, **kwargs):
    name = host.decode() if isinstance(host, bytes) else str(host or "")
    if name.lower() not in _LOCAL:
        raise OSError(f"test network guard: {name} is not allowed (tests are offline; use a fake)")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _local_only
