#!/usr/bin/env bash
# Copy the laptop's data to the server (leads, DNC, history, recordings, test-mode switch, settings).
# It replaces the server's data and leaves its logins alone. Run in Git Bash, with the laptop STOPPED:
#   bash deploy/copy_data.sh --dry-run      # only shows what would happen; sends nothing
#   bash deploy/copy_data.sh
# COPY_SRC / COPY_DEST: for testing on fake data only.
set -euo pipefail
cd "$(dirname "$0")/.."

SERVER=ubuntu@203.0.113.10
KEY=${DIALER_SSH_KEY:-$HOME/.ssh/dialmind-key.pem}
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ServerAliveInterval=30 "$SERVER")
PY=.venv/Scripts/python.exe
SRC=${COPY_SRC:-.}
DEST=${COPY_DEST:-/srv/dialmind}
REAL=0
[ "$DEST" = /srv/dialmind ] && REAL=1
DRY=0
[ "${1:-}" = --dry-run ] && DRY=1

fail() { echo "STOP: $1" >&2; exit 1; }

# Counts from a database, as "leads dnc calls"
counts() {
    "$PY" - "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
one = lambda q: c.execute(q).fetchone()[0]
print(one("select count(*) from lead"), one("select count(*) from lead where dnc = 1"), one("select count(*) from call"))
PY
}

[ -f "$SRC/data/dialer.db" ] || fail "no $SRC/data/dialer.db here"
if [ "$REAL" = 1 ] && curl -s -o /dev/null --max-time 3 http://127.0.0.1:8000/api/public/ping; then
    fail "the laptop's backend is still running. Stop it first: .\\stop_all.ps1"
fi
if [ "$REAL" = 1 ] && "${SSH[@]}" "grep -qs '^DIALER_STANDBY=0' /srv/dialmind/app/.env"; then
    fail "the server is already live: copying now would overwrite its data. Nothing changed."
fi

LOCAL=$(counts "$SRC/data/dialer.db")
echo "Laptop database: $LOCAL (leads, of which DNC, calls)"
du -sh "$SRC/data" "$SRC/config" 2>/dev/null

# The server's .env is a copy from Phase 1. If an ID changed since, the server would point at the wrong thing.
if [ "$REAL" = 1 ]; then
    echo "Comparing IDs and secrets with the server (by hash, never shown):"
    mismatch=0
    for k in CARTESIA_API_KEY CARTESIA_AGENT_ID CARTESIA_SIP_PROVIDER_ID CARTESIA_FROM_NUMBER_ID CARTESIA_WEBHOOK_ID CARTESIA_WEBHOOK_SECRET \
        CARTESIA_TOOL_SECRET CARTESIA_OUTCOME_TOOL_ID CARTESIA_SUPERVISOR_TOOL_ID CARTESIA_EMAIL_TOOL_ID TELNYX_API_KEY TELNYX_PUBLIC_KEY \
        TELNYX_MESSAGING_PROFILE_ID TELNYX_PHONE_NUMBER TEST_CALL_NUMBER; do
        a=$(grep "^$k=" .env | cut -d= -f2- | tr -d '\r' | sha256sum | cut -c1-16)
        b=$("${SSH[@]}" "grep '^$k=' /srv/dialmind/secrets/.env | cut -d= -f2- | sha256sum | cut -c1-16")
        if [ "$a" != "$b" ]; then
            echo "  DIFFERENT: $k"
            mismatch=1
        fi
    done
    [ "$mismatch" = 0 ] && echo "  all the same" || fail "fix the values above in /srv/dialmind/secrets/.env first. Nothing was copied."
fi

if [ "$DRY" = 1 ]; then
    echo "Dry run: nothing sent. Would replace $DEST/data and $DEST/config with:"
    tar -C "$SRC" -cf - data config | tar -tf - | grep -vc '/$' | sed 's/$/ files/'
    exit 0
fi

if [ "$REAL" = 1 ]; then
    echo "== Stopping the server's backend"
    "${SSH[@]}" "cd /srv/dialmind/app && ./backup_db.sh && docker compose stop backend"
fi

echo "== Copying"
"${SSH[@]}" "rm -f '$DEST/data/dialer.db-wal' '$DEST/data/dialer.db-shm'"
tar -C "$SRC" -czf - data config | "${SSH[@]}" "tar xzf - -C '$DEST' --no-same-owner --no-overwrite-dir"

echo "== Checking"
REMOTE=$("${SSH[@]}" "sqlite3 '$DEST/data/dialer.db' 'select count(*) from lead; select count(*) from lead where dnc = 1; select count(*) from call;' | tr '\n' ' ' | sed 's/ \$//'")
echo "Server database: $REMOTE"
[ "$REMOTE" = "$LOCAL" ] || fail "the counts differ (laptop: $LOCAL, server: $REMOTE). Don't go live."
[ "$("${SSH[@]}" "sqlite3 '$DEST/data/dialer.db' 'PRAGMA integrity_check;'")" = ok ] || fail "the copied database is damaged. Don't go live."
"${SSH[@]}" "date -u > '$DEST/data/.copied-from-laptop'"

if [ "$REAL" = 1 ]; then
    echo "== Starting the server's backend (still standby)"
    "${SSH[@]}" "cd /srv/dialmind/app && docker compose start backend && sleep 8 && ./switch.sh status"
fi
echo "== Copy done. Check that test mode says ON above before going on."
