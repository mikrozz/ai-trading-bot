"""CLI: smoke / ingest / version."""

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
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Путь к YAML конфигу",
    )
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
    ingest.add_argument(
        "--no-redis",
        action="store_true",
        help="Только лог событий, без Redis",
    )

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

    async def on_event(event_type: str, payload: dict) -> None:
        # не спамим каждый тик на INFO — только счётчик в конце
        if event_type in {"trade", "bookTicker"}:
            return
        log.debug("md_event", event_type=event_type, symbol=payload.get("s"))

    ingest = BinanceWsIngest(
        ws_base_url=settings.market_ws_base(),
        symbols=settings.symbols,
        intervals=settings.intervals,
        publisher=publisher,
        on_event=on_event,
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
            asyncio.run(
                cmd_ingest(args.config, args.env_file, args.seconds, args.no_redis)
            )
        )

    if args.command == "risk-demo":
        raise SystemExit(cmd_risk_demo(args.equity, args.notional))

    parser.error(f"unknown command {args.command}")
    sys.exit(2)


if __name__ == "__main__":
    main()
