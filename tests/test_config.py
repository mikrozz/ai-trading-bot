from __future__ import annotations

from pathlib import Path

from trading_bot.config import build_settings


def test_default_yaml_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = build_settings(root / "configs" / "default.yaml", env_file=Path("/nonexistent"))
    assert settings.exchange == "binance_spot"
    assert "BTCUSDT" in settings.symbols
    assert settings.risk.daily_drawdown_limit == 0.05
