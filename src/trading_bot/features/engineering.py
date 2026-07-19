"""Feature engineering: klines + orderbook (bookTicker)."""

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


ORDERBOOK_FEATURE_COLUMNS = [
    "ob_spread_bps",
    "ob_imbalance",
    "ob_microprice_premium",  # (microprice - close) / close
    "ob_bid_qty",
    "ob_ask_qty",
]

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
    *ORDERBOOK_FEATURE_COLUMNS,
]


def attach_orderbook_features(
    klines: pd.DataFrame,
    books: pd.DataFrame | None,
) -> pd.DataFrame:
    """
    Присоединяет bookTicker к барам kline (asof backward).
    books: ts, bid_price, bid_qty, ask_price, ask_qty [, spread_bps, imbalance, microprice]
    Если books пуст — заполняет OB нулями (совместимость со старой историей).
    """
    df = klines.copy()
    if books is None or books.empty or "ts" not in df.columns:
        for col in ORDERBOOK_FEATURE_COLUMNS:
            df[col] = 0.0
        return df

    b = books.copy()
    b["ts"] = pd.to_datetime(b["ts"], utc=True)
    b = b.sort_values("ts")
    if "spread_bps" not in b.columns:
        mid = (b["bid_price"] + b["ask_price"]) / 2.0
        b["spread_bps"] = ((b["ask_price"] - b["bid_price"]) / mid.replace(0, np.nan)) * 10_000.0
    if "imbalance" not in b.columns:
        denom = b["bid_qty"] + b["ask_qty"]
        b["imbalance"] = (b["bid_qty"] - b["ask_qty"]) / denom.replace(0, np.nan)
    if "microprice" not in b.columns:
        denom = b["bid_qty"] + b["ask_qty"]
        b["microprice"] = (
            b["ask_price"] * b["bid_qty"] + b["bid_price"] * b["ask_qty"]
        ) / denom.replace(0, np.nan)

    drop_cols = [c for c in ORDERBOOK_FEATURE_COLUMNS + ["microprice"] if c in df.columns]
    left = df.drop(columns=drop_cols, errors="ignore").sort_values("ts").reset_index(drop=True)
    right = b[["ts", "spread_bps", "imbalance", "microprice", "bid_qty", "ask_qty"]].rename(
        columns={
            "spread_bps": "ob_spread_bps",
            "imbalance": "ob_imbalance",
            "bid_qty": "ob_bid_qty",
            "ask_qty": "ob_ask_qty",
        }
    )
    merged = pd.merge_asof(left, right, on="ts", direction="backward")
    close = pd.to_numeric(merged["close"], errors="coerce")
    micro = pd.to_numeric(merged["microprice"], errors="coerce")
    merged["ob_microprice_premium"] = (micro - close) / close.replace(0, np.nan)
    for col in ORDERBOOK_FEATURE_COLUMNS:
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)
    if "microprice" in merged.columns:
        merged = merged.drop(columns=["microprice"])
    return merged


def inject_live_orderbook(
    feats_row: pd.DataFrame,
    *,
    close: float,
    spread_bps: float,
    imbalance: float,
    microprice: float,
    bid_qty: float,
    ask_qty: float,
) -> pd.DataFrame:
    """Подставляет текущий стакан в последнюю строку фич (live inference)."""
    out = feats_row.copy()
    out["ob_spread_bps"] = float(spread_bps)
    out["ob_imbalance"] = float(imbalance)
    out["ob_bid_qty"] = float(bid_qty)
    out["ob_ask_qty"] = float(ask_qty)
    prem = 0.0 if close <= 0 else (float(microprice) - float(close)) / float(close)
    out["ob_microprice_premium"] = prem
    return out

