"""
Static reference data for one tradable security -- what it *is*, not
what it trades at. The universe itself lives in app/instruments/.
"""

from dataclasses import dataclass
from typing import Literal

InstrumentKind = Literal["equity", "preference", "depositary", "etf"]


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    sector: str
    kind: InstrumentKind = "equity"
    currency: str = "GHS"
