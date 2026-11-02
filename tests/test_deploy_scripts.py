"""The scripts in deploy/: switch.sh, backup_db.sh, setup_s3_backup.sh run for real against a fake docker / aws in PATH."""
from _setup import REPO, TMP  # noqa: E402,F401  (FIRST: temp folder, fake .env, offline guard)
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

DEPLOY = REPO / "deploy"
BIN = Path(TMP) / "bin"
BIN.mkdir()
LOG = Path(TMP) / "calls.log"


def find_bash():
    for c in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if not c:
            continue
        try:  # Windows' own bash.exe is WSL: it may have no distribution at all
            if subprocess.run([c, "-c", "echo ok"], capture_output=True, text=True, timeout=20).stdout.strip() == "ok":
                return c
        except (OSError, subprocess.SubprocessError):
            pass
    return None


BASH = find_bash()
if not BASH:
    print("SKIP: no working bash here")
    sys.exit(0)


def fake(name, body):
    p = BIN / name
    p.write_bytes(("#!/usr/bin/env bash\n" + body + "\n").encode())
    p.chmod(0o755)


fake("docker", f"""echo "docker $*" >> "{Path(LOG).as_posix()}"
case "$*" in
  *"inspect"*) echo healthy ;;
  *"check_links.py"*) echo "fake links" ;;
esac""")
ENV = {**os.environ, "PATH": f"{BIN.as_posix()}{os.pathsep}{os.environ['PATH']}"}


def run(script, *args, stdin="", env=None, cwd=None):
    r = subprocess.run([BASH, script, *args], input=stdin.encode(), capture_output=True, timeout=60, env={**ENV, **(env or {})}, cwd=cwd)  # bytes: no \r\n on Windows
    return r.returncode, (r.stdout + r.stderr).decode(errors="replace")


# ---- switch.sh ------------------------------------------------------------------------------
ROOT = Path(TMP) / "srv"
APP = ROOT / "app"
(ROOT / "data").mkdir(parents=True)
APP.mkdir()
shutil.copy(DEPLOY / "switch.sh", APP / "switch.sh")
SWITCH = (APP / "switch.sh").as_posix()
SW_ENV = {"DIALER_ROOT": ROOT.as_posix()}
MARKER = ROOT / "data" / ".copied-from-laptop"
(APP / ".env").write_text("RELEASE_TAG=abc\n")


def flags():
    return dict(line.split("=", 1) for line in (APP / ".env").read_text().splitlines() if "=" in line)


def calls():
    t = LOG.read_text() if LOG.exists() else ""
    LOG.write_text("")
    return t


code, out = run(SWITCH, "status", env=SW_ENV)
assert code == 0 and "fake links" in out, out
assert flags() == {"RELEASE_TAG": "abc"} and "up -d" not in calls()
print("status: reads only")

code, out = run(SWITCH, "live", stdin="LIVE\n", env=SW_ENV)
assert code != 0 and "copied" in out and "DIALER_STANDBY" not in flags() and "up -d" not in calls(), out
print("live: refused until the laptop's data is copied")

MARKER.write_text("copied")
for answer in ("", "yes\n", "live\n"):
    code, out = run(SWITCH, "live", stdin=answer, env=SW_ENV)
    assert code != 0 and "DIALER_STANDBY" not in flags() and "up -d" not in calls(), (answer, out)
print("live: needs the word LIVE typed")

code, out = run(SWITCH, "calls-on", env=SW_ENV)
assert code != 0 and "CARTESIA_CALLS_DISABLED" not in flags(), out
print("calls-on: refused while in standby")

code, out = run(SWITCH, "sms-on", env=SW_ENV)
assert code != 0 and "SMS_SENDING_DISABLED" not in flags(), out
print("sms-on: refused while in standby")

code, out = run(SWITCH, "live", stdin="LIVE\n", env=SW_ENV)
assert code == 0, out
f = flags()
assert f["DIALER_STANDBY"] == "0" and f["CARTESIA_CALLS_DISABLED"] == "1" and f["RELEASE_TAG"] == "abc", f
assert "compose up -d backend" in calls() and "fake links" in out
print("live: standby off, calls still disabled, backend restarted, read-back shown")

code, out = run(SWITCH, "calls-on", env=SW_ENV)
assert code == 0 and flags()["CARTESIA_CALLS_DISABLED"] == "0", out
code, out = run(SWITCH, "calls-off", env=SW_ENV)
assert code == 0 and flags()["CARTESIA_CALLS_DISABLED"] == "1", out
code, out = run(SWITCH, "sms-on", env=SW_ENV)
assert code == 0 and flags()["SMS_SENDING_DISABLED"] == "0", out
code, out = run(SWITCH, "sms-off", env=SW_ENV)
assert code == 0 and flags()["SMS_SENDING_DISABLED"] == "1", out
run(SWITCH, "calls-on", env=SW_ENV)
run(SWITCH, "sms-on", env=SW_ENV)
code, out = run(SWITCH, "standby", env=SW_ENV)
f = flags()
assert code == 0 and f["DIALER_STANDBY"] == "1" and f["CARTESIA_CALLS_DISABLED"] == "1" and f["SMS_SENDING_DISABLED"] == "1", f
assert len([k for k in (APP / ".env").read_text().splitlines() if k.startswith("DIALER_STANDBY")]) == 1
code, out = run(SWITCH, "nonsense", env=SW_ENV)
assert code == 2
print("calls-on / calls-off / standby set the right flags, once; unknown command refused")

# The compose file's safe defaults
compose = (DEPLOY / "docker-compose.yml").read_text()
assert 'DIALER_STANDBY: "${DIALER_STANDBY:-1}"' in compose and 'CARTESIA_CALLS_DISABLED: "${CARTESIA_CALLS_DISABLED:-1}"' in compose
assert 'SMS_SENDING_DISABLED: "${SMS_SENDING_DISABLED:-1}"' in compose  # missing = no texts
assert 'EMAIL_SENDING_DISABLED: "1"' in compose and 'DIALER_REQUIRE_LOGIN: "1"' in compose
backend = compose.split("  frontend:")[0]
assert "ports:" not in backend.replace('Never add "ports:"', "")
caddy = (DEPLOY / "Caddyfile").read_text()
assert "dialer.example.com, 203-0-113-10.sslip.io {" in caddy and "header_up -Cf-Connecting-Ip" in caddy
print("compose: standby + kill switches default to safe, backend not published; Caddy serves both hosts")

# ---- backup_db.sh (needs a real sqlite3 for the backup itself) ------------------------------
if shutil.which("sqlite3", path=ENV["PATH"]):
    B = Path(TMP) / "b"
    for d in ("data", "backups", "app"):
        (B / d).mkdir(parents=True)
    db = sqlite3.connect(B / "data" / "dialer.db")
    db.execute("create table lead (id integer)")
    db.commit()
    db.close()
    BK = (DEPLOY / "backup_db.sh").as_posix()
    BE = {"DIALER_ROOT": B.as_posix()}
    status = lambda: json.loads((B / "data" / "backup_status.json").read_text())  # noqa: E731

    code, out = run(BK, env=BE)
    assert code == 0 and status()["ok"] is True and status()["offsite"] == "off", out
    fake("aws", f'echo "aws $*" >> "{LOG.as_posix()}"')
    (B / "app" / ".env").write_text("S3_BACKUP_BUCKET=my-bucket\n")
    calls()
    code, out = run(BK, env=BE)
    logged = calls()
    assert code == 0 and status()["offsite"] == "ok" and "s3://my-bucket/daily/dialer-" in logged and "us-west-2" in logged, out + logged
    fake("aws", "exit 1")
    code, out = run(BK, env=BE)
    assert code != 0 and status()["ok"] is True and status()["offsite"] == "failed", out
    assert list((B / "backups").glob("dialer-*.db.gz"))  # the local backup survives a failed upload
    print("backup: local only / uploads when a bucket is set / a failed upload shows, local copy stays")
else:
    print("backup: skipped here (no sqlite3 command; tested on the server)")

# ---- setup_s3_backup.sh ---------------------------------------------------------------------
STATE = Path(TMP) / "aws_state"
fake("aws", f"""echo "aws $*" >> "{LOG.as_posix()}"
case "$*" in
  "sts get-caller-identity"*) echo 123456789012 ;;
  "s3api head-bucket"*) exit 1 ;;
  "iam get-role"*|"iam get-instance-profile"*) exit 1 ;;
  "ec2 describe-addresses"*) echo i-0abc ;;
  "ec2 describe-iam-instance-profile-associations"*) echo None ;;
esac""")
fake("sleep", "exit 0")
calls()
code, out = run((DEPLOY / "setup_s3_backup.sh").as_posix())
log = calls()
assert code == 0 and "Bucket name: dialmind-backups-123456789012" in out, out + log
for want in ("--region us-west-2 --create-bucket-configuration LocationConstraint=us-west-2", "BlockPublicPolicy=true",
             '"Days":30', "aws:SecureTransport", "associate-iam-instance-profile --region us-west-2 --instance-id i-0abc",
             "--http-put-response-hop-limit 1"):
    assert want in log, want
policy = next(line for line in log.splitlines() if "put-role-policy" in line)
assert "arn:aws:s3:::dialmind-backups-123456789012" in policy and "s3:Delete" not in policy and '"s3:*"' not in policy and '"Resource":"*"' not in policy, policy
fake("aws", f"""echo "aws $*" >> "{LOG.as_posix()}"
case "$*" in
  "sts get-caller-identity"*) echo 123456789012 ;;
  "ec2 describe-addresses"*) echo i-0abc ;;
  "ec2 describe-iam-instance-profile-associations"*) echo arn:aws:iam::1:instance-profile/someone-elses ;;
esac""")
code, out = run((DEPLOY / "setup_s3_backup.sh").as_posix())
assert code != 0 and "another role" in out and "associate-iam" not in calls(), out
print("S3 setup: private bucket, 30-day rule, role limited to that bucket with no delete; won't replace another role")

print("ALL DEPLOY SCRIPT TESTS PASS")
