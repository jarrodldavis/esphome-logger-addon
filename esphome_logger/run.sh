#!/usr/bin/env bash
# Thin launcher; the real logic is in main.py so that asyncio + Python's
# logging.handlers can manage rotation, retention and multiple concurrent
# device streams cleanly.
set -eu

CONFIG_PATH=/data/options.json

if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "FATAL: $CONFIG_PATH is missing - is this running under HA Supervisor?" >&2
    exit 1
fi

exec python3 -u /opt/main.py
