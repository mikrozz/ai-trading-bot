from __future__ import annotations

from trading_bot.storage.batch_writer import KlineRow, TradeRow, parse_event


def test_parse_trade() -> None:
    row = parse_event(
        "trade",
        {
            "e": "trade",
            "s": "BTCUSDT",
            "t": 123,
            "p": "100.5",
            "q": "0.01",
            "T": 1_700_000_000_000,
            "m": True,
        },
    )
    assert isinstance(row, TradeRow)
    assert row.symbol == "BTCUSDT"
    assert row.trade_id == 123
    assert row.is_buyer_maker is True


def test_parse_kline() -> None:
    row = parse_event(
        "kline",
        {
            "e": "kline",
            "s": "ETHUSDT",
            "k": {
                "t": 1_700_000_000_000,
                "s": "ETHUSDT",
                "i": "1m",
                "o": "1",
                "h": "2",
                "l": "0.5",
                "c": "1.5",
                "v": "10",
                "x": True,
            },
        },
    )
    assert isinstance(row, KlineRow)
    assert row.interval == "1m"
    assert row.close == 1.5


def test_parse_book_ticker_ignored() -> None:
    assert parse_event("bookTicker", {"s": "BTCUSDT", "b": "1", "a": "2"}) is None
