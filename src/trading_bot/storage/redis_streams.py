"""Публикация market-data событий в Redis Streams (hot path)."""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis

from trading_bot.logging_setup import get_logger

log = get_logger(__name__)


class RedisStreamPublisher:
    def __init__(
        self,
        redis: Redis,
        *,
        stream: str = "md:events",
        maxlen: int = 100_000,
    ) -> None:
        self.redis = redis
        self.stream = stream
        self.maxlen = maxlen

    async def publish(self, event_type: str, payload: dict[str, Any]) -> str:
        fields = {
            "type": event_type,
            "payload": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        }
        msg_id = await self.redis.xadd(
            self.stream,
            fields,
            maxlen=self.maxlen,
            approximate=True,
        )
        return msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
