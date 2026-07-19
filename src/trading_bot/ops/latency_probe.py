"""Latency probe: REST (+ optional WS) RTT к Binance testnet/mainnet."""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

import aiohttp

from trading_bot.exchange.binance_spot import BinanceSpotClient
from trading_bot.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_AUDIT = Path("data/latency_probe.jsonl")


@dataclass
class EndpointSample:
    target: str
    endpoint: str
    ok: bool
    latency_ms: float
    error: str = ""


@dataclass
class EndpointSummary:
    target: str
    endpoint: str
    samples: int
    ok: int
    errors: int
    p50_ms: float
    p95_ms: float
    mean_ms: float
    min_ms: float
    max_ms: float


@dataclass
class LatencyProbeResult:
    started_at: str
    host_hint: str
    samples: list[EndpointSample] = field(default_factory=list)
    summaries: list[EndpointSummary] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return bool(self.summaries) and all(s.errors == 0 for s in self.summaries)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def summarize(samples: list[EndpointSample]) -> list[EndpointSummary]:
    groups: dict[tuple[str, str], list[EndpointSample]] = {}
    for s in samples:
        groups.setdefault((s.target, s.endpoint), []).append(s)
    out: list[EndpointSummary] = []
    for (target, endpoint), rows in sorted(groups.items()):
        ok_ms = [r.latency_ms for r in rows if r.ok]
        out.append(
            EndpointSummary(
                target=target,
                endpoint=endpoint,
                samples=len(rows),
                ok=len(ok_ms),
                errors=len(rows) - len(ok_ms),
                p50_ms=_percentile(ok_ms, 50) if ok_ms else 0.0,
                p95_ms=_percentile(ok_ms, 95) if ok_ms else 0.0,
                mean_ms=float(statistics.mean(ok_ms)) if ok_ms else 0.0,
                min_ms=min(ok_ms) if ok_ms else 0.0,
                max_ms=max(ok_ms) if ok_ms else 0.0,
            )
        )
    return out


async def _time_call(
    target: str,
    endpoint: str,
    coro_factory: Callable[[], Awaitable[Any]],
) -> EndpointSample:
    t0 = time.perf_counter()
    try:
        await coro_factory()
        ms = (time.perf_counter() - t0) * 1000.0
        return EndpointSample(target=target, endpoint=endpoint, ok=True, latency_ms=ms)
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000.0
        return EndpointSample(
            target=target,
            endpoint=endpoint,
            ok=False,
            latency_ms=ms,
            error=repr(exc),
        )


async def _ws_first_message_ms(ws_url: str, timeout_sec: float = 10.0) -> float:
    """Время до первого data-frame (TEXT/BINARY); PING/PONG пропускаем."""
    t0 = time.perf_counter()
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    deadline = time.perf_counter() + timeout_sec
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(ws_url, heartbeat=20.0) as ws:
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError("WS first data frame timeout")
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    break
                if msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise RuntimeError(f"WS closed/error: {msg.type}")
                # PING/PONG/CONTINUATION — ждём data
    return (time.perf_counter() - t0) * 1000.0


async def run_latency_probe(
    *,
    rest_targets: dict[str, str],
    ws_targets: dict[str, str] | None = None,
    symbol: str = "BTCUSDT",
    rounds: int = 5,
    pause_sec: float = 0.2,
    audit_path: Path | None = DEFAULT_AUDIT,
    host_hint: str = "docker01",
) -> LatencyProbeResult:
    """
    rest_targets: name → base_url (например mainnet → https://api.binance.com)
    ws_targets: name → full ws url stream (bookTicker)
    """
    result = LatencyProbeResult(
        started_at=datetime.now(timezone.utc).isoformat(),
        host_hint=host_hint,
    )
    clients: dict[str, BinanceSpotClient] = {
        name: BinanceSpotClient(base_url=url, timeout_sec=15.0)
        for name, url in rest_targets.items()
    }
    try:
        for _ in range(max(1, rounds)):
            for name, client in clients.items():
                result.samples.append(
                    await _time_call(name, "ping", client.ping)
                )
                result.samples.append(
                    await _time_call(name, "time", client.server_time)
                )
                result.samples.append(
                    await _time_call(
                        name,
                        "ticker_price",
                        lambda c=client, s=symbol: c.ticker_price(s),
                    )
                )
                result.samples.append(
                    await _time_call(
                        name,
                        "exchange_info",
                        lambda c=client, s=symbol: c.exchange_info(s),
                    )
                )
            if ws_targets:
                for name, url in ws_targets.items():
                    result.samples.append(
                        await _time_call(
                            name,
                            "ws_first_msg",
                            lambda u=url: _ws_first_message_ms(u),
                        )
                    )
            if pause_sec > 0:
                await asyncio.sleep(pause_sec)
    finally:
        for client in clients.values():
            await client.close()

    result.summaries = summarize(result.samples)
    if audit_path is not None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "event": "latency_probe",
            "started_at": result.started_at,
            "host_hint": result.host_hint,
            "summaries": [asdict(s) for s in result.summaries],
        }
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    for s in result.summaries:
        log.info(
            "latency_summary",
            target=s.target,
            endpoint=s.endpoint,
            p50_ms=round(s.p50_ms, 1),
            p95_ms=round(s.p95_ms, 1),
            errors=s.errors,
        )
    return result


def format_latency_table(result: LatencyProbeResult) -> str:
    if not result.summaries:
        return "no samples"
    header = f"{'target':<12} {'endpoint':<14} {'ok':>7} {'p50_ms':>8} {'p95_ms':>8} {'mean_ms':>8}"
    rows = [header, "-" * len(header)]
    for s in result.summaries:
        rows.append(
            f"{s.target:<12} {s.endpoint:<14} {s.ok:>3}/{s.samples:<3} "
            f"{s.p50_ms:8.1f} {s.p95_ms:8.1f} {s.mean_ms:8.1f}"
        )
    return "\n".join(rows)
