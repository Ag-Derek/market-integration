"""
Mock Ghana Commodity Exchange (GCX) connector: simulated spot contracts
(white and yellow maize, soya bean, sesame, sorghum, rice), streamed as
CommodityTick.

It is MockMarketConnector with a GCX quote on top: the same random walk,
backfilled history, trading tiers and book shapes, calibrated by each
contract's "mock" block in data/instruments.json (gse_mock_profiles.py
has the fields; for commodities `avg_volume` and `spread` are kilograms
a day and cedis per tonne). What differs is what the quote means, per
the GCX market report (docs/commodities-gcx.md):

  * prices are cedis per metric tonne, and volumes kilograms;
  * the closing price is the last trade, not a VWAP as on the GSE, so a
    session's close becomes the next one's previous close;
  * the opening price is the session's first trade, blank (None) until
    there is one, as are the high and low.

Like the equity mock it trades only while its calendar (GCX's session,
09:00-15:00 GMT on GSE trading days) says the market is open, with its
activity compressed for a demo: trades come every minute or so in the
busier contracts, each correspondingly small.
"""

import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from app.connectors.market_connector import MockMarketConnector
from app.models.candle import bucket_start
from app.models.commodity import KG_PER_MT, CommodityTick
from app.models.instrument import Instrument

if TYPE_CHECKING:
    from app.session.calendar import MarketCalendar

EXCHANGE_LABEL = "GCX - Simulated Quote - GHS per MT"


class MockCommodityConnector(MockMarketConnector):

    def __init__(
        self,
        symbols: list[str],
        interval_seconds: float = 0.5,
        instruments: Optional[dict[str, Instrument]] = None,
        mock_seeds: Optional[dict[str, dict]] = None,
        calendar: Optional["MarketCalendar"] = None,
    ):
        super().__init__(symbols, interval_seconds, instruments, mock_seeds, calendar)
        not_commodities = [s for s, i in self._instruments.items() if i.asset_class != "commodity"]
        if not_commodities:
            raise ValueError(f"not commodities: {', '.join(not_commodities)}")

    async def connect(self) -> None:
        resuming = bool(self._profiles)
        await super().connect()
        if resuming:
            return
        now = datetime.now(timezone.utc)
        midnight = bucket_start(now, "1d")
        for symbol in self.symbols:
            bars = self._history[symbol]["5m"]
            before = [c for c in bars if c.window_start < midnight]
            today = [c for c in bars if c.window_start >= midnight and c.volume]
            profile = self._profiles[symbol]
            # GCX's closing price is the last trade, so the previous close
            # is the last bar before today, not the GSE's session VWAP.
            if before:
                profile["previous_close"] = before[-1].close
            session = self._session[symbol]
            session["open"] = today[0].open if today else None
            session["trade_count"] = len(today)
            if not today:
                session["day_high"] = session["day_low"] = None

    def _new_session(self, symbol: str) -> None:
        self._profiles[symbol]["previous_close"] = round(self._prices[symbol], 2)
        self._session[symbol] = {
            "open": None,
            "day_high": None,
            "day_low": None,
            "volume": 0,
            "value": 0.0,
            "trade_count": 0,
        }

    def _trade(self, symbol: str, profile: dict, trade_gap: float) -> None:
        session = self._session[symbol]
        # The base class widens the day's range to take in the trade price.
        # On the session's first trade there is no range yet: start from an
        # empty one, so the trade alone sets both ends.
        if session["day_high"] is None:
            session["day_high"], session["day_low"] = 0.0, float("inf")
        super()._trade(symbol, profile, trade_gap)
        if session["open"] is None:
            session["open"] = round(self._prices[symbol], 2)
        session["trade_count"] += 1

    def _quote(self, symbol: str, profile: dict) -> dict:
        instrument = self._instruments[symbol]
        price = round(self._prices[symbol], 2)
        session = self._session[symbol]
        # Until it trades this session, the last price is the previous close.
        if not session["volume"]:
            price = profile["previous_close"]
        bid, ask = self._book(price, profile["book"], profile["spread"])
        return {
            "symbol": symbol,
            "name": instrument.name,
            "commodity": instrument.commodity,
            "grade": instrument.grade,
            "delivery_centre": instrument.delivery_centre,
            "currency": instrument.currency,
            "unit": instrument.unit,
            "exchange_label": EXCHANGE_LABEL,
            "price": price,
            "previous_close": profile["previous_close"],
            "change": round(price - profile["previous_close"], 2),
            "open": session["open"],
            "day_high": session["day_high"],
            "day_low": session["day_low"],
            "week52_high": max(profile["week52_high"], price),
            "week52_low": min(profile["week52_low"], price),
            "bid": bid,
            "bid_size": random.randint(1, 20) * KG_PER_MT if bid is not None else 0,
            "ask": ask,
            "ask_size": random.randint(1, 20) * KG_PER_MT if ask is not None else 0,
            "volume": session["volume"],
            # The base class sums price x quantity: GHS per MT x kg.
            "value_traded": round(session["value"] / KG_PER_MT, 2),
            "trade_count": session["trade_count"],
            "avg_volume": profile["avg_volume"],
            "timestamp": datetime.now(timezone.utc),
            "last_trade_at": self._last_trade_at.get(symbol),
        }

    def normalize(self, raw_data: dict) -> CommodityTick:
        return CommodityTick(**raw_data)
