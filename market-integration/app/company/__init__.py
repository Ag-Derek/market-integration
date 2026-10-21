"""
Company information for equity description pages (#55): profiles,
officers and annual financials that we maintain ourselves.

The source of truth is the checked-in seed folder data/companies/,
one file per company, named after its symbol (data/companies/MTNGH.json):

    {"symbol": "MTNGH",
     "profile": {"employees": {"value": 1500, "source": "Annual report 2025, p. 12",
                               "as_of": "2025-12-31"}, ...},
     "officers": [{"name": "...", "role": "Chief Executive Officer", "display_order": 1,
                   "source": "...", "as_of": "2026-03-31"}],
     "financials": [{"fiscal_year": 2025, "currency": "GHS",
                     "revenue": {"value": 1.2e10, "source": "...", "as_of": "2025-12-31"}, ...}]}

Any field may be left out; it is served as null. A value must say
where it came from and as of when, or the file is rejected. The folder
is loaded and validated at import and copied into SQLite on startup
(store.py). Only equities in the instrument master may have a file.

A bad file costs only its own company: at startup it is logged and
skipped, so that company's description is all-null and every other
page still works. The tests load the folder strictly, so a bad file
fails them before it is deployed.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from app.instruments import INSTRUMENTS
from app.models.company import Company
from app.models.instrument import Instrument

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parents[2] / "data" / "companies"


def _load_file(path: Path, instruments: dict[str, Instrument]) -> Company:
    """Parse and validate one company file. Raises ValueError naming
    the file on anything wrong with it."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path.name}: not valid JSON: {e}") from None
    if not isinstance(entry, dict):
        raise ValueError(f"{path.name}: must hold one company object")
    try:
        company = Company(**entry)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{path.name} is invalid: {e}") from None
    if company.symbol != path.stem:
        raise ValueError(f"{path.name}: holds {company.symbol!r}; the file must be named {company.symbol}.json")
    instrument = instruments.get(company.symbol)
    if instrument is None:
        raise ValueError(f"{path.name}: {company.symbol!r} is not in the instrument master")
    if instrument.asset_class != "equity":
        raise ValueError(f"{path.name}: {company.symbol!r} is a {instrument.asset_class}, not an equity")
    return company


def load_seed(path: Path = SEED_DIR, instruments: Optional[dict[str, Instrument]] = None,
              strict: bool = True) -> dict[str, Company]:
    """Load every <SYMBOL>.json in the folder into symbol -> Company.
    Strict (the default) raises ValueError on the first bad file;
    otherwise a bad file is logged and skipped."""
    instruments = INSTRUMENTS if instruments is None else instruments
    companies: dict[str, Company] = {}
    for file in sorted(Path(path).glob("*.json")):
        try:
            company = _load_file(file, instruments)
        except ValueError as e:
            if strict:
                raise
            logger.error("Skipping company profile %s", e)
            continue
        companies[company.symbol] = company
    return companies


COMPANIES = load_seed(strict=False)
