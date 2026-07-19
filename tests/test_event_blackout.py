"""Unit tests for macro/event blackout window containment."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from trading_bot.config import build_settings
from trading_bot.risk.event_blackout import (
    EventBlackoutConfig,
    EventBlackoutGuard,
    MacroEvent,
    is_in_blackout_window,
    load_events_calendar,
    parse_event_time,
    window_for_event,
)


def test_window_containment_boundaries() -> None:
    event_at = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)
    before, after = 30, 60

    # inclusive edges
    assert is_in_blackout_window(
        event_at - timedelta(minutes=30),
        event_at,
        minutes_before=before,
        minutes_after=after,
    )
    assert is_in_blackout_window(
        event_at + timedelta(minutes=60),
        event_at,
        minutes_before=before,
        minutes_after=after,
    )
    assert is_in_blackout_window(
        event_at,
        event_at,
        minutes_before=before,
        minutes_after=after,
    )

    # outside
    assert not is_in_blackout_window(
        event_at - timedelta(minutes=30, seconds=1),
        event_at,
        minutes_before=before,
        minutes_after=after,
    )
    assert not is_in_blackout_window(
        event_at + timedelta(minutes=60, seconds=1),
        event_at,
        minutes_before=before,
        minutes_after=after,
    )


def test_window_for_event_iso() -> None:
    event = MacroEvent(name="FOMC", at=parse_event_time("2026-07-30T18:00:00Z"))
    win = window_for_event(event, minutes_before=30, minutes_after=60)
    assert win.start == datetime(2026, 7, 30, 17, 30, tzinfo=timezone.utc)
    assert win.end == datetime(2026, 7, 30, 19, 0, tzinfo=timezone.utc)
    assert win.contains(datetime(2026, 7, 30, 17, 45, tzinfo=timezone.utc))
    assert not win.contains(datetime(2026, 7, 30, 17, 29, tzinfo=timezone.utc))


def test_guard_blocks_inside_allows_outside(tmp_path: Path) -> None:
    cal = tmp_path / "events.yaml"
    cal.write_text(
        yaml.dump(
            {
                "events": [
                    {"name": "US_CPI", "at": "2026-08-12T12:30:00Z"},
                ]
            }
        ),
        encoding="utf-8",
    )
    guard = EventBlackoutGuard.from_settings(
        EventBlackoutConfig(
            enabled=True,
            minutes_before=30,
            minutes_after=60,
            calendar_path=str(cal),
        )
    )
    inside = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    hit = guard.should_block_open(inside)
    assert hit is not None
    assert hit.event.name == "US_CPI"
    assert guard.should_block_open(outside) is None


def test_guard_noop_when_disabled_or_empty(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    empty = tmp_path / "empty.yaml"
    empty.write_text("events: []\n", encoding="utf-8")

    assert (
        EventBlackoutGuard.from_settings(
            EventBlackoutConfig(enabled=True, calendar_path=str(missing))
        ).should_block_open()
        is None
    )
    assert (
        EventBlackoutGuard.from_settings(
            EventBlackoutConfig(enabled=True, calendar_path=str(empty))
        ).should_block_open()
        is None
    )
    cal = tmp_path / "events.yaml"
    cal.write_text(
        "events:\n  - name: FOMC\n    at: '2026-07-30T18:00:00Z'\n",
        encoding="utf-8",
    )
    disabled = EventBlackoutGuard.from_settings(
        EventBlackoutConfig(enabled=False, calendar_path=str(cal))
    )
    assert disabled.events == []
    assert (
        disabled.should_block_open(datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc))
        is None
    )


def test_load_repo_calendar() -> None:
    root = Path(__file__).resolve().parents[1]
    events = load_events_calendar(root / "configs" / "events.yaml")
    assert len(events) >= 1
    assert all(e.at.tzinfo is not None for e in events)


def test_default_yaml_events_section() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = build_settings(
        root / "configs" / "default.yaml", env_file=Path("/nonexistent")
    )
    assert settings.events.enabled is True
    assert settings.events.minutes_before == 30
    assert settings.events.minutes_after == 60
    assert settings.events.timezone == "UTC"
    assert settings.events.calendar_path == "configs/events.yaml"
