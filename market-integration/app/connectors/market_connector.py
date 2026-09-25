"""
Mock market data connector.

Simulates a live provider so the rest of the pipeline (queue, processor,
gateway, Symphony delivery) can be built and tested before a real
provider is chosen. When a provider is picked, write a new class here
(e.g. RealMarketConnector) that implements BaseMarketConnector the same
way -- nothing else in the app needs to change.

Beyond the live price tick, this also invents the fundamentals a quote
page needs (previous close, 52-week range, market cap, bid/ask, dividend
info, ...). None of that comes from a real GSE entitlement yet -- it is
generated once per symbol at connect() time so it stays internally
consistent (e.g. bid/ask track the live price) without pretending to be
real market data.

It also invents price *history* at connect() time (fetch_history()), so
charts have years of data to show immediately rather than only what
has been recorded since startup. The history is one continuous random
walk that ends exactly at the live starting price, and the quote's
previous close / open / day range / session volume / 52-week range are
all read off it, so the chart and the quote card agree. The live walk
uses the same per-symbol volatility, so the live tail of a chart looks
like a continuation of the backfilled part rather than a different
instrument.
"""

import asyncio
import math
import random
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Optional

from app.connectors.base_connector import BaseMarketConnector
from app.models.candle import INTERVALS, Candle, bucket_start, resample
from app.models.market_data import MarketData

COMPANY_NAMES = {
    "AAPL": "Apple Inc.",
    "MSFT": "Microsoft Corporation",
    "TSLA": "Tesla, Inc.",
    "NVDA": "NVIDIA Corporation",
    "SNAP": "Snap Inc.",
    "META": "Meta Platforms, Inc.",
}

EXCHANGE_LABEL = "GSE - Simulated Quote - GHS"

# How much synthetic history to invent per symbol. Fine-grained (5m)
# bars cover the recent past that intraday ranges (1D/5D/1M) draw from;
# daily bars cover everything older, back to the "Max" range.
INTRADAY_HISTORY_DAYS = 35
DAILY_HISTORY_YEARS = 10

# The mock trades 24/7, so volatility is annualized over calendar time.
_SECONDS_PER_YEAR = 365 * 24 * 3600


class MockMarketConnector(BaseMarketConnector):

    def __init__(self, symbols: list[str], interval_seconds: float = 0.5):
        super().__init__(symbols)
        self.interval_seconds = interval_seconds
        self._prices: dict[str, float] = {}
        self._profiles: dict[str, dict] = {}
        self._session: dict[str, dict] = {}
        self._history: dict[str, dict[str, list[Candle]]] = {}

    async def connect(self) -> None:
        print("Connecting to (mock) market data provider...")
        await asyncio.sleep(1)

        now = datetime.now(timezone.utc)
        today = now.date()

        for symbol in self.symbols:
            price = round(random.uniform(100, 500), 2)
            self._prices[symbol] = price

            annual_vol = random.uniform(0.25, 0.55)
            avg_volume = random.randint(8_000_000, 30_000_000)
            history = self._generate_history(symbol, price, annual_vol, avg_volume, now)
            self._history[symbol] = history

            # Quote fields that describe the past are read off the history
            # just generated, so the chart and the quote card agree.
            midnight = bucket_start(now, "1d")
            intraday = history["5m"]
            session_bars = [c for c in intraday if c.window_start >= midnight]
            previous_close = next(
                c.close for c in reversed(intraday) if c.window_start < midnight
            )
            year_bars = [c for c in history["1d"] if c.window_start >= now - timedelta(days=365)]

            forward_dividend = round(random.uniform(0, 2.5), 2)
            ex_dividend_date = today + timedelta(days=random.randint(5, 60))

            profile = {
                "name": COMPANY_NAMES.get(symbol, symbol),
                "annual_vol": annual_vol,
                "previous_close": previous_close,
                "week52_low": min(c.low for c in year_bars),
                "week52_high": max(c.high for c in year_bars),
                "market_cap": round(price * random.uniform(2_000_000, 9_000_000), 0),
                "beta": round(random.uniform(0.7, 1.8), 2),
                "pe_ratio": round(random.uniform(10, 40), 2),
                "eps": round(price / random.uniform(12, 30), 2),
                "avg_volume": avg_volume,
                "forward_dividend": forward_dividend,
                "forward_dividend_yield": round((forward_dividend / price) * 100, 2),
                "ex_dividend_date": ex_dividend_date.isoformat(),
                "earnings_date": (today + timedelta(days=random.randint(10, 90))).isoformat(),
                "target_est": round(price * random.uniform(1.05, 1.35), 2),
                "dividend_announcement": None,
            }

            if forward_dividend > 0 and random.random() < 0.5:
                profile["dividend_announcement"] = (
                    f"{symbol} announced a cash dividend of "
                    f"GH₵{forward_dividend:.2f} with an ex-date of {ex_dividend_date.isoformat()}"
                )

            self._profiles[symbol] = profile
            self._session[symbol] = {
                "open": session_bars[0].open,
                "day_high": max(c.high for c in session_bars),
                "day_low": min(c.low for c in session_bars),
                "volume": sum(c.volume for c in session_bars),
            }

        self.running = True
        print("Connected to (mock) market data provider.")

    async def fetch_history(
        self, symbol: str, interval: str, start: Optional[datetime] = None
    ) -> list[Candle]:
        candles = self._history.get(symbol, {}).get(interval, [])
        if start is None:
            return list(candles)
        # Include the bucket that contains `start`, not only those after it.
        first = bucket_start(start, interval)
        return [c for c in candles if c.window_start >= first]

    def _generate_history(
        self,
        symbol: str,
        end_price: float,
        annual_vol: float,
        avg_volume: int,
        now: datetime,
    ) -> dict[str, list[Candle]]:
        """One continuous random walk, generated *backwards* from
        end_price so it lands exactly on the live starting price: 5m bars
        for the recent past, daily bars before that. Every other interval
        is rolled up from those, so all of them agree with each other."""
        bar_minutes = 5
        intraday_start = bucket_start(now - timedelta(days=INTRADAY_HISTORY_DAYS), "1d")
        step = INTERVALS["5m"]
        count = (bucket_start(now, "5m") - intraday_start) // step + 1

        intraday = self._walk_back(
            symbol, "5m", end_price, annual_vol,
            starts=[intraday_start + i * step for i in range(count)],
            mean_volume=avg_volume * bar_minutes / (24 * 60),
        )

        day = INTERVALS["1d"]
        days = DAILY_HISTORY_YEARS * 365
        older_daily = self._walk_back(
            symbol, "1d", intraday[0].open, annual_vol,
            starts=[intraday_start - (days - i) * day for i in range(days)],
            mean_volume=avg_volume,
        )

        daily = older_daily + resample(intraday, "1d")
        return {
            "5m": intraday,
            "15m": resample(intraday, "15m"),
            "1h": resample(intraday, "1h"),
            "1d": daily,
            "1w": resample(daily, "1w"),
        }

    @staticmethod
    def _walk_back(
        symbol: str,
        interval: str,
        end_price: float,
        annual_vol: float,
        starts: list[datetime],
        mean_volume: float,
    ) -> list[Candle]:
        """Geometric random walk over the given bar start times, built
        from the last bar backwards so the final close == end_price."""
        span = INTERVALS[interval]
        sigma = annual_vol * math.sqrt(span.total_seconds() / _SECONDS_PER_YEAR)
        bars: list[Candle] = []
        close = end_price
        for start in reversed(starts):
            open_ = max(round(close / math.exp(random.gauss(0, sigma)), 2), 0.01)
            high = round(max(open_, close) * (1 + abs(random.gauss(0, sigma / 2))), 2)
            low = max(round(min(open_, close) * (1 - abs(random.gauss(0, sigma / 2))), 2), 0.01)
            bars.append(Candle(
                symbol=symbol,
                interval=interval,
                window_start=start,
                window_end=start + span,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=int(mean_volume * random.lognormvariate(-0.08, 0.4)),
            ))
            close = open_
        bars.reverse()
        return bars

    async def stream(self) -> AsyncIterator[MarketData]:
        if not self.running:
            raise RuntimeError("Connector is not connected. Call connect() first.")

        while self.running:
            for symbol in self.symbols:
                profile = self._profiles[symbol]

                # Same volatility as the backfilled history, scaled to the
                # tick interval, so live candles continue the history.
                sigma = profile["annual_vol"] * math.sqrt(self.interval_seconds / _SECONDS_PER_YEAR)
                self._prices[symbol] *= math.exp(random.gauss(0, sigma))
                self._prices[symbol] = max(self._prices[symbol], 0.01)
                price = round(self._prices[symbol], 2)

                session = self._session[symbol]
                session["day_high"] = max(session["day_high"], price)
                session["day_low"] = min(session["day_low"], price)
                # Paced so a full day adds up to roughly avg_volume, the
                # same scale the backfilled bars use.
                per_tick = profile["avg_volume"] * self.interval_seconds / (24 * 3600)
                session["volume"] += max(int(per_tick * random.uniform(0.2, 1.8)), 1)

                # A new high/low extends the 52-week range rather than
                # making the tick fail validation.
                profile["week52_high"] = max(profile["week52_high"], price)
                profile["week52_low"] = min(profile["week52_low"], price)

                spread = max(round(price * 0.0006, 2), 0.01)

                raw = {
                    "symbol": symbol,
                    "name": profile["name"],
                    "exchange_label": EXCHANGE_LABEL,
                    "price": price,
                    "previous_close": profile["previous_close"],
                    "open": round(session["open"], 2),
                    "day_high": round(session["day_high"], 2),
                    "day_low": round(session["day_low"], 2),
                    "week52_high": profile["week52_high"],
                    "week52_low": profile["week52_low"],
                    "market_cap": profile["market_cap"],
                    "beta": profile["beta"],
                    "pe_ratio": profile["pe_ratio"],
                    "eps": profile["eps"],
                    "bid": round(price - spread, 2),
                    "bid_size": random.randint(1, 40) * 100,
                    "ask": round(price + spread, 2),
                    "ask_size": random.randint(1, 40) * 100,
                    "volume": session["volume"],
                    "avg_volume": profile["avg_volume"],
                    "forward_dividend": profile["forward_dividend"],
                    "forward_dividend_yield": profile["forward_dividend_yield"],
                    "ex_dividend_date": profile["ex_dividend_date"],
                    "earnings_date": profile["earnings_date"],
                    "target_est": profile["target_est"],
                    "dividend_announcement": profile["dividend_announcement"],
                    "timestamp": datetime.now(timezone.utc),
                }

                yield self.normalize(raw)

            await asyncio.sleep(self.interval_seconds)

    async def disconnect(self) -> None:
        self.running = False
        print("Disconnected from (mock) market data provider.")

    def normalize(self, raw_data: dict) -> MarketData:
        # The mock already produces canonical field names, so this is a
        # pass-through. A real connector's normalize() would map the
        # provider's actual field names (e.g. sym/px/qty/ts) here.
        return MarketData(**raw_data)
