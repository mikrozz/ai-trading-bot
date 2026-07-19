#!/usr/bin/env bash
# Макро-календарь (Forex Factory week JSON) → configs/events.yaml + Telegram.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="$ROOT/.venv/bin:${PATH:-/usr/bin}"
exec "$ROOT/.venv/bin/trading-bot" --metrics-port 0 event-watcher "$@"
