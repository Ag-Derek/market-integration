"""
The economic calendar (Bloomberg's ECO and WIRP's meeting list, for
Ghana): what's due when, from three official sources and two we already
hold.

  * Bank of Ghana policy rate decisions (bog.py): every MPC decision since
    2002 and the rate it set, from the data behind BoG's Policy Rate
    Trends chart. Pulled.
  * BoG MPC meeting dates (data/economic_calendar.json): BoG publishes
    each year's as a PDF, so they're kept by hand like the holiday list.
  * Ghana Statistical Service releases (gss.py): the year's release
    calendar (CPI, GDP, MIEG, PPI, ...), from the data behind GSS's
    Release Calendar page. Pulled. GSS gives the *week of the month*
    only, never the day, and calls its dates provisional.
  * T-bill auctions (app/auctions/) and market holidays
    (data/market_calendar.json), which the service already has.

store.py keeps what's pulled in SQLite, tracker.py keeps it current,
events.py merges everything into one list of events.
"""

import calendar as _calendar
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

CALENDAR_PATH = Path(__file__).resolve().parents[2] / "data" / "economic_calendar.json"


@dataclass(frozen=True)
class PolicyDecision:
    """An MPC decision: the day BoG announced it and the policy rate (%)
    it set. Unchanged rates are decisions too."""
    date: date
    rate: float


@dataclass(frozen=True)
class GssRelease:
    id: str                       # GSS's own, e.g. "2026-10-cpi-w2"
    year: int
    month: int
    week: Optional[int]           # of the month, 1-5; None where GSS gives none
    title: str
    short_code: Optional[str]     # "CPI", "MIEG", "QGDP", ...; None for reports
    frequency: str                # "monthly", "quarterly", "annual", ...
    category: str                 # GSS's slug: "prices", "economic-growth", ...
    reference_period: Optional[str]
    status: str                   # GSS's: "Released" or "Upcoming"
    needs_verification: bool      # GSS's own flag: the entry is doubtful
    source_note: Optional[str] = None   # GSS's reason, when it flags one

    @property
    def window(self) -> tuple[date, date]:
        """The days `week` covers: week n is days 7n-6 to 7n of the
        month, the last week running to the month's end; no week is the
        whole month. GSS doesn't say how it counts weeks; this is our
        reading, and the page shows the window so it can be checked."""
        last = _calendar.monthrange(self.year, self.month)[1]
        if self.week is None:
            return date(self.year, self.month, 1), date(self.year, self.month, last)
        first = min(7 * (self.week - 1) + 1, last)
        return date(self.year, self.month, first), date(self.year, self.month, min(7 * self.week, last))


@dataclass(frozen=True)
class MpcMeeting:
    start: date
    end: date
    announcement: date            # the press release with the decision


def load_mpc_meetings(path: Path = CALENDAR_PATH) -> list[MpcMeeting]:
    """The MPC meetings in the seed file, in date order. Raises
    ValueError, naming the file, for one it can't read."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        meetings = [
            MpcMeeting(date.fromisoformat(m["start"]), date.fromisoformat(m["end"]),
                       date.fromisoformat(m["announcement"]))
            for m in raw["mpc_meetings"]
        ]
    except (OSError, KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{path}: can't read the MPC meeting dates: {e}") from e
    for m in meetings:
        if not (m.start <= m.end and m.start <= m.announcement):
            raise ValueError(f"{path}: meeting {m.start} ends or announces before it starts")
    return sorted(meetings, key=lambda m: m.announcement)
