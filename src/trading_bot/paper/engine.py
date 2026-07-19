"""Paper trading: virtual portfolio на сигналах модели + hard risk."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trading_bot.features.engineering import build_feature_frame
from trading_bot.logging_setup import get_logger
from trading_bot.ml.train_xgb import load_model
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
        prob_threshold: float = 0.55,
        risk_limits: RiskLimits | None = None,
    ) -> None:
        payload = load_model(model_path)
        self.model = payload["model"]
        self.feature_names: list[str] = list(payload["features"])
        self.symbol = symbol.upper()
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.prob_threshold = prob_threshold
        self.position_fraction = position_fraction
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
        self.risk_state.open_positions = 0
        self.risk_state.position_notionals.pop(pos.symbol, None)
        self._update_equity(close)
        log.debug("paper_close", reason=reason, price=px, equity=self.state.equity)

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
        self.risk_state.open_positions = 1
        self.risk_state.position_notionals[self.symbol] = notional
        self._update_equity(close)
        log.debug("paper_open", price=px, qty=qty, equity=self.state.equity)

    def on_bar(self, history: pd.DataFrame) -> dict[str, Any]:
        """Один бар (live path): фичи по хвосту истории."""
        self.state.bars_seen += 1
        feats = build_feature_frame(history)
        row = feats.iloc[[-1]]
        close = float(history.iloc[-1]["close"])
        ts = pd.Timestamp(history.iloc[-1]["ts"]).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        if row[self.feature_names].isna().any(axis=None):
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
        if proba >= self.prob_threshold:
            self.state.signals_long += 1
            if self.state.position is None:
                self._open_position(close, ts)
                return {"action": "open", "proba": proba, "equity": self.state.equity}
        else:
            self.state.signals_flat += 1
            if self.state.position is not None:
                self._close_position(close, ts, "model_flat")
                return {"action": "close", "proba": proba, "equity": self.state.equity}

        self._update_equity(close)
        return {"action": "hold", "proba": proba, "equity": self.state.equity}

    def run_backfill(self, klines: pd.DataFrame) -> PaperState:
        """Прогон paper по истории: фичи один раз + batch predict."""
        if len(klines) < 50:
            raise ValueError("Need >=50 bars for paper backfill")

        feats = build_feature_frame(klines.reset_index(drop=True))
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
            if proba >= self.prob_threshold:
                self.state.signals_long += 1
                if self.state.position is None:
                    self._open_position(close, ts)
                else:
                    self._update_equity(close)
            else:
                self.state.signals_flat += 1
                if self.state.position is not None:
                    self._close_position(close, ts, "model_flat")
                else:
                    self._update_equity(close)

        last = feats.iloc[-1]
        ts = pd.Timestamp(last["ts"]).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if self.state.position is not None:
            self._close_position(float(last["close"]), ts, "end_of_run")
        return self.state
