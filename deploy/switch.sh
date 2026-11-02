#!/usr/bin/env bash
# THE switch between laptop and server. Runs on the server: /srv/dialmind/app/switch.sh <command>
#
#   status     where things stand; reads back where Cartesia + Telnyx point (changes nothing)
#   live       THE switch-over: standby off, so the backend points the Cartesia webhook, the 3 tools and the Telnyx
#              messaging webhook at PUBLIC_BASE_URL. Calls, texts and emails stay disabled.
#   calls-on   allow calls (test mode still decides where they go)
#   calls-off  no calls
#   sms-on     allow follow-up texts (test mode decides where they go; the rules in app/sms.py still apply)
#   sms-off    no texts
#   standby    back to standby + no calls, no texts. Does NOT move the webhooks back: starting the laptop does that.
#
# The state is in /srv/dialmind/app/.env, and docker-compose.yml falls back to the safe value when it's missing.
set -euo pipefail
cd "$(dirname "$0")"

MARKER=${DIALER_ROOT:-/srv/dialmind}/data/.copied-from-laptop

set_flag() {
    touch .env
    sed -i "/^$1=/d" .env
    echo "$1=$2" >>.env
}

restart_backend() {
    docker compose up -d backend
    echo "Waiting for the backend..."
    for _ in $(seq 1 30); do
        [ "$(docker inspect -f '{{.State.Health.Status}}' dialmind-backend-1 2>/dev/null)" = healthy ] && return 0
        sleep 2
    done
    echo "The backend isn't healthy yet: docker compose logs --tail=50 backend" >&2
    return 1
}

links() {
    docker compose exec -T backend python check_links.py
}

flag_on() {  # usage: flag_on NAME; anything but "0" (or the safe default: missing) counts as on
    [ "$(grep -s "^$1=" .env | cut -d= -f2)" != 0 ]
}

case "${1:-}" in
status)
    links
    ;;
live)
    if [ ! -f "$MARKER" ]; then
        echo "Refused: the laptop's data hasn't been copied here yet (deploy/copy_data.sh). Without it the Do-Not-Contact list is empty." >&2
        exit 1
    fi
    echo "This moves the Cartesia webhook, the 3 tools and the Telnyx messaging webhook to this server."
    echo "The laptop must be STOPPED, or it will pull them back."
    read -r -p "Type LIVE to go on: " answer
    [ "$answer" = LIVE ] || { echo "Nothing changed."; exit 1; }
    set_flag DIALER_STANDBY 0
    set_flag CARTESIA_CALLS_DISABLED 1
    restart_backend
    echo "Giving the backend time to point everything here..."
    sleep 15
    links
    ;;
calls-on)
    if flag_on DIALER_STANDBY; then
        echo "Refused: still in standby. Run: switch.sh live" >&2
        exit 1
    fi
    set_flag CARTESIA_CALLS_DISABLED 0
    restart_backend
    links
    ;;
calls-off)
    set_flag CARTESIA_CALLS_DISABLED 1
    restart_backend
    links
    ;;
sms-on)
    if flag_on DIALER_STANDBY; then
        echo "Refused: still in standby. Run: switch.sh live" >&2
        exit 1
    fi
    set_flag SMS_SENDING_DISABLED 0
    restart_backend
    links
    ;;
sms-off)
    set_flag SMS_SENDING_DISABLED 1
    restart_backend
    links
    ;;
standby)
    set_flag DIALER_STANDBY 1
    set_flag CARTESIA_CALLS_DISABLED 1
    set_flag SMS_SENDING_DISABLED 1
    restart_backend
    links
    echo "Standby. Cartesia + Telnyx still point here until the laptop starts (.\\start_all.ps1)."
    ;;
*)
    sed -n 2,13p "$0"
    exit 2
    ;;
esac
