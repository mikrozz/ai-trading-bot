from __future__ import annotations

from trading_bot.ops.latency_probe import EndpointSample, _percentile, summarize


def test_percentile_basic() -> None:
    assert _percentile([10.0], 95) == 10.0
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert _percentile(vals, 50) == 30.0
    assert _percentile(vals, 95) >= 40.0


def test_summarize_groups() -> None:
    samples = [
        EndpointSample("mainnet", "ping", True, 100.0),
        EndpointSample("mainnet", "ping", True, 120.0),
        EndpointSample("mainnet", "ping", False, 500.0, error="x"),
        EndpointSample("testnet", "ping", True, 80.0),
    ]
    summaries = summarize(samples)
    by = {(s.target, s.endpoint): s for s in summaries}
    assert by[("mainnet", "ping")].ok == 2
    assert by[("mainnet", "ping")].errors == 1
    assert by[("mainnet", "ping")].p50_ms == 110.0
    assert by[("testnet", "ping")].ok == 1
