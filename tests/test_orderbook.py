from __future__ import annotations

from trading_bot.features.orderbook import book_ticker_from_payload, orderbook_feature_dict
from trading_bot.storage.batch_writer import BookTickerRow, parse_event


def test_book_ticker_features() -> None:
    book = book_ticker_from_payload(
        {"s": "BTCUSDT", "b": "100", "B": "2", "a": "101", "A": "1", "E": 1_700_000_000_000}
    )
    assert book is not None
    feats = orderbook_feature_dict(book)
    assert feats["ob_spread"] == 1.0
    assert feats["ob_imbalance"] > 0
    assert 100 < feats["ob_microprice"] < 101


def test_parse_book_ticker_event() -> None:
    row = parse_event(
        "btcusdt@bookTicker",
        {"s": "ETHUSDT", "b": "10", "B": "5", "a": "10.1", "A": "5", "E": 1_700_000_000_000},
    )
    assert isinstance(row, BookTickerRow)
    assert row.symbol == "ETHUSDT"
