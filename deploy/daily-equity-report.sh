#!/usr/bin/env bash
# Дневной отчёт equity (testnet-live) → data/reports/ + Telegram.
# Telegram: прямой Bot API (token file network-monitor) через tinyproxy,
# fallback — POST Alertmanager-compatible payload на :9999 webhook.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="$ROOT/.venv/bin:${PATH:-/usr/bin}"
exec "$ROOT/.venv/bin/trading-bot" --metrics-port 0 daily-equity-report "$@"
