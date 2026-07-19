# Monitoring

Центральный стек (этот хост `192.168.10.155`):

| Сервис | URL |
|--------|-----|
| Grafana | http://192.168.10.155:3001 |
| Dashboard | http://192.168.10.155:3001/d/ai-trading-bot/ai-trading-bot |
| Prometheus | http://192.168.10.155:9091 |

## Метрики бота

systemd экспортеры на хосте:

| Порт | Сервис |
|------|--------|
| 9108 | `trading-bot-ingest` |
| 9109 | `trading-bot-writer` |
| 9110 | `trading-bot-paper-live` (stopped; legacy) |
| 9111 | `trading-bot-testnet-live` (active) |

```bash
curl -s http://192.168.10.155:9111/metrics | grep trading_live
systemctl status trading-bot-ingest trading-bot-writer trading-bot-testnet-live
```

## Daily equity report

- Script: `deploy/daily-equity-report.sh` / `trading-bot daily-equity-report`
- Local files: `data/reports/equity-BTCUSDT-YYYY-MM-DD.txt` and `...-latest.txt`
- Timer: `trading-bot-equity-report.timer` — ежедневно **09:00 MSK**
- Telegram: Bot API через token file `/opt/network-monitor/secrets/telegram_bot_token` + tinyproxy `:3128`; fallback POST на telegram-webhook `http://127.0.0.1:9999/` (тот же путь, что Alertmanager `telegram=yes`)

```bash
# dry-run (только файл)
bash /opt/ai-trading-bot/deploy/daily-equity-report.sh --dry-run
# реальная отправка
systemctl start trading-bot-equity-report.service
systemctl list-timers trading-bot-equity-report.timer
```

## Event watcher (macro calendar)

- Script: `deploy/event-watcher.sh` / `trading-bot event-watcher`
- Source: Forex Factory week JSON → merge `configs/events.manual.yaml` → write `configs/events.yaml`
- Telegram: новые / ближайшие high-impact USD (state: `data/event_watcher_state.json`)
- Timer: `trading-bot-event-watcher.timer` — каждые **6ч** (`:10`)
- Blackout в `testnet-live` перечитывает `events.yaml` по mtime (рестарт не нужен)

```bash
bash /opt/ai-trading-bot/deploy/event-watcher.sh --dry-run
bash /opt/ai-trading-bot/deploy/event-watcher.sh --no-telegram   # только обновить yaml
systemctl start trading-bot-event-watcher.service
systemctl list-timers trading-bot-event-watcher.timer
```

Scrape в центральный Prometheus:  
`/root/monitoring/prometheus/file_sd/ai-trading-bot.json`  
job `ai-trading-bot` в `/root/monitoring/prometheus/prometheus.yml`.

Dashboard provisioning:  
`/root/monitoring/grafana/dashboards/ai-trading-bot.json`  
(копия из `monitoring/grafana/dashboards/trading-bot.json` в репо).

После изменения дашборда в репо:

```bash
bash /opt/ai-trading-bot/deploy/sync-monitoring.sh
```

## Локальный compose profile `monitoring`

Опционален. Основной UI — Grafana на **:3001**. Локальный Grafana на :3003 отключён.
