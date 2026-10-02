"""
Synthetic equities at a fixed, configurable tick rate, for load tests
and the throughput benchmark (bench/benchmark.py). Selected with
FEED_MODE=load in place of the GSE mock; see config.LOAD_*.

  * `instruments` synthetic symbols, LOAD0001, LOAD0002, ...;
  * the rate is `ticks_per_second` overall or, if that is None,
    `ticks_per_instrument` for each, spread evenly (round robin);
  * bursts: from `burst_after` seconds into the stream, the rate is
    multiplied by `burst_multiplier` for `burst_seconds`, once or, with
    `burst_every`, repeating (e.g. 10x normal for 30 s every 5 min).

Every tick is a full MarketData built through the model, as a real
connector's normalize() would, so Pydantic validation is part of the
measured cost. Ticks are internally consistent and timestamped when
built, so they pass the validator while the pipeline keeps up, and a
client can measure latency as receive time minus `timestamp`.

The stream emits whatever is due every `step_seconds`. If the event
loop falls so far behind that more than a second's worth is owed, the
excess is skipped and counted in `skipped` rather than emitted as one
huge catch-up batch.
"""

import asyncio
import random
import time
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Callable, Optional

from app.connectors.base_connector import BaseMarketConnector
from app.models.market_data import MarketData


class LoadConnector(BaseMarketConnector):

    def __init__(
        self,
        instruments: int = 100,
        ticks_per_second: Optional[float] = None,
        ticks_per_instrument: float = 1.0,
        burst_multiplier: float = 10.0,
        burst_seconds: float = 0.0,
        burst_after: float = 30.0,
        burst_every: float = 0.0,
        step_seconds: float = 0.01,
        clock: Callable[[], float] = time.monotonic,
    ):
        if instruments < 1:
            raise ValueError(f"need at least one instrument, got {instruments}")
        if burst_every and burst_every < burst_seconds:
            raise ValueError("burst_every must be at least burst_seconds")
        super().__init__([f"LOAD{n:04d}" for n in range(1, instruments + 1)])
        self.base_rate = ticks_per_second if ticks_per_second is not None else ticks_per_instrument * instruments
        if self.base_rate <= 0:
            raise ValueError(f"tick rate must be positive, got {self.base_rate}")
        self.burst_multiplier = burst_multiplier
        self.burst_seconds = burst_seconds
        self.burst_after = burst_after
        self.burst_every = burst_every
        self.step_seconds = step_seconds
        self._clock = clock
        self.emitted = 0
        self.skipped = 0
        self._state: dict[str, dict] = {}

    # ------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        now = datetime.now(timezone.utc)
        if not self._state:
            for n, symbol in enumerate(self.symbols):
                price = round(1 + (n % 50) * 0.75, 2)
                self._state[symbol] = {
                    "name": f"Load test instrument {n + 1}",
                    "previous_close": price,
                    "price": price,
                    "low": price,
                    "high": price,
                    "volume": 0,
                    "value": 0.0,
                }
        self.running = True
        self.last_heartbeat = now

    async def disconnect(self) -> None:
        self.running = False

    # ------------------------------------------------------------ rate

    def rate(self, elapsed: float) -> float:
        """Ticks per second `elapsed` seconds into the stream."""
        return self.base_rate * (self.burst_multiplier if self.in_burst(elapsed) else 1.0)

    def in_burst(self, elapsed: float) -> bool:
        if self.burst_seconds <= 0 or elapsed < self.burst_after:
            return False
        since = elapsed - self.burst_after
        if self.burst_every:
            since %= self.burst_every
        return since < self.burst_seconds

    # ------------------------------------------------------------ stream

    async def stream(self) -> AsyncIterator[MarketData]:
        if not self.running:
            raise RuntimeError("Connector is not connected. Call connect() first.")
        # A snapshot of every instrument first, as the real feeds send.
        for symbol in self.symbols:
            yield self._tick(symbol, trade=False)
        start = last = self._clock()
        owed = 0.0
        turn = 0
        while self.running:
            now = self._clock()
            # Integrate the rate over the step, so a burst edge mid-step
            # is only partly counted, at either rate.
            owed += self.rate(now - start) * (now - last)
            last = now
            cap = self.rate(now - start)  # at most a second's worth owed
            if owed > cap:
                self.skipped += int(owed - cap)
                owed = cap
            due = int(owed)
            owed -= due
            for _ in range(due):
                yield self._tick(self.symbols[turn % len(self.symbols)], trade=True)
                turn += 1
            self.emitted += due
            self.last_heartbeat = datetime.now(timezone.utc)
            await asyncio.sleep(self.step_seconds)

    def _tick(self, symbol: str, trade: bool) -> MarketData:
        s = self._state[symbol]
        now = datetime.now(timezone.utc)
        if trade:
            # A small random walk, kept well inside the year range.
            price = round(min(max(s["price"] + random.choice((-0.01, 0.0, 0.01)), s["previous_close"] * 0.9),
                              s["previous_close"] * 1.1), 2)
            size = random.randint(1, 500)
            s["price"] = price
            s["low"] = min(s["low"], price)
            s["high"] = max(s["high"], price)
            s["volume"] += size
            s["value"] += price * size
        price = s["price"]
        vwap = round(s["value"] / s["volume"], 4) if s["volume"] else s["previous_close"]
        vwap = min(max(vwap, s["low"]), s["high"])
        traded = s["volume"] > 0
        return MarketData(
            symbol=symbol,
            name=s["name"],
            exchange_label="Load test · GHS",
            price=price,
            previous_close=s["previous_close"],
            open=s["previous_close"],
            vwap=vwap,
            change=round(vwap - s["previous_close"], 4),
            day_high=s["high"],
            day_low=s["low"],
            year_high=round(s["previous_close"] * 2, 2),
            year_low=round(s["previous_close"] * 0.5, 2),
            week52_high=round(s["previous_close"] * 2, 2),
            week52_low=round(s["previous_close"] * 0.5, 2),
            beta=1.0,
            pe_ratio=10.0,
            eps=round(price / 10, 4),
            bid=round(price - 0.01, 2) if price > 0.01 else None,
            bid_size=100,
            ask=round(price + 0.01, 2),
            ask_size=100,
            volume=s["volume"],
            value_traded=round(s["value"], 2),
            avg_volume=10_000,
            forward_dividend=0.0,
            forward_dividend_yield=0.0,
            ex_dividend_date=(now.date() + timedelta(days=30)).isoformat(),
            earnings_date=(now.date() + timedelta(days=60)).isoformat(),
            target_est=round(price * 1.2, 2),
            timestamp=now,
            last_trade_at=now if traded else None,
        )
