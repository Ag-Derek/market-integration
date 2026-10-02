"""
Mock market data connector.

Simulates a live provider so the rest of the pipeline (queue, processor,
gateway, Symphony delivery) can be built and tested before a real
provider is chosen. When a provider is picked, write a new class here
(e.g. RealMarketConnector) that implements BaseMarketConnector the same
way -- nothing else in the app needs to change.

The universe is the GSE instrument master (app/instruments/), and each
symbol behaves according to the "mock" block of its seed entry in
data/instruments.json (see gse_mock_profiles.py for the fields):
it starts at a real recent closing price, moves with a volatility sized
to its real 52-week range, and trades about as often as it does on the
GSE. Thinly traded names can go days without a trade, and many quote
only a bid, only an offer, or nothing -- as they do on the exchange.

Beyond the price, this also invents the fundamentals a quote page needs
(market cap, P/E, dividend info, ...). None of that comes from a real
GSE entitlement yet -- it is generated once per symbol at connect() time
so it stays internally consistent without pretending to be real data.

It also invents price *history* at connect() time (fetch_history()), so
charts have years of data to show immediately rather than only what
has been recorded since startup. The history is one continuous random
walk that ends exactly at the live starting price, with flat,
zero-volume bars wherever the symbol didn't trade, and the quote's
previous close / open / day range / session volume and value / year and
52-week ranges are all read off it, so the chart and the quote card
agree. The live walk uses the same volatility and trade frequency, so
the live tail of a chart looks like a continuation of the backfilled
part.

stream() emits one snapshot per symbol when it starts, so every symbol
has a quote downstream from the outset, and after that only a tick for
each trade. A symbol that doesn't trade sends nothing, so its latest
quote just ages -- that is the no-trade / staleness case consumers need
to handle. Every quote carries `last_trade_at` (when the symbol last
traded, possibly days ago) apart from `timestamp` (when it was
published), and `last_heartbeat` is refreshed on every loop whether or
not anything traded, the way a real provider's keepalive would be.

Given a MarketCalendar, the mock only trades while the market is open,
and the first trade loop of a new session rolls the session over: the
last session's VWAP becomes the previous close and volume starts again
from 0. Without one (tests) it trades around the clock. The backfilled
history is still around the clock either way.
"""

import asyncio
import math
import random
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, AsyncIterator, Optional

from app.connectors.base_connector import BaseMarketConnector
from app.connectors.gse_mock_profiles import (
    DEFAULT_ANNUAL_VOL,
    TRADE_GAP_SECONDS,
    MockProfile,
    mock_profile,
)
from app.instruments import INSTRUMENTS, MOCK_SEEDS, get_instrument
from app.models.candle import INTERVALS, Candle, bucket_start, resample
from app.models.instrument import Instrument
from app.models.market_data import MarketData

if TYPE_CHECKING:
    from app.session.calendar import MarketCalendar

EXCHANGE_LABEL = "GSE - Simulated Quote - GHS"

# GSE equities are priced to the pesewa.
TICK_SIZE = 0.01

# How much synthetic history to invent per symbol, finest last: each
# intraday interval is walked only over the span the chart ranges read
# it for (see app.aggregation.ranges.HISTORY_DEPTH), and each finer
# segment also rolls up into the coarser series above it. Daily bars
# cover everything older, back to the "Max" range. Walking 5 weeks of
# 5m bars instead would cost ~2.5x the startup time across the GSE
# universe for bars no range ever shows.
INTRADAY_SEGMENTS: tuple[tuple[str, int], ...] = (("1h", 35), ("15m", 7), ("5m", 2))
DAILY_HISTORY_YEARS = 10

# The mock trades 24/7, so volatility is annualized over calendar time.
_SECONDS_PER_YEAR = 365 * 24 * 3600
_SECONDS_PER_DAY = 24 * 3600

# For a random walk, the expected high/low log-range over a year is about
# 1.6 x the annual volatility; used to turn a real 52-week range into a
# volatility. Capped so penny stocks with huge ranges stay usable.
_RANGE_TO_VOL = 1.6
_MAX_ANNUAL_VOL = 1.2


def _annual_vol(mp: MockProfile) -> float:
    if mp.year_range is None:
        return DEFAULT_ANNUAL_VOL[mp.tier]
    low, high = mp.year_range
    return min(math.log(high / low) / _RANGE_TO_VOL, _MAX_ANNUAL_VOL)


def _typical_price(c: Candle) -> float:
    """Stand-in for a bar's VWAP: the history is bars, not trades."""
    return (c.high + c.low + c.close) / 3


def _previous_session_vwap(history: dict[str, list[Candle]], midnight: datetime) -> float:
    """The GSE's "previous closing price" is the previous session's VWAP,
    and a session without trades carries the last one over. So: VWAP of
    yesterday's 5m bars, else of the most recent earlier day that traded,
    else (never traded) the last close."""
    yesterday = midnight - INTERVALS["1d"]
    bars = [c for c in history["5m"] if yesterday <= c.window_start < midnight and c.volume]
    if not bars:
        traded_days = [c for c in history["1d"] if c.window_start < yesterday and c.volume]
        bars = traded_days[-1:]
    if not bars:
        return next(c.close for c in reversed(history["5m"]) if c.window_start < midnight)
    volume = sum(c.volume for c in bars)
    return max(round(sum(_typical_price(c) * c.volume for c in bars) / volume, 2), TICK_SIZE)


def _last_traded(history: dict[str, list[Candle]], now: datetime) -> Optional[datetime]:
    """When the backfilled history last traded: the end of the newest bar
    with volume, finest interval first (bars say when, not exactly when
    within them). None if it never did."""
    for interval in ("5m", "15m", "1h", "1d"):
        traded = [c for c in history.get(interval, []) if c.volume]
        if traded:
            return min(traded[-1].window_end, now)
    return None


def _trade_probability(span_seconds: float, trade_gap_seconds: float) -> float:
    """Chance of at least one trade in `span_seconds`, for trades
    arriving on average every `trade_gap_seconds`."""
    return 1 - math.exp(-span_seconds / trade_gap_seconds)


class MockMarketConnector(BaseMarketConnector):

    def __init__(
        self,
        symbols: list[str],
        interval_seconds: float = 0.5,
        instruments: Optional[dict[str, Instrument]] = None,
        mock_seeds: Optional[dict[str, dict]] = None,
        calendar: Optional["MarketCalendar"] = None,
    ):
        """`instruments` / `mock_seeds` default to the instrument master
        loaded from data/instruments.json; pass another load_seed() result
        to run against a different seed file. With a `calendar`, symbols
        only trade while it says the market is open."""
        super().__init__(symbols)
        instruments = INSTRUMENTS if instruments is None else instruments
        mock_seeds = MOCK_SEEDS if mock_seeds is None else mock_seeds
        # Resolved up front so an unknown symbol or a bad mock block fails
        # at construction, not halfway through connect().
        self._instruments = {s: get_instrument(s, instruments) for s in symbols}
        self._mock = {s: mock_profile(mock_seeds.get(s)) for s in symbols}
        self.interval_seconds = interval_seconds
        self._prices: dict[str, float] = {}
        self._profiles: dict[str, dict] = {}
        self._session: dict[str, dict] = {}
        self._history: dict[str, dict[str, list[Candle]]] = {}
        self._last_trade_at: dict[str, Optional[datetime]] = {}
        self._calendar = calendar
        # The (exchange-local) day the current session belongs to.
        self._session_day: Optional[date] = None

    async def connect(self) -> None:
        print("Connecting to (mock) market data provider...")
        await asyncio.sleep(1)

        now = datetime.now(timezone.utc)
        if self._profiles:
            # A reconnect: the simulated market carried on without us, as
            # a real provider's would, so resume it rather than generate a
            # new one (which would reset every price and chart).
            self.running = True
            self.last_heartbeat = now
            print("Reconnected to (mock) market data provider.")
            return
        today = now.date()

        for symbol in self.symbols:
            instrument = self._instruments[symbol]
            mp = self._mock[symbol]
            price = mp.price
            self._prices[symbol] = price

            annual_vol = _annual_vol(mp)
            trade_gap = TRADE_GAP_SECONDS[mp.tier]
            history = self._generate_history(symbol, price, annual_vol, trade_gap, mp.avg_volume, now)
            self._history[symbol] = history

            # Quote fields that describe the past are read off the history
            # just generated, so the chart and the quote card agree.
            midnight = bucket_start(now, "1d")
            intraday = history["5m"]
            session_bars = [c for c in intraday if c.window_start >= midnight]
            previous_close = _previous_session_vwap(history, midnight)
            # The report's year range (calendar year, pending confirmation)
            # and the trailing 52 weeks. Both also cover the carried-over
            # closing VWAP, which can predate them for a name that hasn't
            # traded in a while.
            closes = (previous_close, price)
            year_bars = [c for c in history["1d"] if c.window_start.year == now.year]
            week52_bars = [c for c in history["1d"] if c.window_start >= now - timedelta(days=365)]

            forward_dividend = round(price * random.uniform(0, 0.08), 2)
            ex_dividend_date = today + timedelta(days=random.randint(5, 60))
            pe_ratio = round(random.uniform(3, 15), 2)

            profile = {
                "name": instrument.name,
                "annual_vol": annual_vol,
                "trade_gap": trade_gap,
                "book": mp.book,
                "spread": mp.spread,
                "previous_close": previous_close,
                "year_low": min(min(c.low for c in year_bars), *closes),
                "year_high": max(max(c.high for c in year_bars), *closes),
                "week52_low": min(min(c.low for c in week52_bars), *closes),
                "week52_high": max(max(c.high for c in week52_bars), *closes),
                "beta": round(random.uniform(0.3, 1.2), 2),
                "pe_ratio": pe_ratio,
                "eps": round(price / pe_ratio, 2),
                "avg_volume": mp.avg_volume,
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
            self._last_trade_at[symbol] = _last_traded(history, now)
            self._session[symbol] = {
                # The GSE report's opening price is the previous closing
                # VWAP on every row, traded or not -- a reference price,
                # not a first trade (docs/data-formats.md, equities).
                "open": previous_close,
                "day_high": max(c.high for c in session_bars),
                "day_low": min(c.low for c in session_bars),
                "volume": sum(c.volume for c in session_bars),
                # Running sum of price x shares, for the session VWAP.
                "value": sum(_typical_price(c) * c.volume for c in session_bars),
            }

        if self._calendar is not None:
            self._session_day = self._calendar.local_date(now)
        self.running = True
        self.last_heartbeat = now
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
        trade_gap: float,
        avg_volume: int,
        now: datetime,
    ) -> dict[str, list[Candle]]:
        """One continuous random walk, generated *backwards* from
        end_price so it lands exactly on the live starting price: the
        INTRADAY_SEGMENTS for the recent past, newest (finest) first,
        then daily bars before that. Each coarser series is its own
        older segment plus the finer one rolled up, so every interval
        agrees with the others wherever they overlap."""
        midnight = bucket_start(now, "1d")
        seg_starts = [midnight - timedelta(days=days) for _, days in INTRADAY_SEGMENTS]
        segments: dict[str, list[Candle]] = {}
        price = end_price
        for i in reversed(range(len(INTRADAY_SEGMENTS))):
            interval = INTRADAY_SEGMENTS[i][0]
            step = INTERVALS[interval]
            start = seg_starts[i]
            if i == len(INTRADAY_SEGMENTS) - 1:
                count = (bucket_start(now, interval) - start) // step + 1  # through the open bar
            else:
                count = (seg_starts[i + 1] - start) // step
            bars = self._walk_back(
                symbol, interval, price, annual_vol, trade_gap,
                starts=[start + n * step for n in range(count)],
                mean_volume=avg_volume * step.total_seconds() / _SECONDS_PER_DAY,
            )
            segments[interval] = bars
            price = bars[0].open

        day = INTERVALS["1d"]
        days = DAILY_HISTORY_YEARS * 365
        older_daily = self._walk_back(
            symbol, "1d", price, annual_vol, trade_gap,
            starts=[seg_starts[0] - (days - n) * day for n in range(days)],
            mean_volume=avg_volume,
        )

        # Finest to coarsest, each series = its own segment + the finer
        # series rolled up.
        series: dict[str, list[Candle]] = {}
        finer: list[Candle] = []
        for interval, _ in reversed(INTRADAY_SEGMENTS):
            series[interval] = segments[interval] + (resample(finer, interval) if finer else [])
            finer = series[interval]
        series["1d"] = older_daily + resample(finer, "1d")
        series["1w"] = resample(series["1d"], "1w")
        return series

    @staticmethod
    def _walk_back(
        symbol: str,
        interval: str,
        end_price: float,
        annual_vol: float,
        trade_gap: float,
        starts: list[datetime],
        mean_volume: float,
    ) -> list[Candle]:
        """Geometric random walk over the given bar start times, built
        from the last bar backwards so the final close == end_price.

        A bar only has a trade with the probability implied by
        trade_gap; bars without one are flat at the previous close with
        zero volume. Traded bars move (and carry volume) enough more to
        keep the overall volatility and daily volume unchanged."""
        span = INTERVALS[interval]
        p_trade = _trade_probability(span.total_seconds(), trade_gap)
        sigma = annual_vol * math.sqrt(span.total_seconds() / _SECONDS_PER_YEAR / p_trade)
        bars: list[Candle] = []
        close = end_price
        for start in reversed(starts):
            if random.random() < p_trade:
                open_ = max(round(close / math.exp(random.gauss(0, sigma)), 2), TICK_SIZE)
                high = round(max(open_, close) * (1 + abs(random.gauss(0, sigma / 2))), 2)
                low = max(round(min(open_, close) * (1 - abs(random.gauss(0, sigma / 2))), 2), TICK_SIZE)
                volume = max(int(mean_volume / p_trade * random.lognormvariate(-0.08, 0.4)), 1)
            else:
                open_ = high = low = close
                volume = 0
            bars.append(Candle(
                symbol=symbol,
                interval=interval,
                window_start=start,
                window_end=start + span,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
            ))
            close = open_
        bars.reverse()
        return bars

    async def stream(self) -> AsyncIterator[MarketData]:
        if not self.running:
            raise RuntimeError("Connector is not connected. Call connect() first.")

        snapshot = True
        while self.running:
            now = datetime.now(timezone.utc)
            # The mock provider is "alive" on every loop, trades or not.
            self.last_heartbeat = now
            trading = self._trading(now)
            for symbol in self.symbols:
                profile = self._profiles[symbol]
                trade_gap = profile["trade_gap"]
                traded = trading and random.random() < _trade_probability(self.interval_seconds, trade_gap)
                if traded:
                    self._trade(symbol, profile, trade_gap)
                elif not snapshot:
                    continue
                yield self.normalize(self._quote(symbol, profile))
            snapshot = False
            await asyncio.sleep(self.interval_seconds)

    def _trading(self, now: datetime) -> bool:
        """Whether symbols may trade now; rolls every symbol into a new
        session on the first open loop of a new trading day."""
        if self._calendar is None:
            return True
        if not self._calendar.is_open(now):
            return False
        day = self._calendar.local_date(now)
        if day != self._session_day:
            for symbol in self.symbols:
                self._new_session(symbol)
            self._session_day = day
        return True

    def _new_session(self, symbol: str) -> None:
        """The last session's VWAP becomes the previous close (carried
        over if it had no trades), and today starts with no trades."""
        profile = self._profiles[symbol]
        session = self._session[symbol]
        if session["volume"]:
            profile["previous_close"] = max(round(session["value"] / session["volume"], 2), TICK_SIZE)
        price = round(self._prices[symbol], 2)
        self._session[symbol] = {
            "open": profile["previous_close"],
            "day_high": price,
            "day_low": price,
            "volume": 0,
            "value": 0.0,
        }

    def _trade(self, symbol: str, profile: dict, trade_gap: float) -> None:
        # Same volatility as the backfilled history, per trade rather
        # than per tick, so live candles continue the history.
        sigma = profile["annual_vol"] * math.sqrt(trade_gap / _SECONDS_PER_YEAR)
        self._prices[symbol] *= math.exp(random.gauss(0, sigma))
        self._prices[symbol] = max(self._prices[symbol], TICK_SIZE)
        price = round(self._prices[symbol], 2)

        session = self._session[symbol]
        session["day_high"] = max(session["day_high"], price)
        session["day_low"] = min(session["day_low"], price)
        # Sized so a full day of trades adds up to roughly avg_volume, the
        # same scale the backfilled bars use.
        per_trade = profile["avg_volume"] * trade_gap / _SECONDS_PER_DAY
        size = max(int(per_trade * random.uniform(0.2, 1.8)), 1)
        session["volume"] += size
        session["value"] += price * size
        self._last_trade_at[symbol] = datetime.now(timezone.utc)

        # A new high/low extends the year and 52-week ranges rather than
        # making the tick fail validation.
        for key in ("year", "week52"):
            profile[f"{key}_high"] = max(profile[f"{key}_high"], price)
            profile[f"{key}_low"] = min(profile[f"{key}_low"], price)

    def _quote(self, symbol: str, profile: dict) -> dict:
        price = round(self._prices[symbol], 2)
        session = self._session[symbol]
        bid, ask = self._book(price, profile["book"], profile["spread"])
        # The official closing price is the session VWAP; with no trades
        # yet today it carries the previous one over (docs/data-formats.md,
        # quirk 3), so change is 0.
        if session["volume"]:
            vwap = max(round(session["value"] / session["volume"], 2), TICK_SIZE)
        else:
            vwap = profile["previous_close"]
        return {
            "symbol": symbol,
            "name": profile["name"],
            "exchange_label": EXCHANGE_LABEL,
            "price": price,
            "previous_close": profile["previous_close"],
            "open": round(session["open"], 2),
            "day_high": round(session["day_high"], 2),
            "day_low": round(session["day_low"], 2),
            "year_high": profile["year_high"],
            "year_low": profile["year_low"],
            "week52_high": profile["week52_high"],
            "week52_low": profile["week52_low"],
            "beta": profile["beta"],
            "pe_ratio": profile["pe_ratio"],
            "eps": profile["eps"],
            "bid": bid,
            "bid_size": random.randint(1, 40) * 100 if bid is not None else 0,
            "ask": ask,
            "ask_size": random.randint(1, 40) * 100 if ask is not None else 0,
            "vwap": vwap,
            "change": round(vwap - profile["previous_close"], 2),
            "volume": session["volume"],
            "value_traded": round(session["value"], 2),
            "avg_volume": profile["avg_volume"],
            "forward_dividend": profile["forward_dividend"],
            "forward_dividend_yield": profile["forward_dividend_yield"],
            "ex_dividend_date": profile["ex_dividend_date"],
            "earnings_date": profile["earnings_date"],
            "target_est": profile["target_est"],
            "dividend_announcement": profile["dividend_announcement"],
            "timestamp": datetime.now(timezone.utc),
            "last_trade_at": self._last_trade_at.get(symbol),
        }

    @staticmethod
    def _book(price: float, book: str, spread: float) -> tuple[Optional[float], Optional[float]]:
        """Best bid/ask around `price` for the symbol's usual book shape;
        a side with no resting order is None.

        A lone bid or offer can sit a few ticks on the "wrong" side of the
        last price, as on the GSE (AGA bid 40.70 vs close 37.00, CLYD
        offer 4.01 vs 4.02 on 28-Sep-2026): with no trades, the last price
        is stale. Two-sided books never cross."""
        bid = ask = None
        if book == "both":
            spread = max(spread, TICK_SIZE)
            bid = max(round(price - spread / 2, 2), TICK_SIZE)
            ask = round(bid + spread, 2)
        elif book == "bid":
            bid = max(round(price + TICK_SIZE * random.randint(-3, 2), 2), TICK_SIZE)
        elif book == "ask":
            ask = max(round(price + TICK_SIZE * random.randint(-2, 3), 2), TICK_SIZE)
        return bid, ask

    async def disconnect(self) -> None:
        self.running = False
        print("Disconnected from (mock) market data provider.")

    def normalize(self, raw_data: dict) -> MarketData:
        # The mock already produces canonical field names, so this is a
        # pass-through. A real connector's normalize() would map the
        # provider's actual field names (e.g. sym/px/qty/ts) here.
        return MarketData(**raw_data)
