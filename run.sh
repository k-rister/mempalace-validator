#!/usr/bin/env bash
# Manual foreground start (use the systemd user service for day-to-day).
exec /usr/bin/python3 "$(dirname "$0")/proxy.py" "$@"
