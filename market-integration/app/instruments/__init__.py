"""
Instrument master: the universe of tradable securities and their static
reference data (name, sector, kind).

This is the single source of truth for which symbols exist. Config, the
connectors and the API look symbols up here rather than keeping their
own lists, so a new listing or a delisting is a one-line change in
gse_equities.py.

Only reference data lives here -- nothing about prices or how a mock
should behave (see app/connectors/gse_mock_profiles.py for that).
"""

from app.instruments.gse_equities import GSE_INSTRUMENTS
from app.models.instrument import Instrument

INSTRUMENTS: dict[str, Instrument] = {i.symbol: i for i in GSE_INSTRUMENTS}


def get_instrument(symbol: str) -> Instrument:
    """Look up one instrument; raises KeyError for an unknown symbol."""
    try:
        return INSTRUMENTS[symbol]
    except KeyError:
        raise KeyError(f"Unknown instrument '{symbol}'") from None


def all_symbols() -> list[str]:
    return list(INSTRUMENTS)
