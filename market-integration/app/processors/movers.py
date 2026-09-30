"""
Today's movers among equities, behind GET /movers: top gainers, top
losers and most active.

Equities only. Bills accrete towards par, so nearly every one "gains" a
little each day and would crowd out real moves, and most bonds barely
move in a session.

Change follows the GSE, as everywhere else in the UI: session VWAP
against the previous VWAP close, as a percentage. Gainers are only
names that are up, losers only names that are down; most active is by
shares traded this session, only names that traded. Ties go to the
larger value traded, then symbol.
"""

from typing import Iterable, Literal

from app.models.market_data import MarketData

MoverType = Literal["gainers", "losers", "active"]


def change_percent(quote: MarketData) -> float:
    return round(quote.change / quote.previous_close * 100, 2)


def rank_movers(quotes: Iterable[MarketData], type: MoverType, limit: int) -> list[MarketData]:
    if type == "gainers":
        picked = [q for q in quotes if q.change > 0]
        key = lambda q: (-q.change / q.previous_close, -q.value_traded, q.symbol)  # noqa: E731
    elif type == "losers":
        picked = [q for q in quotes if q.change < 0]
        key = lambda q: (q.change / q.previous_close, -q.value_traded, q.symbol)  # noqa: E731
    else:
        picked = [q for q in quotes if q.volume > 0]
        key = lambda q: (-q.volume, -q.value_traded, q.symbol)  # noqa: E731
    return sorted(picked, key=key)[:limit]
