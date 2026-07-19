"""Feature engineering на klines (MVP: 15+ признаков без orderbook)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _garman_klass(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    # GK variance proxy (не annualized)
    log_hl = np.log(high / low).replace([np.inf, -np.inf], np.nan)
    log_co = np.log(close / open_).replace([np.inf, -np.inf], np.nan)
    return 0.5 * (log_hl**2) - (2.0 * np.log(2.0) - 1.0) * (log_co**2)


@dataclass(slots=True)
class FeatureBuilder:
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    vol_window: int = 20


def build_feature_frame(klines: pd.DataFrame, builder: FeatureBuilder | None = None) -> pd.DataFrame:
    """
    Ожидает колонки: ts, open, high, low, close, volume (и опционально symbol).
    Возвращает исходные колонки + фичи. NaN на прогреве окон — нормально.
    """
    b = builder or FeatureBuilder()
    if klines.empty:
        return klines.copy()

    df = klines.copy()
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["log_return"] = np.log(df["close"] / df["close"].shift(1))
    df["simple_return"] = df["close"].pct_change()
    df["realized_vol"] = df["log_return"].rolling(b.vol_window).std()
    df["parkinson_vol"] = (
        np.sqrt((1.0 / (4.0 * np.log(2.0))) * (np.log(df["high"] / df["low"]) ** 2))
        .replace([np.inf, -np.inf], np.nan)
        .rolling(b.vol_window)
        .mean()
    )
    df["garman_klass"] = (
        _garman_klass(df["open"], df["high"], df["low"], df["close"]).rolling(b.vol_window).mean()
    )
    df["atr"] = _true_range(df["high"], df["low"], df["close"]).rolling(b.vol_window).mean()

    df["rsi"] = _rsi(df["close"], b.rsi_period)
    ema_fast = _ema(df["close"], b.macd_fast)
    ema_slow = _ema(df["close"], b.macd_slow)
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = _ema(df["macd"], b.macd_signal)
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    df["sma_10"] = df["close"].rolling(10).mean()
    df["sma_20"] = df["close"].rolling(20).mean()
    df["ema_10"] = _ema(df["close"], 10)
    df["ema_20"] = _ema(df["close"], 20)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vwap_20"] = (typical * df["volume"]).rolling(20).sum() / df["volume"].rolling(20).sum().replace(
        0.0, np.nan
    )

    df["volume_z"] = (
        (df["volume"] - df["volume"].rolling(b.vol_window).mean())
        / df["volume"].rolling(b.vol_window).std().replace(0.0, np.nan)
    )
    df["obv"] = (np.sign(df["close"].diff().fillna(0.0)) * df["volume"]).cumsum()
    df["volume_change"] = df["volume"].pct_change()

    if "ts" in df.columns:
        ts = pd.to_datetime(df["ts"], utc=True)
        df["hour"] = ts.dt.hour
        df["minute"] = ts.dt.minute
        df["dow"] = ts.dt.dayofweek
        # грубые session flags (UTC)
        df["session_asia"] = ((df["hour"] >= 0) & (df["hour"] < 8)).astype(int)
        df["session_eu"] = ((df["hour"] >= 7) & (df["hour"] < 16)).astype(int)
        df["session_us"] = ((df["hour"] >= 13) & (df["hour"] < 22)).astype(int)

    # target helpers (не для live inference без сдвига — помечаем явно)
    df["target_log_return_5"] = np.log(df["close"].shift(-5) / df["close"])
    df["target_direction_5"] = (df["target_log_return_5"] > 0).astype("float")

    return df


FEATURE_COLUMNS = [
    "log_return",
    "simple_return",
    "realized_vol",
    "parkinson_vol",
    "garman_klass",
    "atr",
    "rsi",
    "macd",
    "macd_signal",
    "macd_hist",
    "sma_10",
    "sma_20",
    "ema_10",
    "ema_20",
    "vwap_20",
    "volume_z",
    "obv",
    "volume_change",
    "hour",
    "minute",
    "dow",
    "session_asia",
    "session_eu",
    "session_us",
]
