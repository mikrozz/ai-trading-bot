"""Синхронизация локального состояния ордера с биржей."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.exchange.binance_spot import BinanceSpotClient
from trading_bot.logging_setup import get_logger

log = get_logger(__name__)

TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})


@dataclass
class SyncedOrder:
    symbol: str
    order_id: int
    client_order_id: str
    status: str
    side: str
    price: float
    orig_qty: float
    executed_qty: float
    synced_at: str
    in_open_orders: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


async def sync_order(
    client: BinanceSpotClient,
    *,
    symbol: str,
    order_id: int,
) -> SyncedOrder:
    """GET /order + проверка присутствия в openOrders."""
    raw = await client.get_order(symbol=symbol, order_id=order_id)
    open_list = await client.open_orders(symbol=symbol)
    open_ids = {int(o["orderId"]) for o in open_list}
    synced = SyncedOrder(
        symbol=str(raw.get("symbol") or symbol).upper(),
        order_id=int(raw["orderId"]),
        client_order_id=str(raw.get("clientOrderId") or ""),
        status=str(raw.get("status") or ""),
        side=str(raw.get("side") or ""),
        price=float(raw.get("price") or 0),
        orig_qty=float(raw.get("origQty") or 0),
        executed_qty=float(raw.get("executedQty") or 0),
        synced_at=datetime.now(timezone.utc).isoformat(),
        in_open_orders=int(raw["orderId"]) in open_ids,
        raw=raw,
    )
    log.info(
        "order_synced",
        order_id=synced.order_id,
        status=synced.status,
        in_open_orders=synced.in_open_orders,
        executed_qty=synced.executed_qty,
    )
    return synced


def append_audit(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def synced_to_dict(synced: SyncedOrder) -> dict[str, Any]:
    d = asdict(synced)
    d.pop("raw", None)
    return d
