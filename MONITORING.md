# Monitoring

## Bot metrics

CLI поднимает Prometheus endpoint:

```bash
trading-bot --metrics-port 9108 paper-live --seconds 300
curl -s localhost:9108/metrics | grep trading_
```

Ключевые метрики:
- `trading_ws_messages_total`
- `trading_writer_rows_total`
- `trading_orders_total`
- `trading_paper_equity`
- `trading_book_updates_total`
- `trading_risk_denies_total`

## Prometheus + Grafana (compose)

```bash
docker compose --profile monitoring up -d prometheus grafana
# Prometheus: http://127.0.0.1:9094
# Grafana:    http://127.0.0.1:3003  (admin/admin — сменить сразу)
```

Порты только на localhost (на этом хосте 9090–9093/3000–3002 заняты другими стеками).  
Dashboard **AI Trading Bot** подтягивается из provisioning.

Scrape targets:
- `host.docker.internal:9108` — ingest
- `host.docker.internal:9109` — writer

## systemd continuous pipeline

```bash
sudo bash /opt/ai-trading-bot/deploy/systemd/install.sh
systemctl status trading-bot-ingest trading-bot-writer trading-bot-paper-live
curl -s localhost:9108/metrics | grep trading_ws
curl -s localhost:9109/metrics | grep trading_writer
curl -s localhost:9110/metrics | grep trading_paper
# state: data/paper_state_BTCUSDT.json
```
