"""Загрузка исторических klines с Binance public REST → TimescaleDB."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from trading_bot.exchange.binance_spot import BinanceSpotClient
from trading_bot.logging_setup import get_logger
from trading_bot.storage.batch_writer import parse_database_url

log = get_logger(__name__)

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def _rows_from_klines(symbol: str, interval: str, raw: list[list[Any]]) -> list[tuple]:
    out: list[tuple] = []
    for k in raw:
        ts = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc)
        out.append(
            (
                ts,
                symbol.upper(),
                interval,
                float(k[1]),
                float(k[2]),
                float(k[3]),
                float(k[4]),
                float(k[5]),
            )
        )
    return out


async def upsert_klines(pool: asyncpg.Pool, rows: list[tuple]) -> int:
    if not rows:
        return 0
    async with pool.acquire() as conn:
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
            rows,
        )
    return len(rows)


async def bootstrap_klines(
    *,
    client: BinanceSpotClient,
    database_url: str,
    symbols: list[str],
    interval: str = "5m",
    months: int = 6,
    sleep_sec: float = 0.15,
    limit: int = 1000,
) -> dict[str, int]:
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30 * months)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step = INTERVAL_MS[interval]

    pool = await asyncpg.create_pool(**parse_database_url(database_url), min_size=1, max_size=2)
    counts: dict[str, int] = {}
    try:
        for symbol in symbols:
            cursor = start_ms
            total = 0
            log.info(
                "bootstrap_start",
                symbol=symbol,
                interval=interval,
                from_ts=start.isoformat(),
                to_ts=end.isoformat(),
            )
            while cursor < end_ms:
                batch = await client.klines(
                    symbol,
                    interval,
                    limit=limit,
                    start_time=cursor,
                    end_time=end_ms,
                )
                if not batch:
                    break
                rows = _rows_from_klines(symbol, interval, batch)
                total += await upsert_klines(pool, rows)
                last_open = int(batch[-1][0])
                next_cursor = last_open + step
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
                if len(batch) < limit:
                    break
                await asyncio.sleep(sleep_sec)
            counts[symbol] = total
            log.info("bootstrap_symbol_done", symbol=symbol, upserts=total)
    finally:
        await pool.close()

    return counts
