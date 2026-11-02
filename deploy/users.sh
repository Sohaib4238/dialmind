#!/usr/bin/env bash
# Dashboard logins on the server. Changes work at once. From the laptop:
#   ssh -t -i ~/.ssh/dialmind-key.pem ubuntu@203.0.113.10 /srv/dialmind/app/users.sh set NAME --role admin
# Commands: set NAME --role admin|supervisor (create, or reset the password) / list / remove NAME
set -euo pipefail
cd "$(dirname "$0")"
exec docker compose exec backend python manage_users.py "$@"
