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
from app.models.market_data import MarketData
from app.validation.market_validator import validate_candle, validate_tick

if TYPE_CHECKING:
    from app.connectors.base_connector import BaseMarketConnector

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("market_data.db")
# Open windows are re-written on every flush, so this bounds how stale
# the database can be; the /candles API also overlays in-memory state,
# so charts don't wait on it.
DEFAULT_FLUSH_INTERVAL_SECONDS = 60


@dataclass
class _WindowAggregate:
    window_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    tick_count: int = 1

    def update(self, price: float, volume_delta: int) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume_delta
        self.tick_count += 1

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
        )


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
        created_at,
    )


class MarketAggregator:
    def __init__(
        self,
        feed: AsyncIterator[MarketData],
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
                    created_at TEXT NOT NULL,
                    UNIQUE (symbol, interval, window_start)
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
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO market_candles
                        (symbol, interval, window_start, window_end,
                         open, high, low, close, volume, tick_count, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [_to_row(c, created_at) for c in candles],
                )
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
            "SELECT window_start, window_end, open, high, low, close, volume, tick_count "
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

    # ------------------------------------------------------------------
    # Live aggregation

    async def _consume(self) -> None:
        try:
            async for tick in self._feed:
                result = validate_tick(tick)
                if not result:
                    logger.warning(
                        "[aggregator] dropped tick for %s: %s",
                        tick.symbol, "; ".join(result.errors),
                    )
                    continue
                self._record(tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Market aggregator consume loop crashed")
            raise

    def _record(self, tick: MarketData) -> None:
        previous = self._last_volume.get(tick.symbol)
        if previous is None:
            delta = 0  # no baseline yet
        elif tick.volume >= previous:
            delta = tick.volume - previous
        else:
            delta = tick.volume  # cumulative counter reset (new session)
        self._last_volume[tick.symbol] = tick.volume

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
                    open=tick.price,
                    high=tick.price,
                    low=tick.price,
                    close=tick.price,
                    volume=delta,
                )
            else:
                window.update(tick.price, delta)

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
        if not closed and not still_open:
            return
        created_at = datetime.now(timezone.utc).isoformat()
        rows = [_to_row(c, created_at) for c in _valid_candles(closed + still_open)]
        await asyncio.to_thread(self._write_rows, rows)
        del self._pending[:len(closed)]
        logger.info("Flushed %d candle(s) to %s", len(rows), self._db_path)

    def _write_rows(self, rows: list[tuple]) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            # INSERT OR REPLACE + the UNIQUE(symbol, interval, window_start)
            # constraint makes a flush idempotent: re-flushing the same
            # window overwrites rather than duplicating.
            conn.executemany(
                """
                INSERT OR REPLACE INTO market_candles
                    (symbol, interval, window_start, window_end,
                     open, high, low, close, volume, tick_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()
