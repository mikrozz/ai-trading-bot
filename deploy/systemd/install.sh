#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT_DIR=/etc/systemd/system

cp -f "$ROOT/deploy/systemd/trading-bot-ingest.service" "$UNIT_DIR/"
cp -f "$ROOT/deploy/systemd/trading-bot-writer.service" "$UNIT_DIR/"
systemctl daemon-reload
systemctl enable --now trading-bot-ingest.service trading-bot-writer.service
systemctl --no-pager --full status trading-bot-ingest.service trading-bot-writer.service || true
echo "INSTALLED: trading-bot-ingest + trading-bot-writer"
