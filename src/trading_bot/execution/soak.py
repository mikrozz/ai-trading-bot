"""Testnet soak: place far LIMIT → sync → cancel → sync (order path + audit)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from trading_bot.exchange.binance_spot import BinanceSpotClient
from trading_bot.execution.order_sync import (
    TERMINAL,
    append_audit,
    sync_order,
    synced_to_dict,
)
from trading_bot.logging_setup import get_logger
from trading_bot.metrics import ORDERS_TOTAL

log = get_logger(__name__)

DEFAULT_AUDIT = Path("data/soak_audit.jsonl")


@dataclass
class SoakResult:
    cycles: int = 0
    placed: int = 0
    cancelled: int = 0
    synced_ok: int = 0
    errors: int = 0
    last_error: str = ""
    order_ids: list[int] = field(default_factory=list)


def _dec_str(value: float, step: str) -> str:
    """Округлить quantity/price вниз к шагу фильтра."""
    step_d = Decimal(step)
    q = Decimal(str(value))
    if step_d <= 0:
        return format(q, "f")
    rounded = (q / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d
    return format(rounded.normalize(), "f")


def parse_symbol_filters(info: dict[str, Any], symbol: str) -> dict[str, str]:
    symbols = info.get("symbols") or []
    for s in symbols:
        if s.get("symbol") == symbol.upper():
            out = {"status": s.get("status", "")}
            for f in s.get("filters", []):
                ft = f.get("filterType")
                if ft == "LOT_SIZE":
                    out["stepSize"] = f["stepSize"]
                    out["minQty"] = f["minQty"]
                elif ft == "PRICE_FILTER":
                    out["tickSize"] = f["tickSize"]
                    out["minPrice"] = f["minPrice"]
                elif ft == "NOTIONAL":
                    out["minNotional"] = f.get("minNotional") or f.get("notional", "0")
                elif ft == "MIN_NOTIONAL":
                    out["minNotional"] = f.get("minNotional", "0")
            return out
    raise ValueError(f"Symbol not found in exchangeInfo: {symbol}")


async def fetch_last_price(client: BinanceSpotClient, symbol: str) -> float:
    """Цена для soak: ticker/price, fallback kline close."""
    try:
        return await client.ticker_price(symbol)
    except Exception:
        kl = await client.klines(symbol, "1m", limit=1)
        if not kl:
            raise RuntimeError("No klines for price")
        return float(kl[0][4])


def parse_bid_multiplier_down(info: dict[str, Any], symbol: str) -> float:
    for s in info.get("symbols") or []:
        if s.get("symbol") != symbol.upper():
            continue
        for f in s.get("filters", []):
            if f.get("filterType") == "PERCENT_PRICE_BY_SIDE":
                return float(f.get("bidMultiplierDown") or 0.5)
    return 0.5


async def run_soak(
    client: BinanceSpotClient,
    *,
    symbol: str = "BTCUSDT",
    cycles: int = 3,
    pause_sec: float = 1.0,
    price_factor: float | None = None,
    audit_path: Path | None = DEFAULT_AUDIT,
) -> SoakResult:
    """Ставит BUY LIMIT далеко от рынка, синхронизирует статус, отменяет, снова sync."""
    result = SoakResult()
    info = await client.exchange_info(symbol)
    filters = parse_symbol_filters(info, symbol)
    if filters.get("status") and filters["status"] != "TRADING":
        raise RuntimeError(f"Symbol not TRADING: {filters.get('status')}")

    step = filters.get("stepSize", "0.00001")
    tick = filters.get("tickSize", "0.01")
    min_qty = float(filters.get("minQty", "0.00001"))
    min_notional = float(filters.get("minNotional", "5"))
    bid_down = parse_bid_multiplier_down(info, symbol)
    # чуть выше нижней границы PERCENT_PRICE_BY_SIDE, иначе testnet режет ордер
    effective_factor = price_factor if price_factor is not None else min(0.85, bid_down * 1.15)
    audit = audit_path or DEFAULT_AUDIT

    for i in range(cycles):
        result.cycles += 1
        ok = False
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                last = await fetch_last_price(client, symbol)
                price = last * effective_factor
                qty = max(min_qty, (min_notional * 1.2) / max(price, 1e-12))
                qty_s = _dec_str(qty, step)
                price_s = _dec_str(price, tick)
                if float(qty_s) <= 0 or float(price_s) <= 0:
                    raise RuntimeError(f"Bad qty/price after round: {qty_s} @ {price_s}")

                while float(qty_s) * float(price_s) < min_notional and float(qty_s) < 1e6:
                    qty *= 1.2
                    qty_s = _dec_str(qty, step)

                client_oid = f"soak{i}a{attempt}t{int(asyncio.get_event_loop().time()) % 1_000_000}"
                order = await client.create_order(
                    symbol=symbol,
                    side="BUY",
                    order_type="LIMIT",
                    quantity=qty_s,
                    price=price_s,
                    time_in_force="GTC",
                    new_client_order_id=client_oid,
                )
                result.placed += 1
                result.order_ids.append(order.order_id)
                ORDERS_TOTAL.labels(action="place", symbol=symbol.upper(), mode="testnet").inc()

                after_place = await sync_order(client, symbol=symbol, order_id=order.order_id)
                if after_place.status not in {"NEW", "PARTIALLY_FILLED"}:
                    raise RuntimeError(
                        f"Unexpected status after place: {after_place.status}"
                    )
                if not after_place.in_open_orders and after_place.status == "NEW":
                    raise RuntimeError("NEW order missing from openOrders")
                result.synced_ok += 1
                append_audit(
                    audit,
                    {
                        "event": "soak_placed_synced",
                        "cycle": i,
                        "attempt": attempt,
                        "order": synced_to_dict(after_place),
                    },
                )
                log.info(
                    "soak_placed",
                    order_id=order.order_id,
                    price=price_s,
                    qty=qty_s,
                    status=after_place.status,
                )

                await asyncio.sleep(pause_sec)
                await client.cancel_order(symbol=symbol, order_id=order.order_id)
                result.cancelled += 1
                ORDERS_TOTAL.labels(action="cancel", symbol=symbol.upper(), mode="testnet").inc()

                after_cancel = await sync_order(client, symbol=symbol, order_id=order.order_id)
                if after_cancel.status not in TERMINAL:
                    raise RuntimeError(
                        f"Order not terminal after cancel: {after_cancel.status}"
                    )
                if after_cancel.in_open_orders:
                    raise RuntimeError("Cancelled order still in openOrders")
                result.synced_ok += 1
                append_audit(
                    audit,
                    {
                        "event": "soak_cancelled_synced",
                        "cycle": i,
                        "attempt": attempt,
                        "order": synced_to_dict(after_cancel),
                    },
                )
                log.info(
                    "soak_cancelled",
                    order_id=order.order_id,
                    status=after_cancel.status,
                )
                ok = True
                break
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "soak_retry",
                    cycle=i,
                    attempt=attempt,
                    error=repr(exc),
                )
                append_audit(
                    audit,
                    {
                        "event": "soak_error",
                        "cycle": i,
                        "attempt": attempt,
                        "error": repr(exc),
                    },
                )
                await asyncio.sleep(pause_sec)

        if not ok:
            result.errors += 1
            result.last_error = repr(last_exc) if last_exc else "unknown"
            log.warning("soak_error", cycle=i, error=result.last_error)

    return result
