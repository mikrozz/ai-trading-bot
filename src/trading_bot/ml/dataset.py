"""Загрузка klines из TimescaleDB для ML."""

from __future__ import annotations

from datetime import datetime

import asyncpg
import pandas as pd

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
