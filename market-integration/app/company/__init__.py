"""
Company information for equity description pages (#55): profiles,
officers and annual financials that we maintain ourselves.

The source of truth is the checked-in seed file
data/company_profiles.json, one entry per company:

    {"symbol": "MTNGH",
     "profile": {"employees": {"value": 1500, "source": "Annual report 2025, p. 12",
                               "as_of": "2025-12-31"}, ...},
     "officers": [{"name": "...", "role": "Chief Executive Officer", "display_order": 1,
                   "source": "...", "as_of": "2026-03-31"}],
     "financials": [{"fiscal_year": 2025, "currency": "GHS",
                     "revenue": {"value": 1.2e10, "source": "...", "as_of": "2025-12-31"}, ...}]}

Any field may be left out; it is served as null. A value must say
where it came from and as of when, or the seed is rejected. It is
loaded and validated at import, like the instrument master, and copied
into SQLite on startup (store.py). Only equities in the instrument
master may have an entry.
"""

import json
from pathlib import Path
from typing import Optional

from app.instruments import INSTRUMENTS
from app.models.company import Company
from app.models.instrument import Instrument

SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "company_profiles.json"


def load_seed(path: Path = SEED_PATH, instruments: Optional[dict[str, Instrument]] = None) -> dict[str, Company]:
    """Parse and validate a seed file into symbol -> Company. Raises
    ValueError naming the entry on a bad, duplicate or non-equity one,
    so a broken file stops startup."""
    instruments = INSTRUMENTS if instruments is None else instruments
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    companies: dict[str, Company] = {}
    for n, entry in enumerate(entries, start=1):
        symbol = entry.get("symbol") if isinstance(entry, dict) else None
        try:
            company = Company(**entry)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{path}: entry {n} ({symbol!r}) is invalid: {e}") from None
        instrument = instruments.get(company.symbol)
        if instrument is None:
            raise ValueError(f"{path}: entry {n}: {company.symbol!r} is not in the instrument master")
        if instrument.asset_class != "equity":
            raise ValueError(f"{path}: entry {n}: {company.symbol!r} is a {instrument.asset_class}, not an equity")
        if company.symbol in companies:
            raise ValueError(f"{path}: duplicate entry for {company.symbol!r}")
        companies[company.symbol] = company
    return companies


COMPANIES = load_seed()
