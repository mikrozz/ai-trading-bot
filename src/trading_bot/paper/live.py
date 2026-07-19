"""Live paper: WS klines + модель + hard risk (без реальных ордеров)."""

from __future__ import annotations

import asyncio
import json
import signal
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from redis.asyncio import Redis

from trading_bot.features.orderbook import orderbook_feature_dict
from trading_bot.logging_setup import get_logger
from trading_bot.marketdata.book_state import BookStateStore
from trading_bot.marketdata.ws_ingest import BinanceWsIngest
from trading_bot.metrics import BOOK_UPDATES, PAPER_EQUITY, PAPER_FILLS, PAPER_KILL_SWITCH
from trading_bot.ml.dataset import load_klines_df
from trading_bot.paper.engine import PaperEngine, PaperPosition
from trading_bot.risk.gates import RiskLimits
from trading_bot.storage.redis_streams import RedisStreamPublisher

log = get_logger(__name__)


def _state_path(symbol: str) -> Path:
    return Path("data") / f"paper_state_{symbol.upper()}.json"


def save_paper_state(engine: PaperEngine, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    st = engine.state
    pos = None
    if st.position is not None:
        pos = {
            "symbol": st.position.symbol,
            "qty": st.position.qty,
            "entry_price": st.position.entry_price,
            "opened_at": st.position.opened_at.isoformat(),
            "notional": st.position.notional,
        }
    payload = {
        "cash": st.cash,
        "equity": st.equity,
        "position": pos,
        "fills_count": len(st.fills),
        "bars_seen": st.bars_seen,
        "signals_long": st.signals_long,
        "signals_flat": st.signals_flat,
        "day_start_equity": engine.risk_state.day_start_equity,
        "week_start_equity": engine.risk_state.week_start_equity,
        "kill_switch": engine.risk_state.kill_switch,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_paper_state(engine: PaperEngine, path: Path) -> bool:
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    engine.state.cash = float(data["cash"])
    engine.state.equity = float(data["equity"])
    engine.state.bars_seen = int(data.get("bars_seen", 0))
    engine.state.signals_long = int(data.get("signals_long", 0))
    engine.state.signals_flat = int(data.get("signals_flat", 0))
    engine.risk_state.equity = engine.state.equity
    engine.risk_state.day_start_equity = float(
        data.get("day_start_equity", engine.state.equity)
    )
    engine.risk_state.week_start_equity = float(
        data.get("week_start_equity", engine.state.equity)
    )
    engine.risk_state.kill_switch = bool(data.get("kill_switch", False))
    pos = data.get("position")
    if pos:
        opened = datetime.fromisoformat(pos["opened_at"])
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        engine.state.position = PaperPosition(
            symbol=pos["symbol"],
            qty=float(pos["qty"]),
            entry_price=float(pos["entry_price"]),
            opened_at=opened,
            notional=float(pos["notional"]),
        )
        engine.risk_state.open_positions = 1
        engine.risk_state.position_notionals[pos["symbol"]] = float(pos["notional"])
    PAPER_EQUITY.labels(symbol=engine.symbol).set(engine.state.equity)
    PAPER_KILL_SWITCH.labels(symbol=engine.symbol).set(
        1.0 if engine.risk_state.kill_switch else 0.0
    )
    log.info("paper_state_loaded", path=str(path), equity=engine.state.equity)
    return True


class LivePaperRunner:
    def __init__(
        self,
        *,
        engine: PaperEngine,
        ws_base_url: str,
        symbol: str,
        interval: str,
        history: pd.DataFrame,
        book_store: BookStateStore | None = None,
        publisher: RedisStreamPublisher | None = None,
        state_file: Path | None = None,
    ) -> None:
        self.engine = engine
        self.symbol = symbol.upper()
        self.interval = interval
        self.history = history.reset_index(drop=True)
        self.book_store = book_store or BookStateStore()
        self.state_file = state_file or _state_path(self.symbol)
        self.closed_bars = 0
        self._ingest = BinanceWsIngest(
            ws_base_url=ws_base_url,
            symbols=[self.symbol],
            intervals=[interval],
            publisher=publisher,
            on_event=self._on_event,
        )

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
        if not self.history.empty:
            last_ts = pd.Timestamp(self.history.iloc[-1]["ts"])
            if pd.Timestamp(row["ts"]) <= last_ts:
                return

        self.history = pd.concat([self.history, pd.DataFrame([row])], ignore_index=True)
        if len(self.history) > 5000:
            self.history = self.history.iloc[-5000:].reset_index(drop=True)

        book = self.book_store.get(self.symbol)
        ob = orderbook_feature_dict(book) if book else {}
        fills_before = len(self.engine.state.fills)
        result = self.engine.on_bar(self.history, orderbook=ob or None)
        self.closed_bars += 1
        equity = float(result.get("equity") or self.engine.state.equity)
        PAPER_EQUITY.labels(symbol=self.symbol).set(equity)
        PAPER_KILL_SWITCH.labels(symbol=self.symbol).set(
            1.0 if self.engine.risk_state.kill_switch else 0.0
        )
        if len(self.engine.state.fills) > fills_before:
            for fill in self.engine.state.fills[fills_before:]:
                PAPER_FILLS.labels(symbol=self.symbol, side=fill.side).inc()
        log.info(
            "live_paper_bar",
            action=result.get("action"),
            equity=result.get("equity"),
            proba=result.get("proba"),
            close=row["close"],
            ob_spread_bps=ob.get("ob_spread_bps"),
            ob_imbalance=ob.get("ob_imbalance"),
            closed_bars=self.closed_bars,
        )
        try:
            save_paper_state(self.engine, self.state_file)
        except Exception as exc:
            log.warning("paper_state_save_failed", error=str(exc))

    async def run(self, *, seconds: int) -> None:
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
                save_paper_state(self.engine, self.state_file)
            except Exception:
                pass


async def run_live_paper(
    *,
    database_url: str,
    ws_base_url: str,
    redis_url: str | None,
    model_path: Path,
    symbol: str,
    interval: str,
    cash: float,
    seconds: int,
    fee_rate: float,
    slippage: float,
    risk_limits: RiskLimits,
    state_file: Path | None = None,
    restore_state: bool = True,
    prob_threshold: float = 0.60,
    min_hold_bars: int = 6,
    cooldown_bars: int = 3,
    position_fraction: float = 0.10,
) -> dict:
    history = await load_klines_df(database_url, symbol=symbol, interval=interval)
    if len(history) < 100:
        raise RuntimeError("Need bootstrap history (>=100 bars) for live paper warmup")
    history = history.iloc[-2000:].reset_index(drop=True)

    engine = PaperEngine(
        model_path=model_path,
        symbol=symbol,
        initial_cash=cash,
        fee_rate=fee_rate,
        slippage=slippage,
        prob_threshold=prob_threshold,
        min_hold_bars=min_hold_bars,
        cooldown_bars=cooldown_bars,
        position_fraction=position_fraction,
        risk_limits=risk_limits,
    )
    sf = state_file or _state_path(symbol)
    if restore_state:
        load_paper_state(engine, sf)

    publisher = None
    redis: Redis | None = None
    if redis_url:
        from redis.asyncio import from_url

        redis = from_url(redis_url, decode_responses=True)
        publisher = RedisStreamPublisher(redis)

    runner = LivePaperRunner(
        engine=engine,
        ws_base_url=ws_base_url,
        symbol=symbol,
        interval=interval,
        history=history,
        publisher=publisher,
        state_file=sf,
    )
    try:
        await runner.run(seconds=seconds)
    finally:
        if redis is not None:
            await redis.aclose()

    st = engine.state
    return {
        "closed_bars": runner.closed_bars,
        "equity": st.equity,
        "fills": len(st.fills),
        "book_updates": runner.book_store.updates,
        "messages_ok": runner._ingest.messages_ok,
        "state_file": str(sf),
    }
