"""
The FX rates the service converts at: Bank of Ghana fixes from the
store, indexed for lookup by currency and day, and kept current by
refresh().

refresh() pulls from BoG (bog.py) into the store, then re-reads it. The
first time, and whenever the stored history doesn't reach back
HISTORY_YEARS, it fetches all of it (about 30 requests, a minute or
so); after that only what's new since the latest stored day. It never
raises: a failure is logged and kept for /health, the rates already
stored stay in use, and the caller tries again later (main.fx_loop).
"""

import asyncio
import logging
import threading
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

from app.fx import BASE, CURRENCIES, Fix, normalize
from app.fx.store import FxStore
from app.models.fx import FxRate

logger = logging.getLogger(__name__)

# How far back to hold fixes: the mock's daily history is 10 years
# (market_connector.DAILY_HISTORY_YEARS), and a weekly candle's fix is
# the one for its last day, so a year more covers every chart.
HISTORY_YEARS = 11
# History reaching to within this of the start counts as complete (the
# start may fall on a weekend or holiday, when BoG publishes nothing).
BACKFILL_SLACK = timedelta(days=7)
# After a failed refresh, how soon to try again.
RETRY_SECONDS = 300


class FxSource(Protocol):
    def fetch_since(self, since: date, on_page: Callable[[list[Fix]], None]) -> int: ...


class _Stopped(Exception):
    pass


class FxRates:
    def __init__(self, store: FxStore, source: FxSource, history_years: int = HISTORY_YEARS):
        self._store = store
        self._source = source
        self._history_years = history_years
        # currency -> (dates, rates), both oldest first.
        self._index: dict[str, tuple[list[date], list[FxRate]]] = {}
        self._closing = threading.Event()
        self.last_attempt_at: Optional[datetime] = None
        self.last_success_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------ loading

    def reload(self) -> None:
        """Re-read the store. Blocking: SQLite."""
        by_code: dict[str, list[FxRate]] = {}
        for fix in self._store.all():
            rate = normalize(fix)
            if rate is not None:
                by_code.setdefault(rate.currency, []).append(rate)
        self._index = {code: ([r.date for r in rates], rates) for code, rates in by_code.items()}

    def history_start(self, today: date) -> date:
        return today - timedelta(days=round(365.25 * self._history_years))

    async def refresh(self, today: Optional[date] = None) -> bool:
        """Pull what's missing from BoG into the store and re-read it.
        True if the pull succeeded. Each page is stored as it arrives, so
        a pull that fails part-way still keeps what it got."""
        now = datetime.now(timezone.utc)
        today = today or now.date()
        self.last_attempt_at = now
        start = self.history_start(today)
        span = await asyncio.to_thread(self._store.span)
        since = start if span is None or span[0] > start + BACKFILL_SLACK else span[1]

        def on_page(fixes: list[Fix]) -> None:
            if self._closing.is_set():
                raise _Stopped()
            self._store.upsert(fixes)
            # Pages come newest first: with nothing held yet (a first
            # start), the first one makes the latest fixes usable at once
            # rather than after the whole backfill.
            if not self._index:
                self.reload()

        try:
            n = await asyncio.to_thread(self._source.fetch_since, since, on_page)
        except _Stopped:
            return False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning("Bank of Ghana FX refresh failed (still using stored rates): %s", self.last_error)
            await asyncio.to_thread(self.reload)
            return False
        await asyncio.to_thread(self.reload)
        self.last_success_at = datetime.now(timezone.utc)
        self.last_error = None
        logger.info("Bank of Ghana FX rates refreshed from %s: %d rows, latest fix %s", since, n, self.as_of)
        return True

    def close(self) -> None:
        """Stop a pull in progress at its next page."""
        self._closing.set()

    # ------------------------------------------------------------ lookups

    def currencies(self) -> list[str]:
        """The currencies we have rates for, in CURRENCIES order."""
        return [code for code in CURRENCIES if code in self._index]

    def has(self, code: str) -> bool:
        return code == BASE or code in self._index

    def latest(self, code: str) -> Optional[FxRate]:
        entry = self._index.get(code)
        return entry[1][-1] if entry else None

    def on(self, code: str, day: date) -> Optional[FxRate]:
        """The fix in force on `day`: the latest published on or before
        it (BoG publishes on business days only). Before the history
        starts, the earliest there is."""
        entry = self._index.get(code)
        if not entry:
            return None
        dates, rates = entry
        return rates[max(bisect_right(dates, day) - 1, 0)]

    def per_ghs(self, code: str, day: Optional[date] = None) -> Optional[float]:
        """Units of `code` one cedi buys at the mid: on `day`, or at the
        latest fix. 1 for cedis; None if we have no rate for it."""
        if code == BASE:
            return 1.0
        rate = self.latest(code) if day is None else self.on(code, day)
        return None if rate is None else rate.per_ghs

    @property
    def as_of(self) -> Optional[date]:
        """The newest fix held, for any currency."""
        dates = [entry[0][-1] for entry in self._index.values()]
        return max(dates) if dates else None

    def describe(self) -> dict:
        """Where the rates stand, for /health and /fx/rates."""
        return {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "currencies": len(self._index),
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error,
        }
