"""In-memory latest bookTicker по символам."""

from __future__ import annotations

from trading_bot.features.orderbook import BookTicker, book_ticker_from_payload


class BookStateStore:
    def __init__(self) -> None:
        self._books: dict[str, BookTicker] = {}
        self.updates = 0

    def update_from_payload(self, payload: dict) -> BookTicker | None:
        book = book_ticker_from_payload(payload)
        if book is None:
            return None
        self._books[book.symbol] = book
        self.updates += 1
        return book

    def get(self, symbol: str) -> BookTicker | None:
        return self._books.get(symbol.upper())
