from __future__ import annotations

from pathlib import Path

from trading_bot.config import build_settings


def test_default_yaml_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = build_settings(root / "configs" / "default.yaml", env_file=Path("/nonexistent"))
    assert settings.exchange == "binance_spot"
    assert "BTCUSDT" in settings.symbols
    assert settings.risk.daily_drawdown_limit == 0.05
    assert settings.paper.prob_threshold == 0.60
    assert settings.paper.min_hold_bars == 6
    assert settings.paper.cooldown_bars == 3
    assert settings.live_testnet.position_fraction == 0.05
    assert settings.live_testnet.max_orders_per_hour == 20
