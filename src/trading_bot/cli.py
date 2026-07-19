"""CLI: smoke / ingest / writer / features / version."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from trading_bot.config import build_settings
from trading_bot.exchange.binance_spot import BinanceSpotClient
from trading_bot.logging_setup import get_logger, setup_logging
from trading_bot.risk.gates import HardRiskGate, RiskLimits, RiskState


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-bot", description="AI Trading Bot MVP")
    parser.add_argument("--config", type=Path, default=None, help="Путь к YAML конфигу")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Путь к env с ключами (по умолчанию ~/.config/trading-bot/binance_testnet.env)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="Версия пакета")
    sub.add_parser("smoke", help="Smoke-check Binance (ping/time/account/openOrders)")

    ingest = sub.add_parser("ingest", help="Запуск WS ingest → Redis Streams")
    ingest.add_argument("--seconds", type=int, default=30, help="Длительность прогона")
    ingest.add_argument("--no-redis", action="store_true", help="Без Redis")

    writer = sub.add_parser("writer", help="Batch writer Redis → TimescaleDB")
    writer.add_argument("--seconds", type=int, default=30, help="Длительность прогона")

    pipeline = sub.add_parser("pipeline", help="Ingest + writer параллельно (короткий прогон)")
    pipeline.add_argument("--seconds", type=int, default=20)

    features = sub.add_parser("features", help="Построить фичи по klines (REST bootstrap)")
    features.add_argument("--symbol", default="BTCUSDT")
    features.add_argument("--interval", default="5m")
    features.add_argument("--limit", type=int, default=200)

    risk = sub.add_parser("risk-demo", help="Демо hard risk gate")
    risk.add_argument("--equity", type=float, default=1000.0)
    risk.add_argument("--notional", type=float, default=50.0)
    return parser


async def cmd_smoke(config: Path | None, env_file: Path | None) -> int:
    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    log = get_logger("smoke")
    settings.require_trading_credentials()

    client = BinanceSpotClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        base_url=settings.binance_base_url,
    )
    try:
        ok = await client.ping()
        server_time = await client.server_time()
        account = await client.account(omit_zero_balances=True)
        open_orders = await client.open_orders()
        bals = account.get("balances", [])
        log.info(
            "smoke_ok",
            ping=ok,
            server_time=server_time,
            can_trade=account.get("canTrade"),
            balances=len(bals),
            open_orders=len(open_orders),
            base_url=settings.binance_base_url,
            execution_mode=settings.execution_mode.value,
        )
        print("SMOKE_OK")
        return 0
    finally:
        await client.close()


async def cmd_ingest(
    config: Path | None,
    env_file: Path | None,
    seconds: int,
    no_redis: bool,
) -> int:
    from trading_bot.marketdata.ws_ingest import BinanceWsIngest

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    log = get_logger("ingest")

    publisher = None
    redis = None
    if not no_redis:
        from redis.asyncio import from_url

        from trading_bot.storage.redis_streams import RedisStreamPublisher

        redis = from_url(settings.redis_url, decode_responses=True)
        publisher = RedisStreamPublisher(redis)

    ingest = BinanceWsIngest(
        ws_base_url=settings.market_ws_base(),
        symbols=settings.symbols,
        intervals=settings.intervals,
        publisher=publisher,
    )
    task = asyncio.create_task(ingest.run())
    try:
        await asyncio.sleep(seconds)
        await ingest.stop()
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
        if redis is not None:
            await redis.aclose()

    log.info(
        "ingest_done",
        messages_ok=ingest.messages_ok,
        messages_err=ingest.messages_err,
        seconds=seconds,
    )
    print(f"INGEST_OK messages={ingest.messages_ok}")
    return 0 if ingest.messages_ok > 0 else 1


async def cmd_writer(config: Path | None, env_file: Path | None, seconds: int) -> int:
    from redis.asyncio import from_url

    from trading_bot.storage.batch_writer import BatchWriter

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    log = get_logger("writer")

    redis = from_url(settings.redis_url, decode_responses=True)
    writer = BatchWriter(redis=redis, database_url=settings.database_url)
    try:
        await writer.run(max_seconds=float(seconds))
    finally:
        await redis.aclose()

    log.info(
        "writer_summary",
        trades=writer.written_trades,
        klines=writer.written_klines,
        errors=writer.errors,
    )
    print(
        f"WRITER_OK trades={writer.written_trades} "
        f"klines={writer.written_klines} errors={writer.errors}"
    )
    return 0 if writer.errors == 0 and (writer.written_trades + writer.written_klines) > 0 else 1


async def cmd_pipeline(config: Path | None, env_file: Path | None, seconds: int) -> int:
    """Параллельно ingest + writer для end-to-end проверки."""
    from redis.asyncio import from_url

    from trading_bot.marketdata.ws_ingest import BinanceWsIngest
    from trading_bot.storage.batch_writer import BatchWriter
    from trading_bot.storage.redis_streams import RedisStreamPublisher

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    log = get_logger("pipeline")

    redis = from_url(settings.redis_url, decode_responses=True)
    publisher = RedisStreamPublisher(redis)
    ingest = BinanceWsIngest(
        ws_base_url=settings.market_ws_base(),
        symbols=settings.symbols,
        intervals=settings.intervals,
        publisher=publisher,
    )
    writer = BatchWriter(redis=redis, database_url=settings.database_url)

    ingest_task = asyncio.create_task(ingest.run())
    writer_task = asyncio.create_task(writer.run(max_seconds=float(seconds)))
    try:
        await asyncio.sleep(seconds)
        await ingest.stop()
        await asyncio.wait_for(ingest_task, timeout=15)
        await asyncio.wait_for(writer_task, timeout=15)
    finally:
        if not ingest_task.done():
            ingest_task.cancel()
        if not writer_task.done():
            await writer.stop()
        await redis.aclose()

    log.info(
        "pipeline_done",
        messages_ok=ingest.messages_ok,
        trades=writer.written_trades,
        klines=writer.written_klines,
        errors=writer.errors,
    )
    print(
        f"PIPELINE_OK messages={ingest.messages_ok} "
        f"trades={writer.written_trades} klines={writer.written_klines}"
    )
    ok = ingest.messages_ok > 0 and (writer.written_trades + writer.written_klines) > 0
    return 0 if ok and writer.errors == 0 else 1


async def cmd_features(
    config: Path | None,
    env_file: Path | None,
    symbol: str,
    interval: str,
    limit: int,
) -> int:
    from datetime import datetime, timezone

    import pandas as pd

    from trading_bot.features.engineering import FEATURE_COLUMNS, build_feature_frame

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    log = get_logger("features")

    client = BinanceSpotClient(base_url=settings.market_rest_base())
    try:
        raw = await client.klines(symbol, interval, limit=limit)
    finally:
        await client.close()

    rows = []
    for k in raw:
        rows.append(
            {
                "ts": datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "symbol": symbol.upper(),
            }
        )
    df = pd.DataFrame(rows)
    feats = build_feature_frame(df)
    present = [c for c in FEATURE_COLUMNS if c in feats.columns]
    last = feats.dropna(subset=["rsi", "macd"]).tail(1)
    log.info(
        "features_ok",
        symbol=symbol,
        interval=interval,
        rows=len(feats),
        feature_count=len(present),
    )
    print(f"FEATURES_OK rows={len(feats)} features={len(present)}")
    if not last.empty:
        print(
            f"last_close={float(last.iloc[0]['close']):.4f} "
            f"rsi={float(last.iloc[0]['rsi']):.2f} "
            f"macd={float(last.iloc[0]['macd']):.6f}"
        )
    return 0 if len(present) >= 15 else 1


def cmd_risk_demo(equity: float, notional: float) -> int:
    setup_logging("INFO", json_logs=False)
    gate = HardRiskGate(RiskLimits())
    state = RiskState(
        equity=equity,
        day_start_equity=equity,
        week_start_equity=equity,
        open_positions=0,
    )
    result = gate.check_new_order(
        state,
        symbol="BTCUSDT",
        order_notional=notional,
        is_new_position=True,
    )
    print(f"decision={result.decision.value} reason={result.reason}")
    return 0 if result.decision.value == "allow" else 1


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        from trading_bot import __version__

        print(__version__)
        return

    if args.command == "smoke":
        raise SystemExit(asyncio.run(cmd_smoke(args.config, args.env_file)))

    if args.command == "ingest":
        raise SystemExit(
            asyncio.run(cmd_ingest(args.config, args.env_file, args.seconds, args.no_redis))
        )

    if args.command == "writer":
        raise SystemExit(asyncio.run(cmd_writer(args.config, args.env_file, args.seconds)))

    if args.command == "pipeline":
        raise SystemExit(asyncio.run(cmd_pipeline(args.config, args.env_file, args.seconds)))

    if args.command == "features":
        raise SystemExit(
            asyncio.run(
                cmd_features(
                    args.config,
                    args.env_file,
                    args.symbol,
                    args.interval,
                    args.limit,
                )
            )
        )

    if args.command == "risk-demo":
        raise SystemExit(cmd_risk_demo(args.equity, args.notional))

    parser.error(f"unknown command {args.command}")
    sys.exit(2)


if __name__ == "__main__":
    main()
