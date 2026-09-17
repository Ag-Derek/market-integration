"""
Aggregates raw ticks into OHLCV candles and persists them to SQLite.

Subscribes to the MarketDataBuffer directly and independently of the
validation branch (see app.validation.market_validator) -- this is a
parallel consumer off the buffer, not something downstream of the
validator. That keeps a slow database write from ever delaying
real-time delivery to other consumers, and vice versa.

Correctness note on volume: MockMarketConnector reports *cumulative*
session volume on every tick (it only ever increases), not a per-tick
trade size. So a window's volume is computed as
(last cumulative volume in window - first cumulative volume in window),
never as a sum of per-tick volumes -- summing would wildly overcount.
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from app.models.market_data import MarketData
from app.validation.market_validator import validate_tick

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("market_data.db")
DEFAULT_FLUSH_INTERVAL_SECONDS = 15 * 60  # 15 minutes
INTERVAL_LABEL = "15m"


@dataclass
class _WindowAggregate:
    window_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume_start: int
    volume_end: int
    tick_count: int = 1

    def update(self, tick: MarketData) -> None:
        self.high = max(self.high, tick.price)
        self.low = min(self.low, tick.price)
        self.close = tick.price
        self.volume_end = tick.volume
        self.tick_count += 1


class MarketAggregator:
    def __init__(
        self,
        feed: AsyncIterator[MarketData],
        db_path: "Path | str" = DEFAULT_DB_PATH,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
    ):
        self._feed = feed
        self._db_path = Path(db_path)
        self._flush_interval_seconds = flush_interval_seconds
        self._windows: dict[str, _WindowAggregate] = {}
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
        window = self._windows.get(tick.symbol)
        if window is None:
            self._windows[tick.symbol] = _WindowAggregate(
                window_start=tick.timestamp,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume_start=tick.volume,
                volume_end=tick.volume,
            )
        else:
            window.update(tick)

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
        if not self._windows:
            return
        windows, self._windows = self._windows, {}
        now = datetime.now(timezone.utc)
        rows = [
            (
                symbol,
                INTERVAL_LABEL,
                w.window_start.isoformat(),
                now.isoformat(),
                w.open,
                w.high,
                w.low,
                w.close,
                max(w.volume_end - w.volume_start, 0),  # cumulative -> delta
                w.tick_count,
                now.isoformat(),
            )
            for symbol, w in windows.items()
        ]
        await asyncio.to_thread(self._write_rows, rows)
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
