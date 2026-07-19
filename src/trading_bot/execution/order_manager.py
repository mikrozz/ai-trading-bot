"""Order manager: risk gate → exchange (testnet/live) или paper no-op."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_bot.exchange.base import OrderResult, SpotExchange
from trading_bot.logging_setup import get_logger
from trading_bot.risk.gates import HardRiskGate, RiskDecision, RiskState

log = get_logger(__name__)


@dataclass(slots=True)
class PlaceOrderRequest:
    symbol: str
    side: str
    order_type: str
    quantity: str
    price: str | None = None
    notional: float = 0.0
    is_new_position: bool = True
    client_order_id: str | None = None


class OrderManager:
    def __init__(
        self,
        *,
        exchange: SpotExchange | None,
        risk_gate: HardRiskGate,
        risk_state: RiskState,
        paper: bool = False,
    ) -> None:
        self.exchange = exchange
        self.risk_gate = risk_gate
        self.risk_state = risk_state
        self.paper = paper

    async def place(self, req: PlaceOrderRequest) -> dict[str, Any]:
        check = self.risk_gate.check_new_order(
            self.risk_state,
            symbol=req.symbol,
            order_notional=req.notional,
            is_new_position=req.is_new_position,
        )
        if check.decision != RiskDecision.ALLOW:
            log.warning(
                "order_denied",
                decision=check.decision.value,
                reason=check.reason,
                symbol=req.symbol,
            )
            return {
                "ok": False,
                "decision": check.decision.value,
                "reason": check.reason,
            }

        if self.paper or self.exchange is None:
            log.info(
                "paper_order",
                symbol=req.symbol,
                side=req.side,
                order_type=req.order_type,
                quantity=req.quantity,
                price=req.price,
            )
            return {
                "ok": True,
                "mode": "paper",
                "symbol": req.symbol,
                "side": req.side,
                "quantity": req.quantity,
                "price": req.price,
            }

        tif = None if req.order_type.upper() == "MARKET" else "GTC"
        result: OrderResult = await self.exchange.create_order(
            symbol=req.symbol,
            side=req.side,
            order_type=req.order_type,
            quantity=req.quantity,
            price=req.price,
            time_in_force=tif,
            new_client_order_id=req.client_order_id,
        )
        log.info(
            "order_placed",
            symbol=result.symbol,
            order_id=result.order_id,
            status=result.status,
            side=result.side,
        )
        return {"ok": True, "mode": "live", "order": result.raw}
