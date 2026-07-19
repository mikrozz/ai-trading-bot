"""Batch writer: Redis Streams → TimescaleDB (trades + closed/updated klines)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import asyncpg
from redis.asyncio import Redis

from trading_bot.logging_setup import get_logger

log = get_logger(__name__)


def _ms_to_dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)


def parse_database_url(url: str) -> dict[str, Any]:
    """postgresql://user:pass@host:port/db → kwargs for asyncpg.connect."""
    parsed = urlparse(url)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme}")
    return {
        "user": parsed.username or "trading",
        "password": parsed.password or "",
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "database": (parsed.path or "/trading").lstrip("/") or "trading",
    }


@dataclass
class TradeRow:
    ts: datetime
    symbol: str
    trade_id: int
    price: float
    qty: float
    is_buyer_maker: bool


@dataclass
class KlineRow:
    ts: datetime
    symbol: str
    interval: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class WriteBuffers:
    trades: list[TradeRow] = field(default_factory=list)
    klines: list[KlineRow] = field(default_factory=list)

    def clear(self) -> None:
        self.trades.clear()
        self.klines.clear()

    def __len__(self) -> int:
        return len(self.trades) + len(self.klines)


def parse_event(event_type: str, payload: dict[str, Any]) -> TradeRow | KlineRow | None:
    """Преобразует Binance WS payload в строку БД."""
    et = event_type
    if et in {"trade", "aggTrade"} or payload.get("e") in {"trade", "aggTrade"}:
        symbol = str(payload.get("s", "")).upper()
        trade_id = int(payload.get("t") or payload.get("a") or 0)
        price = float(payload["p"])
        qty = float(payload["q"])
        ts = _ms_to_dt(int(payload.get("T") or payload.get("E") or 0))
        is_buyer_maker = bool(payload.get("m", False))
        if not symbol or trade_id <= 0:
            return None
        return TradeRow(ts, symbol, trade_id, price, qty, is_buyer_maker)

    if et == "kline" or payload.get("e") == "kline":
        k = payload.get("k") or {}
        # пишем и промежуточные, upsert по PK; closed-флаг не обязателен
        symbol = str(k.get("s") or payload.get("s") or "").upper()
        interval = str(k.get("i") or "")
        if not symbol or not interval:
            return None
        return KlineRow(
            ts=_ms_to_dt(int(k["t"])),
            symbol=symbol,
            interval=interval,
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
        )
    return None


class BatchWriter:
    def __init__(
        self,
        *,
        redis: Redis,
        database_url: str,
        stream: str = "md:events",
        group: str = "writers",
        consumer: str = "writer-1",
        batch_size: int = 200,
        flush_interval_sec: float = 0.2,
    ) -> None:
        self.redis = redis
        self.database_url = database_url
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        self._stop = asyncio.Event()
        self._pool: asyncpg.Pool | None = None
        self.written_trades = 0
        self.written_klines = 0
        self.errors = 0

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(**parse_database_url(self.database_url), min_size=1, max_size=4)
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
            log.info("redis_group_created", stream=self.stream, group=self.group)
        except Exception as exc:
            # BUSYGROUP — уже есть
            if "BUSYGROUP" not in str(exc):
                raise
            log.info("redis_group_exists", stream=self.stream, group=self.group)

    async def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _flush(self, buf: WriteBuffers) -> None:
        if not buf or self._pool is None:
            return
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if buf.trades:
                    await conn.executemany(
                        """
                        INSERT INTO md_trades (ts, symbol, trade_id, price, qty, is_buyer_maker)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (symbol, trade_id, ts) DO NOTHING
                        """,
                        [
                            (t.ts, t.symbol, t.trade_id, t.price, t.qty, t.is_buyer_maker)
                            for t in buf.trades
                        ],
                    )
                    self.written_trades += len(buf.trades)
                if buf.klines:
                    await conn.executemany(
                        """
                        INSERT INTO md_klines (ts, symbol, interval, open, high, low, close, volume)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (symbol, interval, ts) DO UPDATE SET
                          open = EXCLUDED.open,
                          high = EXCLUDED.high,
                          low = EXCLUDED.low,
                          close = EXCLUDED.close,
                          volume = EXCLUDED.volume
                        """,
                        [
                            (k.ts, k.symbol, k.interval, k.open, k.high, k.low, k.close, k.volume)
                            for k in buf.klines
                        ],
                    )
                    self.written_klines += len(buf.klines)
        buf.clear()

    async def run(self, *, max_seconds: float | None = None) -> None:
        await self.start()
        buf = WriteBuffers()
        pending_ids: list[str] = []
        started = asyncio.get_event_loop().time()
        last_flush = started

        try:
            while not self._stop.is_set():
                if max_seconds is not None and (asyncio.get_event_loop().time() - started) >= max_seconds:
                    break

                resp = await self.redis.xreadgroup(
                    groupname=self.group,
                    consumername=self.consumer,
                    streams={self.stream: ">"},
                    count=self.batch_size,
                    block=200,
                )
                if resp:
                    for _stream_name, messages in resp:
                        for msg_id, fields in messages:
                            pending_ids.append(msg_id if isinstance(msg_id, str) else msg_id.decode())
                            event_type = fields.get("type") or fields.get(b"type") or ""
                            if isinstance(event_type, bytes):
                                event_type = event_type.decode()
                            payload_raw = fields.get("payload") or fields.get(b"payload") or "{}"
                            if isinstance(payload_raw, bytes):
                                payload_raw = payload_raw.decode()
                            try:
                                payload = json.loads(payload_raw)
                                row = parse_event(str(event_type), payload)
                                if isinstance(row, TradeRow):
                                    buf.trades.append(row)
                                elif isinstance(row, KlineRow):
                                    buf.klines.append(row)
                            except Exception as exc:
                                self.errors += 1
                                log.warning("parse_error", error=str(exc))

                now = asyncio.get_event_loop().time()
                if len(buf) >= self.batch_size or (len(buf) > 0 and now - last_flush >= self.flush_interval_sec):
                    try:
                        await self._flush(buf)
                        if pending_ids:
                            await self.redis.xack(self.stream, self.group, *pending_ids)
                            pending_ids.clear()
                        last_flush = now
                    except Exception as exc:
                        self.errors += 1
                        log.warning("flush_error", error=str(exc))

            if len(buf) > 0:
                await self._flush(buf)
                if pending_ids:
                    await self.redis.xack(self.stream, self.group, *pending_ids)
        finally:
            await self.close()

        log.info(
            "writer_done",
            trades=self.written_trades,
            klines=self.written_klines,
            errors=self.errors,
        )
