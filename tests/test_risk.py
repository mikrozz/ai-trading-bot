"""Тесты hard risk gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading_bot.risk.gates import HardRiskGate, RiskDecision, RiskLimits, RiskState


def _state(equity: float = 1000.0, **kwargs) -> RiskState:
    return RiskState(
        equity=equity,
        day_start_equity=kwargs.pop("day_start_equity", equity),
        week_start_equity=kwargs.pop("week_start_equity", equity),
        **kwargs,
    )


def test_allow_small_order() -> None:
    gate = HardRiskGate(RiskLimits())
    result = gate.check_new_order(_state(), symbol="BTCUSDT", order_notional=50.0)
    assert result.decision == RiskDecision.ALLOW


def test_deny_position_fraction() -> None:
    gate = HardRiskGate(RiskLimits(max_position_fraction=0.10))
    result = gate.check_new_order(_state(), symbol="BTCUSDT", order_notional=200.0)
    assert result.decision == RiskDecision.DENY
    assert "position_fraction" in result.reason


def test_close_skips_position_fraction() -> None:
    gate = HardRiskGate(RiskLimits(max_position_fraction=0.10))
    state = _state(open_positions=1, position_notionals={"BTCUSDT": 100.0})
    result = gate.check_new_order(
        state,
        symbol="BTCUSDT",
        order_notional=100.0,
        is_new_position=False,
    )
    assert result.decision == RiskDecision.ALLOW


def test_deny_max_positions() -> None:
    gate = HardRiskGate(RiskLimits(max_open_positions=2))
    state = _state(open_positions=2)
    result = gate.check_new_order(state, symbol="ETHUSDT", order_notional=10.0)
    assert result.decision == RiskDecision.DENY
    assert "max_open_positions" in result.reason


def test_daily_drawdown_kill() -> None:
    gate = HardRiskGate(RiskLimits(daily_drawdown_limit=0.05))
    state = _state(equity=940.0, day_start_equity=1000.0)
    result = gate.check_new_order(state, symbol="BTCUSDT", order_notional=10.0)
    assert result.decision == RiskDecision.KILL
    assert state.kill_switch is True


def test_listing_ban() -> None:
    gate = HardRiskGate(RiskLimits(listing_ban_minutes=5))
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    available = now + timedelta(minutes=3)
    state = _state(listing_available_at={"NEWUSDT": available})
    result = gate.check_new_order(
        state, symbol="NEWUSDT", order_notional=10.0, now=now
    )
    assert result.decision == RiskDecision.DENY
    assert "listing_ban" in result.reason


def test_stop_loss_hit() -> None:
    gate = HardRiskGate(RiskLimits(stop_loss=0.02))
    result = gate.check_stop_loss(entry_price=100.0, mark_price=97.5, side="BUY")
    assert result.decision == RiskDecision.DENY
    assert "stop_loss" in result.reason
