"""Testnet live: WS signals → hard risk → MARKET orders + account sync."""

from __future__ import annotations

import asyncio
import json
import signal
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from trading_bot.exchange.binance_spot import BinanceSpotClient
from trading_bot.execution.order_manager import OrderManager, PlaceOrderRequest
from trading_bot.execution.order_sync import append_audit, sync_order
from trading_bot.execution.soak import _dec_str, fetch_last_price, parse_symbol_filters
from trading_bot.features.engineering import (
    ORDERBOOK_FEATURE_COLUMNS,
    build_feature_frame,
    inject_live_orderbook,
)
from trading_bot.features.orderbook import orderbook_feature_dict
from trading_bot.logging_setup import get_logger
from trading_bot.marketdata.book_state import BookStateStore
from trading_bot.marketdata.ws_ingest import BinanceWsIngest
from trading_bot.metrics import (
    BOOK_UPDATES,
    EVENT_BLACKOUT,
    LIVE_EQUITY,
    LIVE_KILL_SWITCH,
    LIVE_POSITION_QTY,
    ORDERS_TOTAL,
    RISK_DENIES,
)
from trading_bot.ml.dataset import load_klines_df
from trading_bot.ml.train_xgb import load_model
from trading_bot.risk.event_blackout import EventBlackoutGuard
from trading_bot.risk.gates import HardRiskGate, RiskDecision, RiskLimits, RiskState
from trading_bot.storage.redis_streams import RedisStreamPublisher

log = get_logger(__name__)

AUDIT_PATH = Path("data/live_audit.jsonl")
MODE = "testnet"


@dataclass
class LivePosition:
    symbol: str
    qty: float
    entry_price: float
    opened_at: datetime
    notional: float


@dataclass
class LiveTraderConfig:
    symbol: str = "BTCUSDT"
    interval: str = "5m"
    quote_asset: str = "USDT"
    base_asset: str = "BTC"
    position_fraction: float = 0.05
    prob_threshold: float = 0.60
    min_hold_bars: int = 6
    cooldown_bars: int = 3
    max_orders_per_hour: int = 20
    dry_run: bool = False


def _state_path(symbol: str) -> Path:
    return Path("data") / f"live_state_{symbol.upper()}.json"


def _client_oid(prefix: str) -> str:
    return f"{prefix}{int(time.time() * 1000) % 10_000_000_000}"[:36]


class TestnetLiveTrader:
    def __init__(
        self,
        *,
        client: BinanceSpotClient,
        model_path: Path,
        cfg: LiveTraderConfig,
        risk_limits: RiskLimits,
        ws_base_url: str,
        history: pd.DataFrame,
        publisher: RedisStreamPublisher | None = None,
        state_file: Path | None = None,
        event_blackout: EventBlackoutGuard | None = None,
    ) -> None:
        if cfg.position_fraction > 0.05 + 1e-12:
            raise ValueError("live_testnet position_fraction capped at 0.05")
        payload = load_model(model_path)
        self.model = payload["model"]
        self.feature_names: list[str] = list(payload["features"])
        self.client = client
        self.cfg = cfg
        self.symbol = cfg.symbol.upper()
        self.interval = cfg.interval
        self.history = history.reset_index(drop=True)
        self.book_store = BookStateStore()
        self.state_file = state_file or _state_path(self.symbol)
        self.event_blackout = event_blackout
        self.position: LivePosition | None = None
        self.equity = 0.0
        self.bars_seen = 0
        self.bars_in_position = 0
        self.bars_since_close = cfg.cooldown_bars
        self.closed_bars = 0
        self.orders_placed = 0
        self._order_ts: deque[float] = deque()
        self.filters: dict[str, str] = {}
        self.risk_gate = HardRiskGate(risk_limits)
        self.risk_state = RiskState(
            equity=1.0,
            day_start_equity=1.0,
            week_start_equity=1.0,
            open_positions=0,
        )
        self.order_manager = OrderManager(
            exchange=client,
            risk_gate=self.risk_gate,
            risk_state=self.risk_state,
            paper=False,
        )
        self._ingest = BinanceWsIngest(
            ws_base_url=ws_base_url,
            symbols=[self.symbol],
            intervals=[cfg.interval],
            publisher=publisher,
            on_event=self._on_event,
        )

    def _rate_limit_ok(self) -> bool:
        now = time.time()
        while self._order_ts and now - self._order_ts[0] > 3600:
            self._order_ts.popleft()
        return len(self._order_ts) < self.cfg.max_orders_per_hour

    async def refresh_account_equity(self, mark: float | None = None) -> float:
        if mark is None:
            mark = await fetch_last_price(self.client, self.symbol)
        account = await self.client.account(omit_zero_balances=True)
        quote_free = 0.0
        quote_locked = 0.0
        base_free = 0.0
        base_locked = 0.0
        for b in account.get("balances") or []:
            asset = str(b.get("asset") or "")
            free = float(b.get("free") or 0)
            locked = float(b.get("locked") or 0)
            if asset == self.cfg.quote_asset:
                quote_free, quote_locked = free, locked
            elif asset == self.cfg.base_asset:
                base_free, base_locked = free, locked
        base_qty = base_free + base_locked
        equity = quote_free + quote_locked + base_qty * mark
        self.equity = equity
        self.risk_state.equity = equity
        # sync position from wallet if we hold base
        if base_qty > float(self.filters.get("minQty") or 0):
            if self.position is None:
                self.position = LivePosition(
                    symbol=self.symbol,
                    qty=base_qty,
                    entry_price=mark,
                    opened_at=datetime.now(timezone.utc),
                    notional=base_qty * mark,
                )
                self.risk_state.open_positions = 1
                self.risk_state.position_notionals[self.symbol] = self.position.notional
            else:
                self.position.qty = base_qty
                self.position.notional = base_qty * self.position.entry_price
                self.risk_state.position_notionals[self.symbol] = self.position.notional
        else:
            self.position = None
            self.risk_state.open_positions = 0
            self.risk_state.position_notionals.pop(self.symbol, None)
        LIVE_EQUITY.labels(symbol=self.symbol, mode=MODE).set(equity)
        LIVE_POSITION_QTY.labels(symbol=self.symbol, mode=MODE).set(
            self.position.qty if self.position else 0.0
        )
        LIVE_KILL_SWITCH.labels(symbol=self.symbol, mode=MODE).set(
            1.0 if self.risk_state.kill_switch else 0.0
        )
        return equity

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        pos = None
        if self.position is not None:
            pos = {
                "symbol": self.position.symbol,
                "qty": self.position.qty,
                "entry_price": self.position.entry_price,
                "opened_at": self.position.opened_at.isoformat(),
                "notional": self.position.notional,
            }
        payload = {
            "equity": self.equity,
            "position": pos,
            "bars_seen": self.bars_seen,
            "bars_in_position": self.bars_in_position,
            "bars_since_close": self.bars_since_close,
            "closed_bars": self.closed_bars,
            "orders_placed": self.orders_placed,
            "day_start_equity": self.risk_state.day_start_equity,
            "week_start_equity": self.risk_state.week_start_equity,
            "kill_switch": self.risk_state.kill_switch,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "mode": MODE,
        }
        self.state_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_state(self) -> bool:
        if not self.state_file.exists():
            return False
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.equity = float(data.get("equity") or 0)
        self.bars_seen = int(data.get("bars_seen", 0))
        self.bars_in_position = int(data.get("bars_in_position", 0))
        self.bars_since_close = int(
            data.get("bars_since_close", self.cfg.cooldown_bars)
        )
        self.closed_bars = int(data.get("closed_bars", 0))
        self.orders_placed = int(data.get("orders_placed", 0))
        self.risk_state.equity = self.equity or 1.0
        self.risk_state.day_start_equity = float(
            data.get("day_start_equity", self.risk_state.equity)
        )
        self.risk_state.week_start_equity = float(
            data.get("week_start_equity", self.risk_state.equity)
        )
        self.risk_state.kill_switch = bool(data.get("kill_switch", False))
        pos = data.get("position")
        if pos:
            opened = datetime.fromisoformat(pos["opened_at"])
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            self.position = LivePosition(
                symbol=pos["symbol"],
                qty=float(pos["qty"]),
                entry_price=float(pos["entry_price"]),
                opened_at=opened,
                notional=float(pos["notional"]),
            )
            self.risk_state.open_positions = 1
            self.risk_state.position_notionals[self.symbol] = self.position.notional
        log.info("live_state_loaded", path=str(self.state_file), equity=self.equity)
        return True

    def _predict(self, orderbook: dict[str, float] | None, close: float) -> float | None:
        feats = build_feature_frame(self.history)
        for col in ORDERBOOK_FEATURE_COLUMNS:
            if col not in feats.columns:
                feats[col] = 0.0
        row = feats.iloc[[-1]].copy()
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
        for col in self.feature_names:
            if col not in row.columns:
                row[col] = 0.0
        if row[self.feature_names].isna().any(axis=None):
            return None
        return float(self.model.predict_proba(row[self.feature_names])[0][1])

    async def _place_market(
        self, side: str, qty: float, *, notional: float, is_new: bool
    ) -> dict[str, Any]:
        if not self._rate_limit_ok():
            RISK_DENIES.labels(decision="deny", reason="max_orders_per_hour").inc()
            return {"ok": False, "reason": "max_orders_per_hour"}

        step = self.filters.get("stepSize", "0.00001")
        qty_s = _dec_str(qty, step)
        if float(qty_s) <= 0:
            return {"ok": False, "reason": "qty_rounded_zero"}

        if self.cfg.dry_run:
            log.info("live_dry_run_order", side=side, qty=qty_s, notional=notional)
            append_audit(
                AUDIT_PATH,
                {
                    "event": "dry_run_order",
                    "side": side,
                    "qty": qty_s,
                    "notional": notional,
                },
            )
            return {"ok": True, "mode": "dry_run", "side": side, "quantity": qty_s}

        req = PlaceOrderRequest(
            symbol=self.symbol,
            side=side,
            order_type="MARKET",
            quantity=qty_s,
            price=None,
            notional=notional,
            is_new_position=is_new,
            client_order_id=_client_oid("lv"),
        )
        result = await self.order_manager.place(req)
        if not result.get("ok"):
            RISK_DENIES.labels(
                decision=str(result.get("decision") or "deny"),
                reason=str(result.get("reason") or "unknown")[:64],
            ).inc()
            return result

        raw = result.get("order") or {}
        order_id = int(raw.get("orderId") or 0)
        ORDERS_TOTAL.labels(action="place", symbol=self.symbol, mode=MODE).inc()
        self._order_ts.append(time.time())
        self.orders_placed += 1
        append_audit(
            AUDIT_PATH,
            {
                "event": "live_order_placed",
                "side": side,
                "order_id": order_id,
                "status": raw.get("status"),
                "executed_qty": float(raw.get("executedQty") or 0),
                "quantity": qty_s,
            },
        )
        if order_id:
            try:
                synced = await sync_order(
                    self.client, symbol=self.symbol, order_id=order_id
                )
                append_audit(
                    AUDIT_PATH,
                    {
                        "event": "live_order_synced",
                        "side": side,
                        "order_id": order_id,
                        "status": synced.status,
                        "executed_qty": synced.executed_qty,
                    },
                )
                result["synced_status"] = synced.status
                result["executed_qty"] = synced.executed_qty
            except Exception as exc:
                log.warning("live_sync_failed", order_id=order_id, error=repr(exc))
        return result

    async def _open_long(self, mark: float) -> str:
        notional = self.equity * self.cfg.position_fraction
        min_notional = float(self.filters.get("minNotional") or 5)
        if notional < min_notional:
            notional = min_notional
        # leave small buffer for fees
        account = await self.client.account(omit_zero_balances=True)
        usdt = 0.0
        for b in account.get("balances") or []:
            if b.get("asset") == self.cfg.quote_asset:
                usdt = float(b.get("free") or 0)
                break
        notional = min(notional, usdt * 0.98)
        if notional < min_notional:
            return "skip_insufficient_quote"
        qty = notional / max(mark, 1e-12)
        res = await self._place_market("BUY", qty, notional=notional, is_new=True)
        if not res.get("ok"):
            return f"open_denied:{res.get('reason')}"
        if self.cfg.dry_run:
            self.position = LivePosition(
                symbol=self.symbol,
                qty=qty,
                entry_price=mark,
                opened_at=datetime.now(timezone.utc),
                notional=notional,
            )
            self.bars_in_position = 0
            self.risk_state.open_positions = 1
            self.risk_state.position_notionals[self.symbol] = notional
            return "open_dry_run"
        await self.refresh_account_equity(mark)
        self.bars_in_position = 0
        return "open"

    async def _close_long(self, mark: float, reason: str) -> str:
        if self.position is None:
            return "no_position"
        qty = self.position.qty
        notional = qty * mark
        res = await self._place_market("SELL", qty, notional=notional, is_new=False)
        if not res.get("ok"):
            return f"close_denied:{res.get('reason')}"
        if self.cfg.dry_run:
            self.position = None
            self.bars_in_position = 0
            self.bars_since_close = 0
            self.risk_state.open_positions = 0
            self.risk_state.position_notionals.pop(self.symbol, None)
            return f"close_dry_run:{reason}"
        await self.refresh_account_equity(mark)
        self.bars_in_position = 0
        self.bars_since_close = 0
        return f"close:{reason}"

    async def on_closed_bar(self, close: float, orderbook: dict[str, float]) -> dict[str, Any]:
        self.bars_seen += 1
        await self.refresh_account_equity(close)
        if self.risk_state.day_start_equity <= 0:
            self.risk_state.day_start_equity = self.equity
            self.risk_state.week_start_equity = self.equity

        if self.position is not None:
            self.bars_in_position += 1
        else:
            self.bars_since_close += 1

        kill = self.risk_gate.evaluate_kill_switch(self.risk_state)
        if kill is not None:
            action = "kill"
            if self.position is not None:
                action = await self._close_long(close, "kill_switch")
            LIVE_KILL_SWITCH.labels(symbol=self.symbol, mode=MODE).set(1.0)
            return {"action": action, "equity": self.equity, "proba": None}

        if self.position is not None:
            sl = self.risk_gate.check_stop_loss(
                entry_price=self.position.entry_price,
                mark_price=close,
                side="BUY",
            )
            if sl.decision != RiskDecision.ALLOW:
                action = await self._close_long(close, "stop_loss")
                return {"action": action, "equity": self.equity, "proba": None}

        proba = self._predict(orderbook or None, close)
        if proba is None:
            return {"action": "warmup", "equity": self.equity, "proba": None}

        if proba >= self.cfg.prob_threshold:
            if self.position is None:
                if self.bars_since_close < self.cfg.cooldown_bars:
                    return {
                        "action": "cooldown",
                        "equity": self.equity,
                        "proba": proba,
                    }
                if self.event_blackout is not None:
                    win = self.event_blackout.should_block_open(
                        datetime.now(timezone.utc)
                    )
                    if win is not None:
                        EVENT_BLACKOUT.labels(
                            symbol=self.symbol, event=win.event.name, mode=MODE
                        ).inc()
                        log.info(
                            "event_blackout",
                            event=win.event.name,
                            event_at=win.event.at_utc.isoformat(),
                            window_start=win.start.isoformat(),
                            window_end=win.end.isoformat(),
                            symbol=self.symbol,
                            mode=MODE,
                        )
                        return {
                            "action": "event_blackout",
                            "equity": self.equity,
                            "proba": proba,
                            "event": win.event.name,
                            "window_start": win.start.isoformat(),
                            "window_end": win.end.isoformat(),
                        }
                action = await self._open_long(close)
                return {"action": action, "equity": self.equity, "proba": proba}
            return {"action": "hold", "equity": self.equity, "proba": proba}

        if self.position is not None:
            if self.bars_in_position < self.cfg.min_hold_bars:
                return {
                    "action": "min_hold",
                    "equity": self.equity,
                    "proba": proba,
                    "bars_in_position": self.bars_in_position,
                }
            action = await self._close_long(close, "model_flat")
            return {"action": action, "equity": self.equity, "proba": proba}
        return {"action": "hold", "equity": self.equity, "proba": proba}

    async def _on_event(self, event_type: str, payload: dict) -> None:
        et = str(payload.get("e") or event_type)
        if "bookTicker" in et or (
            "b" in payload and "a" in payload and "B" in payload and "A" in payload
        ):
            book = self.book_store.update_from_payload(payload)
            if book is not None:
                BOOK_UPDATES.labels(symbol=book.symbol).inc()
            return

        if et != "kline" and "kline" not in et:
            return
        k = payload.get("k") or {}
        if str(k.get("s", "")).upper() != self.symbol:
            return
        if str(k.get("i")) != self.interval:
            return
        if not k.get("x"):
            return

        row = {
            "ts": datetime.fromtimestamp(int(k["t"]) / 1000, tz=timezone.utc),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }
        row_ts = pd.Timestamp(row["ts"])
        if not self.history.empty:
            last_ts = pd.Timestamp(self.history.iloc[-1]["ts"])
            # kline.ts = open time: при close x=true приходит тот же ts, что уже в history
            if row_ts < last_ts:
                return
            if row_ts == last_ts:
                self.history = self.history.iloc[:-1].reset_index(drop=True)
            elif getattr(self, "_last_closed_open_ts", None) is not None and row_ts <= self._last_closed_open_ts:
                return

        self.history = pd.concat([self.history, pd.DataFrame([row])], ignore_index=True)
        if len(self.history) > 5000:
            self.history = self.history.iloc[-5000:].reset_index(drop=True)
        self._last_closed_open_ts = row_ts

        book = self.book_store.get(self.symbol)
        ob = orderbook_feature_dict(book) if book else {}
        result = await self.on_closed_bar(float(row["close"]), ob)
        self.closed_bars += 1
        log.info(
            "testnet_live_bar",
            action=result.get("action"),
            equity=result.get("equity"),
            proba=result.get("proba"),
            close=row["close"],
            closed_bars=self.closed_bars,
            dry_run=self.cfg.dry_run,
            position_qty=self.position.qty if self.position else 0.0,
        )
        try:
            self.save_state()
        except Exception as exc:
            log.warning("live_state_save_failed", error=str(exc))

    async def bootstrap(self) -> None:
        info = await self.client.exchange_info(self.symbol)
        self.filters = parse_symbol_filters(info, self.symbol)
        if self.filters.get("status") and self.filters["status"] != "TRADING":
            raise RuntimeError(f"Symbol not TRADING: {self.filters.get('status')}")
        # base/quote from exchangeInfo if present
        for s in info.get("symbols") or []:
            if s.get("symbol") == self.symbol:
                self.cfg.base_asset = str(s.get("baseAsset") or self.cfg.base_asset)
                self.cfg.quote_asset = str(s.get("quoteAsset") or self.cfg.quote_asset)
                break
        # снять зависшие LIMIT от soak/ручных тестов
        if not self.cfg.dry_run:
            try:
                open_orders = await self.client.open_orders(self.symbol)
                for o in open_orders:
                    oid = int(o.get("orderId") or 0)
                    if oid:
                        await self.client.cancel_order(symbol=self.symbol, order_id=oid)
                        ORDERS_TOTAL.labels(
                            action="cancel", symbol=self.symbol, mode=MODE
                        ).inc()
                        log.info("live_cancelled_stale", order_id=oid)
            except Exception as exc:
                log.warning("live_cancel_stale_failed", error=repr(exc))
        await self.refresh_account_equity()
        if self.risk_state.day_start_equity <= 1.0 and self.equity > 0:
            self.risk_state.day_start_equity = self.equity
            self.risk_state.week_start_equity = self.equity
        log.info(
            "testnet_live_bootstrap",
            equity=self.equity,
            filters=self.filters,
            dry_run=self.cfg.dry_run,
            position_qty=self.position.qty if self.position else 0.0,
        )

    async def run(self, *, seconds: int) -> None:
        await self.bootstrap()
        task = asyncio.create_task(self._ingest.run())
        stop_event = asyncio.Event()

        def _stop() -> None:
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                pass

        try:
            if seconds > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=float(seconds))
                except TimeoutError:
                    pass
            else:
                await stop_event.wait()
            await self._ingest.stop()
            await asyncio.wait_for(task, timeout=15)
        finally:
            if not task.done():
                task.cancel()
            try:
                self.save_state()
            except Exception:
                pass


async def run_testnet_live(
    *,
    client: BinanceSpotClient,
    database_url: str,
    ws_base_url: str,
    redis_url: str | None,
    model_path: Path,
    cfg: LiveTraderConfig,
    risk_limits: RiskLimits,
    seconds: int,
    restore_state: bool = True,
    event_blackout: EventBlackoutGuard | None = None,
) -> dict[str, Any]:
    history = await load_klines_df(
        database_url, symbol=cfg.symbol, interval=cfg.interval
    )
    if len(history) < 100:
        raise RuntimeError("Need bootstrap history (>=100 bars) for testnet-live")
    history = history.iloc[-2000:].reset_index(drop=True)

    publisher = None
    redis = None
    if redis_url:
        from redis.asyncio import from_url

        redis = from_url(redis_url, decode_responses=True)
        publisher = RedisStreamPublisher(redis)

    trader = TestnetLiveTrader(
        client=client,
        model_path=model_path,
        cfg=cfg,
        risk_limits=risk_limits,
        ws_base_url=ws_base_url,
        history=history,
        publisher=publisher,
        event_blackout=event_blackout,
    )
    if restore_state:
        trader.load_state()
    try:
        await trader.run(seconds=seconds)
    finally:
        if redis is not None:
            await redis.aclose()

    return {
        "closed_bars": trader.closed_bars,
        "equity": trader.equity,
        "orders_placed": trader.orders_placed,
        "position_qty": trader.position.qty if trader.position else 0.0,
        "kill_switch": trader.risk_state.kill_switch,
        "dry_run": cfg.dry_run,
        "state_file": str(trader.state_file),
        "messages_ok": trader._ingest.messages_ok,
    }
