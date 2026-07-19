"""Live paper: WS klines + модель + hard risk (без реальных ордеров)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from redis.asyncio import Redis

from trading_bot.features.orderbook import orderbook_feature_dict
from trading_bot.logging_setup import get_logger
from trading_bot.marketdata.book_state import BookStateStore
from trading_bot.marketdata.ws_ingest import BinanceWsIngest
from trading_bot.ml.dataset import load_klines_df
from trading_bot.paper.engine import PaperEngine
from trading_bot.risk.gates import RiskLimits
from trading_bot.storage.redis_streams import RedisStreamPublisher

log = get_logger(__name__)


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
    ) -> None:
        self.engine = engine
        self.symbol = symbol.upper()
        self.interval = interval
        self.history = history.reset_index(drop=True)
        self.book_store = book_store or BookStateStore()
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
            self.book_store.update_from_payload(payload)
            return

        if et != "kline" and "kline" not in et:
            return
        k = payload.get("k") or {}
        if str(k.get("s", "")).upper() != self.symbol:
            return
        if str(k.get("i")) != self.interval:
            return
        if not k.get("x"):
            return  # ждём закрытый бар

        row = {
            "ts": datetime.fromtimestamp(int(k["t"]) / 1000, tz=timezone.utc),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }
        # дедуп по ts
        if not self.history.empty:
            last_ts = pd.Timestamp(self.history.iloc[-1]["ts"])
            if pd.Timestamp(row["ts"]) <= last_ts:
                return

        self.history = pd.concat([self.history, pd.DataFrame([row])], ignore_index=True)
        # хвост для inference
        if len(self.history) > 5000:
            self.history = self.history.iloc[-5000:].reset_index(drop=True)

        result = self.engine.on_bar(self.history)
        self.closed_bars += 1
        book = self.book_store.get(self.symbol)
        ob = orderbook_feature_dict(book) if book else {}
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

    async def run(self, *, seconds: int) -> None:
        task = asyncio.create_task(self._ingest.run())
        try:
            await asyncio.sleep(seconds)
            await self._ingest.stop()
            await asyncio.wait_for(task, timeout=15)
        finally:
            if not task.done():
                task.cancel()


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
) -> dict:
    history = await load_klines_df(database_url, symbol=symbol, interval=interval)
    if len(history) < 100:
        raise RuntimeError("Need bootstrap history (>=100 bars) for live paper warmup")
    # берём хвост
    history = history.iloc[-2000:].reset_index(drop=True)

    engine = PaperEngine(
        model_path=model_path,
        symbol=symbol,
        initial_cash=cash,
        fee_rate=fee_rate,
        slippage=slippage,
        risk_limits=risk_limits,
    )
    # прогрев equity на истории без торговли? пропускаем — стартуем с текущего хвоста
    # прогоним warmup features silently via bars_seen only on live closes

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
    }
