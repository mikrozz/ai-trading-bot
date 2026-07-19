# Runbook

## Smoke fail

1. Проверить `BINANCE_BASE_URL=https://testnet.binance.vision`
2. Проверить права файла env `600`
3. Account без `omitZeroBalances` может таймаутиться — клиент всегда шлёт флаг
4. После reset testnet — пересоздать ключ при необходимости

## Ingest нет сообщений

1. Проверить доступ к WS (`MARKET_DATA_MODE`)
2. Firewall / DNS
3. Логи `ws_disconnected`

## Kill-switch

Сбрасывается только оператором после разбора (не автоматически стратегией).  
Сейчас флаг в `RiskState.kill_switch` — persistent store будет добавлен.
