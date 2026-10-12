"""
Pulls the Bank of Ghana's interbank FX rates from its website.

BoG has no API or download. Its Historical Interbank FX Rates page is a
wpDataTables table (id 40) that the browser fills by POSTing DataTables
parameters to WordPress's admin-ajax.php, which answers JSON: every
published row since 1996, each [date, currency, pair, buying, selling,
mid]. We ask it the same way, newest first, a page at a time, and stop
once we're past the date we need. The same table carries the latest
fix, so one request every so often keeps us current.

The request needs a nonce that the page embeds in a hidden input. It
isn't tied to a cookie, but WordPress rotates it, and an expired one
gets an empty 200, not an error, so a reply that isn't the table's JSON
re-reads the nonce from the page and tries once more.

Blocking (urllib), so call it from a thread; see rates.py.
"""

import json
import logging
import re
import urllib.request
from datetime import date
from typing import Callable, Optional
from urllib.parse import urlencode

from app.fx import Fix, parse_row

logger = logging.getLogger(__name__)

HISTORY_PAGE_URL = "https://www.bog.gov.gh/treasury-and-the-markets/historical-interbank-fx-rates/"
TABLE_ID = 40
TABLE_URL = f"https://www.bog.gov.gh/wp-admin/admin-ajax.php?action=get_wdtable&table_id={TABLE_ID}"
SOURCE = "Bank of Ghana interbank FX rates"

# Rows per request: 19 pairs a business day, so about 100 business
# days. A request this size takes BoG 1-2 seconds.
PAGE_SIZE = 2000
# A full backfill from 2015 is about 30 pages; this only stops a
# misbehaving reply from paging forever.
MAX_PAGES = 200
TIMEOUT_SECONDS = 30
USER_AGENT = "market-integration/1.0 (+FX rates for GSE price conversion)"

_NONCE = re.compile(rf'wdtNonceFrontendServerSide_{TABLE_ID}"[^>]*?value="([^"]+)"')
_COLUMNS = 6


class BogError(Exception):
    """BoG's site didn't give us the rates table."""


def _form(start: int, length: int, nonce: str) -> bytes:
    """The DataTables server-side request the page itself sends: rows
    `start` to `start + length`, sorted by date (column 0), newest first."""
    fields = [
        ("draw", "1"), ("start", str(start)), ("length", str(length)),
        ("order[0][column]", "0"), ("order[0][dir]", "desc"),
        ("search[value]", ""), ("search[regex]", "false"),
        ("wdtNonce", nonce),
    ]
    for i in range(_COLUMNS):
        fields += [
            (f"columns[{i}][data]", str(i)), (f"columns[{i}][searchable]", "true"),
            (f"columns[{i}][orderable]", "true"), (f"columns[{i}][search][value]", ""),
            (f"columns[{i}][search][regex]", "false"),
        ]
    return urlencode(fields).encode()


class BankOfGhanaClient:
    def __init__(self, timeout: float = TIMEOUT_SECONDS):
        self._timeout = timeout
        self._nonce: Optional[str] = None

    def _request(self, url: str, data: Optional[bytes] = None) -> bytes:
        headers = {"User-Agent": USER_AGENT}
        if data is not None:
            headers.update({"X-Requested-With": "XMLHttpRequest", "Referer": HISTORY_PAGE_URL})
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as res:
                return res.read()
        except OSError as e:  # URLError, HTTPError, timeouts
            raise BogError(f"{url}: {e}") from e

    def _read_nonce(self) -> str:
        page = self._request(HISTORY_PAGE_URL).decode("utf-8", errors="replace")
        m = _NONCE.search(page)
        if not m:
            raise BogError(f"no wdtNonce for table {TABLE_ID} on {HISTORY_PAGE_URL}; has the page changed?")
        return m.group(1)

    def _page(self, start: int) -> list[list]:
        """One page of rows, newest first. An empty or non-JSON reply is
        most likely an expired nonce: re-read it and try once more."""
        for attempt in (1, 2):
            if self._nonce is None or attempt == 2:
                self._nonce = self._read_nonce()
            body = self._request(TABLE_URL, _form(start, PAGE_SIZE, self._nonce))
            try:
                rows = json.loads(body)["data"]
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(rows, list):
                return rows
        raise BogError(f"table {TABLE_ID} didn't answer with rows (first bytes: {body[:80]!r})")

    def fetch_since(self, since: date, on_page: Callable[[list[Fix]], None]) -> int:
        """Every fix dated `since` or later, handed to `on_page` a page
        at a time as they arrive, newest first, so a failure part-way
        keeps what was fetched. Returns how many rows were read. Rows
        that don't parse are logged and skipped."""
        total = 0
        for n in range(MAX_PAGES):
            rows = self._page(n * PAGE_SIZE)
            fixes = []
            for row in rows:
                try:
                    fixes.append(parse_row(row))
                except (ValueError, TypeError) as e:
                    logger.warning("Skipping a Bank of Ghana FX row: %s", e)
            wanted = [f for f in fixes if f.date >= since]
            if wanted:
                on_page(wanted)
                total += len(wanted)
            if len(rows) < PAGE_SIZE or not fixes or min(f.date for f in fixes) < since:
                return total
        raise BogError(f"still before {since} after {MAX_PAGES} pages; stopping")
