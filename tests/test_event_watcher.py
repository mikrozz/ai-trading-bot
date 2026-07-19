"""Unit tests for event watcher calendar parse/merge/reload."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from trading_bot.ops.event_watcher import (
    WatchedEvent,
    merge_calendar,
    parse_ff_items,
    short_event_name,
    write_events_yaml,
)
from trading_bot.risk.event_blackout import (
    EventBlackoutConfig,
    EventBlackoutGuard,
    MacroEvent,
)


def test_short_event_name_aliases() -> None:
    assert short_event_name("Non-Farm Employment Change") == "NFP"
    assert short_event_name("CPI m/m") == "US_CPI"
    assert short_event_name("FOMC Statement") == "FOMC"


def test_parse_ff_items_filters_usd_high() -> None:
    now = datetime.now(timezone.utc)
    items = [
        {
            "title": "Non-Farm Payrolls",
            "country": "USD",
            "impact": "High",
            "date": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S-04:00"),
        },
        {
            "title": "Retail Sales",
            "country": "EUR",
            "impact": "High",
            "date": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        {
            "title": "Building Permits",
            "country": "USD",
            "impact": "Low",
            "date": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    ]
    parsed = parse_ff_items(items)
    assert len(parsed) == 1
    assert parsed[0].name == "NFP"
    assert parsed[0].country == "USD"


def test_merge_calendar_manual_and_auto(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    auto = [
        WatchedEvent(
            name="NFP",
            at=now + timedelta(days=2),
            impact="High",
            country="USD",
            title="Non-Farm Payrolls",
        )
    ]
    manual = [MacroEvent(name="CUSTOM", at=now + timedelta(hours=5))]
    previous = [MacroEvent(name="FOMC", at=now + timedelta(days=10))]
    merged = merge_calendar(auto, manual, previous=previous, keep_past_hours=6)
    names = {m["name"] for m in merged}
    assert names == {"NFP", "CUSTOM", "FOMC"}


def test_write_and_reload_blackout(tmp_path: Path) -> None:
    cal = tmp_path / "events.yaml"
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    write_events_yaml(
        cal,
        [
            {
                "name": "US_CPI",
                "at": future.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ],
    )
    guard = EventBlackoutGuard.from_settings(
        EventBlackoutConfig(
            enabled=True,
            minutes_before=30,
            minutes_after=60,
            calendar_path=str(cal),
        )
    )
    assert guard.should_block_open(future) is not None

    # rewrite calendar → guard reloads by mtime without restart
    later = future + timedelta(days=3)
    write_events_yaml(
        cal,
        [{"name": "FOMC", "at": later.strftime("%Y-%m-%dT%H:%M:%SZ")}],
    )
    assert guard.should_block_open(future) is None
    assert guard.should_block_open(later) is not None
    assert guard.should_block_open(later).event.name == "FOMC"


def test_events_manual_yaml_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "configs" / "events.manual.yaml"
    assert path.is_file()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "events" in data
