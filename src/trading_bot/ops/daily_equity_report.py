"""Daily equity report for testnet-live: local file + optional Telegram."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_STATE = Path("data/live_state_BTCUSDT.json")
DEFAULT_REPORT_DIR = Path("data/reports")
DEFAULT_METRICS_URL = "http://127.0.0.1:9111/metrics"
DEFAULT_PROM_URL = "http://127.0.0.1:9091"
DEFAULT_TG_WEBHOOK = "http://127.0.0.1:9999/"
DEFAULT_TOKEN_FILE = "/opt/network-monitor/secrets/telegram_bot_token"
DEFAULT_CHAT_ID = "608509788"
DEFAULT_PROXY = "http://192.168.10.155:3128"
MSK = ZoneInfo("Europe/Moscow")


@dataclass
class EquitySnapshot:
    symbol: str
    mode: str
    equity: float | None
    position_qty: float | None
    kill_switch: bool | None
    orders_placed: int | None
    orders_today: float | None
    day_start_equity: float | None
    saved_at: str | None
    source: str
    collected_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_prometheus_text(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """Minimal Prometheus text exposition parser (gauges/counters)."""
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    line_re = re.compile(
        r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
        r"(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+eE0-9.]+)\s*$"
    )
    label_re = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = line_re.match(line)
        if not m:
            continue
        labels: dict[str, str] = {}
        if m.group("labels"):
            for lm in label_re.finditer(m.group("labels")):
                labels[lm.group(1)] = lm.group(2).replace(r"\"", '"').replace(r"\\", "\\")
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        out.setdefault(m.group("name"), []).append((labels, value))
    return out


def _http_get(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={"Accept": "text/plain, application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_state_json(path: Path, symbol: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    data.setdefault("symbol", symbol)
    return data


def scrape_metrics(metrics_url: str, symbol: str, mode: str) -> dict[str, float | None]:
    result: dict[str, float | None] = {
        "equity": None,
        "position_qty": None,
        "kill_switch": None,
        "orders_total_place": None,
    }
    try:
        parsed = _parse_prometheus_text(_http_get(metrics_url))
    except (urllib.error.URLError, TimeoutError, OSError):
        return result

    def pick(name: str) -> float | None:
        samples = parsed.get(name) or []
        for labels, value in samples:
            if labels.get("symbol", symbol) == symbol and labels.get("mode", mode) == mode:
                return value
        for labels, value in samples:
            if labels.get("symbol", symbol) == symbol:
                return value
        return samples[0][1] if samples else None

    result["equity"] = pick("trading_live_equity")
    result["position_qty"] = pick("trading_live_position_qty")
    ks = pick("trading_live_kill_switch")
    result["kill_switch"] = ks
    place_total = 0.0
    found = False
    for labels, value in parsed.get("trading_orders_total") or []:
        if labels.get("action") != "place":
            continue
        if labels.get("symbol", symbol) not in (symbol, ""):
            continue
        if labels.get("mode", mode) not in (mode, ""):
            continue
        place_total += value
        found = True
    if found:
        result["orders_total_place"] = place_total
    return result


def prom_query_scalar(prom_url: str, expr: str) -> float | None:
    try:
        url = f"{prom_url.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
        raw = _http_get(url, timeout=8.0)
        data = json.loads(raw)
        results = (data.get("data") or {}).get("result") or []
        if not results:
            return None
        return float(results[0]["value"][1])
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError, ValueError, TypeError):
        return None


def collect_snapshot(
    *,
    symbol: str,
    mode: str,
    state_path: Path,
    metrics_url: str,
    prom_url: str,
) -> EquitySnapshot:
    state = load_state_json(state_path, symbol)
    metrics = scrape_metrics(metrics_url, symbol, mode)

    equity = metrics.get("equity")
    if equity is None and state.get("equity") is not None:
        equity = float(state["equity"])

    position_qty = metrics.get("position_qty")
    if position_qty is None:
        pos = state.get("position")
        if isinstance(pos, dict) and pos.get("qty") is not None:
            position_qty = float(pos["qty"])
        elif pos is None:
            position_qty = 0.0

    kill: bool | None
    if metrics.get("kill_switch") is not None:
        kill = float(metrics["kill_switch"] or 0.0) > 0.0
    elif "kill_switch" in state:
        kill = bool(state.get("kill_switch"))
    else:
        kill = None

    orders_placed = state.get("orders_placed")
    if orders_placed is not None:
        orders_placed = int(orders_placed)

    orders_today = prom_query_scalar(
        prom_url,
        f'sum(increase(trading_orders_total{{action="place",mode="{mode}",symbol="{symbol}"}}[1d]))',
    )
    if orders_today is None and metrics.get("orders_total_place") is not None:
        # fallback: lifetime counter when Prometheus increase unavailable
        orders_today = None

    day_start = state.get("day_start_equity")
    if day_start is not None:
        day_start = float(day_start)

    sources: list[str] = []
    if state:
        sources.append("state_json")
    if any(v is not None for k, v in metrics.items() if k != "orders_total_place"):
        sources.append("metrics")
    if orders_today is not None:
        sources.append("prometheus")

    return EquitySnapshot(
        symbol=symbol,
        mode=str(state.get("mode") or mode),
        equity=equity,
        position_qty=position_qty,
        kill_switch=kill,
        orders_placed=orders_placed,
        orders_today=orders_today,
        day_start_equity=day_start,
        saved_at=str(state.get("saved_at")) if state.get("saved_at") else None,
        source="+".join(sources) if sources else "none",
        collected_at=_now_iso(),
    )


def format_ru_message(snap: EquitySnapshot) -> str:
    ts_msk = datetime.now(MSK).strftime("%Y-%m-%d %H:%M %Z")
    equity_s = f"{snap.equity:,.2f}" if snap.equity is not None else "n/a"
    qty_s = f"{snap.position_qty:.8f}".rstrip("0").rstrip(".") if snap.position_qty is not None else "n/a"
    kill_s = "ВКЛ" if snap.kill_switch else ("ВЫКЛ" if snap.kill_switch is False else "n/a")
    if snap.orders_today is not None:
        orders_s = f"{snap.orders_today:.0f}"
    elif snap.orders_placed is not None:
        orders_s = f"{snap.orders_placed} (lifetime)"
    else:
        orders_s = "n/a"

    day_delta = ""
    if snap.equity is not None and snap.day_start_equity not in (None, 0):
        delta = snap.equity - float(snap.day_start_equity)
        pct = (delta / float(snap.day_start_equity)) * 100.0
        day_delta = f"\nΔ день: {delta:+,.2f} ({pct:+.2f}%)"

    return (
        f"AI Trading Bot — дневной отчёт ({snap.mode})\n"
        f"Символ: {snap.symbol}\n"
        f"Equity: {equity_s} USDT{day_delta}\n"
        f"Позиция qty: {qty_s}\n"
        f"Ордера сегодня: {orders_s}\n"
        f"Kill-switch: {kill_s}\n"
        f"Время: {ts_msk}"
    )


def write_report(report_dir: Path, snap: EquitySnapshot, message: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(MSK).strftime("%Y-%m-%d")
    path = report_dir / f"equity-{snap.symbol}-{day}.txt"
    payload = {
        "message": message,
        "snapshot": asdict(snap),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest = report_dir / f"equity-{snap.symbol}-latest.txt"
    latest.write_text(message + "\n", encoding="utf-8")
    return path


def send_telegram_direct(
    text: str,
    *,
    token_file: Path,
    chat_id: str,
    proxy: str | None,
) -> None:
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("telegram token file empty")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id, "text": text[:4000]}, ensure_ascii=False).encode(
        "utf-8"
    )
    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with opener.open(req, timeout=30) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    if not out.get("ok"):
        raise RuntimeError("telegram API rejected message")


def send_telegram_via_webhook(webhook_url: str, text: str) -> None:
    """Fallback: Alertmanager-compatible payload → telegram-webhook :9999."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "status": "firing",
        "groupLabels": {"alert_group": "ai-trading-bot"},
        "commonLabels": {"alert_group": "ai-trading-bot", "telegram": "yes"},
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "TradingBotDailyEquityReport",
                    "alert_group": "ai-trading-bot",
                    "telegram": "yes",
                },
                "annotations": {
                    "summary": "Дневной отчёт equity (testnet-live)",
                    "description": text,
                },
                "startsAt": now,
            }
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"webhook status {resp.status}")


def deliver_telegram(
    text: str,
    *,
    dry_run: bool,
    no_telegram: bool,
    token_file: Path,
    chat_id: str,
    proxy: str | None,
    webhook_url: str,
) -> str:
    if dry_run or no_telegram:
        return "skipped"
    errors: list[str] = []
    if token_file.is_file():
        try:
            send_telegram_direct(text, token_file=token_file, chat_id=chat_id, proxy=proxy)
            return "direct"
        except Exception as exc:  # noqa: BLE001 — ops path: try webhook next
            errors.append(f"direct:{type(exc).__name__}")
    try:
        send_telegram_via_webhook(webhook_url, text)
        return "webhook"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"webhook:{type(exc).__name__}")
        raise RuntimeError("telegram delivery failed: " + ",".join(errors)) from exc


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Daily equity report (testnet-live)")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--mode", default="testnet")
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--metrics-url", default=os.environ.get("METRICS_URL", DEFAULT_METRICS_URL))
    p.add_argument("--prom-url", default=os.environ.get("PROM_URL", DEFAULT_PROM_URL))
    p.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    p.add_argument("--dry-run", action="store_true", help="Только локальный файл, без Telegram")
    p.add_argument("--no-telegram", action="store_true", help="Не отправлять в Telegram")
    p.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.environ.get("TELEGRAM_TOKEN_FILE", DEFAULT_TOKEN_FILE)),
    )
    p.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID))
    p.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY", DEFAULT_PROXY))
    p.add_argument(
        "--webhook-url",
        default=os.environ.get("TELEGRAM_WEBHOOK_URL", DEFAULT_TG_WEBHOOK),
    )
    return p


def run_report(args: argparse.Namespace) -> int:
    snap = collect_snapshot(
        symbol=args.symbol,
        mode=args.mode,
        state_path=args.state,
        metrics_url=args.metrics_url,
        prom_url=args.prom_url,
    )
    message = format_ru_message(snap)
    path = write_report(args.report_dir, snap, message)
    delivery = "skipped"
    try:
        delivery = deliver_telegram(
            message,
            dry_run=bool(args.dry_run),
            no_telegram=bool(args.no_telegram),
            token_file=args.token_file,
            chat_id=str(args.chat_id),
            proxy=args.proxy or None,
            webhook_url=args.webhook_url,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"REPORT_OK path={path} telegram=FAILED error={type(exc).__name__}", flush=True)
        print(message, flush=True)
        return 1
    print(f"REPORT_OK path={path} telegram={delivery} source={snap.source}", flush=True)
    print(message, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return run_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
