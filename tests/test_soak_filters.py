from __future__ import annotations

from trading_bot.execution.soak import _dec_str, parse_symbol_filters


def test_dec_str_rounds_down() -> None:
    assert _dec_str(1.239, "0.01") == "1.23"
    assert float(_dec_str(0.0000199, "0.00001")) == 0.00001


def test_parse_filters() -> None:
    info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01"},
                    {"filterType": "NOTIONAL", "minNotional": "5"},
                ],
            }
        ]
    }
    f = parse_symbol_filters(info, "BTCUSDT")
    assert f["stepSize"] == "0.00001"
    assert f["minNotional"] == "5"
