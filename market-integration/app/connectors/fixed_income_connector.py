"""
Streams the mock GFIM market (fixed_income_mock.py) into the pipeline as
FixedIncomeTick and RepoTick, the way MockMarketConnector streams
equities.

What it sends:

  * on start, a snapshot: every security's quote;
  * then, while GFIM is in session, a tick for every security that
    traded, and a fresh two-way quote for each security each time its
    quote interval passes -- the curve drifts, so bids and offers move
    between trades -- plus a RepoTick for each bond with a new
    sell/buy-back.

Tick rates are per instrument: `quote_intervals` maps a symbol or a
segment to seconds between quotes (0 = only when it trades). A symbol
beats its segment, which beats DEFAULT_QUOTE_INTERVALS.

Burst mode quotes every security `rate` times a second whether or not
the market is in session, for load and throughput tests: with the
~170 securities of the sample, rate 100 is ~17,000 ticks a second
offered to the pipeline. Start it with burst() or, to run in burst for
the whole process, `burst_rate` at construction (FI_BURST_RATE).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Callable, Mapping, Optional

from app.connectors.base_connector import BaseMarketConnector
from app.connectors.fixed_income_mock import MockFixedIncomeMarket
from app.models.candle import Candle
from app.models.tick import Tick

# Seconds between two-way quotes per security, by segment. Corporates are
# quoted by price only and have no two-way quote in the mock, so they
# only tick when they trade.
DEFAULT_QUOTE_INTERVALS: dict[str, float] = {
    "treasury_bill": 5.0,
    "new_gog": 5.0,
    "ddep": 10.0,
    "old_gog": 30.0,
    "corporate": 0.0,
}


class MockFixedIncomeConnector(BaseMarketConnector):

    def __init__(
        self,
        market: Optional[MockFixedIncomeMarket] = None,
        interval_seconds: float = 0.5,
        quote_intervals: Optional[Mapping[str, float]] = None,
        burst_rate: float = 0.0,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        """`interval_seconds` is how often the stream wakes to check for
        trades and due quotes; `quote_intervals` overrides the per-segment
        defaults by segment or symbol; `burst_rate` > 0 runs in burst mode
        from the start, indefinitely."""
        super().__init__([])
        self.market = market or MockFixedIncomeMarket()
        self.interval_seconds = interval_seconds
        self._quote_intervals = {**DEFAULT_QUOTE_INTERVALS, **(quote_intervals or {})}
        self._burst_rate = burst_rate
        self._burst_until: Optional[datetime] = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._next_quote: dict[str, datetime] = {}

    # ------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        if not self.market.ready:
            self.market.start()
        self._sync_symbols()
        self.running = True
        self.last_heartbeat = self._clock()

    async def disconnect(self) -> None:
        self.running = False

    async def fetch_history(
        self, symbol: str, interval: str, start: Optional[datetime] = None
    ) -> list[Candle]:
        return self.market.history(symbol, interval, start)

    def _sync_symbols(self) -> None:
        """Add newly issued bills to `symbols`. Matured ones stay, so their
        charts can still be read."""
        known = set(self.symbols)
        self.symbols.extend(s for s in self.market.symbols() if s not in known)

    # ------------------------------------------------------------ rates

    def quote_interval(self, symbol: str) -> float:
        if symbol in self._quote_intervals:
            return self._quote_intervals[symbol]
        return self._quote_intervals.get(self.market.segment(symbol), 0.0)

    def burst(self, rate: float, seconds: Optional[float] = None) -> None:
        """Quote every security `rate` times a second, for `seconds` (None
        = until stop_burst())."""
        if rate <= 0:
            raise ValueError(f"burst rate must be positive, got {rate}")
        self._burst_rate = rate
        self._burst_until = None if seconds is None else self._clock() + timedelta(seconds=seconds)

    def stop_burst(self) -> None:
        self._burst_rate = 0.0
        self._burst_until = None

    def bursting(self, now: Optional[datetime] = None) -> bool:
        if self._burst_rate <= 0:
            return False
        if self._burst_until is not None and (now or self._clock()) >= self._burst_until:
            self.stop_burst()
            return False
        return True

    # ------------------------------------------------------------ stream

    async def stream(self) -> AsyncIterator[Tick]:
        if not self.running:
            raise RuntimeError("Connector is not connected. Call connect() first.")
        for tick in self.snapshot(self._clock()):
            yield tick
        while self.running:
            now = self._clock()
            for tick in self.poll(now):
                yield tick
            await asyncio.sleep(1 / self._burst_rate if self.bursting(now) else self.interval_seconds)

    def snapshot(self, now: datetime) -> list[Tick]:
        """Every security's quote, and the quote schedule started."""
        self.market.advance(now)
        self.market.drain_traded()
        self._sync_symbols()
        ticks = []
        for symbol in self.market.symbols():
            ticks.append(self.market.tick(symbol))
            self._schedule(symbol, now)
        return ticks

    def poll(self, now: datetime) -> list[Tick]:
        """One pass of the stream at `now`: a tick for each security that
        traded since the last pass or whose quote is due (all of them, in
        burst mode), and a RepoTick for each new sell/buy-back."""
        # The mock provider is "alive" on every pass, trades or not.
        self.last_heartbeat = now
        self.market.advance(now)
        traded, repos = self.market.drain_traded()
        self._sync_symbols()
        live = self.market.symbols()
        if self.bursting(now):
            due = set(live)
        elif self.market.is_open(now):
            due = {s for s in live if self._is_due(s, now)}
        else:
            due = set()
        ticks: list[Tick] = []
        for symbol in live:
            if symbol in due or symbol in traded:
                ticks.append(self.market.tick(symbol))
                self._schedule(symbol, now)
        ticks += [self.market.repo_tick(s) for s in live if s in repos]
        return ticks

    def _is_due(self, symbol: str, now: datetime) -> bool:
        if self.quote_interval(symbol) <= 0:
            return False
        return self._next_quote.get(symbol, now) <= now

    def _schedule(self, symbol: str, now: datetime) -> None:
        interval = self.quote_interval(symbol)
        if interval > 0:
            self._next_quote[symbol] = now + timedelta(seconds=interval)
