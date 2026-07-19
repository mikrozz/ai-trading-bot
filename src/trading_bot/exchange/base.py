"""Абстракция биржевого адаптера (Binance / будущий MEXC)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Balance:
    asset: str
    free: float
    locked: float


@dataclass(slots=True)
class OrderResult:
    symbol: str
    order_id: int
    client_order_id: str
    status: str
    side: str
    order_type: str
    price: float
    orig_qty: float
    executed_qty: float
    raw: dict[str, Any]


class SpotExchange(ABC):
    @abstractmethod
    async def ping(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def server_time(self) -> int:
        raise NotImplementedError

    @abstractmethod
    async def exchange_info(self, symbol: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def account(self, omit_zero_balances: bool = True) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def create_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        price: str | None = None,
        time_in_force: str | None = "GTC",
        new_client_order_id: str | None = None,
    ) -> OrderResult:
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(
        self,
        *,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError
