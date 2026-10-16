"""
Pulls the Bank of Ghana's interbank FX rates from its website.

The Historical Interbank FX Rates page is a wpDataTables table (id 40;
see app/bog.py for how those are read): every published row since 1996,
each [date, currency, pair, buying, selling, mid]. We ask for it newest
first, a page at a time, and stop once we're past the date we need. The
same table carries the latest fix, so one request every so often keeps
us current.

Blocking (urllib), so call it from a thread; see rates.py.
"""

import logging
from datetime import date
from typing import Callable

from app.bog import AJAX_URL, TIMEOUT_SECONDS, BogError, WpDataTable  # noqa: F401  (BogError re-exported)
from app.fx import Fix, parse_row

logger = logging.getLogger(__name__)

HISTORY_PAGE_URL = "https://www.bog.gov.gh/treasury-and-the-markets/historical-interbank-fx-rates/"
TABLE_ID = 40
TABLE_URL = AJAX_URL.format(table_id=TABLE_ID)
SOURCE = "Bank of Ghana interbank FX rates"

# Rows per request: 19 pairs a business day, so about 100 business
# days. A request this size takes BoG 1-2 seconds.
PAGE_SIZE = 2000
# A full backfill from 2015 is about 30 pages; this only stops a
# misbehaving reply from paging forever.
MAX_PAGES = 200
_COLUMNS = 6


class BankOfGhanaClient(WpDataTable):
    def __init__(self, timeout: float = TIMEOUT_SECONDS):
        super().__init__(HISTORY_PAGE_URL, TABLE_ID, _COLUMNS, PAGE_SIZE, timeout)

    def fetch_since(self, since: date, on_page: Callable[[list[Fix]], None]) -> int:
        """Every fix dated `since` or later, handed to `on_page` a page
        at a time as they arrive, newest first, so a failure part-way
        keeps what was fetched. Returns how many rows were read. Rows
        that don't parse are logged and skipped."""
        total = 0
        for n in range(MAX_PAGES):
            rows = self.page(n * PAGE_SIZE)
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
