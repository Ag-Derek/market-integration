"""
The commodity prices the service offers: what's stored, indexed by
commodity and month, and kept current by refresh().

refresh() pulls each source's whole release (the World Bank sends every
month back to 1960 in one workbook) into the store, then re-reads it.
Each source is pulled on its own, so one being down doesn't hold up the
others. It never raises: a failure is logged and kept for /health, the
prices already stored stay in use, and the caller tries again later
(main.commodity_loop).

A commodity's prices come from the first source in its `series` that
has any, and only if they're in the unit it expects: if a publisher
changed cocoa from $/kg to $/mt, showing the new numbers under the old
unit would be wrong by a factor of 1,000, so it is dropped (and named in
describe()) until COMMODITIES is updated.
"""

import asyncio
import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional, Protocol, Sequence

from app.commodities import COMMODITIES, Price, Release, month_key
from app.commodities.store import CommodityStore

logger = logging.getLogger(__name__)

# After a failed refresh, how soon to try again.
RETRY_SECONDS = 1800


class CommoditySource(Protocol):
    source: str

    def fetch(self) -> Release: ...


@dataclass
class _Pull:
    """One source's last pull."""
    release: Optional[date] = None
    last_attempt_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None

    def describe(self) -> dict:
        return {
            "release": self.release.isoformat() if self.release else None,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error,
        }


class CommodityPrices:
    def __init__(self, store: CommodityStore, sources: Sequence[CommoditySource]):
        self._store = store
        self._sources = list(sources)
        self._pulls = {s.source: _Pull() for s in self._sources}
        # code -> (months, prices), both oldest first.
        self._index: dict[str, tuple[list[date], list[Price]]] = {}
        # code -> why it isn't offered despite having prices.
        self._unit_mismatches: dict[str, str] = {}

    # ------------------------------------------------------------ loading

    def reload(self) -> None:
        """Re-read the store. Blocking: SQLite."""
        index, mismatches = {}, {}
        for code, c in COMMODITIES.items():
            for source, name in c.series.items():
                prices = self._store.series(source, [name])
                if not prices:
                    continue
                units = {p.unit for p in prices}
                if units != {c.unit}:
                    mismatches[code] = f"{source} publishes {name!r} in {', '.join(sorted(units))}, expected {c.unit}"
                    continue
                index[code] = ([p.month for p in prices], prices)
                mismatches.pop(code, None)
                break
        self._index, self._unit_mismatches = index, mismatches

    async def refresh(self) -> bool:
        """Pull every source into the store and re-read it. True if all
        succeeded."""
        ok = True
        for source in self._sources:
            ok &= await self._pull(source)
        await asyncio.to_thread(self.reload)
        return ok

    async def _pull(self, source: CommoditySource) -> bool:
        pull = self._pulls[source.source]
        pull.last_attempt_at = datetime.now(timezone.utc)
        try:
            release = await asyncio.to_thread(source.fetch)
            await asyncio.to_thread(self._store.upsert, release.prices)
        except Exception as e:
            pull.last_error = f"{type(e).__name__}: {e}"
            logger.warning("Commodity prices: %s pull failed (still using what's stored): %s",
                           source.source, pull.last_error)
            return False
        pull.release = release.updated_on
        pull.last_success_at = datetime.now(timezone.utc)
        pull.last_error = None
        latest = max(p.month for p in release.prices)
        logger.info("Commodity prices: %s refreshed, %d prices, latest month %s (release of %s)",
                    source.source, len(release.prices), month_key(latest), release.updated_on)
        return True

    # ------------------------------------------------------------ lookups

    def commodities(self) -> list[str]:
        """The commodities we have prices for, in COMMODITIES order."""
        return [code for code in COMMODITIES if code in self._index]

    def has(self, code: str) -> bool:
        return code in self._index

    def latest(self, code: str) -> Optional[Price]:
        entry = self._index.get(code)
        return entry[1][-1] if entry else None

    def on(self, code: str, month: date) -> Optional[Price]:
        """The latest price for `month` or before (a series can skip a
        month), or None before its history starts."""
        entry = self._index.get(code)
        if not entry:
            return None
        months, prices = entry
        i = bisect_right(months, month)
        return prices[i - 1] if i else None

    def history(self, code: str, since: Optional[date] = None) -> list[Price]:
        """Every price held for `code` from `since` (all if None), oldest
        first; empty if we have none."""
        entry = self._index.get(code)
        if not entry:
            return []
        months, prices = entry
        return prices[bisect_left(months, since):] if since else list(prices)

    @property
    def as_of(self) -> Optional[date]:
        """The newest month held, for any commodity."""
        months = [entry[0][-1] for entry in self._index.values()]
        return max(months) if months else None

    def describe(self) -> dict:
        """Where the prices stand, for /health and /commodities/prices."""
        return {
            "as_of": month_key(self.as_of) if self.as_of else None,
            "commodities": len(self._index),
            "sources": {name: pull.describe() for name, pull in self._pulls.items()},
            "unit_mismatches": dict(self._unit_mismatches),
        }
