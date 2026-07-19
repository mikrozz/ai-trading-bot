"""Binance Spot REST клиент (testnet / mainnet через base_url)."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from trading_bot.exchange.base import OrderResult, SpotExchange
from trading_bot.logging_setup import get_logger

log = get_logger(__name__)


class BinanceAPIError(RuntimeError):
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.payload = payload
        super().__init__(f"Binance API error status={status} payload={payload}")


class BinanceSpotClient(SpotExchange):
    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://testnet.binance.vision",
        session: aiohttp.ClientSession | None = None,
        recv_window: int = 5000,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.recv_window = recv_window
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        params = dict(params or {})
        headers: dict[str, str] = {}
        if signed:
            if not self.api_key or not self.api_secret:
                raise ValueError("Signed request requires API key/secret")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self.recv_window
            params["signature"] = self._sign(params)
            headers["X-MBX-APIKEY"] = self.api_key
        elif self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        session = await self._get_session()
        url = f"{self.base_url}{path}"
        async with session.request(method, url, params=params, headers=headers) as resp:
            text = await resp.text()
            try:
                payload: Any = await resp.json(content_type=None)
            except Exception:
                payload = text
            if resp.status >= 400:
                raise BinanceAPIError(resp.status, payload)
            return payload

    async def ping(self) -> bool:
        await self._request("GET", "/api/v3/ping")
        return True

    async def server_time(self) -> int:
        data = await self._request("GET", "/api/v3/time")
        return int(data["serverTime"])

    async def exchange_info(self, symbol: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        return await self._request("GET", "/api/v3/exchangeInfo", params=params)

    async def account(self, omit_zero_balances: bool = True) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if omit_zero_balances:
            params["omitZeroBalances"] = "true"
        return await self._request("GET", "/api/v3/account", params=params, signed=True)

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        data = await self._request("GET", "/api/v3/openOrders", params=params, signed=True)
        return list(data)

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
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity,
        }
        if order_type.upper() == "LIMIT":
            if price is None:
                raise ValueError("LIMIT order requires price")
            params["price"] = price
            params["timeInForce"] = time_in_force or "GTC"
        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id

        raw = await self._request("POST", "/api/v3/order", params=params, signed=True)
        return OrderResult(
            symbol=raw["symbol"],
            order_id=int(raw["orderId"]),
            client_order_id=raw.get("clientOrderId", ""),
            status=raw.get("status", ""),
            side=raw.get("side", side),
            order_type=raw.get("type", order_type),
            price=float(raw.get("price") or 0),
            orig_qty=float(raw.get("origQty") or 0),
            executed_qty=float(raw.get("executedQty") or 0),
            raw=raw,
        )

    async def cancel_order(
        self,
        *,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        if order_id is None and client_order_id is None:
            raise ValueError("order_id или client_order_id обязателен")
        return await self._request("DELETE", "/api/v3/order", params=params, signed=True)

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        return await self._request("GET", "/api/v3/klines", params=params)
