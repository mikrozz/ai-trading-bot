"""Event watcher: публичный макро-календарь → events.yaml + Telegram."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from trading_bot.logging_setup import get_logger, setup_logging
from trading_bot.ops.daily_equity_report import deliver_telegram
from trading_bot.risk.event_blackout import MacroEvent, parse_event_time

log = get_logger(__name__)

DEFAULT_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
DEFAULT_CALENDAR = Path("configs/events.yaml")
DEFAULT_MANUAL = Path("configs/events.manual.yaml")
DEFAULT_STATE = Path("data/event_watcher_state.json")
DEFAULT_CACHE = Path("data/ff_calendar_cache.json")
DEFAULT_TOKEN_FILE = Path("/opt/network-monitor/secrets/telegram_bot_token")
DEFAULT_CHAT_ID = "608509788"
DEFAULT_PROXY = "http://127.0.0.1:3128"
DEFAULT_WEBHOOK = "http://127.0.0.1:9999/"
CACHE_MAX_AGE_HOURS = 48

# Короткие имена для типовых US high-impact
_TITLE_ALIASES = (
    ("non-farm", "NFP"),
    ("nonfarm", "NFP"),
    ("payroll", "NFP"),
    ("cpi", "US_CPI"),
    ("consumer price", "US_CPI"),
    ("fomc", "FOMC"),
    ("federal funds", "FOMC"),
    ("interest rate decision", "FOMC"),
    ("fed chair", "FED_SPEECH"),
    ("pce", "US_PCE"),
    ("gdp", "US_GDP"),
    ("unemployment rate", "US_UNEMPLOYMENT"),
    ("retail sales", "US_RETAIL"),
    ("ism manufacturing", "ISM_MFG"),
    ("ism services", "ISM_SERVICES"),
)


@dataclass(frozen=True)
class WatchedEvent:
    name: str
    at: datetime
    impact: str
    country: str
    title: str
    source: str = "forexfactory"

    @property
    def key(self) -> str:
        return f"{self.name}|{self.at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}"

    def to_calendar_item(self) -> dict[str, str]:
        return {
            "name": self.name,
            "at": self.at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def short_event_name(title: str) -> str:
    low = title.lower()
    for needle, alias in _TITLE_ALIASES:
        if needle in low:
            return alias
    # fallback: UPPER_SNAKE truncated
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in title.upper())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:40] or "EVENT"


def save_ff_cache(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_ff_cache(
    path: Path,
    *,
    max_age_hours: int = CACHE_MAX_AGE_HOURS,
) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    fetched_raw = payload.get("fetched_at")
    items = payload.get("items")
    if not fetched_raw or not isinstance(items, list):
        return None
    try:
        fetched = parse_event_time(fetched_raw)
    except (TypeError, ValueError):
        return None
    age = datetime.now(timezone.utc) - fetched
    if age > timedelta(hours=max_age_hours):
        return None
    return items


def fetch_ff_calendar(
    url: str = DEFAULT_FF_URL,
    *,
    timeout: float = 30.0,
    retries: int = 5,
    cache_path: Path | None = DEFAULT_CACHE,
) -> list[dict[str, Any]]:
    req = urllib.request.Request(url, headers={"User-Agent": "ai-trading-bot-event-watcher/1.0"})
    last_exc: Exception | None = None
    cached = load_ff_cache(cache_path) if cache_path is not None else None
    # If fresh cache exists, avoid long backoff on 429 — fall back quickly.
    effective_retries = 2 if cached is not None else retries
    for attempt in range(effective_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, list):
                raise RuntimeError(f"unexpected calendar payload type: {type(data)}")
            if cache_path is not None:
                save_ff_cache(cache_path, data)
            return data
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429 and cached is not None:
                break
            if exc.code not in {429, 502, 503, 504} or attempt + 1 >= effective_retries:
                break
            sleep_s = min(90.0, 5.0 * (2**attempt))
            log.warning(
                "ff_calendar_retry",
                code=exc.code,
                attempt=attempt + 1,
                sleep_s=sleep_s,
            )
            time.sleep(sleep_s)
        except urllib.error.URLError as exc:
            last_exc = exc
            if cached is not None or attempt + 1 >= effective_retries:
                break
            sleep_s = min(90.0, 5.0 * (2**attempt))
            log.warning(
                "ff_calendar_retry",
                error=type(exc).__name__,
                attempt=attempt + 1,
                sleep_s=sleep_s,
            )
            time.sleep(sleep_s)
    if cached is not None:
        log.warning(
            "ff_calendar_using_cache",
            path=str(cache_path),
            n=len(cached),
            error=str(last_exc),
        )
        return cached
    raise RuntimeError(f"ff calendar fetch failed: {last_exc}")


def parse_ff_items(
    items: list[dict[str, Any]],
    *,
    countries: set[str] | None = None,
    impacts: set[str] | None = None,
) -> list[WatchedEvent]:
    countries = {c.upper() for c in (countries or {"USD"})}
    impacts = {i.title() for i in (impacts or {"High"})}
    out: list[WatchedEvent] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        country = str(raw.get("country") or "").upper()
        impact = str(raw.get("impact") or "").title()
        if country not in countries or impact not in impacts:
            continue
        title = str(raw.get("title") or "").strip()
        date_raw = raw.get("date")
        if not title or not date_raw:
            continue
        try:
            at = parse_event_time(date_raw)
        except (TypeError, ValueError):
            continue
        out.append(
            WatchedEvent(
                name=short_event_name(title),
                at=at,
                impact=impact,
                country=country,
                title=title,
            )
        )
    # unique by key, keep earliest title variant
    by_key: dict[str, WatchedEvent] = {}
    for ev in sorted(out, key=lambda e: e.at):
        by_key.setdefault(ev.key, ev)
    return sorted(by_key.values(), key=lambda e: e.at)


def load_manual_events(path: Path) -> list[MacroEvent]:
    if not path.is_file():
        return []
    from trading_bot.risk.event_blackout import load_events_calendar

    return load_events_calendar(path)


def merge_calendar(
    auto_events: list[WatchedEvent],
    manual: list[MacroEvent],
    *,
    previous: list[MacroEvent] | None = None,
    keep_past_hours: int = 6,
) -> list[dict[str, str]]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=keep_past_hours)
    items: dict[str, dict[str, str]] = {}

    def _add(name: str, at: datetime) -> None:
        if at < cutoff:
            return
        key = f"{name}|{at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}"
        items[key] = {
            "name": name,
            "at": at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    # previous calendar first — auto/manual overwrite same key
    for m in previous or []:
        _add(m.name, m.at_utc)
    for m in manual:
        _add(m.name, m.at_utc)
    for ev in auto_events:
        _add(ev.name, ev.at)
    return [items[k] for k in sorted(items.keys(), key=lambda k: items[k]["at"])]


def write_events_yaml(path: Path, events: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# AUTO-GENERATED by event-watcher (+ merge configs/events.manual.yaml).\n"
        "# Do not edit auto events by hand — put overrides in events.manual.yaml.\n"
        "# Times are UTC ISO-8601.\n"
        "#\n"
    )
    body = yaml.safe_dump({"events": events}, sort_keys=False, allow_unicode=True)
    path.write_text(header + body, encoding="utf-8")


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"notified_keys": [], "last_run": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"notified_keys": [], "last_run": None}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def format_telegram(
    *,
    written: list[dict[str, str]],
    new_events: list[WatchedEvent],
    upcoming: list[WatchedEvent],
    hours_ahead: int,
) -> str:
    lines = [
        "AI Trading Bot — event watcher",
        f"Календарь обновлён: {len(written)} событий (USD High + manual)",
    ]
    if new_events:
        lines.append("")
        lines.append("Новые:")
        for ev in new_events[:8]:
            lines.append(
                f"• {ev.name} {ev.at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')}Z "
                f"({ev.title})"
            )
    if upcoming:
        lines.append("")
        lines.append(f"Ближайшие {hours_ahead}ч (blackout −30/+60м):")
        for ev in upcoming[:8]:
            lines.append(
                f"• {ev.name} {ev.at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')}Z"
            )
    else:
        lines.append("")
        lines.append(f"В ближайшие {hours_ahead}ч high-impact USD нет.")
    return "\n".join(lines)


def run_event_watcher(
    *,
    calendar_path: Path = DEFAULT_CALENDAR,
    manual_path: Path = DEFAULT_MANUAL,
    state_path: Path = DEFAULT_STATE,
    ff_url: str = DEFAULT_FF_URL,
    hours_ahead: int = 48,
    dry_run: bool = False,
    no_telegram: bool = False,
    force_notify: bool = False,
    token_file: Path = DEFAULT_TOKEN_FILE,
    chat_id: str = DEFAULT_CHAT_ID,
    proxy: str | None = DEFAULT_PROXY,
    webhook_url: str = DEFAULT_WEBHOOK,
) -> dict[str, Any]:
    raw = fetch_ff_calendar(ff_url)
    auto = parse_ff_items(raw)
    manual = load_manual_events(manual_path)
    previous = load_manual_events(calendar_path) if calendar_path.is_file() else []
    # Keep only future previous events that are still in FF week OR manual names.
    # Drop stale placeholders once their time passed (cutoff in merge).
    merged = merge_calendar(auto, manual, previous=previous)
    if not dry_run:
        write_events_yaml(calendar_path, merged)
    else:
        log.info("event_watcher_dry_run_skip_write", path=str(calendar_path), n=len(merged))

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours_ahead)
    upcoming = [e for e in auto if now <= e.at <= horizon]
    state = load_state(state_path)
    known = set(state.get("notified_keys") or [])
    new_events = [e for e in auto if e.key not in known and e.at >= now - timedelta(hours=1)]
    # notify if first run, new calendar items, or upcoming not yet reminded
    remind_keys = {e.key for e in upcoming}
    first_run = not state.get("last_run")
    should_notify = (
        force_notify
        or first_run
        or bool(new_events)
        or bool(remind_keys - known)
    )

    text = format_telegram(
        written=merged,
        new_events=new_events,
        upcoming=upcoming,
        hours_ahead=hours_ahead,
    )
    report_path = Path("data/reports") / "event-watcher-latest.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text + "\n", encoding="utf-8")

    delivery = "skipped"
    if should_notify:
        delivery = deliver_telegram(
            text,
            dry_run=dry_run,
            no_telegram=no_telegram,
            token_file=token_file,
            chat_id=chat_id,
            proxy=proxy,
            webhook_url=webhook_url,
        )
        if delivery != "skipped":
            known |= {e.key for e in auto if e.at >= now - timedelta(days=1)}
            known |= remind_keys
    elif dry_run or no_telegram:
        delivery = "skipped"

    state = {
        "last_run": now.isoformat(),
        "notified_keys": sorted(known)[-200:],
        "calendar_count": len(merged),
        "auto_count": len(auto),
        "upcoming_count": len(upcoming),
    }
    if not dry_run:
        save_state(state_path, state)

    log.info(
        "event_watcher_done",
        auto=len(auto),
        written=len(merged),
        upcoming=len(upcoming),
        new=len(new_events),
        telegram=delivery,
        notified=should_notify,
    )
    return {
        "auto_count": len(auto),
        "written_count": len(merged),
        "upcoming_count": len(upcoming),
        "new_count": len(new_events),
        "telegram": delivery,
        "notified": should_notify,
        "calendar_path": str(calendar_path),
        "report_path": str(report_path),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Macro event watcher → events.yaml + Telegram")
    p.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR)
    p.add_argument("--manual", type=Path, default=DEFAULT_MANUAL)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--ff-url", default=DEFAULT_FF_URL)
    p.add_argument("--hours-ahead", type=int, default=48)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-telegram", action="store_true")
    p.add_argument("--force-notify", action="store_true", help="Всегда слать Telegram")
    p.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    p.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    p.add_argument("--proxy", default=DEFAULT_PROXY)
    p.add_argument("--webhook-url", default=DEFAULT_WEBHOOK)
    return p


def main(argv: list[str] | None = None) -> int:
    setup_logging("INFO")
    args = build_arg_parser().parse_args(argv)
    proxy = None if args.proxy in {"", "none", "None"} else args.proxy
    result = run_event_watcher(
        calendar_path=args.calendar,
        manual_path=args.manual,
        state_path=args.state,
        ff_url=args.ff_url,
        hours_ahead=args.hours_ahead,
        dry_run=args.dry_run,
        no_telegram=args.no_telegram,
        force_notify=args.force_notify,
        token_file=args.token_file,
        chat_id=args.chat_id,
        proxy=proxy,
        webhook_url=args.webhook_url,
    )
    print(
        f"EVENT_WATCHER_OK auto={result['auto_count']} written={result['written_count']} "
        f"upcoming={result['upcoming_count']} new={result['new_count']} "
        f"telegram={result['telegram']} notified={int(result['notified'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
