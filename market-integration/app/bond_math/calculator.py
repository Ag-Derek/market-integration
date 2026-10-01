"""
The bond detail page's calculator (#39, a simplified Bloomberg YAS):
for a bill or bond, a settlement date, a face amount and either a clean
price or a yield, the other of the two plus the risk numbers and the
trade invoice.

Out of scope for v1: OAS, Z-spread and after-tax yield.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.bond_math.bills import (
    bill_convexity,
    bill_dv01,
    bill_invoice,
    bill_modified_duration,
    bill_price,
    bill_yield,
)
from app.bond_math.bonds import (
    Bond,
    bond_invoice,
    bond_yield,
    clean_price,
    convexity,
    dirty_price,
    dv01,
    macaulay_duration,
    modified_duration,
    next_coupon_date,
    previous_coupon_date,
)
from app.bond_math.conventions import BILL_DAY_BASIS
from app.bond_math.invoice import Invoice
from app.models.fixed_income import FixedIncomeTick
from app.models.instrument import Instrument


@dataclass(frozen=True)
class Security:
    """What the calculator prices: a bill (zero coupon, ACT/364 simple
    interest) or a fixed-coupon bond."""
    maturity: date
    bond: Optional[Bond] = None    # None for a bill

    @property
    def kind(self) -> str:
        return "bill" if self.bond is None else "bond"

    @property
    def day_count(self) -> str:
        return f"ACT/{BILL_DAY_BASIS}" if self.bond is None else self.bond.day_count


def security_for(tick: FixedIncomeTick, instrument: Optional[Instrument]) -> Security:
    """The security behind a quote. Bills need only the quote's maturity
    (the mock rolls bills that aren't in the instrument master); bonds
    need the master's coupon. Raises ValueError, with the reason, for
    what the v1 conventions don't price: corporates, GFSF and USD DDE
    bonds."""
    if tick.segment == "treasury_bill":
        return Security(tick.maturity_date)
    if instrument is None:
        raise ValueError(f"{tick.symbol} isn't in the instrument master, so its coupon is unknown")
    return Security(instrument.maturity_date, Bond.from_instrument(instrument))


def quoted_yield(tick: FixedIncomeTick) -> Optional[float]:
    """Where the calculator starts: the closing yield (GFIM's end-of-day
    methodology, "so far" during the session), else the bid/ask mid.
    None if the security has no yield quote at all."""
    if tick.closing_yield is not None:
        return tick.closing_yield
    if tick.bid_yield is not None and tick.ask_yield is not None:
        return (tick.bid_yield + tick.ask_yield) / 2
    return tick.bid_yield if tick.bid_yield is not None else tick.ask_yield


@dataclass(frozen=True)
class Calculation:
    security: Security
    settlement_date: date
    face: float
    solved_for: str                # "price" or "yield": the one that was computed
    clean_price: float             # per 100 face
    dirty_price: float
    yield_pct: float               # % a year
    previous_coupon: Optional[date]
    next_coupon: Optional[date]
    macaulay_duration: Optional[float]   # years; None for bills, where it is just the time to maturity
    modified_duration: float       # years
    convexity: float               # years squared
    dv01: float                    # price change per 100 face for a 1bp fall in yield
    invoice: Invoice

    @property
    def position_dv01(self) -> float:
        """DV01 of the whole face amount, in the security's currency."""
        return self.dv01 * self.face / 100


def calculate(
    sec: Security,
    settle: date,
    face: float,
    *,
    price: Optional[float] = None,
    yield_pct: Optional[float] = None,
) -> Calculation:
    """Solve for whichever of clean `price` and `yield_pct` isn't given.
    Raises ValueError for bad inputs, including a settlement date on or
    after maturity."""
    if (price is None) == (yield_pct is None):
        raise ValueError("give either a price or a yield, not both")
    if face <= 0:
        raise ValueError(f"face amount must be positive, got {face}")
    if price is not None and price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    solved_for = "yield" if price is not None else "price"

    if sec.bond is None:
        m = sec.maturity
        if price is None:
            price = bill_price(settle, m, yield_pct)
        else:
            yield_pct = bill_yield(settle, m, price)
        return Calculation(
            security=sec, settlement_date=settle, face=face, solved_for=solved_for,
            clean_price=price, dirty_price=price, yield_pct=yield_pct,
            previous_coupon=None, next_coupon=None, macaulay_duration=None,
            modified_duration=bill_modified_duration(settle, m, yield_pct),
            convexity=bill_convexity(settle, m, yield_pct),
            dv01=bill_dv01(settle, m, yield_pct),
            invoice=bill_invoice(settle, m, face, price),
        )

    b = sec.bond
    if price is None:
        price = clean_price(b, settle, yield_pct)
    else:
        yield_pct = bond_yield(b, settle, price)
    return Calculation(
        security=sec, settlement_date=settle, face=face, solved_for=solved_for,
        clean_price=price, dirty_price=dirty_price(b, settle, yield_pct), yield_pct=yield_pct,
        previous_coupon=previous_coupon_date(b, settle), next_coupon=next_coupon_date(b, settle),
        macaulay_duration=macaulay_duration(b, settle, yield_pct),
        modified_duration=modified_duration(b, settle, yield_pct),
        convexity=convexity(b, settle, yield_pct),
        dv01=dv01(b, settle, yield_pct),
        invoice=bond_invoice(b, settle, face, price),
    )
