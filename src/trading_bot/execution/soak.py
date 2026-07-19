"""Testnet soak: place far LIMIT → cancel (проверка order path + audit)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Any

from trading_bot.exchange.binance_spot import BinanceSpotClient
from trading_bot.logging_setup import get_logger
from trading_bot.metrics import ORDERS_TOTAL

log = get_logger(__name__)


@dataclass
class SoakResult:
    cycles: int = 0
    placed: int = 0
    cancelled: int = 0
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
    # убрать экспоненту
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
    # публичный ticker через exchange — используем klines last close
    kl = await client.klines(symbol, "1m", limit=1)
    if not kl:
        raise RuntimeError("No klines for price")
    return float(kl[0][4])


async def run_soak(
    client: BinanceSpotClient,
    *,
    symbol: str = "BTCUSDT",
    cycles: int = 3,
    pause_sec: float = 1.0,
    price_factor: float = 0.5,
) -> SoakResult:
    """Ставит BUY LIMIT далеко от рынка и сразу отменяет."""
    result = SoakResult()
    info = await client.exchange_info(symbol)
    filters = parse_symbol_filters(info, symbol)
    if filters.get("status") and filters["status"] != "TRADING":
        raise RuntimeError(f"Symbol not TRADING: {filters.get('status')}")

    step = filters.get("stepSize", "0.00001")
    tick = filters.get("tickSize", "0.01")
    min_qty = float(filters.get("minQty", "0.00001"))
    min_notional = float(filters.get("minNotional", "5"))

    for i in range(cycles):
        result.cycles += 1
        ok = False
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                last = await fetch_last_price(client, symbol)
                price = last * price_factor
                qty = max(min_qty, (min_notional * 1.2) / max(price, 1e-12))
                qty_s = _dec_str(qty, step)
                price_s = _dec_str(price, tick)
                if float(qty_s) <= 0 or float(price_s) <= 0:
                    raise RuntimeError(f"Bad qty/price after round: {qty_s} @ {price_s}")

                while float(qty_s) * float(price_s) < min_notional and float(qty_s) < 1e6:
                    qty *= 1.2
                    qty_s = _dec_str(qty, step)

                order = await client.create_order(
                    symbol=symbol,
                    side="BUY",
                    order_type="LIMIT",
                    quantity=qty_s,
                    price=price_s,
                    time_in_force="GTC",
                    new_client_order_id=f"soak{i}a{attempt}t{int(asyncio.get_event_loop().time()) % 1_000_000}",
                )
                result.placed += 1
                result.order_ids.append(order.order_id)
                ORDERS_TOTAL.labels(action="place", symbol=symbol.upper(), mode="testnet").inc()
                log.info(
                    "soak_placed",
                    order_id=order.order_id,
                    price=price_s,
                    qty=qty_s,
                    status=order.status,
                )

                await asyncio.sleep(pause_sec)
                await client.cancel_order(symbol=symbol, order_id=order.order_id)
                result.cancelled += 1
                ORDERS_TOTAL.labels(action="cancel", symbol=symbol.upper(), mode="testnet").inc()
                log.info("soak_cancelled", order_id=order.order_id)
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
                await asyncio.sleep(pause_sec)

        if not ok:
            result.errors += 1
            result.last_error = repr(last_exc) if last_exc else "unknown"
            log.warning("soak_error", cycle=i, error=result.last_error)

    return result
