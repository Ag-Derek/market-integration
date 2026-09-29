"""
Calibration for MockMarketConnector: where each GSE security starts, how
volatile it is, how often it trades and what its order book looks like.

The per-symbol values live in the optional "mock" block of each entry
in data/instruments.json, next to the instrument they describe, so
adding a security to the seed file is enough for the mock to trade it.
Every field is optional; an entry with no mock block at all trades as
DEFAULT_PROFILE. A real connector ignores all of this.

The seeded values are calibrated from the GSE "Daily Shares & ETFs"
report for 28-Sep-2026 (closing VWAP, year high/low, shares traded,
closing bid/offer) for the 25 codes it covers, and from
afx.kwayisi.org/gse/ closing prices for 29-Sep-2026 for the rest, whose
book shapes are assumed. Each block's "source" field says which.

What the fields drive:
  price       -- the live starting price; the backfilled history walks
                 backwards from it so the chart ends exactly here.
  year_range  -- [low, high]; sets the random walk's volatility so the
                 simulated 52-week range is about the real one's width.
                 Omitted = use the tier's default volatility.
  tier        -- how often the name trades (see TRADE_GAP_SECONDS). The
                 28-Sep report had 6 of its 25 names with no trades at
                 all; those are "dormant" and will sit for days without a
                 trade, which is what exercises no-trade and staleness
                 handling downstream.
  book        -- which side(s) of the book usually has a resting order:
                 "both", "bid" only, "ask" only, or "none". Many GSE names
                 close with only a bid or only an offer.
  spread      -- typical bid/ask gap in GHS when both sides are quoted.
  avg_volume  -- typical shares traded per day.
  source      -- provenance note; not used by the mock.
"""

from dataclasses import dataclass
from typing import Literal, Optional, get_args

Tier = Literal["active", "moderate", "thin", "dormant"]
Book = Literal["both", "bid", "ask", "none"]

# Mean time between trades for each tier. The mock runs in real time, so
# "active" is compressed well below real GSE activity to keep a demo
# moving; the other tiers are roughly what the daily reports show.
TRADE_GAP_SECONDS: dict[Tier, float] = {
    "active": 3,
    "moderate": 60,
    "thin": 15 * 60,
    "dormant": 3 * 24 * 3600,
}

# Annualized volatility when a symbol has no known year range.
DEFAULT_ANNUAL_VOL: dict[Tier, float] = {
    "active": 0.35,
    "moderate": 0.30,
    "thin": 0.25,
    "dormant": 0.0,
}


@dataclass(frozen=True)
class MockProfile:
    price: float = 1.00
    tier: Tier = "thin"
    book: Book = "both"
    avg_volume: int = 100
    spread: float = 0.01
    year_range: Optional[tuple[float, float]] = None
    source: Optional[str] = None

    def __post_init__(self):
        if self.tier not in get_args(Tier):
            raise ValueError(f"unknown mock tier {self.tier!r}")
        if self.book not in get_args(Book):
            raise ValueError(f"unknown mock book {self.book!r}")
        if self.price <= 0:
            raise ValueError(f"mock price must be positive, got {self.price}")
        if self.year_range is not None:
            low, high = self.year_range
            if not 0 < low <= high:
                raise ValueError(f"mock year_range must be 0 < low <= high, got {self.year_range}")


DEFAULT_PROFILE = MockProfile()


def mock_profile(raw: Optional[dict]) -> MockProfile:
    """Build a MockProfile from a seed entry's "mock" block (None = all
    defaults). Unknown keys or bad values raise, naming the problem."""
    if raw is None:
        return DEFAULT_PROFILE
    raw = dict(raw)
    year_range = raw.pop("year_range", None)
    return MockProfile(**raw, year_range=tuple(year_range) if year_range else None)
