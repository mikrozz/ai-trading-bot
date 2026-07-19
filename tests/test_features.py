from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from trading_bot.features.engineering import FEATURE_COLUMNS, build_feature_frame


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
