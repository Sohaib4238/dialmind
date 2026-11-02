#!/usr/bin/env bash
# Deploy the current commit: sends it to the server, builds the images there and restarts. Run in Git Bash:
#   bash deploy/release.sh
# Builds on the server (2 GB swap, ~3 min, high CPU), so it needs a quiet moment: NEVER run it during a call.
# Only committed code goes out (git archive), so .env, data/ and keys can't. Uncommitted changes stop it.
# The standby / live state (switch.sh) isn't changed by a deploy.
set -euo pipefail
cd "$(dirname "$0")/.."

SERVER=ubuntu@203.0.113.10
KEY=${DIALER_SSH_KEY:-$HOME/.ssh/dialmind-key.pem}
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ServerAliveInterval=30 "$SERVER")

[ -z "$(git status --porcelain)" ] || { echo "Commit your changes first (git status isn't clean)." >&2; exit 1; }
TAG=$(git rev-parse --short HEAD)

echo "== Sending commit $TAG"
git archive --format=tar.gz HEAD .dockerignore shared dashboard/backend dashboard/frontend config/agent_settings.yaml deploy |
    "${SSH[@]}" 'rm -rf /tmp/dialmind-build && mkdir /tmp/dialmind-build && tar xz -C /tmp/dialmind-build'

echo "== Building on the server and starting"
"${SSH[@]}" "set -e
cd /tmp/dialmind-build
docker build --quiet -f deploy/Dockerfile.backend -t dialmind-backend:$TAG .
docker build --quiet -f deploy/Dockerfile.frontend -t dialmind-frontend:$TAG .
install -m 644 deploy/docker-compose.yml deploy/Caddyfile /srv/dialmind/app/
install -m 755 deploy/backup_db.sh deploy/switch.sh deploy/users.sh /srv/dialmind/app/
cd /srv/dialmind/app
rm -rf /tmp/dialmind-build
touch .env
sed -i '/^RELEASE_TAG=/d; /^DIALER_HOSTNAME=/d' .env
echo 'RELEASE_TAG=$TAG' >>.env
[ -s /srv/dialmind/secrets/.env ] || { echo 'Missing /srv/dialmind/secrets/.env' >&2; exit 1; }
docker compose up -d --remove-orphans
# The Caddyfile is one mounted file: a new version only shows after re-creating the container
docker compose up -d --force-recreate caddy
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
docker builder prune -af >/dev/null
docker image prune -f >/dev/null"
echo "== Done: https://dialer.example.com"
