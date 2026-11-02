#!/usr/bin/env bash
# Daily database backup (systemd timer dialmind-backup, 03:00 UTC). Safe while the app runs: SQLite's own .backup,
# not a file copy. Keeps the last 7 here, and uploads to S3 when S3_BACKUP_BUCKET is set in /srv/dialmind/app/.env
# (the bucket deletes after 30 days). The result goes to data/backup_status.json for the Health page.
# Run by hand with: /srv/dialmind/app/backup_db.sh
set -uo pipefail

ROOT=${DIALER_ROOT:-/srv/dialmind}
DB=$ROOT/data/dialer.db
DIR=$ROOT/backups
STATUS=$ROOT/data/backup_status.json
KEEP=7
BUCKET=${S3_BACKUP_BUCKET:-$(grep -s '^S3_BACKUP_BUCKET=' "$ROOT/app/.env" | cut -d= -f2)}

umask 077

status() {  # ok, file, offsite (ok / failed / off), detail
    printf '{"ok": %s, "at": %s, "file": "%s", "offsite": "%s", "detail": "%s"}\n' "$1" "$(date +%s)" "$2" "$3" "$4" >"$STATUS.tmp" &&
        mv "$STATUS.tmp" "$STATUS"
}

die() {
    echo "Backup FAILED: $1" >&2
    status false "" off "$1"
    exit 1
}

OUT="$DIR/dialer-$(date -u +%Y%m%d-%H%M%S).db"
sqlite3 "$DB" ".timeout 30000" ".backup '$OUT'" || die "sqlite backup failed"
result=$(sqlite3 "$OUT" "PRAGMA integrity_check;")
if [ "$result" != "ok" ]; then
    rm -f "$OUT"
    die "the copy failed its integrity check"
fi
gzip -f "$OUT" || die "gzip failed"
ls -1t "$DIR"/dialer-*.db.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
NAME=$(basename "$OUT.gz")
echo "Backup OK: $OUT.gz ($(du -h "$OUT.gz" | cut -f1))"

if [ -z "$BUCKET" ]; then
    status true "$NAME" off ""
    exit 0
fi
AWS=$(command -v aws || echo /snap/bin/aws)
if "$AWS" s3 cp "$OUT.gz" "s3://$BUCKET/daily/$NAME" --only-show-errors --region us-west-2; then
    echo "Uploaded to s3://$BUCKET/daily/$NAME"
    status true "$NAME" ok ""
else
    echo "Upload to S3 FAILED (the local backup is fine)" >&2
    status true "$NAME" failed "upload failed"
    exit 1  # shows as failed in systemctl and journalctl
fi
