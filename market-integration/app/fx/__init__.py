"""
Currency conversion at the Bank of Ghana's interbank FX rates.

Every price in the service is in cedis. To show one in another currency
we use the mid-rate the Bank of Ghana (BoG) publishes each business day
for that currency against the cedi. bog.py pulls the rates from BoG's
website, store.py keeps them in SQLite exactly as published, and
rates.py turns them into lookups: the latest fix, or the fix for a given
day.

BoG publishes 19 rows a day. Most are cedis per unit of the other
currency ("USDGHS"); its West African neighbours are quoted the other
way round ("GHSNGN" is naira per cedi). `normalize()` turns both into
cedis per unit. Three rows aren't offered for conversion: the leone,
which BoG moved to the redenominated scale on 11 Sep 2026 while keeping
its old code, so its history jumps 1000x; the ouguiya, still quoted in
the pre-2018 unit, which no longer exists; and the ECOWAS unit of
account, which isn't a currency. They are still stored. See
docs/fx-rates.md.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from app.models.fx import FxRate

BASE = "GHS"

# ISO 4217 code -> (name, symbol for display, quoted as GHS<code>, i.e.
# units of the currency per cedi, rather than <code>GHS).
CURRENCIES: dict[str, tuple[str, str, bool]] = {
    "USD": ("US dollar", "US$", False),
    "EUR": ("Euro", "€", False),
    "GBP": ("Pound sterling", "£", False),
    "CHF": ("Swiss franc", "CHF ", False),
    "JPY": ("Japanese yen", "¥", False),
    "CNY": ("Chinese yuan", "CN¥", False),
    "CAD": ("Canadian dollar", "C$", False),
    "AUD": ("Australian dollar", "A$", False),
    "NZD": ("New Zealand dollar", "NZ$", False),
    "ZAR": ("South African rand", "R", False),
    "DKK": ("Danish krone", "DKK ", False),
    "NOK": ("Norwegian krone", "NOK ", False),
    "SEK": ("Swedish krona", "SEK ", False),
    "NGN": ("Nigerian naira", "₦", True),
    "XOF": ("West African CFA franc", "CFA ", True),
    "GMD": ("Gambian dalasi", "D", True),
}
BASE_NAME, BASE_SYMBOL = "Ghanaian cedi", "GH₵"

# BoG's pair code -> our currency code.
PAIRS: dict[str, str] = {
    (BASE + code if inverted else code + BASE): code
    for code, (_, _, inverted) in CURRENCIES.items()
}


@dataclass(frozen=True)
class Fix:
    """One row of BoG's interbank rates table, as published: the pair as
    BoG writes it, and its buying, selling and mid rates in that pair's
    direction."""

    date: date
    currency: str    # BoG's name for it, e.g. "US Dollar", "BCEAO"
    pair: str        # e.g. USDGHS, GHSNGN
    buying: float
    selling: float
    mid: float


def parse_row(row: list) -> Fix:
    """A row as BoG's table sends it: ["05 Oct 2026", "US Dollar",
    "USDGHS", "11.7591", "11.7709", "11.7650"]. Numbers may carry
    thousands separators ("1,437.2700"). Raises ValueError for anything
    else, including a rate that isn't a positive number."""
    if len(row) != 6:
        raise ValueError(f"expected 6 columns, got {len(row)}: {row!r}")
    day = datetime.strptime(str(row[0]).strip(), "%d %b %Y").date()
    rates = []
    for value in row[3:]:
        n = float(str(value).replace(",", "").strip())
        if not math.isfinite(n) or n <= 0:
            raise ValueError(f"rate must be a positive number, got {value!r}: {row!r}")
        rates.append(n)
    return Fix(day, str(row[1]).strip(), str(row[2]).strip().upper(), *rates)


def normalize(fix: Fix) -> Optional[FxRate]:
    """The fix as cedis per unit of the other currency, or None for a
    pair we don't offer. Inverting a GHS<code> quote swaps buying and
    selling, so buying stays the lower of the two."""
    code = PAIRS.get(fix.pair)
    if code is None:
        return None
    if fix.pair.startswith(BASE):
        mid, buying, selling = 1 / fix.mid, 1 / fix.selling, 1 / fix.buying
    else:
        mid, buying, selling = fix.mid, fix.buying, fix.selling
    return FxRate(currency=code, date=fix.date, mid=mid, buying=buying, selling=selling, source_pair=fix.pair)
