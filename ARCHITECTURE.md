# Architecture

```text
Binance WS (prod public) ──► ingest ──► Redis Streams (hot)
                                      └► batch writer ──► TimescaleDB (cold)

Signals / model (later) ──► HardRiskGate ──► OrderManager
                                              ├─ paper
                                              └─ BinanceSpot (testnet/live)
```

## Компоненты

- `trading_bot.exchange` — адаптер биржи (сейчас Binance Spot)
- `trading_bot.marketdata` — WS ingest с reconnect/backoff
- `trading_bot.storage` — Redis Streams publisher
- `trading_bot.risk` — hardcoded limits, kill-switch
- `trading_bot.execution` — gate + place/cancel path

## Принципы MVP

1. Hot path не ждёт commit в Postgres.
2. Risk gate нельзя отключить стратегией.
3. Testnet ≠ research truth; PnL/Sharpe с testnet не acceptance.
4. Adapter interface — задел под MEXC fallback.
