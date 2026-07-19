"""Unit-тесты подписи Binance без сети."""

from __future__ import annotations

from trading_bot.exchange.binance_spot import BinanceSpotClient


def test_sign_stable() -> None:
    client = BinanceSpotClient(api_key="k", api_secret="secret", base_url="https://example")
    # Пример из документации Binance (упрощённый): детерминированность HMAC
    sig1 = client._sign({"symbol": "BTCUSDT", "timestamp": 1_700_000_000_000})
    sig2 = client._sign({"symbol": "BTCUSDT", "timestamp": 1_700_000_000_000})
    assert sig1 == sig2
    assert len(sig1) == 64
