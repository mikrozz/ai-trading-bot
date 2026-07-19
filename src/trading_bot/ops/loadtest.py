"""Нагрузочные бенчмарки hot-path бота (без ордеров на бирже)."""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trading_bot.features.engineering import build_feature_frame
from trading_bot.logging_setup import get_logger
from trading_bot.storage.batch_writer import parse_database_url, parse_event

log = get_logger(__name__)

DEFAULT_AUDIT = Path("data/loadtest.jsonl")


@dataclass
class BenchResult:
    name: str
    ops: int
    seconds: float
    ops_per_sec: float
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    error: str = ""


@dataclass
class LoadTestReport:
    started_at: str
    host_hint: str
    results: list[BenchResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def _synth_klines(n: int, *, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2025, 1, 1, tzinfo=timezone.utc)
    price = 100.0
    rows = []
    for i in range(n):
        price *= 1.0 + (0.0008 if i % 11 < 6 else -0.0006)
        rows.append(
            {
                "ts": start + timedelta(minutes=5 * i),
                "open": price,
                "high": price * 1.001,
                "low": price * 0.999,
                "close": price,
                "volume": 50.0 + (i % 25),
            }
        )
    return pd.DataFrame(rows)


def _trade_payload(i: int, symbol: str = "BTCUSDT") -> dict[str, Any]:
    return {
        "e": "trade",
        "s": symbol,
        "t": 90_000_000 + i,
        "p": f"{65000 + (i % 100) * 0.01:.2f}",
        "q": "0.001",
        "T": int(time.time() * 1000) + i,
        "m": i % 2 == 0,
    }


def _book_payload(i: int, symbol: str = "BTCUSDT") -> dict[str, Any]:
    mid = 65000.0 + (i % 50) * 0.1
    return {
        "e": "bookTicker",
        "s": symbol,
        "b": f"{mid - 0.5:.2f}",
        "B": "1.5",
        "a": f"{mid + 0.5:.2f}",
        "A": "1.2",
        "E": int(time.time() * 1000) + i,
    }


def bench_parse_events(n: int = 100_000) -> BenchResult:
    payloads = [
        ("trade", _trade_payload(i)) if i % 3 else ("bookTicker", _book_payload(i))
        for i in range(n)
    ]
    t0 = time.perf_counter()
    parsed = 0
    for et, p in payloads:
        if parse_event(et, p) is not None:
            parsed += 1
    dt = time.perf_counter() - t0
    return BenchResult(
        name="parse_events",
        ops=parsed,
        seconds=dt,
        ops_per_sec=parsed / dt if dt > 0 else 0.0,
        extra={"requested": n},
    )


def bench_features(bars: int = 5_000, repeats: int = 20) -> BenchResult:
    df = _synth_klines(bars)
    # warmup
    build_feature_frame(df)
    lat: list[float] = []
    t0 = time.perf_counter()
    for _ in range(repeats):
        s = time.perf_counter()
        out = build_feature_frame(df)
        lat.append((time.perf_counter() - s) * 1000.0)
        assert len(out) == bars
    dt = time.perf_counter() - t0
    return BenchResult(
        name="features_build",
        ops=repeats,
        seconds=dt,
        ops_per_sec=repeats / dt if dt > 0 else 0.0,
        p50_ms=_pct(lat, 50),
        p95_ms=_pct(lat, 95),
        extra={"bars": bars},
    )


def bench_paper_backfill(model_path: Path, bars: int = 8_000) -> BenchResult:
    from trading_bot.paper.engine import PaperEngine

    if not model_path.exists():
        return BenchResult(
            name="paper_backfill",
            ops=0,
            seconds=0.0,
            ops_per_sec=0.0,
            ok=False,
            error=f"model not found: {model_path}",
        )
    df = _synth_klines(bars)
    engine = PaperEngine(
        model_path=model_path,
        symbol="BTCUSDT",
        initial_cash=1000.0,
        prob_threshold=0.55,
        min_hold_bars=3,
        cooldown_bars=1,
    )
    t0 = time.perf_counter()
    state = engine.run_backfill(df)
    dt = time.perf_counter() - t0
    return BenchResult(
        name="paper_backfill",
        ops=state.bars_seen,
        seconds=dt,
        ops_per_sec=state.bars_seen / dt if dt > 0 else 0.0,
        extra={
            "bars": bars,
            "fills": len(state.fills),
            "equity": round(state.equity, 4),
        },
    )


def bench_paper_live_path(
    model_path: Path,
    *,
    history_bars: int = 500,
    closed_bars: int = 200,
) -> BenchResult:
    """Имитирует live: на каждый closed bar полный rebuild features + predict."""
    from trading_bot.paper.engine import PaperEngine

    if not model_path.exists():
        return BenchResult(
            name="paper_live_path",
            ops=0,
            seconds=0.0,
            ops_per_sec=0.0,
            ok=False,
            error=f"model not found: {model_path}",
        )
    history = _synth_klines(history_bars)
    engine = PaperEngine(
        model_path=model_path,
        symbol="BTCUSDT",
        initial_cash=1000.0,
        prob_threshold=0.55,
        min_hold_bars=3,
        cooldown_bars=1,
    )
    lat: list[float] = []
    t0 = time.perf_counter()
    for i in range(closed_bars):
        nxt = {
            "ts": history.iloc[-1]["ts"] + timedelta(minutes=5),
            "open": float(history.iloc[-1]["close"]),
            "high": float(history.iloc[-1]["close"]) * 1.001,
            "low": float(history.iloc[-1]["close"]) * 0.999,
            "close": float(history.iloc[-1]["close"])
            * (1.0005 if i % 2 == 0 else 0.9995),
            "volume": 55.0,
        }
        history = pd.concat([history, pd.DataFrame([nxt])], ignore_index=True)
        if len(history) > 2000:
            history = history.iloc[-2000:].reset_index(drop=True)
        s = time.perf_counter()
        engine.on_bar(history)
        lat.append((time.perf_counter() - s) * 1000.0)
    dt = time.perf_counter() - t0
    return BenchResult(
        name="paper_live_path",
        ops=closed_bars,
        seconds=dt,
        ops_per_sec=closed_bars / dt if dt > 0 else 0.0,
        p50_ms=_pct(lat, 50),
        p95_ms=_pct(lat, 95),
        extra={
            "history_bars_end": len(history),
            "fills": len(engine.state.fills),
            "budget_5m_bar_ms": 300_000.0,
            "headroom_x": (300_000.0 / _pct(lat, 95)) if lat and _pct(lat, 95) > 0 else 0.0,
        },
    )


def bench_model_predict(model_path: Path, rows: int = 20_000) -> BenchResult:
    from trading_bot.ml.train_xgb import load_model

    if not model_path.exists():
        return BenchResult(
            name="model_predict",
            ops=0,
            seconds=0.0,
            ops_per_sec=0.0,
            ok=False,
            error=f"model not found: {model_path}",
        )
    payload = load_model(model_path)
    model = payload["model"]
    feats = list(payload["features"])
    x = pd.DataFrame(
        np.random.default_rng(42).normal(size=(rows, len(feats))),
        columns=feats,
    )
    # warmup
    model.predict_proba(x.iloc[:100])
    t0 = time.perf_counter()
    proba = model.predict_proba(x)[:, 1]
    dt = time.perf_counter() - t0
    return BenchResult(
        name="model_predict",
        ops=rows,
        seconds=dt,
        ops_per_sec=rows / dt if dt > 0 else 0.0,
        extra={"mean_proba": float(proba.mean()), "n_features": len(feats)},
    )


async def bench_redis_publish(
    redis_url: str,
    *,
    n: int = 20_000,
    stream: str = "md:loadtest",
) -> BenchResult:
    """Последовательный xadd (как ingest) + pipeline burst."""
    from redis.asyncio import from_url

    from trading_bot.storage.redis_streams import RedisStreamPublisher

    redis = from_url(redis_url, decode_responses=True)
    try:
        await redis.delete(stream)
        pub = RedisStreamPublisher(redis, stream=stream, maxlen=max(n * 2, 10_000))
        t0 = time.perf_counter()
        for i in range(n):
            await pub.publish("trade", _trade_payload(i))
        dt_seq = time.perf_counter() - t0
        seq_ops = n / dt_seq if dt_seq > 0 else 0.0

        # pipeline burst: пиковая ёмкость под bookTicker spikes
        pipe_n = min(n, 10_000)
        await redis.delete(stream)
        t1 = time.perf_counter()
        pipe = redis.pipeline(transaction=False)
        for i in range(pipe_n):
            fields = {
                "type": "trade",
                "payload": json.dumps(_trade_payload(i), separators=(",", ":")),
            }
            pipe.xadd(stream, fields, maxlen=max(pipe_n, 10_000), approximate=True)
        await pipe.execute()
        dt_pipe = time.perf_counter() - t1
        pipe_ops = pipe_n / dt_pipe if dt_pipe > 0 else 0.0

        length = await redis.xlen(stream)
        await redis.delete(stream)
        return BenchResult(
            name="redis_xadd",
            ops=n,
            seconds=dt_seq,
            ops_per_sec=seq_ops,
            extra={
                "stream": stream,
                "xlen_end": length,
                "pipeline_ops_per_sec": round(pipe_ops, 1),
                "pipeline_n": pipe_n,
            },
        )
    except Exception as exc:
        return BenchResult(
            name="redis_xadd",
            ops=0,
            seconds=0.0,
            ops_per_sec=0.0,
            ok=False,
            error=repr(exc),
        )
    finally:
        await redis.aclose()


async def bench_db_insert(
    database_url: str,
    *,
    n: int = 10_000,
    batch: int = 500,
) -> BenchResult:
    """INSERT в временную таблицу (не трогает prod hypertables)."""
    import asyncpg

    conn = await asyncpg.connect(**parse_database_url(database_url))
    try:
        await conn.execute(
            """
            CREATE TEMP TABLE loadtest_trades (
                ts TIMESTAMPTZ NOT NULL,
                symbol TEXT NOT NULL,
                trade_id BIGINT NOT NULL,
                price DOUBLE PRECISION NOT NULL,
                qty DOUBLE PRECISION NOT NULL,
                is_buyer_maker BOOLEAN NOT NULL
            ) ON COMMIT PRESERVE ROWS
            """
        )
        rows = [
            (
                datetime.now(timezone.utc),
                "BTCUSDT",
                90_000_000 + i,
                65000.0 + (i % 100) * 0.01,
                0.001,
                i % 2 == 0,
            )
            for i in range(n)
        ]
        t0 = time.perf_counter()
        for i in range(0, n, batch):
            chunk = rows[i : i + batch]
            await conn.executemany(
                """
                INSERT INTO loadtest_trades
                (ts, symbol, trade_id, price, qty, is_buyer_maker)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                chunk,
            )
        dt = time.perf_counter() - t0
        count = await conn.fetchval("SELECT count(*) FROM loadtest_trades")
        return BenchResult(
            name="db_temp_insert",
            ops=n,
            seconds=dt,
            ops_per_sec=n / dt if dt > 0 else 0.0,
            extra={"batch": batch, "count": int(count or 0)},
        )
    except Exception as exc:
        return BenchResult(
            name="db_temp_insert",
            ops=0,
            seconds=0.0,
            ops_per_sec=0.0,
            ok=False,
            error=repr(exc),
        )
    finally:
        await conn.close()


async def run_loadtest(
    *,
    model_path: Path,
    redis_url: str | None,
    database_url: str | None,
    profile: str = "all",
    host_hint: str = "docker01",
    audit_path: Path | None = DEFAULT_AUDIT,
    parse_n: int = 80_000,
    redis_n: int = 15_000,
    db_n: int = 8_000,
    live_bars: int = 150,
) -> LoadTestReport:
    report = LoadTestReport(
        started_at=datetime.now(timezone.utc).isoformat(),
        host_hint=host_hint,
    )
    profile = profile.lower().strip()
    want_cpu = profile in {"all", "cpu"}
    want_redis = profile in {"all", "redis"}
    want_db = profile in {"all", "db"}

    if want_cpu:
        report.results.append(bench_parse_events(parse_n))
        report.results.append(bench_features(bars=4_000, repeats=15))
        report.results.append(bench_model_predict(model_path, rows=15_000))
        report.results.append(bench_paper_backfill(model_path, bars=6_000))
        report.results.append(
            bench_paper_live_path(model_path, history_bars=400, closed_bars=live_bars)
        )

    if want_redis:
        if not redis_url:
            report.results.append(
                BenchResult(
                    name="redis_xadd",
                    ops=0,
                    seconds=0.0,
                    ops_per_sec=0.0,
                    ok=False,
                    error="redis_url missing",
                )
            )
        else:
            report.results.append(await bench_redis_publish(redis_url, n=redis_n))

    if want_db:
        if not database_url:
            report.results.append(
                BenchResult(
                    name="db_temp_insert",
                    ops=0,
                    seconds=0.0,
                    ops_per_sec=0.0,
                    ok=False,
                    error="database_url missing",
                )
            )
        else:
            report.results.append(await bench_db_insert(database_url, n=db_n))

    for r in report.results:
        log.info(
            "loadtest_bench",
            name=r.name,
            ops_per_sec=round(r.ops_per_sec, 1),
            p95_ms=round(r.p95_ms, 2),
            ok=r.ok,
            error=r.error or None,
        )

    if audit_path is not None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "event": "loadtest",
                        "started_at": report.started_at,
                        "host_hint": report.host_hint,
                        "profile": profile,
                        "results": [asdict(r) for r in report.results],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return report


def format_loadtest_table(report: LoadTestReport) -> str:
    header = (
        f"{'bench':<18} {'ops':>8} {'ops/s':>12} {'p50_ms':>9} {'p95_ms':>9} {'ok':>4}"
    )
    lines = [header, "-" * len(header)]
    for r in report.results:
        lines.append(
            f"{r.name:<18} {r.ops:>8} {r.ops_per_sec:>12.1f} "
            f"{r.p50_ms:>9.2f} {r.p95_ms:>9.2f} {'Y' if r.ok else 'N':>4}"
        )
        if r.error:
            lines.append(f"  ERROR: {r.error}")
        if r.extra:
            interesting = {
                k: v
                for k, v in r.extra.items()
                if k
                in {
                    "fills",
                    "headroom_x",
                    "bars",
                    "batch",
                    "n_features",
                    "pipeline_ops_per_sec",
                }
            }
            if interesting:
                lines.append(f"  extra={interesting}")
    return "\n".join(lines)


def evaluate_gates(report: LoadTestReport) -> list[str]:
    """Мягкие пороги относительно 5m paper / WS нагрузки."""
    fails: list[str] = []
    by = {r.name: r for r in report.results if r.ok}
    if "parse_events" in by and by["parse_events"].ops_per_sec < 50_000:
        fails.append("parse_events < 50k/s")
    if "features_build" in by and by["features_build"].p95_ms > 500:
        fails.append("features_build p95 > 500ms")
    if "paper_live_path" in by:
        p95 = by["paper_live_path"].p95_ms
        if p95 > 2_000:
            fails.append(f"paper_live_path p95 {p95:.0f}ms > 2000ms (5m bar budget)")
    # sequential xadd ~как ingest; 800/s хватает на 2–4 символа bookTicker+trades
    if "redis_xadd" in by and by["redis_xadd"].ops_per_sec < 800:
        fails.append("redis_xadd < 800/s")
    if "db_temp_insert" in by and by["db_temp_insert"].ops_per_sec < 1_000:
        fails.append("db_temp_insert < 1k/s")
    for r in report.results:
        if not r.ok:
            fails.append(f"{r.name} failed: {r.error}")
    return fails
