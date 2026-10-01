"""
Candle fidelity under load: does the aggregator see every tick, and do
its candles match ones built straight from the raw feed?

Benchmark target: ~16,500 ticks/s (TARGET_RATE) is an assumed stress-test
rate, derived from the fixed-income connector's documented burst rate of
100 quotes per bill and bond per second (~160 securities) plus 40
equities trading 10 times a second. It is not a production traffic
requirement: expected production traffic is far lower (the default mock
settings give well under 1% of it) and should be measured separately
against a real provider.

What runs, wired the way app.main wires it:

                     replayed feed
                          |
                          v
                    MarketDataBuffer
             /            |             \\
      "aggregator"     "alerts"*       "processor"
      lossless         lossless        drop-oldest
      queue 10,000     queue 10,000    queue 200
      (each lossless subscriber has its own queue)
            |             |               |
            v             v               v
      MarketAggregator  records what   validator -> processor
      -> SQLite         it reads       -> simulated gateway
                                          (JSON per client)

  * The alert engine doesn't exist yet; "alerts" is a stand-in lossless
    subscriber, optionally made to stall (stall_alerts_at/for).

The feed is a pool of ticks generated up front from the mocks (every
bill and bond quoted, every equity trading) and replayed on a wall-clock
schedule, one frame per poll, looping over the pool for as long as the
run lasts. Like a socket read, each read returns every frame due by
then, back to back without yielding to the event loop -- so frames that
came due while the loop was busy arrive as one burst. That is what makes
a queue overflow; the in-process mocks behind CompositeConnector instead
slow down to whatever the loop can take, so they never do. Each tick is
re-stamped with its delivery time, so freshness checks see a live feed,
and logged as a compact record (a full tick is ~3 KB; a run keeps only
the pool's ticks). Afterwards:

  * every branch's ticks add up: read + dropped + still queued = sent,
    and each read them in the order they were sent;
  * the aggregator dropped nothing, and (unless it was told to stall)
    neither did alerts;
  * every candle the aggregator stored equals, field for field (OHLC,
    volume, tick count, yield OHLC), one built from the tick log by
    reference_candles() -- an independent re-implementation of the
    aggregation rules, not a call into MarketAggregator -- and neither
    side has a candle the other lacks. A tick the aggregator rejected as
    stale because the pipeline fell behind counts as a mismatch.

Past what the event loop can process, the lossless queues fill and then
hold the feed back rather than drop, so overload shows up as lag behind
the schedule -- and once that exceeds the validator's 30 s freshness
limit, as rejected ticks and wrong candles -- not as drops.

    python -m benchmarks.candle_fidelity                       # target rate, 10 s
    python -m benchmarks.candle_fidelity --duration 60
    python -m benchmarks.candle_fidelity --sweep 16500,25000,50000 --duration 10
    python -m benchmarks.candle_fidelity --stall-alerts-at 2   # alerts stops reading
    python -m benchmarks.candle_fidelity --no-lossless         # the old policy, for contrast

Exit status 1 if any check fails. tests/test_candle_fidelity.py runs
short versions of it.
"""

import argparse
import asyncio
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional

from app.aggregation.market_aggregator import MarketAggregator
from app.connectors.fixed_income_connector import MockFixedIncomeConnector
from app.connectors.fixed_income_mock import MockFixedIncomeMarket
from app.connectors.market_connector import MockMarketConnector
from app.instruments import streamable_symbols
from app.models.candle import INTERVALS, bucket_start
from app.models.fixed_income import RepoTick
from app.models.market_data import MarketData
from app.models.tick import Tick
from app.processors.market_processor import MarketProcessor
from app.queue.market_buffer import MarketDataBuffer
from app.validation.market_validator import ValidatingStream, validate_tick

TARGET_RATE = 16_500.0
EQUITY_RATE = 10.0
# Ticks generated up front and replayed in a loop: ~3 KB each.
MAX_POOL_TICKS = 100_000

_FIELDS = (
    "open", "high", "low", "close", "volume", "tick_count",
    "yield_open", "yield_high", "yield_low", "yield_close",
)
CandleKey = tuple[str, str, str]  # symbol, interval, window_start (ISO)
Frame = tuple[float, list[Tick]]  # seconds after the start, ticks
# What the reference needs from a tick that never changes on replay:
# symbol, whether it counts towards candles (not repo, valid), charted
# price, yield, cumulative volume.
Static = tuple[str, bool, Optional[float], Optional[float], int]
Record = tuple[Static, datetime]  # ... and when it was delivered


@dataclass
class Branch:
    """What one subscriber got, as counted on its side of the buffer."""
    received: int = 0
    out_of_order: int = 0
    last_seq: int = -1
    dropped: int = 0
    depth_end: int = 0
    peak_depth: int = 0
    capacity: int = 0
    blocked_seconds: float = 0.0

    def adds_up(self, sent: int) -> bool:
        return self.received + self.dropped + self.depth_end == sent


@dataclass
class Result:
    rate: float
    generated: int
    seconds: float
    max_lag: float
    branches: dict[str, Branch]
    candles: int
    incomplete_candles: int
    alerts_stalled: bool = False
    # Wrong candles not flagged possibly_incomplete.
    unflagged_mismatches: int = 0
    mismatches: list[str] = field(default_factory=list)

    @property
    def achieved_rate(self) -> float:
        return self.generated / self.seconds if self.seconds else 0.0

    def problems(self) -> list[str]:
        out = []
        for name, b in self.branches.items():
            if not b.adds_up(self.generated):
                out.append(
                    f"{name}: read {b.received:,} + dropped {b.dropped:,} + queued "
                    f"{b.depth_end:,} != sent {self.generated:,}"
                )
            if b.out_of_order:
                out.append(f"{name}: {b.out_of_order:,} tick(s) read out of order")
        lossless = ["aggregator"] if self.alerts_stalled else ["aggregator", "alerts"]
        for name in lossless:
            b = self.branches[name]
            if b.dropped or b.received != self.generated:
                out.append(f"{name}: read {b.received:,} of {self.generated:,}, dropped {b.dropped:,}")
        if self.incomplete_candles:
            out.append(f"{self.incomplete_candles} candle(s) flagged possibly_incomplete")
        if self.mismatches:
            out.append(
                f"{len(self.mismatches)} candle mismatch(es), "
                f"{self.unflagged_mismatches} wrong but not flagged"
            )
        return out

    @property
    def ok(self) -> bool:
        return not self.problems()

    def report(self) -> str:
        lines = [
            f"rate:        {self.rate:,.0f} ticks/s target, {self.achieved_rate:,.0f}/s achieved",
            f"ticks:       {self.generated:,} in {self.seconds:.1f}s",
            f"max lag:     {self.max_lag * 1000:,.0f} ms behind schedule",
            f"candles:     {self.candles:,} checked, {self.incomplete_candles} flagged incomplete",
            "",
            f"{'branch':<11} {'read':>11} {'dropped':>9} {'peak/cap':>13} {'blocked':>9}",
        ]
        for name, b in self.branches.items():
            lines.append(
                f"{name:<11} {b.received:>11,} {b.dropped:>9,} "
                f"{f'{b.peak_depth:,}/{b.capacity:,}':>13} {b.blocked_seconds:>8.3f}s"
            )
        lines.append("")
        problems = self.problems()
        lines += [f"  {p}" for p in problems]
        lines += [f"  {m}" for m in self.mismatches[:10]]
        lines.append("PASS" if not problems else "FAIL")
        return "\n".join(lines)

    def row(self) -> str:
        agg, display = self.branches["aggregator"], self.branches["processor"]
        wrong = len(self.mismatches)
        return (
            f"| {self.rate:>9,.0f} | {self.achieved_rate:>9,.0f} | {self.seconds:>5.0f}s "
            f"| {self.max_lag * 1000:>9,.0f} | {agg.peak_depth:>6,}/{agg.capacity:,} "
            f"| {agg.blocked_seconds:>7.2f}s | {agg.dropped:>5,} | {wrong:>5,} "
            f"| {self.incomplete_candles:>7,} | {display.dropped / self.generated:>6.0%} |"
        )


TABLE_HEADER = (
    "|    target |  achieved |  time |   max lag |  agg peak/cap | agg wait "
    "| drops | wrong | flagged | display dropped |\n"
    "|----------:|----------:|------:|----------:|--------------:|---------:"
    "|------:|------:|--------:|----------------:|"
)


# ----------------------------------------------------------------------
# The feed


def _static(tick: Tick) -> Static:
    if isinstance(tick, MarketData):
        price, yield_ = tick.price, None
    elif isinstance(tick, RepoTick):
        price, yield_ = None, None
    else:
        price, yield_ = tick.closing_price, tick.closing_yield
    counts = not isinstance(tick, RepoTick) and bool(validate_tick(tick, now=tick.timestamp))
    return tick.symbol, counts, price, yield_, tick.volume


async def generate(seconds: float, rate: float) -> tuple[list[Frame], float]:
    """`seconds` of feed at about `rate` ticks a second, as timed frames:
    the bills and bonds' snapshot, then a burst-mode poll per frame, with
    every equity trading and quoting EQUITY_RATE times a second (folded
    into the frame it falls in). Returns the frames and the frame rate."""
    clock = [datetime.now(timezone.utc)]
    market = MockFixedIncomeMarket(clock=lambda: clock[0], history=False)
    fixed_income = MockFixedIncomeConnector(market, clock=lambda: clock[0])
    await fixed_income.connect()
    # Its stream runs on the wall clock, so drive a trade and a quote
    # per symbol directly; timestamps are re-stamped on replay anyway.
    equities = MockMarketConnector(symbols=streamable_symbols())
    await equities.connect()

    burst_rate = max(rate - len(equities.symbols) * EQUITY_RATE, 1.0) / len(market.symbols())
    fixed_income.burst(burst_rate)

    def equity_ticks() -> list[Tick]:
        out = []
        for symbol in equities.symbols:
            profile = equities._profiles[symbol]
            equities._trade(symbol, profile, profile["trade_gap"])
            out.append(equities.normalize(equities._quote(symbol, profile)))
        return out

    start = clock[0]
    frames: list[Frame] = [(0.0, fixed_income.snapshot(start) + equity_ticks())]
    step = 1 / burst_rate
    equity_every = max(round(burst_rate / EQUITY_RATE), 1)
    for i in range(1, max(int(seconds * burst_rate), 2)):
        offset = i * step
        clock[0] = start + timedelta(seconds=offset)
        ticks = fixed_income.poll(clock[0])
        if i % equity_every == 0:
            ticks += equity_ticks()
        frames.append((offset, ticks))
    await equities.disconnect()
    await fixed_income.disconnect()
    return frames, burst_rate


def _restamp(tick: Tick, ts: datetime) -> None:
    shift = ts - tick.timestamp
    tick.timestamp = ts
    if tick.last_trade_at is not None:
        tick.last_trade_at += shift


async def replay(
    pool: list[Frame],
    frame_rate: float,
    duration: float,
    log: list[Record],
    seq_of: dict[int, int],
    lag: list[float],
) -> AsyncIterator[Tick]:
    """Deliver `duration` seconds of frames from `pool`, looping over it,
    each at its time and stamped with it. Like a socket read, each read
    returns every frame due by then, back to back without yielding to
    the event loop -- so frames that came due while the loop was busy
    arrive as one burst. Logs each tick as it goes, with its sequence
    number in `seq_of` (by object: pool ticks are reused); lag[0] is the
    furthest behind schedule a frame was delivered, lag[1] how far
    behind the latest read was."""
    static = {id(t): _static(t) for _, ticks in pool for t in ticks}
    total = max(int(duration * frame_rate), 1)
    loop = asyncio.get_running_loop()
    start = loop.time()
    wall = datetime.now(timezone.utc)
    i = 0
    while i < total:
        now = loop.time() - start
        due = i / frame_rate
        if due > now:
            await asyncio.sleep(due - now)
            continue
        lag[1] = now - due
        lag[0] = max(lag[0], lag[1])
        while i < total and i / frame_rate <= now:
            ts = wall + timedelta(seconds=i / frame_rate)
            for tick in pool[i % len(pool)][1]:
                _restamp(tick, ts)
                seq_of[id(tick)] = len(log)
                log.append((static[id(tick)], ts))
                yield tick
            i += 1
        await asyncio.sleep(0)


async def _counted(feed: AsyncIterator[Tick], branch: Branch, seq_of: dict[int, int]) -> AsyncIterator[Tick]:
    """Pass `feed` through, counting what's read and checking its order.
    No await of its own, so a consumer still runs straight off the get."""
    async for tick in feed:
        seq = seq_of[id(tick)]
        if seq <= branch.last_seq:
            branch.out_of_order += 1
        branch.last_seq = seq
        branch.received += 1
        yield tick


# ----------------------------------------------------------------------
# Checks


def reference_candles(
    log: Iterable[Record], intervals: Iterable[str] = tuple(INTERVALS)
) -> dict[CandleKey, dict]:
    """Candles built straight from a raw tick log, by the rules in
    app/aggregation/market_aggregator.py: invalid and repo ticks are
    skipped; a tick's volume is its cumulative volume less the symbol's
    previous tick's (0 for the first, the raw value after a reset);
    unpriced ticks count towards volume only; equities chart price,
    bills and bonds the closing price, with closing-yield OHLC beside.

    Ticks are validated as of their own timestamp, so one the
    aggregator rejected as stale because it fell behind is a mismatch."""
    intervals = tuple(intervals)
    last_volume: dict[str, int] = {}
    out: dict[CandleKey, dict] = {}
    for (symbol, counts, price, yield_, volume), ts in log:
        if not counts:
            continue
        previous = last_volume.get(symbol)
        if previous is None:
            delta = 0
        elif volume >= previous:
            delta = volume - previous
        else:
            delta = volume
        last_volume[symbol] = volume
        if price is None:
            continue

        for interval in intervals:
            key = (symbol, interval, bucket_start(ts, interval).isoformat())
            c = out.get(key)
            if c is None:
                out[key] = {
                    "open": price, "high": price, "low": price, "close": price,
                    "volume": delta, "tick_count": 1,
                    "yield_open": yield_, "yield_high": yield_,
                    "yield_low": yield_, "yield_close": yield_,
                }
                continue
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price
            c["volume"] += delta
            c["tick_count"] += 1
            if yield_ is not None:
                if c["yield_close"] is None:
                    c["yield_open"] = c["yield_high"] = c["yield_low"] = yield_
                else:
                    c["yield_high"] = max(c["yield_high"], yield_)
                    c["yield_low"] = min(c["yield_low"], yield_)
                c["yield_close"] = yield_
    return out


def stored_candles(db_path: Path) -> dict[CandleKey, dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT symbol, interval, window_start, possibly_incomplete, {', '.join(_FIELDS)} "
            "FROM market_candles"
        ).fetchall()
    finally:
        conn.close()
    return {
        (r[0], r[1], r[2]): {"possibly_incomplete": bool(r[3]), **dict(zip(_FIELDS, r[4:]))}
        for r in rows
    }


def compare(
    stored: dict[CandleKey, dict], reference: dict[CandleKey, dict]
) -> tuple[list[str], int]:
    """Every difference between the two, and how many stored candles are
    wrong without being flagged possibly_incomplete -- which should
    never happen, drops or not."""
    mismatches = []
    unflagged = 0
    for key in sorted(reference.keys() - stored.keys()):
        mismatches.append(f"{key}: missing from the aggregator")
    for key in sorted(stored.keys() - reference.keys()):
        mismatches.append(f"{key}: not in the reference")
    for key in sorted(stored.keys() & reference.keys()):
        got, want = stored[key], reference[key]
        diffs = [f"{f} {got[f]!r} != {want[f]!r}" for f in _FIELDS if got[f] != want[f]]
        if diffs and not got["possibly_incomplete"]:
            unflagged += 1
        if got["possibly_incomplete"]:
            diffs.append("flagged possibly_incomplete")
        if diffs:
            mismatches.append(f"{key}: {'; '.join(diffs)}")
    return mismatches, unflagged


# ----------------------------------------------------------------------
# The run


async def run(
    duration: float = 10.0,
    rate: float = TARGET_RATE,
    lossless: bool = True,
    maxsize: int = 200,
    lossless_maxsize: int = 10_000,
    block_timeout: float = 0.5,
    flush_interval: float = 1.0,
    display_clients: int = 3,
    stall_alerts_at: Optional[float] = None,
    stall_alerts_for: Optional[float] = None,
    progress_every: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> Result:
    """Replay `duration` seconds of feed at `rate` ticks a second, drain
    the pipeline, and check every branch's counts and the aggregator's
    candles against the raw tick log.

    `display_clients` is how many WebSocket clients the simulated gateway
    serializes each tick for; a flush every `flush_interval` seconds puts
    the database write in the measured window. With `stall_alerts_at`,
    the alerts stand-in stops reading that many seconds in, for
    `stall_alerts_for` seconds (None = for good). With `progress_every`,
    prints the rate, lag and aggregator queue depth every that many
    seconds, to see whether a long run degrades."""
    db_path = db_path or Path(tempfile.mkdtemp(prefix="candle-fidelity-")) / "candles.db"
    # Big enough that a tick is never re-sent while a queue still holds
    # it from the last time round.
    pool_seconds = max(min(duration, MAX_POOL_TICKS / rate), 3 * lossless_maxsize / rate)
    pool, frame_rate = await generate(pool_seconds, rate)

    log: list[Record] = []
    seq_of: dict[int, int] = {}
    lag = [0.0, 0.0]
    buffer = MarketDataBuffer(
        replay(pool, frame_rate, duration, log, seq_of, lag),
        maxsize=maxsize,
        lossless_maxsize=lossless_maxsize,
        block_timeout=block_timeout,
    )
    branches = {name: Branch() for name in ("aggregator", "alerts", "processor")}

    aggregator = MarketAggregator(
        _counted(
            buffer.subscribe(
                "aggregator", lossless=lossless, on_drop=lambda t: aggregator.mark_dropped(t)
            ),
            branches["aggregator"], seq_of,
        ),
        db_path=db_path,
        flush_interval_seconds=flush_interval,
    )

    # Stand-in for the alert engine.
    alerts_feed = _counted(buffer.subscribe("alerts", lossless=lossless), branches["alerts"], seq_of)
    stall_forever = stall_alerts_at is not None and stall_alerts_for is None
    loop = asyncio.get_running_loop()

    async def alerts() -> None:
        stalled = False
        async for _ in alerts_feed:
            if stall_alerts_at is not None and not stalled and loop.time() - started >= stall_alerts_at:
                stalled = True
                await (asyncio.Event().wait() if stall_forever else asyncio.sleep(stall_alerts_for))

    # Display branch, as app.main wires it, with the gateway's per-client
    # JSON serialization standing in for real WebSocket sends.
    processor = MarketProcessor()
    display_feed = ValidatingStream(
        _counted(buffer.subscribe("processor"), branches["processor"], seq_of), name="processor"
    )

    async def display(tick: Tick) -> None:
        for _ in range(display_clients):
            tick.model_dump_json()

    async def progress() -> None:
        sent = 0
        while True:
            await asyncio.sleep(progress_every)
            depth = next(s["depth"] for s in buffer.stats() if s["subscriber"] == "aggregator")
            print(
                f"  {loop.time() - started:6.0f}s  {(len(log) - sent) / progress_every:>9,.0f} ticks/s"
                f"  lag {lag[1] * 1000:>7,.0f} ms  aggregator queue {depth:>6,}",
                flush=True,
            )
            sent = len(log)

    await aggregator.start()
    started = loop.time()
    tasks = [
        asyncio.create_task(processor.consume(display_feed, on_processed=display)),
        asyncio.create_task(alerts()),
    ]
    if progress_every:
        tasks.append(asyncio.create_task(progress()))
    await buffer.start()
    drained = ("aggregator",) if stall_forever else ("aggregator", "alerts")
    try:
        # The pump ends with the replay; then let the branches drain.
        while buffer.healthy:
            await asyncio.sleep(0.01)
        seconds = loop.time() - started
        while any(s["depth"] for s in buffer.stats() if s["subscriber"] in drained):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)
        for s in buffer.stats():
            b = branches[s["subscriber"]]
            b.dropped, b.depth_end = s["dropped"], s["depth"]
            b.peak_depth, b.capacity = s["peak_depth"], s["capacity"]
            b.blocked_seconds = s["blocked_seconds"]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await aggregator.stop()  # final flush: every window, closed or open
        await buffer.stop()

    stored = stored_candles(db_path)
    mismatches, unflagged = compare(stored, reference_candles(log, aggregator.intervals))
    return Result(
        rate=rate,
        generated=len(log),
        seconds=seconds,
        max_lag=lag[0],
        branches=branches,
        candles=len(stored),
        incomplete_candles=aggregator.incomplete_candles,
        alerts_stalled=stall_alerts_at is not None,
        unflagged_mismatches=unflagged,
        mismatches=mismatches,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--duration", type=float, default=10.0, help="seconds of feed per run (default 10)")
    parser.add_argument(
        "--rate", type=float, default=TARGET_RATE,
        help=f"ticks a second (default {TARGET_RATE:,.0f}, the assumed benchmark target)",
    )
    parser.add_argument(
        "--sweep", type=lambda s: [float(x) for x in s.split(",")],
        help="comma-separated rates to run one after another, reported as a table",
    )
    parser.add_argument(
        "--no-lossless", dest="lossless", action="store_false",
        help="subscribe the aggregator and alerts with plain drop-oldest, as before",
    )
    parser.add_argument("--stall-alerts-at", type=float, help="seconds in when alerts stops reading")
    parser.add_argument("--stall-alerts-for", type=float, help="for how long (default: for good)")
    parser.add_argument("--progress", type=float, help="print rate, lag and queue depth every N seconds")
    parser.add_argument("--maxsize", type=int, default=200)
    parser.add_argument("--lossless-maxsize", type=int, default=10_000)
    parser.add_argument("--block-timeout", type=float, default=0.5)
    parser.add_argument("--display-clients", type=int, default=3)
    args = parser.parse_args()

    ok = True
    for rate in args.sweep or [args.rate]:
        result = asyncio.run(run(
            duration=args.duration,
            rate=rate,
            lossless=args.lossless,
            maxsize=args.maxsize,
            lossless_maxsize=args.lossless_maxsize,
            block_timeout=args.block_timeout,
            display_clients=args.display_clients,
            stall_alerts_at=args.stall_alerts_at,
            stall_alerts_for=args.stall_alerts_for,
            progress_every=args.progress,
        ))
        print(result.report(), flush=True)
        if args.sweep:
            print(TABLE_HEADER + "\n" + result.row() + "\n", flush=True)
        ok = ok and result.ok
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
