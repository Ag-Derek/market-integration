"""
Instrument master: the universe of tradable securities and their static
reference data (name, asset class, sector, ISIN, status).

The source of truth is the checked-in seed file data/instruments.json.
It is loaded (and validated) once at import, and on startup copied into
the `instruments` SQLite table the /instruments API reads from (see
store.py). Config, the connector and the API all take their symbol
universe from here, so adding a security to the seed file is enough to
put it in the feed, the API and the UI -- no code change.

A seed entry may also carry an optional "mock" block calibrating how
MockMarketConnector simulates it (see
app/connectors/gse_mock_profiles.py). That block is not reference data
and is kept out of Instrument.
"""

import json
from pathlib import Path
from typing import Iterable, Optional

from app.models.instrument import Instrument

SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "instruments.json"


def load_seed(path: Path = SEED_PATH) -> tuple[dict[str, Instrument], dict[str, dict]]:
    """Parse a seed file into (symbol -> Instrument, symbol -> raw mock
    block). Raises ValueError naming the offending entry on any bad or
    duplicate row, so a broken seed file stops startup."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    instruments: dict[str, Instrument] = {}
    mock: dict[str, dict] = {}
    isin_owner: dict[str, str] = {}
    for n, entry in enumerate(entries, start=1):
        entry = dict(entry)
        mock_block = entry.pop("mock", None)
        try:
            instrument = Instrument(**entry)
        except ValueError as e:
            raise ValueError(f"{path}: entry {n} ({entry.get('symbol')!r}) is invalid: {e}") from None
        if instrument.symbol in instruments:
            raise ValueError(f"{path}: duplicate symbol {instrument.symbol!r}")
        # Two entries sharing an ISIN means at least one of them is wrong.
        if instrument.isin is not None:
            if instrument.isin in isin_owner:
                raise ValueError(
                    f"{path}: ISIN {instrument.isin} is on both "
                    f"{isin_owner[instrument.isin]!r} and {instrument.symbol!r}"
                )
            isin_owner[instrument.isin] = instrument.symbol
        instruments[instrument.symbol] = instrument
        if mock_block is not None:
            mock[instrument.symbol] = mock_block
    return instruments, mock


INSTRUMENTS, MOCK_SEEDS = load_seed()


def get_instrument(symbol: str, instruments: Optional[dict[str, Instrument]] = None) -> Instrument:
    """Look up one instrument; raises KeyError for an unknown symbol."""
    try:
        return (instruments or INSTRUMENTS)[symbol]
    except KeyError:
        raise KeyError(f"Unknown instrument '{symbol}'") from None


def streamable(instrument: Instrument) -> bool:
    """Whether the equity feed (MARKET_SYMBOLS) can carry this instrument:
    active equities only, since a suspended or delisted name has no live
    market. Bills and bonds come from the fixed-income feed, which
    carries every active one."""
    return instrument.asset_class == "equity" and instrument.status == "active"


def streamable_symbols(instruments: Optional[Iterable[Instrument]] = None) -> list[str]:
    return [i.symbol for i in (instruments or INSTRUMENTS.values()) if streamable(i)]
