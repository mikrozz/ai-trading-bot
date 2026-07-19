from __future__ import annotations

from pathlib import Path

from trading_bot.ops.loadtest import (
    bench_features,
    bench_parse_events,
    evaluate_gates,
    LoadTestReport,
    BenchResult,
)


def test_parse_bench_fast_enough() -> None:
    r = bench_parse_events(20_000)
    assert r.ok
    assert r.ops_per_sec > 10_000


def test_features_bench_runs() -> None:
    r = bench_features(bars=800, repeats=3)
    assert r.ok
    assert r.p95_ms > 0


def test_gates_detect_slow_parse() -> None:
    report = LoadTestReport(
        started_at="x",
        host_hint="t",
        results=[
            BenchResult(
                name="parse_events",
                ops=100,
                seconds=1.0,
                ops_per_sec=100.0,
            )
        ],
    )
    fails = evaluate_gates(report)
    assert any("parse_events" in f for f in fails)
