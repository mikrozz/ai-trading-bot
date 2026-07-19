from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from trading_bot.features.engineering import (
    FEATURE_COLUMNS,
    ORDERBOOK_FEATURE_COLUMNS,
    attach_orderbook_features,
    build_feature_frame,
)


def test_feature_count_and_columns() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for i in range(80):
        price *= 1.001 if i % 2 == 0 else 0.999
        rows.append(
            {
                "ts": start + timedelta(minutes=5 * i),
                "open": price,
                "high": price * 1.002,
                "low": price * 0.998,
                "close": price,
                "volume": 10 + i,
            }
        )
    df = build_feature_frame(pd.DataFrame(rows))
    present = [c for c in FEATURE_COLUMNS if c in df.columns]
    assert len(present) >= 15
    ready = df.dropna(subset=["rsi", "macd", "realized_vol"])
    assert len(ready) > 0


def test_attach_orderbook_features_asof() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    klines = pd.DataFrame(
        [
            {
                "ts": start + timedelta(minutes=5 * i),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0 + i,
                "volume": 10.0,
            }
            for i in range(5)
        ]
    )
    books = pd.DataFrame(
        [
            {
                "ts": start + timedelta(minutes=5 * i, seconds=30),
                "bid_price": 100.0 + i - 0.1,
                "bid_qty": 2.0,
                "ask_price": 100.0 + i + 0.1,
                "ask_qty": 1.0,
            }
            for i in range(5)
        ]
    )
    # asof backward: bar ts=T берёт book с ts<=T; book в T+30s попадает в следующий бар
    out = attach_orderbook_features(klines, books)
    for col in ORDERBOOK_FEATURE_COLUMNS:
        assert col in out.columns
    assert float(out.loc[1, "ob_spread_bps"]) > 0
    assert float(out.loc[1, "ob_imbalance"]) > 0
    empty = attach_orderbook_features(klines, None)
    assert float(empty["ob_spread_bps"].sum()) == 0.0
