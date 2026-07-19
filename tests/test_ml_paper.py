from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from trading_bot.ml.train_xgb import prepare_xy, train_walk_forward
from trading_bot.paper.engine import PaperEngine


def _synth_klines(n: int = 1200) -> pd.DataFrame:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for i in range(n):
        # слабый mean-reversion + шум
        price *= 1.0 + (0.001 if i % 17 < 8 else -0.0008)
        rows.append(
            {
                "ts": start + timedelta(minutes=5 * i),
                "open": price,
                "high": price * 1.001,
                "low": price * 0.999,
                "close": price,
                "volume": 50 + (i % 20),
            }
        )
    return pd.DataFrame(rows)


def test_prepare_xy_has_rows() -> None:
    x, y, cols = prepare_xy(_synth_klines(300))
    assert len(cols) >= 15
    assert len(x) > 50
    assert set(y.unique()).issubset({0, 1})


def test_train_and_paper(tmp_path: Path) -> None:
    df = _synth_klines(1500)
    model_path = tmp_path / "m.joblib"
    _model, result = train_walk_forward(
        df, n_folds=3, min_train_rows=200, model_out=model_path
    )
    assert model_path.exists()
    assert len(result.folds) >= 1
    engine = PaperEngine(
        model_path=model_path,
        symbol="BTCUSDT",
        initial_cash=1000.0,
        prob_threshold=0.5,
        min_hold_bars=3,
        cooldown_bars=2,
    )
    state = engine.run_backfill(df)
    assert state.bars_seen > 0
    assert state.equity > 0


def test_min_hold_reduces_churn(tmp_path: Path) -> None:
    df = _synth_klines(800)
    model_path = tmp_path / "m2.joblib"
    train_walk_forward(df, n_folds=3, min_train_rows=150, model_out=model_path)
    aggressive = PaperEngine(
        model_path=model_path,
        symbol="BTCUSDT",
        initial_cash=1000.0,
        prob_threshold=0.45,
        min_hold_bars=1,
        cooldown_bars=0,
    )
    calm = PaperEngine(
        model_path=model_path,
        symbol="BTCUSDT",
        initial_cash=1000.0,
        prob_threshold=0.45,
        min_hold_bars=12,
        cooldown_bars=6,
    )
    a = aggressive.run_backfill(df.copy())
    c = calm.run_backfill(df.copy())
    assert len(c.fills) <= len(a.fills)
