"""Конфигурация приложения (YAML + env)."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutionMode(str, Enum):
    TESTNET = "testnet"
    PAPER = "paper"
    LIVE = "live"


class MarketDataMode(str, Enum):
    PROD_PUBLIC = "prod_public"
    TESTNET = "testnet"


class RiskConfig(BaseSettings):
    daily_drawdown_limit: float = 0.05
    weekly_drawdown_limit: float = 0.10
    max_position_fraction: float = 0.10
    max_open_positions: int = 5
    stop_loss: float = 0.02
    listing_ban_minutes: int = 5


class PaperConfig(BaseSettings):
    prob_threshold: float = 0.60
    min_hold_bars: int = 6
    cooldown_bars: int = 3
    position_fraction: float = 0.10


class LiveTestnetConfig(BaseSettings):
    position_fraction: float = 0.05
    prob_threshold: float = 0.60
    min_hold_bars: int = 6
    cooldown_bars: int = 3
    max_orders_per_hour: int = 20


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    exchange: str = "binance_spot"
    execution_mode: ExecutionMode = ExecutionMode.TESTNET
    market_data_mode: MarketDataMode = MarketDataMode.PROD_PUBLIC

    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    intervals: list[str] = Field(default_factory=lambda: ["1m", "5m", "15m"])

    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_base_url: str = "https://testnet.binance.vision"
    binance_ws_base_url: str = "wss://stream.testnet.binance.vision"

    binance_prod_base_url: str = "https://api.binance.com"
    binance_prod_ws_base_url: str = "wss://stream.binance.com:9443"

    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql://trading:trading@localhost:5432/trading"

    log_level: str = "INFO"
    risk: RiskConfig = Field(default_factory=RiskConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    live_testnet: LiveTestnetConfig = Field(default_factory=LiveTestnetConfig)

    fees_maker: float = 0.001
    fees_taker: float = 0.001
    slippage_liquid: float = 0.0005
    slippage_illiquid: float = 0.002

    @field_validator("symbols", mode="before")
    @classmethod
    def split_symbols(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [s.strip().upper() for s in value.split(",") if s.strip()]
        return value

    def require_trading_credentials(self) -> None:
        if self.execution_mode == ExecutionMode.PAPER:
            return
        if not self.binance_api_key or not self.binance_api_secret:
            raise ValueError(
                "BINANCE_API_KEY/BINANCE_API_SECRET обязательны для testnet/live. "
                "Ожидается файл ~/.config/trading-bot/binance_testnet.env"
            )

    def market_rest_base(self) -> str:
        if self.market_data_mode == MarketDataMode.TESTNET:
            return self.binance_base_url
        return self.binance_prod_base_url

    def market_ws_base(self) -> str:
        if self.market_data_mode == MarketDataMode.TESTNET:
            return self.binance_ws_base_url
        return self.binance_prod_ws_base_url


def load_yaml_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Конфиг должен быть mapping: {path}")
    return data


def load_env_file(path: Path) -> None:
    """Простой loader KEY=VALUE без зависимости от python-dotenv."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def build_settings(
    config_path: Path | None = None,
    env_file: Path | None = None,
) -> Settings:
    """Собирает Settings из YAML + env (+ опциональный env-файл с ключами)."""
    yaml_data: dict[str, Any] = {}
    if config_path is None:
        default = Path(__file__).resolve().parents[2] / "configs" / "default.yaml"
        if default.exists():
            config_path = default
    if config_path and config_path.exists():
        yaml_data = load_yaml_config(config_path)

    risk_raw = yaml_data.pop("risk", {}) or {}
    paper_raw = yaml_data.pop("paper", {}) or {}
    live_testnet_raw = yaml_data.pop("live_testnet", {}) or {}
    fees = yaml_data.pop("fees", {}) or {}
    slippage = yaml_data.pop("slippage", {}) or {}
    yaml_data.pop("ingest", None)
    yaml_data.pop("logging", None)

    flat: dict[str, Any] = {**yaml_data}
    if fees:
        flat["fees_maker"] = fees.get("maker", 0.001)
        flat["fees_taker"] = fees.get("taker", 0.001)
    if slippage:
        flat["slippage_liquid"] = slippage.get("liquid", 0.0005)
        flat["slippage_illiquid"] = slippage.get("illiquid", 0.002)

    default_secret = Path.home() / ".config" / "trading-bot" / "binance_testnet.env"
    if env_file and env_file.exists():
        load_env_file(env_file)
    elif default_secret.exists():
        load_env_file(default_secret)

    settings = Settings(**flat)
    if risk_raw:
        settings.risk = RiskConfig(**risk_raw)
    if paper_raw:
        settings.paper = PaperConfig(**paper_raw)
    if live_testnet_raw:
        frac = float(live_testnet_raw.get("position_fraction", 0.05))
        live_testnet_raw["position_fraction"] = min(frac, 0.05)
        settings.live_testnet = LiveTestnetConfig(**live_testnet_raw)
    else:
        settings.live_testnet.position_fraction = min(
            settings.live_testnet.position_fraction, 0.05
        )
    return settings
