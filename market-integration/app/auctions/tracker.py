"""
The auction results the service shows: everything stored, indexed by
security, and kept current by refresh().

refresh() pulls from BoG (bog.py) into the store, then re-reads it. The
first time it fetches the whole table (four or five requests); after
that it re-fetches the last REFETCH_DAYS, so a row BoG corrects after
publishing is picked up too. It never raises: a failure is logged and
kept for /health, the results already stored stay in use, and the
caller tries again later (main.auction_loop).
"""

import asyncio
import logging
import threading
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

from app.auctions import AuctionResult
from app.auctions.store import AuctionStore

logger = logging.getLogger(__name__)

REFETCH_DAYS = 28
RETRY_SECONDS = 300


class AuctionSource(Protocol):
    def fetch_since(self, since: Optional[date], on_page: Callable[[list[AuctionResult]], None]) -> int: ...


class _Stopped(Exception):
    pass


class AuctionTracker:
    def __init__(self, store: AuctionStore, source: AuctionSource):
        self._store = store
        self._source = source
        # security -> (issue dates, results), both oldest first.
        self._index: dict[str, tuple[list[date], list[AuctionResult]]] = {}
        self._closing = threading.Event()
        self.last_attempt_at: Optional[datetime] = None
        self.last_success_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    def reload(self) -> None:
        """Re-read the store. Blocking: SQLite."""
        by_security: dict[str, list[AuctionResult]] = {}
        for r in self._store.all():
            by_security.setdefault(r.security, []).append(r)
        self._index = {s: ([r.issue_date for r in rs], rs) for s, rs in by_security.items()}

    async def refresh(self) -> bool:
        """Pull what's missing from BoG into the store and re-read it.
        True if the pull succeeded."""
        self.last_attempt_at = datetime.now(timezone.utc)
        latest = await asyncio.to_thread(self._store.latest_date)
        since = None if latest is None else latest - timedelta(days=REFETCH_DAYS)

        def on_page(results: list[AuctionResult]) -> None:
            if self._closing.is_set():
                raise _Stopped()
            self._store.upsert(results)

        try:
            n = await asyncio.to_thread(self._source.fetch_since, since, on_page)
        except _Stopped:
            return False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning("Bank of Ghana auction refresh failed (still using stored results): %s", self.last_error)
            await asyncio.to_thread(self.reload)
            return False
        await asyncio.to_thread(self.reload)
        self.last_success_at = datetime.now(timezone.utc)
        self.last_error = None
        logger.info("Bank of Ghana auction results refreshed from %s: %d rows, latest issue %s",
                    since or "the start", n, self.as_of)
        return True

    def close(self) -> None:
        """Stop a pull in progress at its next page."""
        self._closing.set()

    # ------------------------------------------------------------ lookups

    def securities(self) -> list[str]:
        """Every security auctioned, bills first by tenor, then the rest
        by their latest auction, newest first."""
        def key(s: str):
            r = self._index[s][1][-1]
            return (r.kind != "bill", r.tenor_days or 0, -r.issue_date.toordinal())
        return sorted(self._index, key=key)

    def history(self, security: str, since: Optional[date] = None) -> list[AuctionResult]:
        """`security`'s results issued on or after `since` (all if None),
        oldest first; empty for a security never auctioned."""
        entry = self._index.get(security)
        if not entry:
            return []
        dates, results = entry
        return results[bisect_left(dates, since):] if since else list(results)

    def results(self, since: Optional[date] = None) -> list[AuctionResult]:
        """Every result issued on or after `since`, newest first."""
        out = [r for s in self._index for r in self.history(s, since)]
        return sorted(out, key=lambda r: (r.issue_date, r.security), reverse=True)

    def latest(self, security: str) -> Optional[AuctionResult]:
        entry = self._index.get(security)
        return entry[1][-1] if entry else None

    def previous(self, result: AuctionResult) -> Optional[AuctionResult]:
        """The same security's auction before `result`, or None."""
        entry = self._index.get(result.security)
        if not entry:
            return None
        i = bisect_left(entry[0], result.issue_date)
        return entry[1][i - 1] if i > 0 else None

    @property
    def as_of(self) -> Optional[date]:
        """The newest issue date held."""
        dates = [entry[0][-1] for entry in self._index.values()]
        return max(dates) if dates else None

    def describe(self) -> dict:
        """Where the results stand, for /health and /auctions."""
        return {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "results": sum(len(e[1]) for e in self._index.values()),
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error,
        }
