"""CLI: smoke / ingest / writer / features / version."""

from __future__ import annotations

import argparse
import asyncio
import signal
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
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=9108,
        help="Prometheus /metrics port (0 = выкл)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="Версия пакета")
    sub.add_parser("smoke", help="Smoke-check Binance (ping/time/account/openOrders)")

    latency = sub.add_parser(
        "latency",
        help="Latency probe REST/WS → testnet + mainnet (публичные эндпоинты)",
    )
    latency.add_argument("--rounds", type=int, default=5)
    latency.add_argument("--symbol", default="BTCUSDT")
    latency.add_argument("--no-ws", action="store_true", help="Без WS first-message")
    latency.add_argument(
        "--targets",
        default="testnet,mainnet",
        help="CSV: testnet,mainnet",
    )
    latency.add_argument(
        "--hold-sec",
        type=float,
        default=0.0,
        help="Держать процесс для scrape метрик (с --metrics-port)",
    )

    mainnet = sub.add_parser(
        "mainnet-check",
        help="Dry-run mainnet API (только чтение, без ордеров)",
    )
    mainnet.add_argument("--symbol", default="BTCUSDT")
    mainnet.add_argument(
        "--mainnet-env",
        type=Path,
        default=None,
        help="Env с mainnet ключами (default: ~/.config/trading-bot/binance_mainnet.env)",
    )
    mainnet.add_argument(
        "--require-signed",
        action="store_true",
        help="Требовать успешный signed account/openOrders",
    )

    ingest = sub.add_parser("ingest", help="Запуск WS ingest → Redis Streams")
    ingest.add_argument(
        "--seconds",
        type=int,
        default=30,
        help="Длительность прогона (0 = до SIGTERM/systemd)",
    )
    ingest.add_argument("--no-redis", action="store_true", help="Без Redis")

    writer = sub.add_parser("writer", help="Batch writer Redis → TimescaleDB")
    writer.add_argument(
        "--seconds",
        type=int,
        default=30,
        help="Длительность прогона (0 = до SIGTERM/systemd)",
    )

    pipeline = sub.add_parser("pipeline", help="Ingest + writer параллельно (короткий прогон)")
    pipeline.add_argument("--seconds", type=int, default=20)

    features = sub.add_parser("features", help="Построить фичи по klines (REST bootstrap)")
    features.add_argument("--symbol", default="BTCUSDT")
    features.add_argument("--interval", default="5m")
    features.add_argument("--limit", type=int, default=200)

    bootstrap = sub.add_parser("bootstrap", help="Исторические klines → TimescaleDB")
    bootstrap.add_argument("--months", type=int, default=6)
    bootstrap.add_argument("--interval", default="5m")
    bootstrap.add_argument("--symbols", default=None, help="CSV, иначе из конфига")

    train = sub.add_parser("train", help="Walk-forward XGBoost + сохранить модель")
    train.add_argument("--symbol", default="BTCUSDT")
    train.add_argument("--interval", default="5m")
    train.add_argument("--folds", type=int, default=5)
    train.add_argument(
        "--model-out",
        type=Path,
        default=Path("data/models/xgb_btc_5m.joblib"),
    )

    paper = sub.add_parser("paper", help="Paper backfill на истории из БД")
    paper.add_argument("--symbol", default="BTCUSDT")
    paper.add_argument("--interval", default="5m")
    paper.add_argument(
        "--model",
        type=Path,
        default=Path("data/models/xgb_btc_5m.joblib"),
    )
    paper.add_argument("--cash", type=float, default=1000.0)

    live = sub.add_parser("paper-live", help="Live paper на WS (закрытые klines)")
    live.add_argument("--symbol", default="BTCUSDT")
    live.add_argument("--interval", default="5m")
    live.add_argument(
        "--model",
        type=Path,
        default=Path("data/models/xgb_btc_5m.joblib"),
    )
    live.add_argument("--cash", type=float, default=1000.0)
    live.add_argument(
        "--seconds",
        type=int,
        default=120,
        help="Длительность (0 = до SIGTERM/systemd)",
    )
    live.add_argument("--no-redis", action="store_true")
    live.add_argument(
        "--no-restore",
        action="store_true",
        help="Не восстанавливать paper state с диска",
    )

    soak = sub.add_parser("soak", help="Testnet soak: LIMIT far + cancel")
    soak.add_argument("--symbol", default="BTCUSDT")
    soak.add_argument("--cycles", type=int, default=3)
    soak.add_argument("--pause", type=float, default=1.0)

    risk = sub.add_parser("risk-demo", help="Демо hard risk gate")
    risk.add_argument("--equity", type=float, default=1000.0)
    risk.add_argument("--notional", type=float, default=50.0)

    load = sub.add_parser(
        "loadtest",
        help="Нагрузочные бенчмарки hot-path (без ордеров на бирже)",
    )
    load.add_argument(
        "--profile",
        choices=("all", "cpu", "redis", "db"),
        default="all",
        help="all=cpu+redis+db",
    )
    load.add_argument(
        "--model",
        type=Path,
        default=Path("data/models/xgb_btc_5m.joblib"),
    )
    load.add_argument("--parse-n", type=int, default=80_000)
    load.add_argument("--redis-n", type=int, default=15_000)
    load.add_argument("--db-n", type=int, default=8_000)
    load.add_argument("--live-bars", type=int, default=150)
    load.add_argument(
        "--no-gates",
        action="store_true",
        help="Не валить exit code при soft gate fail",
    )
    return parser


async def cmd_latency(
    config: Path | None,
    env_file: Path | None,
    *,
    rounds: int,
    symbol: str,
    no_ws: bool,
    targets: str,
    hold_sec: float,
    metrics_port: int,
) -> int:
    from trading_bot.metrics import (
        LATENCY_PROBE_ERRORS,
        LATENCY_PROBE_P50_MS,
        LATENCY_PROBE_P95_MS,
        start_metrics_server,
    )
    from trading_bot.ops.latency_probe import format_latency_table, run_latency_probe

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    wanted = {t.strip().lower() for t in targets.split(",") if t.strip()}
    rest: dict[str, str] = {}
    ws: dict[str, str] = {}
    if "testnet" in wanted:
        rest["testnet"] = settings.binance_base_url
        if not no_ws:
            ws["testnet"] = (
                f"{settings.binance_ws_base_url.rstrip('/')}/ws/"
                f"{symbol.lower()}@bookTicker"
            )
    if "mainnet" in wanted:
        rest["mainnet"] = settings.binance_prod_base_url
        if not no_ws:
            ws["mainnet"] = (
                f"{settings.binance_prod_ws_base_url.rstrip('/')}/ws/"
                f"{symbol.lower()}@bookTicker"
            )
    if not rest:
        print("LATENCY_FAIL no targets")
        return 2

    # oneshot не должен занимать :9108 (ingest). Метрики — только с hold для scrape.
    expose_metrics = metrics_port > 0 and hold_sec > 0
    if expose_metrics:
        start_metrics_server(metrics_port)

    result = await run_latency_probe(
        rest_targets=rest,
        ws_targets=ws or None,
        symbol=symbol,
        rounds=rounds,
    )
    if expose_metrics:
        for s in result.summaries:
            LATENCY_PROBE_P50_MS.labels(target=s.target, endpoint=s.endpoint).set(
                s.p50_ms
            )
            LATENCY_PROBE_P95_MS.labels(target=s.target, endpoint=s.endpoint).set(
                s.p95_ms
            )
            LATENCY_PROBE_ERRORS.labels(target=s.target, endpoint=s.endpoint).set(
                s.errors
            )

    print(format_latency_table(result))
    mainnet_rows = [s for s in result.summaries if s.target == "mainnet"]
    testnet_rows = [s for s in result.summaries if s.target == "testnet"]
    # go-live gate: mainnet критичен; testnet с docker01 может флапать
    if "mainnet" in wanted:
        gate_ok = bool(mainnet_rows) and all(s.errors == 0 for s in mainnet_rows)
    else:
        gate_ok = bool(result.summaries) and all(s.errors == 0 for s in result.summaries)
    p95_rest = [
        s.p95_ms
        for s in mainnet_rows
        if s.endpoint in {"ping", "time", "ticker_price"} and s.ok > 0
    ]
    verdict = "LATENCY_OK" if gate_ok else "LATENCY_FAIL"
    extra = ""
    if p95_rest:
        extra = f" mainnet_rest_p95_ms={max(p95_rest):.1f}"
        if max(p95_rest) > 500:
            extra += " WARN_high_latency"
    if testnet_rows and any(s.errors for s in testnet_rows):
        extra += " testnet_partial_errors"
    if metrics_port > 0 and hold_sec <= 0:
        extra += " metrics_skipped_no_hold"
    print(f"{verdict} host={result.host_hint} rounds={rounds}{extra}")
    if hold_sec > 0:
        await asyncio.sleep(hold_sec)
    return 0 if gate_ok else 1


async def cmd_mainnet_check(
    config: Path | None,
    env_file: Path | None,
    *,
    symbol: str,
    mainnet_env: Path | None,
    require_signed: bool,
) -> int:
    from trading_bot.metrics import MAINNET_CHECK_OK
    from trading_bot.ops.mainnet_check import load_mainnet_credentials, run_mainnet_check

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    api_key, api_secret = load_mainnet_credentials(mainnet_env)
    result = await run_mainnet_check(
        base_url=settings.binance_prod_base_url,
        symbol=symbol,
        api_key=api_key,
        api_secret=api_secret,
        require_signed=require_signed,
    )
    for item in result.items:
        status = "OK" if item.ok else "FAIL"
        print(
            f"  [{status}] {item.name:14} {item.latency_ms:7.1f}ms  {item.detail}"
        )
    if require_signed and not result.signed_attempted:
        MAINNET_CHECK_OK.set(0)
        print("MAINNET_CHECK_FAIL signed_required_but_no_keys")
        print(
            "Put keys in ~/.config/trading-bot/binance_mainnet.env "
            "(BINANCE_MAINNET_API_KEY/SECRET) — read-only permissions."
        )
        return 2

    # ключи были — signed обязан пройти (иначе ложный OK на битых/testnet ключах)
    ok = result.ok if result.signed_attempted or require_signed else result.public_ok
    MAINNET_CHECK_OK.set(1.0 if ok else 0.0)
    tag = "MAINNET_CHECK_OK" if ok else "MAINNET_CHECK_FAIL"
    mode = "public+signed" if result.signed_attempted else "public_only"
    geo = " geo_blocked=1" if result.geo_blocked else ""
    print(
        f"{tag} mode={mode} base={result.base_url} "
        f"public_ok={int(result.public_ok)} signed_ok={int(result.signed_ok)}{geo}"
    )
    print("notes: " + ", ".join(result.notes))
    return 0 if ok else 1


async def cmd_loadtest(
    config: Path | None,
    env_file: Path | None,
    *,
    profile: str,
    model_path: Path,
    parse_n: int,
    redis_n: int,
    db_n: int,
    live_bars: int,
    no_gates: bool,
) -> int:
    from trading_bot.ops.loadtest import (
        evaluate_gates,
        format_loadtest_table,
        run_loadtest,
    )

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    report = await run_loadtest(
        model_path=model_path,
        redis_url=settings.redis_url if profile in {"all", "redis"} else None,
        database_url=settings.database_url if profile in {"all", "db"} else None,
        profile=profile,
        parse_n=parse_n,
        redis_n=redis_n,
        db_n=db_n,
        live_bars=live_bars,
    )
    print(format_loadtest_table(report))
    gates = evaluate_gates(report)
    if gates:
        print("GATES_FAIL: " + "; ".join(gates))
        if not no_gates:
            return 1
    tag = "LOADTEST_OK" if report.ok and not gates else "LOADTEST_WARN"
    if not report.ok:
        tag = "LOADTEST_FAIL"
    print(f"{tag} profile={profile} benches={len(report.results)} host={report.host_hint}")
    return 0 if (report.ok and (no_gates or not gates)) else 1


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
    stop_event = asyncio.Event()

    def _signal_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_stop)
        except NotImplementedError:
            pass

    try:
        if seconds > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=float(seconds))
            except TimeoutError:
                pass
        else:
            # seconds<=0 → работаем до SIGTERM (systemd)
            await stop_event.wait()
        await ingest.stop()
        await asyncio.wait_for(task, timeout=15)
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
    max_seconds = float(seconds) if seconds > 0 else None
    task = asyncio.create_task(writer.run(max_seconds=max_seconds))
    stop_event = asyncio.Event()

    def _signal_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_stop)
        except NotImplementedError:
            pass

    try:
        if seconds > 0:
            await task
        else:
            await stop_event.wait()
            await writer.stop()
            await asyncio.wait_for(task, timeout=30)
    finally:
        if not task.done():
            await writer.stop()
            try:
                await asyncio.wait_for(task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
        await redis.aclose()

    log.info(
        "writer_summary",
        trades=writer.written_trades,
        klines=writer.written_klines,
        errors=writer.errors,
    )
    print(
        f"WRITER_OK trades={writer.written_trades} "
        f"klines={writer.written_klines} books={writer.written_books} "
        f"errors={writer.errors}"
    )
    written = writer.written_trades + writer.written_klines + writer.written_books
    # для continuous (seconds<=0) нулевая запись после короткого стопа допустима только если errors
    if seconds <= 0:
        return 0 if writer.errors == 0 else 1
    return 0 if writer.errors == 0 and written > 0 else 1


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
        f"trades={writer.written_trades} klines={writer.written_klines} "
        f"books={writer.written_books}"
    )
    written = writer.written_trades + writer.written_klines + writer.written_books
    ok = ingest.messages_ok > 0 and written > 0
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


async def cmd_bootstrap(
    config: Path | None,
    env_file: Path | None,
    months: int,
    interval: str,
    symbols_csv: str | None,
) -> int:
    from trading_bot.storage.kline_bootstrap import bootstrap_klines

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    log = get_logger("bootstrap")
    symbols = (
        [s.strip().upper() for s in symbols_csv.split(",") if s.strip()]
        if symbols_csv
        else settings.symbols
    )
    client = BinanceSpotClient(base_url=settings.market_rest_base())
    try:
        counts = await bootstrap_klines(
            client=client,
            database_url=settings.database_url,
            symbols=symbols,
            interval=interval,
            months=months,
        )
    finally:
        await client.close()
    log.info("bootstrap_done", counts=counts, months=months, interval=interval)
    total = sum(counts.values())
    print(f"BOOTSTRAP_OK symbols={counts} total_upserts={total}")
    return 0 if total > 0 else 1


async def cmd_train(
    config: Path | None,
    env_file: Path | None,
    symbol: str,
    interval: str,
    folds: int,
    model_out: Path,
) -> int:
    from trading_bot.ml.dataset import load_book_ticker_df, load_klines_df
    from trading_bot.ml.train_xgb import train_walk_forward

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    log = get_logger("train")
    df = await load_klines_df(settings.database_url, symbol=symbol, interval=interval)
    if df.empty:
        print("TRAIN_FAIL empty dataset — сначала trading-bot bootstrap")
        return 1
    books = await load_book_ticker_df(settings.database_url, symbol=symbol)
    log.info("train_dataset", klines=len(df), book_rows=len(books))
    _model, result = train_walk_forward(
        df, books=books, n_folds=folds, model_out=model_out, min_train_rows=400
    )
    log.info(
        "train_done",
        mean_accuracy=result.mean_accuracy,
        mean_f1=result.mean_f1,
        folds=len(result.folds),
        model=result.model_path,
    )
    print(
        f"TRAIN_OK folds={len(result.folds)} "
        f"acc={result.mean_accuracy:.4f} f1={result.mean_f1:.4f} "
        f"model={result.model_path}"
    )
    return 0


async def cmd_paper(
    config: Path | None,
    env_file: Path | None,
    symbol: str,
    interval: str,
    model_path: Path,
    cash: float,
) -> int:
    from trading_bot.ml.dataset import load_klines_df
    from trading_bot.paper.engine import PaperEngine

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    log = get_logger("paper")
    if not model_path.exists():
        print(f"PAPER_FAIL model not found: {model_path}")
        return 1
    df = await load_klines_df(settings.database_url, symbol=symbol, interval=interval)
    if len(df) < 100:
        print("PAPER_FAIL need more klines — bootstrap first")
        return 1
    engine = PaperEngine(
        model_path=model_path,
        symbol=symbol,
        initial_cash=cash,
        fee_rate=settings.fees_taker,
        slippage=settings.slippage_liquid,
        prob_threshold=settings.paper.prob_threshold,
        min_hold_bars=settings.paper.min_hold_bars,
        cooldown_bars=settings.paper.cooldown_bars,
        position_fraction=settings.paper.position_fraction,
        risk_limits=RiskLimits(
            daily_drawdown_limit=settings.risk.daily_drawdown_limit,
            weekly_drawdown_limit=settings.risk.weekly_drawdown_limit,
            max_position_fraction=settings.risk.max_position_fraction,
            max_open_positions=settings.risk.max_open_positions,
            stop_loss=settings.risk.stop_loss,
            listing_ban_minutes=settings.risk.listing_ban_minutes,
        ),
    )
    state = engine.run_backfill(df)
    ret = (state.equity / cash) - 1.0
    log.info(
        "paper_done",
        equity=state.equity,
        return_pct=ret,
        fills=len(state.fills),
        signals_long=state.signals_long,
    )
    print(
        f"PAPER_OK equity={state.equity:.2f} return={ret*100:.2f}% "
        f"fills={len(state.fills)} long_signals={state.signals_long}"
    )
    return 0


async def cmd_paper_live(
    config: Path | None,
    env_file: Path | None,
    symbol: str,
    interval: str,
    model_path: Path,
    cash: float,
    seconds: int,
    no_redis: bool,
    no_restore: bool = False,
) -> int:
    from trading_bot.paper.live import run_live_paper

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    log = get_logger("paper_live")
    if not model_path.exists():
        print(f"PAPER_LIVE_FAIL model not found: {model_path}")
        return 1
    summary = await run_live_paper(
        database_url=settings.database_url,
        ws_base_url=settings.market_ws_base(),
        redis_url=None if no_redis else settings.redis_url,
        model_path=model_path,
        symbol=symbol,
        interval=interval,
        cash=cash,
        seconds=seconds,
        fee_rate=settings.fees_taker,
        slippage=settings.slippage_liquid,
        prob_threshold=settings.paper.prob_threshold,
        min_hold_bars=settings.paper.min_hold_bars,
        cooldown_bars=settings.paper.cooldown_bars,
        position_fraction=settings.paper.position_fraction,
        risk_limits=RiskLimits(
            daily_drawdown_limit=settings.risk.daily_drawdown_limit,
            weekly_drawdown_limit=settings.risk.weekly_drawdown_limit,
            max_position_fraction=settings.risk.max_position_fraction,
            max_open_positions=settings.risk.max_open_positions,
            stop_loss=settings.risk.stop_loss,
            listing_ban_minutes=settings.risk.listing_ban_minutes,
        ),
        restore_state=not no_restore,
    )
    log.info("paper_live_done", **summary)
    print(
        f"PAPER_LIVE_OK closed_bars={summary['closed_bars']} "
        f"equity={summary['equity']:.2f} fills={summary['fills']} "
        f"book_updates={summary['book_updates']} ws_msg={summary['messages_ok']}"
    )
    if seconds <= 0:
        return 0
    return 0 if summary["messages_ok"] > 0 else 1


async def cmd_soak(
    config: Path | None,
    env_file: Path | None,
    symbol: str,
    cycles: int,
    pause: float,
) -> int:
    from trading_bot.execution.soak import run_soak

    settings = build_settings(config, env_file)
    setup_logging(settings.log_level)
    log = get_logger("soak")
    settings.require_trading_credentials()
    client = BinanceSpotClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        base_url=settings.binance_base_url,
        timeout_sec=60.0,
    )
    try:
        result = await run_soak(
            client, symbol=symbol, cycles=cycles, pause_sec=pause
        )
    finally:
        await client.close()
    log.info(
        "soak_done",
        placed=result.placed,
        cancelled=result.cancelled,
        synced_ok=result.synced_ok,
        errors=result.errors,
        last_error=result.last_error,
    )
    ok = (
        result.errors == 0
        and result.placed >= cycles
        and result.cancelled >= cycles
        and result.synced_ok >= cycles * 2
    )
    tag = "SOAK_OK" if ok else "SOAK_FAIL"
    print(
        f"{tag} placed={result.placed} cancelled={result.cancelled} "
        f"synced_ok={result.synced_ok} errors={result.errors} orders={result.order_ids}"
    )
    return 0 if ok else 1


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

    # метрики для долгоживущих команд
    if args.command in {"ingest", "writer", "pipeline", "paper-live", "soak"} and args.metrics_port:
        from trading_bot.metrics import start_metrics_server

        start_metrics_server(args.metrics_port)

    if args.command == "smoke":
        raise SystemExit(asyncio.run(cmd_smoke(args.config, args.env_file)))

    if args.command == "loadtest":
        raise SystemExit(
            asyncio.run(
                cmd_loadtest(
                    args.config,
                    args.env_file,
                    profile=args.profile,
                    model_path=args.model,
                    parse_n=args.parse_n,
                    redis_n=args.redis_n,
                    db_n=args.db_n,
                    live_bars=args.live_bars,
                    no_gates=args.no_gates,
                )
            )
        )

    if args.command == "latency":
        raise SystemExit(
            asyncio.run(
                cmd_latency(
                    args.config,
                    args.env_file,
                    rounds=args.rounds,
                    symbol=args.symbol,
                    no_ws=args.no_ws,
                    targets=args.targets,
                    hold_sec=args.hold_sec,
                    metrics_port=args.metrics_port,
                )
            )
        )

    if args.command == "mainnet-check":
        raise SystemExit(
            asyncio.run(
                cmd_mainnet_check(
                    args.config,
                    args.env_file,
                    symbol=args.symbol,
                    mainnet_env=args.mainnet_env,
                    require_signed=args.require_signed,
                )
            )
        )

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

    if args.command == "bootstrap":
        raise SystemExit(
            asyncio.run(
                cmd_bootstrap(
                    args.config,
                    args.env_file,
                    args.months,
                    args.interval,
                    args.symbols,
                )
            )
        )

    if args.command == "train":
        raise SystemExit(
            asyncio.run(
                cmd_train(
                    args.config,
                    args.env_file,
                    args.symbol,
                    args.interval,
                    args.folds,
                    args.model_out,
                )
            )
        )

    if args.command == "paper":
        raise SystemExit(
            asyncio.run(
                cmd_paper(
                    args.config,
                    args.env_file,
                    args.symbol,
                    args.interval,
                    args.model,
                    args.cash,
                )
            )
        )

    if args.command == "paper-live":
        raise SystemExit(
            asyncio.run(
                cmd_paper_live(
                    args.config,
                    args.env_file,
                    args.symbol,
                    args.interval,
                    args.model,
                    args.cash,
                    args.seconds,
                    args.no_redis,
                    args.no_restore,
                )
            )
        )

    if args.command == "soak":
        raise SystemExit(
            asyncio.run(
                cmd_soak(
                    args.config,
                    args.env_file,
                    args.symbol,
                    args.cycles,
                    args.pause,
                )
            )
        )

    if args.command == "risk-demo":
        raise SystemExit(cmd_risk_demo(args.equity, args.notional))

    parser.error(f"unknown command {args.command}")
    sys.exit(2)


if __name__ == "__main__":
    main()
