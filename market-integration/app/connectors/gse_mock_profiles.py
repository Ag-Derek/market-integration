"""
Calibration for MockMarketConnector: where each GSE security starts, how
volatile it is, how often it trades and what its order book looks like.

This is mock behaviour, not reference data, so it lives next to the mock
rather than in the instrument master (app/instruments/). A real
connector ignores this file entirely.

Calibrated from:
  * GSE "Daily Shares & ETFs" report for 28-Sep-2026 (closing VWAP, year
    high/low, shares traded, closing bid/offer) for the 25 codes it
    covers;
  * afx.kwayisi.org/gse/ closing prices and volumes for 29-Sep-2026 for
    the rest, where no year range or order book is available -- those
    rows use tier defaults and an assumed book shape (marked "assumed").

SWL has no published price in either source; its 0.05 is a placeholder.

What the fields drive:
  price       -- the live starting price; the backfilled history walks
                 backwards from it so the chart ends exactly here.
  year_range  -- (low, high); sets the random walk's volatility so the
                 simulated 52-week range is about the real one's width.
                 None = use the tier's default volatility.
  tier        -- how often the name trades (see TRADE_GAP_SECONDS). The
                 28-Sep report had 6 of its 25 names with no trades at
                 all; those are "dormant" here and will sit for days
                 without a trade, which is what exercises no-trade and
                 staleness handling downstream.
  book        -- which side(s) of the book usually has a resting order:
                 "both", "bid" only, "ask" only, or "none". Many GSE names
                 close with only a bid or only an offer.
  spread      -- typical bid/ask gap in GHS when both sides are quoted.
  avg_volume  -- typical shares traded per day.
"""

from dataclasses import dataclass
from typing import Literal, Optional

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
    price: float
    tier: Tier
    book: Book
    avg_volume: int
    spread: float = 0.0
    year_range: Optional[tuple[float, float]] = None


MOCK_PROFILES: dict[str, MockProfile] = {
    # --- from the 28-Sep-2026 daily shares report ----------------------
    "ACCESS": MockProfile(20.67, "moderate", "bid", 3_600, year_range=(16.20, 46.64)),
    "ADB": MockProfile(5.30, "thin", "ask", 50, year_range=(5.06, 5.30)),
    "AGA": MockProfile(37.00, "dormant", "bid", 100, year_range=(37.00, 37.00)),
    "ALLGH": MockProfile(5.30, "moderate", "ask", 1_600, year_range=(5.30, 8.46)),
    "ALW": MockProfile(0.10, "dormant", "none", 1_000, year_range=(0.10, 0.10)),
    "ASG": MockProfile(8.89, "dormant", "bid", 100, year_range=(8.89, 8.89)),
    "BOPP": MockProfile(75.00, "moderate", "ask", 1_000, year_range=(55.82, 100.00)),
    "CAL": MockProfile(0.70, "active", "both", 295_000, spread=0.01, year_range=(0.61, 0.94)),
    "CLYD": MockProfile(4.02, "thin", "ask", 50, year_range=(0.46, 6.57)),
    "CMLT": MockProfile(0.14, "dormant", "bid", 500, year_range=(0.14, 0.14)),
    "CPC": MockProfile(0.26, "moderate", "bid", 2_000, year_range=(0.05, 0.26)),
    "DASPHARMA": MockProfile(1.19, "active", "both", 61_000, spread=0.09, year_range=(0.38, 1.98)),
    "EGH": MockProfile(39.00, "moderate", "both", 1_400, spread=1.00, year_range=(25.00, 57.00)),
    "EGL": MockProfile(7.00, "active", "both", 59_000, spread=0.99, year_range=(3.48, 12.16)),
    "ETI": MockProfile(1.66, "active", "both", 60_000, spread=0.02, year_range=(0.76, 2.82)),
    "FAB": MockProfile(8.40, "moderate", "ask", 4_500, year_range=(7.71, 8.40)),
    "FML": MockProfile(14.00, "moderate", "ask", 3_100, year_range=(8.00, 16.35)),
    "GCB": MockProfile(40.04, "active", "both", 13_000, spread=0.04, year_range=(20.11, 52.00)),
    "GGBL": MockProfile(10.70, "moderate", "ask", 900, year_range=(6.60, 16.50)),
    "GOIL": MockProfile(6.10, "active", "both", 37_000, spread=0.10, year_range=(2.96, 8.01)),
    "KASA": MockProfile(1.81, "active", "both", 157_000, spread=0.09, year_range=(1.20, 2.30)),
    "MAC": MockProfile(5.20, "dormant", "bid", 100, year_range=(5.20, 5.20)),
    "MTNGH": MockProfile(6.54, "active", "both", 300_000, spread=0.01, year_range=(4.20, 7.15)),
    "PBC": MockProfile(0.02, "dormant", "none", 1_000, year_range=(0.02, 0.02)),
    "RBGH": MockProfile(4.00, "moderate", "ask", 2_000, year_range=(1.30, 5.58)),
    # --- from 29-Sep-2026 closing prices; book shape assumed -----------
    "AADS": MockProfile(0.42, "dormant", "none", 100),
    "DIGICUT": MockProfile(0.47, "moderate", "both", 1_500, spread=0.02),
    "GLD": MockProfile(462.38, "thin", "ask", 10),
    "HORDS": MockProfile(0.66, "moderate", "both", 1_700, spread=0.02),
    "IIL": MockProfile(0.53, "moderate", "both", 7_000, spread=0.02),
    "MMH": MockProfile(0.12, "dormant", "none", 100),
    "SAMBA": MockProfile(0.55, "thin", "bid", 200),
    "SCB": MockProfile(69.89, "thin", "ask", 50),
    "SCB-PREF": MockProfile(0.99, "dormant", "none", 100),
    "SIC": MockProfile(5.00, "moderate", "both", 3_600, spread=0.05),
    "SOGEGH": MockProfile(5.50, "thin", "bid", 100),
    "SWL": MockProfile(0.05, "dormant", "none", 100),
    "TBL": MockProfile(1.20, "dormant", "ask", 100),
    "TLW": MockProfile(13.11, "thin", "ask", 50),
    "TOTAL": MockProfile(35.32, "thin", "both", 300, spread=0.30),
    "UNIL": MockProfile(40.00, "thin", "ask", 50),
    "ZEN": MockProfile(9.01, "moderate", "both", 1_000, spread=0.10),
}
