"""
Chart ranges (the 1D / 5D / ... / Max selector) and the candle interval
each one is drawn from, plus how much history to backfill per interval
so every range is fully covered on startup.

Each range picks the coarsest interval that still gives a smooth chart
(roughly 100-750 points), rather than reading e.g. 5 years of 5m bars.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass(frozen=True)
class RangeSpec:
    interval: str
    lookback: Optional[timedelta]  # None = no lower bound (Max / YTD)
    year_to_date: bool = False

    def start(self, now: datetime) -> Optional[datetime]:
        if self.year_to_date:
            return datetime(now.year, 1, 1, tzinfo=timezone.utc)
        if self.lookback is None:
            return None
        return now - self.lookback


RANGES: dict[str, RangeSpec] = {
    "1D": RangeSpec("5m", timedelta(days=1)),
    "5D": RangeSpec("15m", timedelta(days=5)),
    "1M": RangeSpec("1h", timedelta(days=30)),
    "6M": RangeSpec("1d", timedelta(days=182)),
    "YTD": RangeSpec("1d", None, year_to_date=True),
    "1Y": RangeSpec("1d", timedelta(days=365)),
    "5Y": RangeSpec("1w", timedelta(days=5 * 365 + 1)),
    "Max": RangeSpec("1w", None),
}

# Most candles /candles?warmup= returns ahead of a range: enough for the
# longest indicator the chart offers (a 200-period moving average).
MAX_WARMUP = 200

# How far back to ask the connector for history per interval at startup.
# Must cover the longest range served from that interval plus MAX_WARMUP
# candles ahead of it, so indicators have a value from the chart's first
# candle. None = everything the provider has.
HISTORY_DEPTH: dict[str, Optional[timedelta]] = {
    "5m": timedelta(days=2),    # 1D + a day (288 candles)
    "15m": timedelta(days=10),  # 5D + 5 days (480)
    "1h": timedelta(days=44),   # 1M + 14 days (336)
    # Everything: the stock page's Seasonality view reads every year of
    # daily closes. Costs startup next to nothing (~0.2 s for 10 mock years).
    "1d": None,
    "1w": None,
}
