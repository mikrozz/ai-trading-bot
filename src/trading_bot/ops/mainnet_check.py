"""Dry-run проверка Binance mainnet API (только чтение, без ордеров)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.exchange.binance_spot import BinanceAPIError, BinanceSpotClient
from trading_bot.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_AUDIT = Path("data/mainnet_check.jsonl")
DEFAULT_MAINNET_ENV = Path.home() / ".config" / "trading-bot" / "binance_mainnet.env"


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str = ""
    latency_ms: float = 0.0


@dataclass
class MainnetCheckResult:
    started_at: str
    base_url: str
    public_ok: bool
    signed_attempted: bool
    signed_ok: bool
    geo_blocked: bool
    items: list[CheckItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if not self.public_ok:
            return False
        if self.signed_attempted and not self.signed_ok:
            return False
        return True


def load_mainnet_credentials(
    env_file: Path | None = None,
) -> tuple[str, str]:
    """
    Ключи mainnet (отдельно от testnet).
    Приоритет: BINANCE_MAINNET_API_KEY/SECRET в окружении,
    затем файл ~/.config/trading-bot/binance_mainnet.env
    (в файле допускаются и BINANCE_API_KEY — только из этого файла).
    Testnet env намеренно не читаем.
    """
    key = (os.environ.get("BINANCE_MAINNET_API_KEY") or "").strip()
    secret = (os.environ.get("BINANCE_MAINNET_API_SECRET") or "").strip()
    if key and secret:
        return key, secret

    path = env_file or DEFAULT_MAINNET_ENV
    if not path.exists():
        return "", ""

    file_vars: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, value = line.split("=", 1)
        file_vars[k.strip()] = value.strip().strip("'").strip('"')

    key = (
        file_vars.get("BINANCE_MAINNET_API_KEY")
        or file_vars.get("BINANCE_API_KEY")
        or ""
    ).strip()
    secret = (
        file_vars.get("BINANCE_MAINNET_API_SECRET")
        or file_vars.get("BINANCE_API_SECRET")
        or ""
    ).strip()
    return key, secret


def _geo_hint_from_error(exc: BaseException) -> bool:
    text = repr(exc).lower()
    markers = (
        "451",
        "403",
        "restricted",
        "unavailable for legal",
        "cloudfront",
        "not available in your country",
        "blocked",
    )
    if isinstance(exc, BinanceAPIError):
        if exc.status in {403, 418, 451}:
            return True
        text = f"{text} {exc.payload!r}".lower()
    return any(m in text for m in markers)


async def _timed(name: str, coro) -> CheckItem:
    import time

    t0 = time.perf_counter()
    try:
        data = await coro
        ms = (time.perf_counter() - t0) * 1000.0
        detail = _summarize_payload(name, data)
        return CheckItem(name=name, ok=True, detail=detail, latency_ms=ms)
    except BinanceAPIError as exc:
        ms = (time.perf_counter() - t0) * 1000.0
        geo = _geo_hint_from_error(exc)
        return CheckItem(
            name=name,
            ok=False,
            detail=f"status={exc.status} geo_hint={geo} payload={exc.payload!r}"[:300],
            latency_ms=ms,
        )
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000.0
        geo = _geo_hint_from_error(exc)
        detail = f"geo_hint={geo} error={exc!r}"[:300]
        return CheckItem(name=name, ok=False, detail=detail, latency_ms=ms)


def _summarize_payload(name: str, data: Any) -> str:
    if name == "ping":
        return "pong"
    if name == "time" and isinstance(data, int):
        return f"serverTime={data}"
    if name == "ticker_price":
        return f"price={data}"
    if name == "exchange_info" and isinstance(data, dict):
        syms = data.get("symbols") or []
        status = syms[0].get("status") if syms else "?"
        return f"symbols={len(syms)} status={status}"
    if name == "account" and isinstance(data, dict):
        return (
            f"canTrade={data.get('canTrade')} "
            f"balances={len(data.get('balances') or [])}"
        )
    if name == "open_orders" and isinstance(data, list):
        return f"open_orders={len(data)}"
    return "ok"


async def run_mainnet_check(
    *,
    base_url: str = "https://api.binance.com",
    symbol: str = "BTCUSDT",
    api_key: str = "",
    api_secret: str = "",
    require_signed: bool = False,
    audit_path: Path | None = DEFAULT_AUDIT,
) -> MainnetCheckResult:
    """
    Dry-run: public REST + опционально signed account/openOrders.
    Ордера НЕ создаются и НЕ отменяются.
    """
    result = MainnetCheckResult(
        started_at=datetime.now(timezone.utc).isoformat(),
        base_url=base_url.rstrip("/"),
        public_ok=False,
        signed_attempted=False,
        signed_ok=False,
        geo_blocked=False,
    )
    client = BinanceSpotClient(
        api_key=api_key,
        api_secret=api_secret,
        base_url=base_url,
        timeout_sec=30.0,
    )
    try:
        public_factories = [
            ("ping", client.ping),
            ("time", client.server_time),
            ("ticker_price", lambda: client.ticker_price(symbol)),
            ("exchange_info", lambda: client.exchange_info(symbol)),
        ]
        public_items: list[CheckItem] = []
        for name, factory in public_factories:
            item = await _timed(name, factory())
            public_items.append(item)
            result.items.append(item)
            if not item.ok and "geo_hint=True" in item.detail:
                result.geo_blocked = True

        result.public_ok = all(i.ok for i in public_items)
        if not result.public_ok:
            result.notes.append("public_endpoints_failed")

        if api_key and api_secret:
            result.signed_attempted = True
            result.notes.append("signed_read_only_account_openOrders")
            signed_items = [
                await _timed("account", client.account(omit_zero_balances=True)),
                await _timed("open_orders", client.open_orders(symbol)),
            ]
            result.items.extend(signed_items)
            result.signed_ok = all(i.ok for i in signed_items)
            for item in signed_items:
                if not item.ok and "geo_hint=True" in item.detail:
                    result.geo_blocked = True
            if not result.signed_ok:
                result.notes.append("signed_failed")
        else:
            result.notes.append("signed_skipped_no_mainnet_keys")
            if require_signed:
                result.notes.append("require_signed_failed")
    finally:
        await client.close()

    # явный запрет: этот модуль не должен импортировать create_order в runtime path
    result.notes.append("no_orders_placed")

    if audit_path is not None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "event": "mainnet_check",
                        **{k: v for k, v in asdict(result).items()},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    log.info(
        "mainnet_check_done",
        public_ok=result.public_ok,
        signed_attempted=result.signed_attempted,
        signed_ok=result.signed_ok,
        geo_blocked=result.geo_blocked,
    )
    return result
