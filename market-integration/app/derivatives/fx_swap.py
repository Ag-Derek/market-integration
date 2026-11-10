"""
FX swaps against the cedi: two opposite exchanges of the same amount of
foreign currency, on a near date and a far date, both priced today.

A buy/sell swap buys the foreign currency on the near date and sells it
back on the far date; a sell/buy swap does the reverse. Treasurers use
them to roll or move a payment: hold dollars now but need them in three
months, or need them now and will have them back later.

**Pricing.** Each leg is the synthetic forward to its value date, from
app/fx/forwards.py, so the far rate is the /fx forward for the same day
and nothing here re-derives covered interest parity. A spot-start swap's
near leg is 0 days from spot, which is the spot rate itself. A
forward-start swap (near date after spot) prices both legs off the
forward curve, each with the cedi rate read at its own days. No
QuantLib: the maths is two forwards.

**The net cedi amount is a cash difference, not a profit.** Cedi rates
are far above dollar, euro and sterling rates, so the forward sits above
spot. Buying dollars now and selling them back later gives up cedis that
would have earned the cedi rate and holds a currency that earns less;
the swap points pay back exactly that difference. That is what covered
interest parity says.

Every rate is an indicative mid, not a dealer quote. See docs/fx-rates.md.
"""

from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional, Sequence

from app.fx.forwards import PIP, interpolate, synthetic_forward

Direction = Literal["buy_sell", "sell_buy"]


class SwapError(ValueError):
    """Terms that can't be priced."""


@dataclass(frozen=True)
class Leg:
    value_date: date
    days: int                   # from spot
    ghs_rate: Optional[float]   # % a year, ACT/364, read at `days`; None at spot (no rate is used)
    rate: float                 # cedis per unit of the foreign currency
    foreign_amount: float       # + received, − paid, in the foreign currency
    ghs_amount: float           # + received, − paid, in cedis


@dataclass(frozen=True)
class Swap:
    direction: Direction
    notional: float
    near: Leg
    far: Leg
    points: float               # (far − near) in pips
    premium_pa: float           # (far / near − 1), annualised on 365 days over the swap's life, %
    net_ghs: float              # the two cedi amounts added up: a cash difference, not a profit


def price(direction: Direction, notional: float, spot: float, spot_date: date, near_date: date, far_date: date,
          ghs_curve: Sequence[tuple[int, float]], foreign_rate: float, foreign_basis: int) -> Swap:
    """Price an FX swap. `spot` is cedis per unit on `spot_date`;
    `ghs_curve` is (days, % a year) points as the forwards use (one point
    for a flat rate); `foreign_rate` is % a year on ACT/`foreign_basis`."""
    if direction not in ("buy_sell", "sell_buy"):
        raise SwapError(f"Unknown direction '{direction}'. Expected buy_sell or sell_buy")
    if notional <= 0:
        raise SwapError("The notional must be positive")
    if spot <= 0:
        raise SwapError("The spot rate must be positive")
    if near_date < spot_date:
        raise SwapError(f"The near date can't be before the spot date, {spot_date.isoformat()}")
    if far_date <= near_date:
        raise SwapError("The far date must be after the near date")

    # Buy/sell: receive the currency on the near date, pay it on the far.
    sign = 1 if direction == "buy_sell" else -1

    def leg(value_date: date, foreign_sign: int) -> Leg:
        days = (value_date - spot_date).days
        ghs_rate = interpolate(ghs_curve, days) if days > 0 else None
        rate = spot if days == 0 else synthetic_forward(spot, days, ghs_rate, foreign_rate, foreign_basis)
        foreign = foreign_sign * notional
        return Leg(value_date, days, ghs_rate, rate, foreign, -foreign * rate)

    near = leg(near_date, sign)
    far = leg(far_date, -sign)
    return Swap(
        direction=direction, notional=notional, near=near, far=far,
        points=(far.rate - near.rate) / PIP,
        premium_pa=(far.rate / near.rate - 1) * 365 / (far.days - near.days) * 100,
        net_ghs=near.ghs_amount + far.ghs_amount,
    )
