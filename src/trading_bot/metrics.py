"""Prometheus metrics для компонентов бота."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

WS_MESSAGES = Counter(
    "trading_ws_messages_total",
    "WebSocket messages processed",
    ["symbol", "event"],
)
WS_ERRORS = Counter(
    "trading_ws_errors_total",
    "WebSocket / parse errors",
    ["component"],
)
WRITER_ROWS = Counter(
    "trading_writer_rows_total",
    "Rows written to TimescaleDB",
    ["table"],
)
WRITER_ERRORS = Counter(
    "trading_writer_errors_total",
    "Batch writer errors",
)
ORDERS_TOTAL = Counter(
    "trading_orders_total",
    "Orders placed/cancelled",
    ["action", "symbol", "mode"],
)
RISK_DENIES = Counter(
    "trading_risk_denies_total",
    "Risk gate deny/kill decisions",
    ["decision", "reason"],
)
PAPER_EQUITY = Gauge(
    "trading_paper_equity",
    "Current paper equity",
    ["symbol"],
)
PAPER_KILL_SWITCH = Gauge(
    "trading_paper_kill_switch",
    "Paper kill-switch active (1=on)",
    ["symbol"],
)
PAPER_FILLS = Counter(
    "trading_paper_fills_total",
    "Paper fills",
    ["symbol", "side"],
)
BOOK_UPDATES = Counter(
    "trading_book_updates_total",
    "bookTicker updates",
    ["symbol"],
)
REQUEST_LATENCY = Histogram(
    "trading_request_latency_seconds",
    "Outbound request latency",
    ["endpoint"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
LATENCY_PROBE_P50_MS = Gauge(
    "trading_latency_probe_p50_ms",
    "Latency probe p50 (ms)",
    ["target", "endpoint"],
)
LATENCY_PROBE_P95_MS = Gauge(
    "trading_latency_probe_p95_ms",
    "Latency probe p95 (ms)",
    ["target", "endpoint"],
)
LATENCY_PROBE_ERRORS = Gauge(
    "trading_latency_probe_errors",
    "Latency probe error count in last run",
    ["target", "endpoint"],
)
MAINNET_CHECK_OK = Gauge(
    "trading_mainnet_check_ok",
    "Last mainnet dry-run check (1=ok)",
)
LIVE_EQUITY = Gauge(
    "trading_live_equity",
    "Live/testnet equity estimate from account",
    ["symbol", "mode"],
)
LIVE_POSITION_QTY = Gauge(
    "trading_live_position_qty",
    "Live/testnet base position quantity",
    ["symbol", "mode"],
)
LIVE_KILL_SWITCH = Gauge(
    "trading_live_kill_switch",
    "Live/testnet kill-switch active (1=on)",
    ["symbol", "mode"],
)

_started = False


def start_metrics_server(port: int = 9108) -> None:
    """Идемпотентный старт HTTP /metrics."""
    global _started
    if _started or port <= 0:
        return
    start_http_server(port)
    _started = True
