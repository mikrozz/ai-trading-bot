#!/usr/bin/env bash
# Синхронизация dashboard/scrape targets/rules в центральный monitoring (/root/monitoring).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_DASH=/root/monitoring/grafana/dashboards/ai-trading-bot.json
DEST_SD=/root/monitoring/prometheus/file_sd/ai-trading-bot.json
DEST_RULES=/root/monitoring/prometheus/rules/ai-trading-bot.yml

cp -f "$ROOT/monitoring/grafana/dashboards/trading-bot.json" "$DEST_DASH"
cp -f "$ROOT/monitoring/prometheus/rules/ai-trading-bot.yml" "$DEST_RULES"
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

# validate + reload prometheus if lifecycle enabled
if curl -fsS http://127.0.0.1:9091/-/healthy >/dev/null 2>&1; then
  curl -fsS -X POST http://127.0.0.1:9091/-/reload >/dev/null && echo "prometheus reloaded" || echo "prometheus reload skipped"
else
  echo "prometheus not healthy — rules copied, reload later"
fi
echo "SYNC_OK dashboard=$DEST_DASH rules=$DEST_RULES"
echo "Open: http://192.168.10.155:3001/d/ai-trading-bot/ai-trading-bot"
