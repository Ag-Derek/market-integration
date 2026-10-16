"""
Reading tables off the Bank of Ghana's website.

BoG has no API or download. Its data pages (interbank FX rates, T-bill
auction rates, ...) are wpDataTables tables that the browser fills by
POSTing DataTables parameters to WordPress's admin-ajax.php, which
answers JSON. WpDataTable asks the same way, a page of rows at a time,
sorted by the first column (a date), newest first.

The request needs a nonce that the page embeds in a hidden input. It
isn't tied to a cookie, but WordPress rotates it, and an expired one
gets an empty 200, not an error, so a reply that isn't the table's JSON
re-reads the nonce from the page and tries once more.

Blocking (urllib), so call it from a thread.
"""

import json
import re
import urllib.request
from typing import Optional
from urllib.parse import urlencode

TIMEOUT_SECONDS = 30
USER_AGENT = "market-integration/1.0 (+BoG FX and T-bill rates for GSE/GFIM data)"
AJAX_URL = "https://www.bog.gov.gh/wp-admin/admin-ajax.php?action=get_wdtable&table_id={table_id}"


class BogError(Exception):
    """BoG's site didn't give us the table."""


def request(url: str, data: Optional[bytes] = None, referer: Optional[str] = None,
            timeout: float = TIMEOUT_SECONDS) -> bytes:
    """GET `url` (POST `data` as the page's own AJAX call does, if given).
    Raises BogError for any network or HTTP failure."""
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers.update({"X-Requested-With": "XMLHttpRequest", "Referer": referer or url})
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read()
    except OSError as e:  # URLError, HTTPError, timeouts
        raise BogError(f"{url}: {e}") from e


class WpDataTable:
    def __init__(self, page_url: str, table_id: int, columns: int, page_size: int,
                 timeout: float = TIMEOUT_SECONDS):
        self.page_url = page_url
        self.table_id = table_id
        self.table_url = AJAX_URL.format(table_id=table_id)
        self.page_size = page_size
        self._columns = columns
        self._timeout = timeout
        self._nonce: Optional[str] = None
        self._nonce_re = re.compile(rf'wdtNonceFrontendServerSide_{table_id}"[^>]*?value="([^"]+)"')

    def _request(self, url: str, data: Optional[bytes] = None) -> bytes:
        return request(url, data, referer=self.page_url, timeout=self._timeout)

    def _read_nonce(self) -> str:
        page = self._request(self.page_url).decode("utf-8", errors="replace")
        m = self._nonce_re.search(page)
        if not m:
            raise BogError(f"no wdtNonce for table {self.table_id} on {self.page_url}; has the page changed?")
        return m.group(1)

    def _form(self, start: int, length: int, nonce: str) -> bytes:
        """The DataTables server-side request the page itself sends: rows
        `start` to `start + length`, sorted by date (column 0), newest first."""
        fields = [
            ("draw", "1"), ("start", str(start)), ("length", str(length)),
            ("order[0][column]", "0"), ("order[0][dir]", "desc"),
            ("search[value]", ""), ("search[regex]", "false"),
            ("wdtNonce", nonce),
        ]
        for i in range(self._columns):
            fields += [
                (f"columns[{i}][data]", str(i)), (f"columns[{i}][searchable]", "true"),
                (f"columns[{i}][orderable]", "true"), (f"columns[{i}][search][value]", ""),
                (f"columns[{i}][search][regex]", "false"),
            ]
        return urlencode(fields).encode()

    def page(self, start: int) -> list[list]:
        """One page of rows from `start`, newest first. An empty or non-JSON
        reply is most likely an expired nonce: re-read it and try once more."""
        for attempt in (1, 2):
            if self._nonce is None or attempt == 2:
                self._nonce = self._read_nonce()
            body = self._request(self.table_url, self._form(start, self.page_size, self._nonce))
            try:
                rows = json.loads(body)["data"]
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(rows, list):
                return rows
        raise BogError(f"table {self.table_id} didn't answer with rows (first bytes: {body[:80]!r})")
