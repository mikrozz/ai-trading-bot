"""Фичи стакана из bookTicker / L2 snapshot."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class BookTicker:
    symbol: str
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float
    event_time_ms: int = 0

    @property
    def mid(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid <= 0:
            return 0.0
        return (self.spread / mid) * 10_000.0

    @property
    def microprice(self) -> float:
        denom = self.bid_qty + self.ask_qty
        if denom <= 0:
            return self.mid
        return (self.ask_price * self.bid_qty + self.bid_price * self.ask_qty) / denom

    @property
    def imbalance(self) -> float:
        denom = self.bid_qty + self.ask_qty
        if denom <= 0:
            return 0.0
        return (self.bid_qty - self.ask_qty) / denom


def book_ticker_from_payload(payload: dict) -> BookTicker | None:
    """Binance bookTicker stream payload → BookTicker."""
    try:
        symbol = str(payload.get("s") or "").upper()
        if not symbol:
            return None
        return BookTicker(
            symbol=symbol,
            bid_price=float(payload["b"]),
            bid_qty=float(payload["B"]),
            ask_price=float(payload["a"]),
            ask_qty=float(payload["A"]),
            event_time_ms=int(payload.get("E") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def orderbook_feature_dict(book: BookTicker) -> dict[str, float]:
    return {
        "ob_spread": book.spread,
        "ob_spread_bps": book.spread_bps,
        "ob_microprice": book.microprice,
        "ob_imbalance": book.imbalance,
        "ob_bid_qty": book.bid_qty,
        "ob_ask_qty": book.ask_qty,
        "ob_mid": book.mid,
    }
