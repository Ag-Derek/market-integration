"""
Pulls the World Bank's monthly commodity prices, the "Pink Sheet".

The World Bank publishes them as one Excel workbook, updated in the first
week of each month with the month before (and revisions to earlier ones).
Its address carries a document id that changes from year to year, so we
read the current link off the Commodity Markets page each time, then
download the workbook (about 600 KB) and read its "Monthly Prices" sheet:

    row  A          B                     C                  ...
    4    "Updated on October 02, 2026"
    5               "Crude oil, average"  "Crude oil, Brent" ...   <- series names
    6               "($/bbl)"             "($/bbl)"          ...   <- units
    7    "1960M01"  1.6                   1.6                ...
    ...
         "2026M09"  102.3                 116.8              ...

Every price is a monthly average in nominal US dollars. A month a series
wasn't priced is "…" or "...". Some names end in "**", a footnote that
the series replaced another in a price index; it is dropped. See
docs/commodity-prices.md.

An .xlsx is a zip of XML, so this reads it with the standard library
rather than adding a spreadsheet dependency for one sheet.

Blocking (urllib), so call it from a thread; see prices.py.
"""

import io
import logging
import math
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime

from app.commodities import Price, Release

logger = logging.getLogger(__name__)

SOURCE = "worldbank"
PAGE_URL = "https://www.worldbank.org/en/research/commodity-markets"
SHEET = "Monthly Prices"
TIMEOUT_SECONDS = 60
USER_AGENT = "market-integration/1.0 (+commodity reference prices for GSE/GFIM data)"

_LINK = re.compile(r'href="([^"]*CMO-Historical-Data-Monthly\.xlsx)"')
_UPDATED = re.compile(r"Updated on (\w+ \d{1,2}, \d{4})")
_MONTH = re.compile(r"(\d{4})M(\d{2})")
_MISSING = {"", "…", "..."}
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class WorldBankError(Exception):
    """The World Bank didn't give us its prices, or they didn't parse."""


# ---------------------------------------------------------------- the page

def monthly_url(page_html: str) -> str:
    """The monthly workbook's address, from the Commodity Markets page."""
    m = _LINK.search(page_html)
    if not m:
        raise WorldBankError(f"no CMO-Historical-Data-Monthly.xlsx link on {PAGE_URL}; has the page changed?")
    return m.group(1)


# ---------------------------------------------------------------- the workbook

def _column(ref: str) -> int:
    """"BR12" -> 69 (0-based)."""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        data = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    return ["".join(t.text or "" for t in si.iter(_NS + "t")) for si in ET.fromstring(data).findall(_NS + "si")]


def _sheet_path(z: zipfile.ZipFile, name: str) -> str:
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    targets = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    for sheet in workbook.iter(_NS + "sheet"):
        if sheet.get("name") == name:
            target = targets.get(sheet.get(_REL_ID), "")
            return target.lstrip("/") if target.startswith("/") else "xl/" + target
    raise WorldBankError(f"no '{name}' sheet in the workbook; has it changed?")


def _cell(c: ET.Element, strings: list[str]) -> str:
    kind = c.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in c.iter(_NS + "t")).strip()
    v = c.find(_NS + "v")
    if v is None or v.text is None:
        return ""
    return (strings[int(v.text)] if kind == "s" else v.text).strip()


def _rows(z: zipfile.ZipFile, path: str) -> list[list[str]]:
    """The sheet's rows as lists of cell text, "" for an empty cell."""
    strings = _shared_strings(z)
    out = []
    for row in ET.fromstring(z.read(path)).iter(_NS + "row"):
        cells = {}
        for i, c in enumerate(row.findall(_NS + "c")):
            ref = c.get("r")
            cells[_column(ref) if ref else i] = _cell(c, strings)
        out.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return out


def _is_units(row: list[str]) -> bool:
    filled = [v for v in row[1:] if v]
    return bool(filled) and all(v.startswith("(") and v.endswith(")") for v in filled)


def _series_name(text: str) -> str:
    """"Rubber, TSR20 **" -> "Rubber, TSR20"; "Urea " -> "Urea"."""
    return text.rstrip(" *").strip()


def parse_monthly(xlsx: bytes) -> Release:
    """Every price in the workbook's Monthly Prices sheet. Raises
    WorldBankError if the sheet isn't laid out as above. A cell that is
    neither a positive number nor a missing mark is logged and skipped."""
    try:
        z = zipfile.ZipFile(io.BytesIO(xlsx))
        rows = _rows(z, _sheet_path(z, SHEET))
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, IndexError) as e:
        raise WorldBankError(f"the workbook didn't read: {type(e).__name__}: {e}") from e

    first = next((i for i, r in enumerate(rows) if r and _MONTH.fullmatch(r[0])), None)
    units_at = next((i for i in range(first - 1, -1, -1) if _is_units(rows[i])), None) if first else None
    names_at = next((i for i in range(units_at - 1, -1, -1) if any(rows[i][1:])), None) if units_at else None
    if names_at is None:
        raise WorldBankError(f"'{SHEET}' has no series names, units and months; has the layout changed?")

    names, units = rows[names_at], rows[units_at]
    columns: dict[int, tuple[str, str]] = {}
    for j in range(1, len(names)):
        if not names[j]:
            continue
        name = _series_name(names[j])
        if any(name == n for n, _ in columns.values()):
            raise WorldBankError(f"'{name}' heads two columns; which is which?")
        unit = units[j].strip("() ") if j < len(units) else ""
        columns[j] = (name, unit)

    updated_on = None
    for row in rows[:names_at]:
        m = _UPDATED.search(" ".join(row))
        if m:
            try:
                updated_on = datetime.strptime(m.group(1), "%B %d, %Y").date()
            except ValueError:
                pass
            break

    prices, skipped = [], []
    for row in rows[first:]:
        m = _MONTH.fullmatch(row[0]) if row else None
        if not m or not 1 <= int(m.group(2)) <= 12:
            continue
        month = date(int(m.group(1)), int(m.group(2)), 1)
        for j, (name, unit) in columns.items():
            text = row[j] if j < len(row) else ""
            if text in _MISSING:
                continue
            try:
                value = float(text)
            except ValueError:
                value = math.nan
            if not math.isfinite(value) or value <= 0:
                skipped.append(f"{name} {month.isoformat()[:7]} = {text!r}")
                continue
            prices.append(Price(SOURCE, name, unit, month, value))
    if not prices:
        raise WorldBankError(f"'{SHEET}' has no prices; has the layout changed?")
    if skipped:
        # A few old cells hold spreadsheet errors (#VALUE!) or 0 for a
        # month not priced: one line, not one per cell, every pull.
        logger.warning("Skipped %d World Bank cells that aren't prices, e.g. %s", len(skipped), "; ".join(skipped[:3]))
    return Release(updated_on, prices)


# ---------------------------------------------------------------- the client

def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            return res.read()
    except OSError as e:  # URLError, HTTPError, timeouts
        raise WorldBankError(f"{url}: {e}") from e


class WorldBankClient:
    source = SOURCE

    def fetch(self) -> Release:
        """The whole current workbook: every series, every month."""
        url = monthly_url(_get(PAGE_URL).decode("utf-8", errors="replace"))
        return parse_monthly(_get(url))
