from __future__ import annotations

from trading_bot.metrics import WS_MESSAGES, start_metrics_server


def test_metrics_inc() -> None:
    before = WS_MESSAGES.labels(symbol="BTCUSDT", event="trade")._value.get()
    WS_MESSAGES.labels(symbol="BTCUSDT", event="trade").inc()
    after = WS_MESSAGES.labels(symbol="BTCUSDT", event="trade")._value.get()
    assert after == before + 1


def test_start_metrics_idempotent() -> None:
    start_metrics_server(0)  # no-op
    start_metrics_server(0)
