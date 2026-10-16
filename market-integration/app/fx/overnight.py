"""
Overnight interest rates for the currencies we price synthetic forwards
in (app/fx/forwards.py): the risk-free overnight benchmark each central
bank publishes every business day.

| Currency | Benchmark | Published by                      | Day count |
|----------|-----------|-----------------------------------|-----------|
| USD      | SOFR      | Federal Reserve Bank of New York  | ACT/360   |
| EUR      | €STR      | European Central Bank             | ACT/360   |
| GBP      | SONIA     | Bank of England                   | ACT/365   |

All three are free downloads with no key. A pull fetches the last month
or so of each (a few KB) and upserts it, so a rate the publisher revises
is corrected and a missed day is filled. Each currency is pulled on its
own, so one site being down doesn't hold up the others. refresh() never
raises: a failure is logged and kept for /health, what's stored stays in
use, and the caller tries again later (main.overnight_loop).

The other 13 currencies BoG fixes have no forward: we hold no rate for
them. See docs/fx-rates.md.
"""

import asyncio
import csv
import io
import json
import logging
import math
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
USER_AGENT = "market-integration/1.0 (+overnight rates for GHS synthetic forwards)"
RETRY_SECONDS = 600
# Each pull fetches this many recent days (calendar days for SONIA,
# observations for the others): enough to cover a long holiday or a
# few missed pulls, and to pick up revisions.
RECENT = 30


@dataclass(frozen=True)
class Benchmark:
    currency: str
    name: str           # "SOFR"
    publisher: str
    url: str            # where a person can read it
    basis: int          # day-count denominator: 360 or 365


BENCHMARKS: dict[str, Benchmark] = {
    "USD": Benchmark("USD", "SOFR", "Federal Reserve Bank of New York",
                     "https://www.newyorkfed.org/markets/reference-rates/sofr", 360),
    "EUR": Benchmark("EUR", "€STR", "European Central Bank",
                     "https://www.ecb.europa.eu/stats/financial_markets_and_interest_rates/"
                     "euro_short-term_rate/html/index.en.html", 360),
    "GBP": Benchmark("GBP", "SONIA", "Bank of England",
                     "https://www.bankofengland.co.uk/markets/sonia-benchmark", 365),
}

SOFR_URL = "https://markets.newyorkfed.org/api/rates/secured/sofr/last/{n}.json"
ESTR_URL = ("https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT"
            "?lastNObservations={n}&format=csvdata")
SONIA_URL = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
             "?csv.x=yes&Datefrom={since}&Dateto=now&SeriesCodes=IUDSOIA&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")


class OvernightError(Exception):
    """A publisher didn't give us its rates."""


@dataclass(frozen=True)
class OvernightRate:
    currency: str
    date: date      # the business day the rate is for
    rate: float     # % a year


def _rate(currency: str, day: date, value) -> OvernightRate:
    r = float(str(value).strip())
    # Negative is fine (€STR was, for years); beyond these it's a bad row.
    if not math.isfinite(r) or not -5 < r < 50:
        raise OvernightError(f"{currency} rate out of range on {day}: {value!r}")
    return OvernightRate(currency, day, r)


def _nonempty(currency: str, rates: list[OvernightRate]) -> list[OvernightRate]:
    if not rates:
        raise OvernightError(f"no {BENCHMARKS[currency].name} rates in the reply; has the format changed?")
    return sorted(rates, key=lambda r: r.date)


def parse_sofr(body: bytes) -> list[OvernightRate]:
    """The NY Fed's JSON: {"refRates": [{"effectiveDate": "2026-10-14",
    "type": "SOFR", "percentRate": 3.88, ...}, ...]}."""
    try:
        rows = json.loads(body)["refRates"]
        rates = [_rate("USD", date.fromisoformat(r["effectiveDate"]), r["percentRate"])
                 for r in rows if r.get("type") == "SOFR"]
    except (ValueError, KeyError, TypeError) as e:
        raise OvernightError(f"SOFR reply didn't parse: {e}") from e
    return _nonempty("USD", rates)


def parse_estr(body: bytes) -> list[OvernightRate]:
    """The ECB data API's CSV: one row per day, the day in TIME_PERIOD
    (2026-10-15) and the rate in OBS_VALUE (2.44)."""
    try:
        rows = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
        rates = [_rate("EUR", date.fromisoformat(r["TIME_PERIOD"]), r["OBS_VALUE"])
                 for r in rows if r.get("OBS_VALUE")]
    except (ValueError, KeyError, TypeError) as e:
        raise OvernightError(f"€STR reply didn't parse: {e}") from e
    return _nonempty("EUR", rates)


def parse_sonia(body: bytes) -> list[OvernightRate]:
    """The Bank of England database's CSV: "DATE,IUDSOIA" then rows like
    "14 Oct 2026,3.7324"."""
    try:
        rows = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
        rates = [_rate("GBP", datetime.strptime(r["DATE"].strip(), "%d %b %Y").date(), r["IUDSOIA"])
                 for r in rows if r.get("IUDSOIA")]
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise OvernightError(f"SONIA reply didn't parse: {e}") from e
    return _nonempty("GBP", rates)


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            return res.read()
    except OSError as e:  # URLError, HTTPError, timeouts
        raise OvernightError(f"{url}: {e}") from e


class CentralBankClient:
    """Fetches each benchmark from its publisher. Blocking (urllib), so
    call it from a thread."""

    def fetch(self, currency: str, today: date) -> list[OvernightRate]:
        if currency == "USD":
            return parse_sofr(_get(SOFR_URL.format(n=RECENT)))
        if currency == "EUR":
            return parse_estr(_get(ESTR_URL.format(n=RECENT)))
        if currency == "GBP":
            since = today - timedelta(days=RECENT)
            return parse_sonia(_get(SONIA_URL.format(since=since.strftime("%d/%b/%Y"))))
        raise ValueError(f"no overnight benchmark for {currency}")


class OvernightSource(Protocol):
    def fetch(self, currency: str, today: date) -> list[OvernightRate]: ...


class OvernightStoreLike(Protocol):
    """app.fx.store.OvernightStore (which imports this module, hence a
    protocol rather than an import)."""
    def upsert(self, rates: list[OvernightRate]) -> None: ...
    def all(self) -> list[OvernightRate]: ...


@dataclass
class _Pull:
    """One benchmark's last pull."""
    last_attempt_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None

    def describe(self) -> dict:
        return {
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error,
        }


class OvernightRates:
    def __init__(self, store: OvernightStoreLike, source: OvernightSource):
        self._store = store
        self._source = source
        self._pulls = {code: _Pull() for code in BENCHMARKS}
        # currency -> (dates, rates), both oldest first.
        self._index: dict[str, tuple[list[date], list[OvernightRate]]] = {}

    def reload(self) -> None:
        """Re-read the store. Blocking: SQLite."""
        by_code: dict[str, list[OvernightRate]] = {}
        for r in self._store.all():
            by_code.setdefault(r.currency, []).append(r)
        self._index = {code: ([r.date for r in rs], rs) for code, rs in by_code.items()}

    async def refresh(self, today: Optional[date] = None) -> bool:
        """Pull each benchmark; True if all succeeded."""
        today = today or datetime.now(timezone.utc).date()
        ok = True
        for code in BENCHMARKS:
            ok &= await self._pull(code, today, self._store.upsert)
        await asyncio.to_thread(self.reload)
        return ok

    async def _pull(self, code: str, today: date, save: Callable[[list[OvernightRate]], None]) -> bool:
        pull = self._pulls[code]
        pull.last_attempt_at = datetime.now(timezone.utc)
        try:
            rates = await asyncio.to_thread(self._source.fetch, code, today)
            await asyncio.to_thread(save, rates)
        except Exception as e:
            pull.last_error = f"{type(e).__name__}: {e}"
            logger.warning("Overnight rates: %s pull failed (still using what's stored): %s",
                           BENCHMARKS[code].name, pull.last_error)
            return False
        pull.last_success_at = datetime.now(timezone.utc)
        pull.last_error = None
        logger.info("Overnight rates: %s refreshed, %d rows, latest %s",
                    BENCHMARKS[code].name, len(rates), rates[-1].date)
        return True

    def latest(self, currency: str) -> Optional[OvernightRate]:
        entry = self._index.get(currency)
        return entry[1][-1] if entry else None

    def on(self, currency: str, day: date) -> Optional[OvernightRate]:
        """The latest rate for a day on or before `day`, or None."""
        entry = self._index.get(currency)
        if not entry:
            return None
        dates, rates = entry
        i = bisect_right(dates, day)
        return rates[i - 1] if i else None

    def describe(self) -> dict:
        """Where each benchmark stands, for /health and /fx/forwards."""
        out = {}
        for code, b in BENCHMARKS.items():
            latest = self.latest(code)
            out[b.name] = {
                "currency": code,
                "latest": latest.date.isoformat() if latest else None,
                **self._pulls[code].describe(),
            }
        return out
