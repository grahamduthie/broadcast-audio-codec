#!/usr/bin/env bash
# Run briclite on macOS.
# Copy config.mac.json to config.json (after filling in device UIDs), then run this.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export BRICLITE_HOME="$SCRIPT_DIR/briclite"

cd "$SCRIPT_DIR/briclite"
exec "$SCRIPT_DIR/.venv/bin/python3" -m uvicorn main:app \
    --host 127.0.0.1 \
    --port 8080 \
    --reload
