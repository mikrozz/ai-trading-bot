"""WebSocket ingest Binance (trades / bookTicker / klines) → Redis Streams."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from trading_bot.logging_setup import get_logger
from trading_bot.storage.redis_streams import RedisStreamPublisher

log = get_logger(__name__)

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class BinanceWsIngest:
    def __init__(
        self,
        *,
        ws_base_url: str,
        symbols: list[str],
        intervals: list[str],
        publisher: RedisStreamPublisher | None = None,
        on_event: EventHandler | None = None,
        reconnect_base_sec: float = 1.0,
        reconnect_max_sec: float = 60.0,
    ) -> None:
        self.ws_base_url = ws_base_url.rstrip("/")
        self.symbols = [s.lower() for s in symbols]
        self.intervals = intervals
        self.publisher = publisher
        self.on_event = on_event
        self.reconnect_base_sec = reconnect_base_sec
        self.reconnect_max_sec = reconnect_max_sec
        self._stop = asyncio.Event()
        self.messages_ok = 0
        self.messages_err = 0

    def _stream_names(self) -> list[str]:
        streams: list[str] = []
        for symbol in self.symbols:
            streams.append(f"{symbol}@trade")
            streams.append(f"{symbol}@bookTicker")
            for interval in self.intervals:
                streams.append(f"{symbol}@kline_{interval}")
        return streams

    def _combined_url(self) -> str:
        streams = "/".join(self._stream_names())
        # combined stream path
        return f"{self.ws_base_url}/stream?streams={streams}"

    async def stop(self) -> None:
        self._stop.set()

    async def _dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publisher is not None:
            await self.publisher.publish(event_type, payload)
        if self.on_event is not None:
            await self.on_event(event_type, payload)

    async def _handle_message(self, raw: str) -> None:
        data = json.loads(raw)
        payload = data.get("data", data)
        event = payload.get("e") or data.get("stream", "unknown")
        await self._dispatch(str(event), payload)
        self.messages_ok += 1

    async def run(self) -> None:
        delay = self.reconnect_base_sec
        url = self._combined_url()
        log.info("ws_ingest_start", url=url, symbols=self.symbols)

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=1024,
                ) as ws:
                    delay = self.reconnect_base_sec
                    log.info("ws_connected")
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.messages_err += 1
                log.warning("ws_disconnected", error=str(exc), reconnect_in=delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, self.reconnect_max_sec)

        log.info(
            "ws_ingest_stopped",
            messages_ok=self.messages_ok,
            messages_err=self.messages_err,
        )

    async def _read_loop(self, ws: ClientConnection) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except TimeoutError:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                await self._handle_message(raw)
            except Exception as exc:
                self.messages_err += 1
                log.warning("ws_message_error", error=str(exc))
