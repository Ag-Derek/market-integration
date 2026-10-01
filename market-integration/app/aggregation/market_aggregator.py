"""
Aggregates raw ticks into OHLCV candles on several intervals at once
(see app.models.candle.INTERVALS) and persists them to SQLite.

Subscribes to the MarketDataBuffer directly and independently of the
validation branch (see app.validation.market_validator) -- this is a
parallel consumer off the buffer, not something downstream of the
validator. That keeps a slow database write from ever delaying
real-time delivery to other consumers, and vice versa.

Windows are aligned to fixed buckets (bucket_start()), not to whenever a
flush happens, so live candles line up with backfilled ones. A window
is finalized when the first tick of the next bucket arrives; each flush
writes finalized windows plus a snapshot of the still-open ones (as
partial candles), and INSERT OR REPLACE keeps that idempotent.

Correctness note on volume: MockMarketConnector reports *cumulative*
session volume on every tick (it only ever increases), not a per-tick
trade size. So each tick contributes (its cumulative volume - the
previous tick's) to every window it lands in -- never the raw value,
which would wildly overcount. Doing the delta per tick rather than per
window also means volume traded between the last tick of one window and
the first tick of the next isn't lost.

History: backfill() pulls historical candles from the connector on
startup and replaces whatever is stored for that span, so charts have
data older than "since the service started".

Fixed income: a bill or bond tick is charted on its clean closing price
(the GFIM "so far" close), and each candle also carries the same window
in closing yield, so a chart can toggle between the two. A tick with no
price (an unquoted security) makes no candle. Repo (sell/buy-back)
ticks never touch candles: they are financing, not outright trades, so
they go to a repo_trades table of their own, one row per bond per
session.

Lost ticks: the aggregator's buffer subscription is lossless (see
app.queue.market_buffer), but if its queue still overflows the buffer
hands each dropped tick to mark_dropped(). That flags the window the
tick would have landed in, and the windows of the symbol's next tick
(which absorbs the lost tick's volume), as possibly_incomplete -- in
the database, /candles and the CSV export.
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Iterable, Optional

from app.aggregation.ranges import HISTORY_DEPTH
from app.models.candle import INTERVALS, Candle, bucket_end, bucket_start
from app.models.fixed_income import RepoTick
from app.models.market_data import MarketData
from app.models.tick import Tick
from app.validation.market_validator import validate_candle, validate_tick

if TYPE_CHECKING:
    from app.connectors.base_connector import BaseMarketConnector

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("market_data.db")
# Open windows are re-written on every flush, so this bounds how stale
# the database can be; the /candles API also overlays in-memory state,
# so charts don't wait on it.
DEFAULT_FLUSH_INTERVAL_SECONDS = 60


_CANDLE_COLUMNS = (
    "symbol", "interval", "window_start", "window_end",
    "open", "high", "low", "close", "volume", "tick_count",
    "yield_open", "yield_high", "yield_low", "yield_close",
    "possibly_incomplete", "created_at",
)
_INSERT_CANDLES = (
    f"INSERT OR REPLACE INTO market_candles ({', '.join(_CANDLE_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_CANDLE_COLUMNS))})"
)
# Added after the table first shipped; _init_db adds them to older databases.
_ADDED_COLUMNS = {
    "yield_open": "REAL",
    "yield_high": "REAL",
    "yield_low": "REAL",
    "yield_close": "REAL",
    "possibly_incomplete": "INTEGER NOT NULL DEFAULT 0",
}


@dataclass
class _WindowAggregate:
    window_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    tick_count: int = 1
    yield_open: Optional[float] = None
    yield_high: Optional[float] = None
    yield_low: Optional[float] = None
    yield_close: Optional[float] = None
    possibly_incomplete: bool = False

    def update(self, price: float, yield_: Optional[float], volume_delta: int) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume_delta
        self.tick_count += 1
        if yield_ is not None:
            if self.yield_close is None:
                self.yield_open = self.yield_high = self.yield_low = yield_
            else:
                self.yield_high = max(self.yield_high, yield_)
                self.yield_low = min(self.yield_low, yield_)
            self.yield_close = yield_

    def to_candle(self, symbol: str, interval: str) -> Candle:
        return Candle(
            symbol=symbol,
            interval=interval,
            window_start=self.window_start,
            window_end=bucket_end(self.window_start, interval),
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            tick_count=self.tick_count,
            yield_open=self.yield_open,
            yield_high=self.yield_high,
            yield_low=self.yield_low,
            yield_close=self.yield_close,
            possibly_incomplete=self.possibly_incomplete,
        )


def _mark(tick: Tick) -> tuple[Optional[float], Optional[float]]:
    """The (price, yield) a tick is charted at. Fixed income charts the
    clean closing price and closing yield: the GFIM close so far, which
    is what the report and the chart's history are made of."""
    if isinstance(tick, MarketData):
        return tick.price, None
    return tick.closing_price, tick.closing_yield


def _valid_candles(candles: list[Candle]) -> list[Candle]:
    """Drop (and log) candles that break OHLCV invariants, so a bad
    provider bar or an aggregation bug never reaches the database."""
    valid = []
    for c in candles:
        result = validate_candle(c)
        if result:
            valid.append(c)
        else:
            logger.warning(
                "[aggregator] dropped %s %s candle at %s: %s",
                c.symbol, c.interval, c.window_start.isoformat(), "; ".join(result.errors),
            )
    return valid


def _to_row(c: Candle, created_at: str) -> tuple:
    return (
        c.symbol,
        c.interval,
        c.window_start.isoformat(),
        c.window_end.isoformat(),
        c.open,
        c.high,
        c.low,
        c.close,
        c.volume,
        c.tick_count,
        c.yield_open,
        c.yield_high,
        c.yield_low,
        c.yield_close,
        int(c.possibly_incomplete),
        created_at,
    )


_REPO_COLUMNS = (
    "symbol", "session_date", "bond_yield", "bond_price", "repo_rate",
    "volume", "trade_count", "last_trade_at",
)


def _session_date(t: RepoTick) -> str:
    return t.timestamp.astimezone(timezone.utc).date().isoformat()


def _repo_row(t: RepoTick) -> tuple:
    return (
        t.symbol,
        _session_date(t),
        t.bond_yield,
        t.bond_price,
        t.repo_rate,
        t.volume,
        t.trade_count,
        t.last_trade_at.isoformat() if t.last_trade_at else None,
    )


class MarketAggregator:
    def __init__(
        self,
        feed: AsyncIterator[Tick],
        db_path: "Path | str" = DEFAULT_DB_PATH,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        intervals: Iterable[str] = tuple(INTERVALS),
    ):
        self._feed = feed
        self._db_path = Path(db_path)
        self._flush_interval_seconds = flush_interval_seconds
        self._intervals = tuple(intervals)
        # (symbol, interval) -> the window currently being built.
        self._windows: dict[tuple[str, str], _WindowAggregate] = {}
        # Windows that have closed but not been written yet.
        self._pending: list[Candle] = []
        # symbol -> cumulative volume on its last recorded tick.
        self._last_volume: dict[str, int] = {}
        # (symbol, session date) -> latest repo tick not yet written. Its
        # values are cumulative for the session, so the latest one wins.
        self._repos: dict[tuple[str, str], RepoTick] = {}
        # Symbols whose last tick before the next one recorded was lost:
        # that next tick's volume delta spans the gap.
        self._gap_symbols: set[str] = set()
        self._incomplete_candles = 0
        self._consume_task: Optional[asyncio.Task] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    tick_count INTEGER NOT NULL,
                    yield_open REAL,
                    yield_high REAL,
                    yield_low REAL,
                    yield_close REAL,
                    possibly_incomplete INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE (symbol, interval, window_start)
                )
                """
            )
            existing = {row[1] for row in conn.execute("PRAGMA table_info(market_candles)")}
            for column, decl in _ADDED_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE market_candles ADD COLUMN {column} {decl}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS repo_trades (
                    symbol TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    bond_yield REAL,
                    bond_price REAL,
                    repo_rate REAL,
                    volume INTEGER NOT NULL,
                    trade_count INTEGER NOT NULL,
                    last_trade_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE (symbol, session_date)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def intervals(self) -> tuple[str, ...]:
        return self._intervals

    @property
    def healthy(self) -> bool:
        """False until start() has run, or once either the consume or
        flush task has stopped or crashed."""
        tasks = (self._consume_task, self._flush_task)
        return all(task is not None and not task.done() for task in tasks)

    @property
    def incomplete_candles(self) -> int:
        """Live candles flagged possibly_incomplete since startup."""
        return self._incomplete_candles

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._consume_task = asyncio.create_task(self._consume())
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self, final_flush: bool = True) -> None:
        self._running = False
        for task in (self._consume_task, self._flush_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._consume_task = None
        self._flush_task = None
        if final_flush:
            await self._flush()

    # ------------------------------------------------------------------
    # History

    async def backfill(
        self, connector: "BaseMarketConnector", now: Optional[datetime] = None
    ) -> int:
        """Pull history for every symbol x interval from the connector
        and store it, replacing anything already stored from the start of
        the fetched span onward (the provider is the source of truth for
        the past; and for the mock, a previous run's candles belong to a
        different invented price path).

        If the newest fetched candle is the bucket that's still open, it
        also seeds the live window with it, so e.g. today's daily candle
        keeps the open/high/low from before startup instead of being
        overwritten by one built only from live ticks.

        Call before start(). Returns the number of candles stored.
        """
        now = now or datetime.now(timezone.utc)
        total = 0
        for symbol in connector.symbols:
            for interval in self._intervals:
                depth = HISTORY_DEPTH.get(interval)
                start = now - depth if depth is not None else None
                candles = _valid_candles(await connector.fetch_history(symbol, interval, start))
                if not candles:
                    continue
                await asyncio.to_thread(self._replace_rows, symbol, interval, candles)
                total += len(candles)

                last = candles[-1]
                if last.window_start == bucket_start(now, interval):
                    self._windows[(symbol, interval)] = _WindowAggregate(
                        window_start=last.window_start,
                        open=last.open,
                        high=last.high,
                        low=last.low,
                        close=last.close,
                        volume=last.volume,
                        tick_count=last.tick_count,
                        yield_open=last.yield_open,
                        yield_high=last.yield_high,
                        yield_low=last.yield_low,
                        yield_close=last.yield_close,
                    )
        logger.info("Backfilled %d historical candle(s) into %s", total, self._db_path)
        return total

    def _replace_rows(self, symbol: str, interval: str, candles: list[Candle]) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "DELETE FROM market_candles "
                    "WHERE symbol = ? AND interval = ? AND window_start >= ?",
                    (symbol, interval, candles[0].window_start.isoformat()),
                )
                conn.executemany(_INSERT_CANDLES, [_to_row(c, created_at) for c in candles])
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Reads

    async def get_candles(
        self,
        symbol: str,
        interval: str,
        start: Optional[datetime] = None,
    ) -> list[Candle]:
        """Candles for one symbol/interval, oldest first, from the bucket
        containing `start` (None = all). Stored rows are overlaid with
        closed-but-unflushed and still-open windows, so the newest candle
        is always current rather than as of the last flush."""
        first = bucket_start(start, interval) if start is not None else None
        rows = await asyncio.to_thread(self._read_rows, symbol, interval, first)

        by_start: dict[datetime, Candle] = {
            datetime.fromisoformat(r[0]): Candle(
                symbol=symbol,
                interval=interval,
                window_start=datetime.fromisoformat(r[0]),
                window_end=datetime.fromisoformat(r[1]),
                open=r[2],
                high=r[3],
                low=r[4],
                close=r[5],
                volume=r[6],
                tick_count=r[7],
                yield_open=r[8],
                yield_high=r[9],
                yield_low=r[10],
                yield_close=r[11],
                possibly_incomplete=bool(r[12]),
            )
            for r in rows
        }
        live = [c for c in self._pending if c.symbol == symbol and c.interval == interval]
        window = self._windows.get((symbol, interval))
        if window is not None:
            live.append(window.to_candle(symbol, interval))
        for c in live:
            if first is None or c.window_start >= first:
                by_start[c.window_start] = c

        return [by_start[k] for k in sorted(by_start)]

    def _read_rows(self, symbol: str, interval: str, first: Optional[datetime]) -> list[tuple]:
        query = (
            "SELECT window_start, window_end, open, high, low, close, volume, tick_count, "
            "yield_open, yield_high, yield_low, yield_close, possibly_incomplete "
            "FROM market_candles WHERE symbol = ? AND interval = ?"
        )
        params: list = [symbol, interval]
        if first is not None:
            query += " AND window_start >= ?"
            params.append(first.isoformat())
        query += " ORDER BY window_start"
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    async def get_repo_trades(self, symbol: str) -> list[dict]:
        """A bond's sell/buy-back (repo) activity, one row per session,
        oldest first, including sessions not yet flushed."""
        rows = await asyncio.to_thread(self._read_repo_rows, symbol)
        by_session = {r["session_date"]: r for r in rows}
        for (s, session), t in self._repos.items():
            if s == symbol:
                by_session[session] = dict(zip(_REPO_COLUMNS, _repo_row(t)))
        return [by_session[k] for k in sorted(by_session)]

    def _read_repo_rows(self, symbol: str) -> list[dict]:
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                f"SELECT {', '.join(_REPO_COLUMNS)} FROM repo_trades "
                "WHERE symbol = ? ORDER BY session_date",
                (symbol,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(zip(_REPO_COLUMNS, r)) for r in rows]

    # ------------------------------------------------------------------
    # Live aggregation

    async def _consume(self) -> None:
        try:
            async for tick in self._feed:
                result = validate_tick(tick)
                if not result:
                    logger.warning(
                        "[aggregator] dropped %s tick for %s: %s",
                        tick.tick_type, tick.symbol, "; ".join(result.errors),
                    )
                    continue
                if isinstance(tick, RepoTick):
                    self._record_repo(tick)
                else:
                    self._record(tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Market aggregator consume loop crashed")
            raise

    def mark_dropped(self, tick: Tick) -> None:
        """The buffer lost `tick` before this aggregator read it (its
        on_drop callback). Flag the window it would have landed in, and
        the windows of the symbol's next recorded tick, as possibly
        incomplete.

        The buffer drops the oldest unread tick, so everything before it
        has been recorded and nothing after it has: the windows open now
        are exactly the ones the tick would have met."""
        if isinstance(tick, RepoTick):
            return  # session totals: the next repo tick supersedes it
        if not validate_tick(tick):
            return  # would have been rejected anyway
        self._gap_symbols.add(tick.symbol)
        price, _ = _mark(tick)
        if price is None:
            return
        for interval in self._intervals:
            window = self._windows.get((tick.symbol, interval))
            # A later bucket would have opened a new window, which the
            # next tick flags if it lands there too.
            if window is not None and bucket_start(tick.timestamp, interval) <= window.window_start:
                self._flag_incomplete(window)

    def _flag_incomplete(self, window: _WindowAggregate) -> None:
        if not window.possibly_incomplete:
            window.possibly_incomplete = True
            self._incomplete_candles += 1

    def _record_repo(self, tick: RepoTick) -> None:
        self._repos[(tick.symbol, _session_date(tick))] = tick

    def _record(self, tick: Tick) -> None:
        previous = self._last_volume.get(tick.symbol)
        if previous is None:
            delta = 0  # no baseline yet
        elif tick.volume >= previous:
            delta = tick.volume - previous
        else:
            delta = tick.volume  # cumulative counter reset (new session)
        self._last_volume[tick.symbol] = tick.volume

        price, yield_ = _mark(tick)
        if price is None:
            return  # an unquoted bill or bond: nothing to chart
        after_gap = tick.symbol in self._gap_symbols
        self._gap_symbols.discard(tick.symbol)

        for interval in self._intervals:
            key = (tick.symbol, interval)
            start = bucket_start(tick.timestamp, interval)
            window = self._windows.get(key)

            # Roll over only on a *later* bucket; a late/out-of-order tick
            # just folds into the current window.
            if window is not None and start > window.window_start:
                self._pending.append(window.to_candle(tick.symbol, interval))
                window = None

            if window is None:
                self._windows[key] = _WindowAggregate(
                    window_start=start,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=delta,
                    yield_open=yield_,
                    yield_high=yield_,
                    yield_low=yield_,
                    yield_close=yield_,
                )
            else:
                window.update(price, yield_, delta)
            if after_gap:
                self._flag_incomplete(self._windows[key])

    async def _flush_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._flush_interval_seconds)
                await self._flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Market aggregator flush loop crashed")
            raise

    async def _flush(self) -> None:
        # Closed windows stay in _pending (and so visible to get_candles())
        # until their write has landed; only what was snapshotted here is
        # removed afterwards, since more can be appended during the write.
        closed = list(self._pending)
        still_open = [w.to_candle(s, i) for (s, i), w in self._windows.items()]
        repos = dict(self._repos)
        if not closed and not still_open and not repos:
            return
        created_at = datetime.now(timezone.utc).isoformat()
        rows = [_to_row(c, created_at) for c in _valid_candles(closed + still_open)]
        repo_rows = [_repo_row(t) + (created_at,) for t in repos.values()]
        await asyncio.to_thread(self._write_rows, rows, repo_rows)
        del self._pending[:len(closed)]
        # Only drop what was written; a newer tick for the same key that
        # arrived during the write stays for the next flush.
        for key, tick in repos.items():
            if self._repos.get(key) is tick:
                del self._repos[key]
        logger.info(
            "Flushed %d candle(s) and %d repo row(s) to %s", len(rows), len(repo_rows), self._db_path
        )

    def _write_rows(self, rows: list[tuple], repo_rows: list[tuple] = ()) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            # INSERT OR REPLACE + the UNIQUE(symbol, interval, window_start)
            # constraint makes a flush idempotent: re-flushing the same
            # window overwrites rather than duplicating. Likewise per
            # bond and session for repos.
            conn.executemany(_INSERT_CANDLES, rows)
            conn.executemany(
                f"INSERT OR REPLACE INTO repo_trades ({', '.join(_REPO_COLUMNS)}, updated_at) "
                f"VALUES ({', '.join('?' * (len(_REPO_COLUMNS) + 1))})",
                repo_rows,
            )
            conn.commit()
        finally:
            conn.close()
