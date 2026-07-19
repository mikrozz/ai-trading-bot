#!/usr/bin/env bash
# Синхронизация dashboard/scrape targets в центральный monitoring (/root/monitoring).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_DASH=/root/monitoring/grafana/dashboards/ai-trading-bot.json
DEST_SD=/root/monitoring/prometheus/file_sd/ai-trading-bot.json

cp -f "$ROOT/monitoring/grafana/dashboards/trading-bot.json" "$DEST_DASH"
cat >"$DEST_SD" <<'EOF'
[
  {
    "targets": [
      "192.168.10.155:9108",
      "192.168.10.155:9109",
      "192.168.10.155:9110"
    ],
    "labels": {
      "service": "ai-trading-bot",
      "env": "docker01"
    }
  }
]
EOF

# reload prometheus if lifecycle enabled
curl -fsS -X POST http://127.0.0.1:9091/-/reload >/dev/null && echo "prometheus reloaded" || echo "prometheus reload skipped"
echo "SYNC_OK dashboard=$DEST_DASH"
echo "Open: http://192.168.10.155:3001/d/ai-trading-bot/ai-trading-bot"
