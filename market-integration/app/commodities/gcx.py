"""
Ghana Commodity Exchange (GCX) spot contracts against the world
benchmarks: which Pink Sheet series a GCX crop is compared with, and the
gap between the two in cedis per tonne.

The comparison is indicative only. A benchmark is a monthly average
export price at a foreign port (maize is US no. 2 yellow f.o.b. Gulf,
rice is Thai 5% broken milled f.o.b. Bangkok), while a GCX price is
today's spot price for a grade delivered to a warehouse in Ghana. The
gap mixes quality, transport and the cedi with the price itself. See
docs/commodities-gcx.md.

Sesame and sorghum have no benchmark: the Pink Sheet has no sesame
series, and its sorghum series ends in August 2020.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.commodities import COMMODITIES

# GCX crop (Instrument.commodity) -> COMMODITIES code.
BENCHMARKS: dict[str, str] = {
    "white_maize": "MAIZE",
    "yellow_maize": "MAIZE",
    "soya_bean": "SOYBEANS",
    "rice": "RICE",
}


@dataclass(frozen=True)
class Benchmark:
    code: str                   # COMMODITIES key
    name: str
    month: date                 # the month the average is for
    usd_per_mt: float
    ghs_per_mt: Optional[float]  # at that month's average BoG USD mid; None with no fixes held

    def basis_percent(self, gcx_ghs_per_mt: float) -> Optional[float]:
        """How far the GCX price is above (+) or below (-) the
        benchmark, both in cedis per tonne. None without a cedi price."""
        if self.ghs_per_mt is None or self.ghs_per_mt <= 0:
            return None
        return (gcx_ghs_per_mt / self.ghs_per_mt - 1) * 100


def benchmark_code(commodity: str) -> Optional[str]:
    """The benchmark a GCX crop is compared with, if it has one."""
    code = BENCHMARKS.get(commodity)
    if code is None:
        return None
    # Every benchmark must be per tonne to compare with a GCX price.
    if COMMODITIES[code].unit != "$/mt":
        raise ValueError(f"benchmark {code} for {commodity} is in {COMMODITIES[code].unit}, not $/mt")
    return code
