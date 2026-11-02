#!/bin/sh
# First start: the settings file on the data disk doesn't exist yet, so start from the copy in the image.
set -e
if [ -n "$AGENT_SETTINGS_PATH" ] && [ ! -f "$AGENT_SETTINGS_PATH" ]; then
    cp /app/config/agent_settings.yaml "$AGENT_SETTINGS_PATH"
    echo "First start: copied agent_settings.yaml to $AGENT_SETTINGS_PATH"
fi
# Exactly one worker: the dialer, supervisor pop-ups and live streams are held in memory.
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --timeout-graceful-shutdown 30
