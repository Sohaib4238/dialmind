"""Runs every offline test suite (fakes only: no calls, no texts, no emails, no real keys needed).

    python tests/run_tests.py                 # all suites
    python tests/run_tests.py dialer sms      # some suites (names without test_ / .py)

Each suite runs in its own process (suites set env vars before importing the app) with its own temp folder
(_setup.py). Logs: <temp>/logs/<suite>.log, printed when a suite fails. Afterwards the real .env, config/,
data/ and logs/ must be byte-for-byte unchanged, or the run fails.
"""

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
REAL = [REPO / ".env", REPO / "config", REPO / "data", REPO / "logs"]


def snapshot() -> dict[str, str]:
    """Hash of every real file the tests must never touch."""
    out = {}
    for p in REAL:
        for f in ([p] if p.is_file() else sorted(p.rglob("*")) if p.is_dir() else []):
            if f.is_file():
                out[str(f.relative_to(REPO))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def main() -> int:
    only = [a.removeprefix("test_").removesuffix(".py") for a in sys.argv[1:]]
    suites = sorted(p.stem.removeprefix("test_") for p in TESTS.glob("test_*.py"))
    if only:
        missing = [s for s in only if s not in suites]
        if missing:
            print(f"No such suite: {', '.join(missing)}. Suites: {', '.join(suites)}")
            return 2
        suites = only
    root = Path(tempfile.mkdtemp(prefix="dialer-tests-"))
    (root / "logs").mkdir()
    env = {**os.environ, "TEST_TMP_DIR": str(root), "PYTHONIOENCODING": "utf-8"}
    before = snapshot()
    failed = []
    t0 = time.time()
    for s in suites:
        log = root / "logs" / f"{s}.log"
        start = time.time()
        with open(log, "wb") as out:
            code = subprocess.call([sys.executable, str(TESTS / f"test_{s}.py")], cwd=REPO, env=env,
                                   stdout=out, stderr=subprocess.STDOUT, timeout=600)
        took = time.time() - start
        if code == 0:
            print(f"PASS {s} ({took:.0f} s)")
        else:
            failed.append(s)
            print(f"FAIL {s} ({took:.0f} s), last lines of {log}:")
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
            print("\n".join("    " + line for line in tail))
    after = snapshot()
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    if changed:
        print("FAIL: tests changed real files (or the real backend was running): " + ", ".join(changed))
        failed.append("real-files")
    print(f"Temp files: {root}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"ALL PASS ({len(suites)} suites, {time.time() - t0:.0f} s); real .env, config/, data/, logs/ untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
