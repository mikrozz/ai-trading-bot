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

## Prometheus (compose)

```bash
docker compose --profile monitoring up -d prometheus
# UI: http://127.0.0.1:9090  (не публиковать наружу без VPN)
```

Scrape: `host.docker.internal:9108` → процесс `trading-bot` на хосте.
