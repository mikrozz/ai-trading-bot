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
| 9110 | `trading-bot-paper-live` |

```bash
curl -s http://192.168.10.155:9110/metrics | grep trading_paper
systemctl status trading-bot-ingest trading-bot-writer trading-bot-paper-live
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
