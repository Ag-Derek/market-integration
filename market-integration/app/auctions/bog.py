"""
Pulls the Bank of Ghana's T-bill (and historical note/bond) auction
rates: the Treasury Bill Rates page's wpDataTables table (id 2; see
app/bog.py), [issue date, tender, security, discount rate, interest
rate], newest first.

Blocking (urllib), so call it from a thread; see tracker.py.
"""

import logging
from datetime import date
from typing import Callable, Optional

from app.auctions import AuctionResult, NoRate, parse_row
from app.bog import TIMEOUT_SECONDS, BogError, WpDataTable

logger = logging.getLogger(__name__)

PAGE_URL = "https://www.bog.gov.gh/treasury-and-the-markets/treasury-bill-rates/"
TABLE_ID = 2
SOURCE = "Bank of Ghana treasury bill rates"
# BoG's table says 2,017 rows in all but serves 1,392 of them
# (recordsFiltered, checked 16-Oct-2026: Aug 2013 on), which is what its
# page shows too. Three bills a week is about 150 a year, so 500 is
# three years a page.
PAGE_SIZE = 500
# The whole history (about 2,000 rows) is four or five pages.
MAX_PAGES = 50
_COLUMNS = 5


class BogAuctionClient(WpDataTable):
    def __init__(self, timeout: float = TIMEOUT_SECONDS):
        super().__init__(PAGE_URL, TABLE_ID, _COLUMNS, PAGE_SIZE, timeout)

    def fetch_since(self, since: Optional[date], on_page: Callable[[list[AuctionResult]], None]) -> int:
        """Every result issued `since` or later (everything if None),
        handed to `on_page` a page at a time, newest first. Returns how
        many were read; rows that don't parse are logged and skipped."""
        total = 0
        for n in range(MAX_PAGES):
            rows = self.page(n * self.page_size)
            results = []
            for row in rows:
                try:
                    results.append(parse_row(row))
                except NoRate as e:
                    logger.debug("Skipping a Bank of Ghana auction row: %s", e)
                except (ValueError, TypeError) as e:
                    logger.warning("Skipping a Bank of Ghana auction row: %s", e)
            wanted = [r for r in results if since is None or r.issue_date >= since]
            if wanted:
                on_page(wanted)
                total += len(wanted)
            if len(rows) < self.page_size or not results or (
                since is not None and min(r.issue_date for r in results) < since
            ):
                return total
        raise BogError(f"auction table still going after {MAX_PAGES} pages; stopping")
