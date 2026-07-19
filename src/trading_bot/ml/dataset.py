"""Загрузка klines / bookTicker из TimescaleDB для ML."""

from __future__ import annotations

from datetime import datetime

import asyncpg
import pandas as pd

from trading_bot.features.engineering import attach_orderbook_features, build_feature_frame
from trading_bot.storage.batch_writer import parse_database_url


async def load_klines_df(
    database_url: str,
    *,
    symbol: str,
    interval: str = "5m",
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    where = ["symbol = $1", "interval = $2"]
    args: list[object] = [symbol.upper(), interval]
    idx = 3
    if start is not None:
        where.append(f"ts >= ${idx}")
        args.append(start)
        idx += 1
    if end is not None:
        where.append(f"ts <= ${idx}")
        args.append(end)
        idx += 1

    sql = f"""
        SELECT ts, open, high, low, close, volume
        FROM md_klines
        WHERE {' AND '.join(where)}
        ORDER BY ts ASC
    """
    conn = await asyncpg.connect(**parse_database_url(database_url))
    try:
        rows = await conn.fetch(sql, *args)
    finally:
        await conn.close()

    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame([dict(r) for r in rows])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


async def load_book_ticker_df(
    database_url: str,
    *,
    symbol: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    where = ["symbol = $1"]
    args: list[object] = [symbol.upper()]
    idx = 2
    if start is not None:
        where.append(f"ts >= ${idx}")
        args.append(start)
        idx += 1
    if end is not None:
        where.append(f"ts <= ${idx}")
        args.append(end)
        idx += 1

    sql = f"""
        SELECT ts, bid_price, bid_qty, ask_price, ask_qty,
               spread_bps, imbalance, microprice
        FROM md_book_ticker
        WHERE {' AND '.join(where)}
        ORDER BY ts ASC
    """
    conn = await asyncpg.connect(**parse_database_url(database_url))
    try:
        rows = await conn.fetch(sql, *args)
    finally:
        await conn.close()

    if not rows:
        return pd.DataFrame(
            columns=[
                "ts",
                "bid_price",
                "bid_qty",
                "ask_price",
                "ask_qty",
                "spread_bps",
                "imbalance",
                "microprice",
            ]
        )
    df = pd.DataFrame([dict(r) for r in rows])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


async def load_training_frame(
    database_url: str,
    *,
    symbol: str,
    interval: str = "5m",
) -> pd.DataFrame:
    """Klines + asof book features для обучения."""
    klines = await load_klines_df(database_url, symbol=symbol, interval=interval)
    books = await load_book_ticker_df(database_url, symbol=symbol)
    enriched = attach_orderbook_features(klines, books)
    return build_feature_frame(enriched)
