"""Hardcoded risk gates — AI не может отключить."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


class RiskDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    KILL = "kill"


@dataclass(slots=True)
class RiskLimits:
    daily_drawdown_limit: float = 0.05
    weekly_drawdown_limit: float = 0.10
    max_position_fraction: float = 0.10
    max_open_positions: int = 5
    stop_loss: float = 0.02
    listing_ban_minutes: int = 5


@dataclass
class RiskState:
    equity: float
    day_start_equity: float
    week_start_equity: float
    open_positions: int = 0
    kill_switch: bool = False
    # symbol -> first_trade_available_at (UTC)
    listing_available_at: dict[str, datetime] = field(default_factory=dict)
    # symbol -> position notional in quote
    position_notionals: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RiskCheckResult:
    decision: RiskDecision
    reason: str


class HardRiskGate:
    """Жёсткие правила риск-менеджмента (не отключаются стратегией/ML)."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def _drawdown(self, start: float, current: float) -> float:
        if start <= 0:
            return 1.0
        return max(0.0, (start - current) / start)

    def evaluate_kill_switch(self, state: RiskState) -> RiskCheckResult | None:
        if state.kill_switch:
            return RiskCheckResult(RiskDecision.KILL, "kill_switch_active")

        daily_dd = self._drawdown(state.day_start_equity, state.equity)
        if daily_dd >= self.limits.daily_drawdown_limit:
            state.kill_switch = True
            return RiskCheckResult(
                RiskDecision.KILL,
                f"daily_drawdown={daily_dd:.4f}>={self.limits.daily_drawdown_limit}",
            )

        weekly_dd = self._drawdown(state.week_start_equity, state.equity)
        if weekly_dd >= self.limits.weekly_drawdown_limit:
            state.kill_switch = True
            return RiskCheckResult(
                RiskDecision.KILL,
                f"weekly_drawdown={weekly_dd:.4f}>={self.limits.weekly_drawdown_limit}",
            )
        return None

    def check_new_order(
        self,
        state: RiskState,
        *,
        symbol: str,
        order_notional: float,
        now: datetime | None = None,
        is_new_position: bool = True,
    ) -> RiskCheckResult:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        kill = self.evaluate_kill_switch(state)
        if kill is not None:
            return kill

        available_at = state.listing_available_at.get(symbol.upper())
        if available_at is not None:
            if available_at.tzinfo is None:
                available_at = available_at.replace(tzinfo=timezone.utc)
            if now < available_at:
                return RiskCheckResult(
                    RiskDecision.DENY,
                    f"listing_ban_until={available_at.isoformat()}",
                )

        if is_new_position and state.open_positions >= self.limits.max_open_positions:
            return RiskCheckResult(
                RiskDecision.DENY,
                f"max_open_positions={self.limits.max_open_positions}",
            )

        if state.equity <= 0:
            return RiskCheckResult(RiskDecision.DENY, "equity_non_positive")

        # fraction только на открытие/увеличение; закрытие (SELL) не режем
        if is_new_position:
            max_notional = state.equity * self.limits.max_position_fraction
            current = state.position_notionals.get(symbol.upper(), 0.0)
            if current + order_notional > max_notional + 1e-9:
                return RiskCheckResult(
                    RiskDecision.DENY,
                    f"position_fraction order={order_notional:.4f} "
                    f"current={current:.4f} max={max_notional:.4f}",
                )

        return RiskCheckResult(RiskDecision.ALLOW, "ok")

    def check_stop_loss(
        self,
        *,
        entry_price: float,
        mark_price: float,
        side: str,
    ) -> RiskCheckResult:
        if entry_price <= 0 or mark_price <= 0:
            return RiskCheckResult(RiskDecision.DENY, "invalid_price")
        side_u = side.upper()
        if side_u == "BUY":
            pnl_frac = (mark_price - entry_price) / entry_price
        else:
            pnl_frac = (entry_price - mark_price) / entry_price
        if pnl_frac <= -self.limits.stop_loss:
            return RiskCheckResult(
                RiskDecision.DENY,
                f"stop_loss_hit pnl={pnl_frac:.4f}",
            )
        return RiskCheckResult(RiskDecision.ALLOW, "ok")

    @staticmethod
    def listing_available_from(
        listed_at: datetime,
        ban_minutes: int,
    ) -> datetime:
        if listed_at.tzinfo is None:
            listed_at = listed_at.replace(tzinfo=timezone.utc)
        return listed_at + timedelta(minutes=ban_minutes)
