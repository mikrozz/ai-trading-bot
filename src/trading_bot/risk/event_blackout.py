"""Macro/event blackout: block NEW opens around scheduled events; exits stay allowed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from trading_bot.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class MacroEvent:
    name: str
    at: datetime  # UTC

    @property
    def at_utc(self) -> datetime:
        if self.at.tzinfo is None:
            return self.at.replace(tzinfo=timezone.utc)
        return self.at.astimezone(timezone.utc)


@dataclass(frozen=True)
class EventBlackoutConfig:
    enabled: bool = True
    minutes_before: int = 30
    minutes_after: int = 60
    timezone: str = "UTC"
    calendar_path: str = "configs/events.yaml"


@dataclass(frozen=True)
class BlackoutWindow:
    event: MacroEvent
    start: datetime
    end: datetime

    def contains(self, now: datetime) -> bool:
        ts = _as_utc(now)
        return self.start <= ts <= self.end


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_event_time(value: Any) -> datetime:
    """Parse UTC ISO timestamp (Z or +00:00). Naive values treated as UTC."""
    if isinstance(value, datetime):
        return _as_utc(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return _as_utc(dt)


def window_for_event(
    event: MacroEvent,
    *,
    minutes_before: int,
    minutes_after: int,
) -> BlackoutWindow:
    at = event.at_utc
    start = at - timedelta(minutes=max(0, int(minutes_before)))
    end = at + timedelta(minutes=max(0, int(minutes_after)))
    return BlackoutWindow(event=event, start=start, end=end)


def is_in_blackout_window(
    now: datetime,
    event_at: datetime,
    *,
    minutes_before: int,
    minutes_after: int,
) -> bool:
    """True if now is inside [event_at - before, event_at + after] (inclusive)."""
    event = MacroEvent(name="_", at=_as_utc(event_at))
    return window_for_event(
        event, minutes_before=minutes_before, minutes_after=minutes_after
    ).contains(now)


def load_events_calendar(path: Path | str | None) -> list[MacroEvent]:
    """Load events from YAML. Missing/empty file → []."""
    if path is None:
        return []
    p = Path(path)
    if not p.is_file():
        log.info("event_calendar_missing", path=str(p))
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("events") or []
    else:
        return []
    if not items:
        return []

    out: list[MacroEvent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        at_raw = item.get("at")
        if not name or at_raw is None:
            continue
        try:
            out.append(MacroEvent(name=name, at=parse_event_time(at_raw)))
        except (TypeError, ValueError) as exc:
            log.warning(
                "event_calendar_skip",
                name=name,
                at=str(at_raw),
                error=str(exc),
            )
    out.sort(key=lambda e: e.at_utc)
    return out


class EventBlackoutGuard:
    """Returns active blackout window for open-blocking; no-op if disabled/empty."""

    def __init__(
        self,
        config: EventBlackoutConfig,
        events: list[MacroEvent] | None = None,
    ) -> None:
        self.config = config
        self.events = list(events or [])

    @classmethod
    def from_settings(
        cls,
        config: EventBlackoutConfig,
        *,
        calendar_path: Path | str | None = None,
        repo_root: Path | None = None,
    ) -> EventBlackoutGuard:
        path = calendar_path if calendar_path is not None else config.calendar_path
        p = Path(path)
        if not p.is_absolute() and repo_root is not None:
            p = repo_root / p
        events = load_events_calendar(p) if config.enabled else []
        return cls(config=config, events=events)

    def active_window(self, now: datetime | None = None) -> BlackoutWindow | None:
        if not self.config.enabled or not self.events:
            return None
        ts = _as_utc(now or datetime.now(timezone.utc))
        for event in self.events:
            win = window_for_event(
                event,
                minutes_before=self.config.minutes_before,
                minutes_after=self.config.minutes_after,
            )
            if win.contains(ts):
                return win
        return None

    def should_block_open(self, now: datetime | None = None) -> BlackoutWindow | None:
        return self.active_window(now)
