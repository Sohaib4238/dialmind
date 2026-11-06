"""Health -> Cartesia balance: Pro plan ($6 a month at $0.03/min), real usage with an admin key, estimate without.
Offline: no admin key in the fake .env, and the usage API's answer is set by hand."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import asyncio
import os
import time

os.environ["CARTESIA_ADMIN_API_KEY"] = ""
import env_utils  # noqa: E402,F401

from app import health  # noqa: E402
from app.db import Call, get_session, init_db, utcnow  # noqa: E402

init_db()
assert health.PLAN_DOLLARS == 6.0 and health.AGENT_RATE_PER_MIN == 0.03

# no calls, no admin key: the whole $6 = 200 minutes, an estimate
b = health.budget()
assert b["source"] == "estimate" and b["left_dollars"] == 6.0 and b["minutes_left"] == 200.0, b
r = asyncio.run(health.check_minutes())
assert r["name"] == "Cartesia balance" and r["ok"] and r.get("warn"), r
assert "$6.00 of $6.00 left" in r["detail"] and "about 200 min" in r["detail"] and "CARTESIA_ADMIN_API_KEY" in r["detail"], r
assert "Overages are off" in r["detail"]
print("no admin key: $6.00 left = about 200 min, marked as an estimate")

# 10 minutes of our own calls this month: $0.30 used
with get_session() as s:
    s.add(Call(room="ac_b1", number="+15555550100", status="ended", duration_s=600, created_at=utcnow()))
    s.commit()
b = health.budget()
assert b["used_dollars"] == 0.3 and b["left_dollars"] == 5.7 and b["minutes_left"] == 190.0, b
assert health.minutes_left() == 190.0
print("estimate: 10 min used -> $5.70 left, about 190 min")

# Cartesia's real numbers win while fresh (what refresh_usage stores from GET /usage/agents)
health._usage.update(at=time.time(), cents=450, minutes=14.2, calls=9, error=None)
b = health.budget()
assert b["source"] == "cartesia" and b["used_dollars"] == 4.5 and b["left_dollars"] == 1.5 and b["minutes_left"] == 50.0, b
health._usage["at"] = time.time() - health.USAGE_MAX_AGE_S - 1  # stale -> back to the estimate
assert health.budget()["source"] == "estimate"
health._usage.update(at=time.time(), cents=700)  # over the plan (overages off): 0 left, never negative
assert health.budget()["left_dollars"] == 0.0 and health.minutes_left() == 0.0
print("usage API: $4.50 used -> $1.50 left = 50 min; stale data falls back; never below $0")
print("ALL BUDGET TESTS PASSED")
