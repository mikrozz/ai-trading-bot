"""Paper trading: virtual portfolio на сигналах модели + hard risk."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trading_bot.features.engineering import (
    ORDERBOOK_FEATURE_COLUMNS,
    attach_orderbook_features,
    build_feature_frame,
    inject_live_orderbook,
)
from trading_bot.logging_setup import get_logger
from trading_bot.metrics import EVENT_BLACKOUT
from trading_bot.ml.train_xgb import load_model
from trading_bot.risk.event_blackout import EventBlackoutGuard
from trading_bot.risk.gates import HardRiskGate, RiskDecision, RiskLimits, RiskState

log = get_logger(__name__)


@dataclass
class PaperPosition:
    symbol: str
    qty: float
    entry_price: float
    opened_at: datetime
    notional: float


@dataclass
class PaperFill:
    ts: datetime
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    reason: str


@dataclass
class PaperState:
    cash: float
    equity: float
    position: PaperPosition | None = None
    fills: list[PaperFill] = field(default_factory=list)
    bars_seen: int = 0
    signals_long: int = 0
    signals_flat: int = 0


class PaperEngine:
    def __init__(
        self,
        *,
        model_path: Path,
        symbol: str,
        initial_cash: float = 1000.0,
        position_fraction: float = 0.10,
        fee_rate: float = 0.001,
        slippage: float = 0.0005,
        prob_threshold: float = 0.60,
        min_hold_bars: int = 6,
        cooldown_bars: int = 3,
        risk_limits: RiskLimits | None = None,
        event_blackout: EventBlackoutGuard | None = None,
    ) -> None:
        payload = load_model(model_path)
        self.model = payload["model"]
        self.feature_names: list[str] = list(payload["features"])
        self.symbol = symbol.upper()
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.prob_threshold = prob_threshold
        self.position_fraction = position_fraction
        self.min_hold_bars = max(1, int(min_hold_bars))
        self.cooldown_bars = max(0, int(cooldown_bars))
        self.bars_in_position = 0
        self.bars_since_close = self.cooldown_bars  # сразу можно открыть
        self.event_blackout = event_blackout
        self.risk_gate = HardRiskGate(risk_limits or RiskLimits())
        self.state = PaperState(cash=initial_cash, equity=initial_cash)
        self.risk_state = RiskState(
            equity=initial_cash,
            day_start_equity=initial_cash,
            week_start_equity=initial_cash,
            open_positions=0,
        )

    def _mark_price(self, close: float, side: str) -> float:
        if side.upper() == "BUY":
            return close * (1.0 + self.slippage)
        return close * (1.0 - self.slippage)

    def _update_equity(self, mark: float) -> None:
        pos_value = 0.0
        if self.state.position is not None:
            pos_value = self.state.position.qty * mark
        self.state.equity = self.state.cash + pos_value
        self.risk_state.equity = self.state.equity

    def _close_position(self, close: float, ts: datetime, reason: str) -> None:
        pos = self.state.position
        if pos is None:
            return
        px = self._mark_price(close, "SELL")
        proceeds = pos.qty * px
        fee = proceeds * self.fee_rate
        self.state.cash += proceeds - fee
        self.state.fills.append(PaperFill(ts, pos.symbol, "SELL", pos.qty, px, fee, reason))
        self.state.position = None
        self.bars_in_position = 0
        self.bars_since_close = 0
        self.risk_state.open_positions = 0
        self.risk_state.position_notionals.pop(pos.symbol, None)
        self._update_equity(close)
        log.debug("paper_close", reason=reason, price=px, equity=self.state.equity)

    def _apply_signal(self, close: float, ts: datetime, proba: float) -> dict[str, Any]:
        """Вход/выход с учётом min_hold и cooldown (снижает turnover)."""
        if self.state.position is not None:
            self.bars_in_position += 1
        else:
            self.bars_since_close += 1

        if proba >= self.prob_threshold:
            self.state.signals_long += 1
            if self.state.position is None:
                if self.bars_since_close < self.cooldown_bars:
                    self._update_equity(close)
                    return {
                        "action": "cooldown",
                        "proba": proba,
                        "equity": self.state.equity,
                    }
                if self.event_blackout is not None:
                    win = self.event_blackout.should_block_open(ts)
                    if win is not None:
                        EVENT_BLACKOUT.labels(
                            symbol=self.symbol, event=win.event.name, mode="paper"
                        ).inc()
                        log.info(
                            "event_blackout",
                            event=win.event.name,
                            event_at=win.event.at_utc.isoformat(),
                            window_start=win.start.isoformat(),
                            window_end=win.end.isoformat(),
                            symbol=self.symbol,
                            mode="paper",
                        )
                        self._update_equity(close)
                        return {
                            "action": "event_blackout",
                            "proba": proba,
                            "equity": self.state.equity,
                            "event": win.event.name,
                            "window_start": win.start.isoformat(),
                            "window_end": win.end.isoformat(),
                        }
                self._open_position(close, ts)
                return {"action": "open", "proba": proba, "equity": self.state.equity}
            self._update_equity(close)
            return {"action": "hold", "proba": proba, "equity": self.state.equity}

        self.state.signals_flat += 1
        if self.state.position is not None:
            if self.bars_in_position < self.min_hold_bars:
                self._update_equity(close)
                return {
                    "action": "min_hold",
                    "proba": proba,
                    "equity": self.state.equity,
                    "bars_in_position": self.bars_in_position,
                }
            self._close_position(close, ts, "model_flat")
            return {"action": "close", "proba": proba, "equity": self.state.equity}

        self._update_equity(close)
        return {"action": "hold", "proba": proba, "equity": self.state.equity}

    def _open_position(self, close: float, ts: datetime) -> None:
        if self.state.position is not None:
            return
        notional = self.state.equity * self.position_fraction
        check = self.risk_gate.check_new_order(
            self.risk_state,
            symbol=self.symbol,
            order_notional=notional,
            is_new_position=True,
            now=ts,
        )
        if check.decision != RiskDecision.ALLOW:
            log.debug("paper_skip", reason=check.reason)
            return

        px = self._mark_price(close, "BUY")
        if px <= 0:
            return
        fee = notional * self.fee_rate
        if self.state.cash < notional + fee:
            return
        qty = notional / px
        self.state.cash -= notional + fee
        self.state.position = PaperPosition(
            symbol=self.symbol,
            qty=qty,
            entry_price=px,
            opened_at=ts,
            notional=notional,
        )
        self.state.fills.append(PaperFill(ts, self.symbol, "BUY", qty, px, fee, "model_long"))
        self.bars_in_position = 0
        self.risk_state.open_positions = 1
        self.risk_state.position_notionals[self.symbol] = notional
        self._update_equity(close)
        log.debug("paper_open", price=px, qty=qty, equity=self.state.equity)

    def on_bar(
        self,
        history: pd.DataFrame,
        *,
        orderbook: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Один бар (live path): фичи по хвосту истории + опциональный стакан."""
        self.state.bars_seen += 1
        feats = build_feature_frame(history)
        for col in ORDERBOOK_FEATURE_COLUMNS:
            if col not in feats.columns:
                feats[col] = 0.0
        row = feats.iloc[[-1]].copy()
        close = float(history.iloc[-1]["close"])
        ts = pd.Timestamp(history.iloc[-1]["ts"]).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        if orderbook:
            row = inject_live_orderbook(
                row,
                close=close,
                spread_bps=float(orderbook.get("ob_spread_bps", 0.0)),
                imbalance=float(orderbook.get("ob_imbalance", 0.0)),
                microprice=float(orderbook.get("ob_microprice", close)),
                bid_qty=float(orderbook.get("ob_bid_qty", 0.0)),
                ask_qty=float(orderbook.get("ob_ask_qty", 0.0)),
            )

        needed = [c for c in self.feature_names if c in row.columns]
        missing_cols = [c for c in self.feature_names if c not in row.columns]
        for col in missing_cols:
            row[col] = 0.0
        if row[needed].isna().any(axis=None):
            self._update_equity(close)
            return {"action": "warmup", "equity": self.state.equity}

        if self.state.position is not None:
            sl = self.risk_gate.check_stop_loss(
                entry_price=self.state.position.entry_price,
                mark_price=close,
                side="BUY",
            )
            if sl.decision != RiskDecision.ALLOW:
                self._close_position(close, ts, "stop_loss")
                return {"action": "stop_loss", "equity": self.state.equity}

        kill = self.risk_gate.evaluate_kill_switch(self.risk_state)
        if kill is not None:
            if self.state.position is not None:
                self._close_position(close, ts, "kill_switch")
            return {"action": "kill", "reason": kill.reason, "equity": self.state.equity}

        proba = float(self.model.predict_proba(row[self.feature_names])[0][1])
        return self._apply_signal(close, ts, proba)

    def run_backfill(
        self,
        klines: pd.DataFrame,
        books: pd.DataFrame | None = None,
    ) -> PaperState:
        """Прогон paper по истории: фичи один раз + batch predict."""
        if len(klines) < 50:
            raise ValueError("Need >=50 bars for paper backfill")

        enriched = attach_orderbook_features(klines.reset_index(drop=True), books)
        feats = build_feature_frame(enriched)
        for col in self.feature_names:
            if col not in feats.columns:
                feats[col] = 0.0
        start_i = 40
        x_all = feats.iloc[start_i:][self.feature_names]
        valid = ~x_all.isna().any(axis=1)
        # batch probabilities; NaN-строки заполним 0.0 (не торгуем)
        proba_all = np.zeros(len(x_all), dtype=float)
        if valid.any():
            proba_all[valid.to_numpy()] = self.model.predict_proba(x_all.loc[valid])[:, 1]

        for offset, (_, row) in enumerate(feats.iloc[start_i:].iterrows()):
            self.state.bars_seen += 1
            close = float(row["close"])
            ts = pd.Timestamp(row["ts"]).to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            if not bool(valid.iloc[offset]):
                self._update_equity(close)
                continue

            if self.state.position is not None:
                sl = self.risk_gate.check_stop_loss(
                    entry_price=self.state.position.entry_price,
                    mark_price=close,
                    side="BUY",
                )
                if sl.decision != RiskDecision.ALLOW:
                    self._close_position(close, ts, "stop_loss")
                    continue

            kill = self.risk_gate.evaluate_kill_switch(self.risk_state)
            if kill is not None:
                if self.state.position is not None:
                    self._close_position(close, ts, "kill_switch")
                continue

            proba = float(proba_all[offset])
            self._apply_signal(close, ts, proba)

        last = feats.iloc[-1]
        ts = pd.Timestamp(last["ts"]).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if self.state.position is not None:
            self._close_position(float(last["close"]), ts, "end_of_run")
        return self.state
