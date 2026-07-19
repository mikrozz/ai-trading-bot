# AI Trading Bot MVP

Автономный spot-контур под **Binance Spot Testnet** (ТЗ MVP v0.2).

## Что есть сейчас

- Exchange adapter `BinanceSpotClient` (REST, signed)
- Hard risk gate (DD / position / listing ban / kill-switch / stop-loss)
- Order manager (paper / testnet)
- WS ingest → Redis Streams → batch writer → TimescaleDB
- Feature engineering (24 признака на klines)
- Historical bootstrap klines → TimescaleDB
- XGBoost walk-forward train + paper backfill
- Docker Compose: Redis + TimescaleDB
- CLI: `smoke`, `ingest`, `writer`, `pipeline`, `features`, `bootstrap`, `train`, `paper`

## Секреты

Ключи **не в git**. Локально:

```bash
mkdir -p ~/.config/trading-bot && chmod 700 ~/.config/trading-bot
# файл ~/.config/trading-bot/binance_testnet.env (chmod 600)
```

См. `.env.example`.

## Быстрый старт

```bash
cd /opt/ai-trading-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

trading-bot smoke
trading-bot risk-demo
trading-bot features --symbol BTCUSDT --interval 5m
pytest -q
```

Инфра:

```bash
docker compose up -d redis timescaledb
trading-bot pipeline --seconds 20   # ingest + writer E2E
trading-bot bootstrap --months 6 --interval 5m
trading-bot train --symbol BTCUSDT --interval 5m
trading-bot paper --symbol BTCUSDT --model data/models/xgb_btc_5m.joblib
```

> `train`/`paper` — research tools. Sharpe/PnL не являются критерием приёмки MVP.

## Режимы

| Режим | Назначение |
|-------|------------|
| `execution_mode=testnet` | Ордера на testnet.binance.vision |
| `execution_mode=paper` | Virtual portfolio, без ордеров |
| `market_data_mode=prod_public` | Публичные prod WS/REST для данных |

## Документы

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [DEPLOYMENT.md](DEPLOYMENT.md)
- [TRADING.md](TRADING.md)
- [RUNBOOK.md](RUNBOOK.md)
