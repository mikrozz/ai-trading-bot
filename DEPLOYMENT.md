# Deployment

## Локально

1. Python 3.10+, Docker
2. Секреты в `~/.config/trading-bot/binance_testnet.env`
3. `pip install -e ".[dev]"`
4. `docker compose up -d redis timescaledb`
5. `trading-bot smoke`

## Docker app profile

```bash
export BINANCE_ENV_FILE=$HOME/.config/trading-bot/binance_testnet.env
docker compose --profile app up -d --build
```

Prometheus/Grafana/Ansible — следующий этап после стабилизации ingest.

## Безопасность

- Не публиковать 5432/6379/9090/3000 в интернет без VPN
- Mainnet keys: trade only, withdraw disabled
- Secrets только через env-file / Vault / Passwork
