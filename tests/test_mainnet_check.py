from __future__ import annotations

from pathlib import Path

from trading_bot.ops.mainnet_check import (
    MainnetCheckResult,
    _geo_hint_from_error,
    load_mainnet_credentials,
)
from trading_bot.exchange.binance_spot import BinanceAPIError


def test_load_mainnet_credentials_from_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_MAINNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_MAINNET_API_SECRET", raising=False)
    env = tmp_path / "mainnet.env"
    env.write_text(
        "BINANCE_MAINNET_API_KEY=mk\nBINANCE_MAINNET_API_SECRET=ms\n",
        encoding="utf-8",
    )
    key, secret = load_mainnet_credentials(env)
    assert key == "mk"
    assert secret == "ms"


def test_load_ignores_process_testnet_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "testnet-secret")
    monkeypatch.delenv("BINANCE_MAINNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_MAINNET_API_SECRET", raising=False)
    missing = tmp_path / "nope.env"
    key, secret = load_mainnet_credentials(missing)
    assert key == ""
    assert secret == ""


def test_result_ok_requires_signed_when_attempted() -> None:
    r = MainnetCheckResult(
        started_at="x",
        base_url="https://api.binance.com",
        public_ok=True,
        signed_attempted=True,
        signed_ok=False,
        geo_blocked=False,
    )
    assert r.ok is False
    r.signed_ok = True
    assert r.ok is True


def test_geo_hint_detects_status_and_text() -> None:
    assert _geo_hint_from_error(BinanceAPIError(451, {"msg": "denied"}))
    assert _geo_hint_from_error(RuntimeError("unavailable for legal reasons"))
    assert not _geo_hint_from_error(RuntimeError("connection reset by peer"))
