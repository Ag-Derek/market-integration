"""
Market conventions shared by bills and bonds: day counts, the coupon
calendar and the settlement date. The values follow
docs/fixed-income-sources-and-conventions.md (the #33 spike).
"""

import calendar
from datetime import date, timedelta
from typing import Literal, Protocol

# "ACT/ACT" is ACT/ACT ICMA: a coupon period's length is its actual days.
# "ACT/365" and "ACT/364" treat a period as basis/frequency days. "30/360"
# is the US (NASD) 30/360 of Excel's basis 0, kept so the engine can be
# checked against Excel's published examples.
DayCount = Literal["ACT/ACT", "ACT/365", "ACT/364", "30/360"]

# GoG notes and bonds: ACT/ACT fits the GFIM sample best, but ACT/365 and
# ACT/364 fit almost as well; the GSE hasn't confirmed which it uses.
GOG_BOND_DAY_COUNT: DayCount = "ACT/ACT"
GOG_BOND_FREQUENCY = 2
# T-bills: simple interest on ACT/364. Reproduces the GFIM sample exactly
# and converts BoG's published discount rates to its interest rates.
BILL_DAY_BASIS = 364
# GFIM Rule 28. Counterparties may agree T+0 or T+1 bilaterally.
SETTLEMENT_LAG = 2


class BusinessCalendar(Protocol):
    def is_trading_day(self, day: date) -> bool: ...


def settlement_date(trade_date: date, business_days: BusinessCalendar, lag: int = SETTLEMENT_LAG) -> date:
    """`lag` business days after `trade_date` -- T+2 by default.
    `business_days` is anything with is_trading_day(), such as the
    MarketCalendar in app/session/calendar.py."""
    day = trade_date
    for _ in range(lag):
        day += timedelta(days=1)
        while not business_days.is_trading_day(day):
            day += timedelta(days=1)
    return day


def add_months(d: date, months: int) -> date:
    """`d` moved by whole months, clamped to the end of a shorter month."""
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def days_30_360(start: date, end: date) -> int:
    """Days between two dates on US (NASD) 30/360, as Excel's basis 0."""
    d1, d2 = start.day, end.day
    if _is_last_of_february(start):
        if _is_last_of_february(end):
            d2 = 30
        d1 = 30
    if d2 == 31 and d1 >= 30:
        d2 = 30
    if d1 == 31:
        d1 = 30
    return (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1)


def _is_last_of_february(d: date) -> bool:
    return d.month == 2 and d.day == calendar.monthrange(d.year, 2)[1]
