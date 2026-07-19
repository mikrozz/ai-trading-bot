#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT_DIR=/etc/systemd/system

cp -f "$ROOT/deploy/systemd/trading-bot-ingest.service" "$UNIT_DIR/"
cp -f "$ROOT/deploy/systemd/trading-bot-writer.service" "$UNIT_DIR/"
cp -f "$ROOT/deploy/systemd/trading-bot-paper-live.service" "$UNIT_DIR/"
cp -f "$ROOT/deploy/systemd/trading-bot-db-backup.service" "$UNIT_DIR/"
cp -f "$ROOT/deploy/systemd/trading-bot-db-backup.timer" "$UNIT_DIR/"
cp -f "$ROOT/deploy/systemd/trading-bot-soak.service" "$UNIT_DIR/"
cp -f "$ROOT/deploy/systemd/trading-bot-soak.timer" "$UNIT_DIR/"
cp -f "$ROOT/deploy/systemd/trading-bot-latency.service" "$UNIT_DIR/"
cp -f "$ROOT/deploy/systemd/trading-bot-latency.timer" "$UNIT_DIR/"
chmod +x "$ROOT/deploy/backup-timescaledb.sh" "$ROOT/deploy/restore-timescaledb.sh" "$ROOT/deploy/sync-monitoring.sh"
systemctl daemon-reload
systemctl enable --now \
  trading-bot-ingest.service \
  trading-bot-writer.service \
  trading-bot-paper-live.service \
  trading-bot-db-backup.timer \
  trading-bot-soak.timer \
  trading-bot-latency.timer
systemctl --no-pager --full status \
  trading-bot-ingest.service \
  trading-bot-writer.service \
  trading-bot-paper-live.service || true
systemctl --no-pager list-timers 'trading-bot-*.timer' || true
echo "INSTALLED: ingest + writer + paper-live + timers (backup/soak/latency)"
