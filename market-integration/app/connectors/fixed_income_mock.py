"""
Mock GFIM (Ghana Fixed Income Market) data, in the shape of the GFIM
daily trading report (see app/models/fixed_income.py and
docs/data-formats.md).

Stands in for the real fixed-income feed until the GSE API exists. The
universe is every active bill and bond in the instrument master, and
each starts from its closing values in the 28-Sep-2026 sample report
(the "mock" block of its seed entry). From there it simulates what the
report shows:

  * securities trade at about their sample rate, so most rows on a
    given day have no volume, and the ones that were blank in the
    sample (no prices at all) stay blank;
  * closing yields follow an end-of-day methodology -- they move only
    part-way toward each trade -- so they can end up outside the day's
    traded range, and some bonds trade over wide yield ranges;
  * T-bills roll weekly: bills are issued on Mondays for 91, 182 and 364
    days, the ones that have matured drop out, and a new issue has no
    opening price or yield on its first day. Bills of different tenors
    that mature on the same date share one price, as in the sample.
    Bills issued after the sample aren't in the instrument master, so
    they get synthetic "GHMK..." ISINs and "...-MOCK-0" descriptions;
  * a day low/high range is carried over from the last day a security
    traded, as the report does.

It does not reproduce the report's data-entry errors (shifted maturity
dates, junk or reversed price ranges, prices in yield columns); those
are for the real connector to handle, and docs/data-formats.md lists
them.

Like the equity mock it trades 24/7, with a session per UTC day (Ghana
is on UTC). There is no background task: every read first advances the
market to "now", simulating the trades that would have happened since
the last read, so the state is always current and tests can drive it
with a fake clock.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional

from app.bond_math import Bond, bill_price, clean_price
from app.instruments import INSTRUMENTS, MOCK_SEEDS
from app.models.fixed_income import (
    REPORT_SECTIONS,
    CorporateBondQuote,
    FixedIncomeReport,
    FixedIncomeSummary,
    GovernmentBondQuote,
    LargestTrade,
    SectionSummary,
    SellBuyBackQuote,
    TreasuryBillQuote,
)
from app.models.instrument import Instrument

# The session the seed calibration comes from.
CALIBRATION_DATE = date(2026, 9, 28)

BILL_TENOR_DAYS = {"91-DAY BILL": 91, "182-DAY BILL": 182, "364-DAY BILL": 364}
_BILL_SYNTHETIC_CODE = {91: "A", 182: "B", 364: "C"}
DEFAULT_BILL_YIELD = 8.0  # only if the seed has no bills to build a curve from

# Trades per day for a security with fewer (or none) in the sample.
BASE_TRADES_PER_DAY = {
    "new_gog": 1.0,
    "ddep": 0.5,
    "old_gog": 0.15,
    "treasury_bill": 1.0,
    "corporate": 0.03,
}
# Across all New GoG and DDEP bonds; the sample had 10.
SELL_BUY_BACK_TRADES_PER_DAY = 10.0
# Typical face value per trade: each section's volume / number traded in
# the sample (corporates had no trades; 500k is a guess).
MEAN_TRADE_SIZE = {
    "new_gog": 50_000,
    "ddep": 22_000_000,
    "old_gog": 140_000,
    "treasury_bill": 3_500_000,
    "corporate": 500_000,
    "sell_buy_back": 20_000_000,
}
# End-of-day closing methodology: the close moves this far toward each trade.
CLOSE_WEIGHT = 0.25
# Standard deviation of trade yields around the close, in % points. Bonds
# that traded over a wide range in the sample use half that range.
DEFAULT_YIELD_DISPERSION = 0.15
MAX_YIELD_DISPERSION = 2.0
MIN_YIELD = 0.1
CORPORATE_PRICE_DISPERSION = 0.01  # relative

_SECONDS_PER_DAY = 24 * 3600


# ---------------------------------------------------------------- state

@dataclass
class _Day:
    """What a security did this session, plus the range carried over from
    the last session it traded in."""
    volume: int = 0
    trade_count: int = 0
    low: Optional[float] = None
    high: Optional[float] = None

    def record(self, value: float, size: int) -> None:
        if self.trade_count == 0:  # first trade replaces the carried range
            self.low = self.high = value
        else:
            self.low, self.high = min(self.low, value), max(self.high, value)
        self.volume += size
        self.trade_count += 1

    def new_session(self) -> None:
        self.volume = self.trade_count = 0


@dataclass
class _Gov:
    inst: Instrument
    rate: float
    quoted: bool
    close_yield: Optional[float] = None
    prev_close_yield: Optional[float] = None
    offset: float = 0.0
    dispersion: float = DEFAULT_YIELD_DISPERSION
    day: _Day = field(default_factory=_Day)
    # Sell/buy-back trades; yield and price carry over from the last
    # session with trades, as in the report.
    sbb_yield: Optional[float] = None
    sbb_price: Optional[float] = None
    sbb_volume: int = 0
    sbb_count: int = 0
    sbb_yield_value: float = 0.0
    sbb_price_value: float = 0.0

    def price(self, yield_pct: float, settle: date) -> float:
        return clean_price(self.bond, settle, yield_pct) + self.offset

    @property
    def bond(self) -> Bond:
        return Bond(self.inst.maturity_date, self.inst.coupon_rate or 0.0)


@dataclass
class _Bill:
    symbol: str
    description: str
    tenor: str
    maturity: date
    issue_date: date
    rate: float
    day: _Day = field(default_factory=_Day)


@dataclass
class _CurvePoint:
    """Bills of any tenor maturing on the same date share one close."""
    close: float
    prev_close: Optional[float] = None


@dataclass
class _Corp:
    inst: Instrument
    rate: float
    quoted: bool
    close_price: Optional[float] = None
    prev_close_price: Optional[float] = None
    day: _Day = field(default_factory=_Day)


def _poisson(lam: float) -> int:
    if lam <= 0:
        return 0
    threshold, k, p = math.exp(-lam), 0, random.random()
    while p > threshold:
        k += 1
        p *= random.random()
    return k


def _trade_size(section: str) -> int:
    sigma = 1.0
    mean = MEAN_TRADE_SIZE[section]
    return max(int(random.lognormvariate(math.log(mean) - sigma ** 2 / 2, sigma)), 1_000)


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=timezone.utc)


# ---------------------------------------------------------------- market

class MockFixedIncomeMarket:

    def __init__(
        self,
        instruments: Optional[dict[str, Instrument]] = None,
        mock_seeds: Optional[dict[str, dict]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self._instruments = INSTRUMENTS if instruments is None else instruments
        self._seeds = MOCK_SEEDS if mock_seeds is None else mock_seeds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._gov: list[_Gov] = []
        self._corp: list[_Corp] = []
        self._bills: dict[tuple[str, date], _Bill] = {}
        self._curve: dict[date, _CurvePoint] = {}
        self._seeded_bills: dict[tuple[str, date], Instrument] = {}
        self._session: Optional[date] = None
        self._last: Optional[datetime] = None

    @property
    def ready(self) -> bool:
        return self._session is not None

    # ------------------------------------------------------------ setup

    def start(self) -> None:
        """Load the calibration as the previous session's closes, open
        today's session and simulate it up to now."""
        now = self._clock()
        for inst in self._instruments.values():
            if inst.asset_class == "equity" or inst.status != "active":
                continue
            seed = self._seeds.get(inst.symbol, {})
            rate = max(float(seed.get("trades", 0)), BASE_TRADES_PER_DAY[inst.segment])
            if inst.segment in ("new_gog", "ddep", "old_gog"):
                self._gov.append(self._load_gov(inst, seed, rate))
            elif inst.segment == "corporate":
                close = seed.get("closing_price")
                day = _Day(low=seed.get("day_low"), high=seed.get("day_high"))
                self._corp.append(_Corp(inst, rate, close is not None, close, day=day))
            elif inst.segment == "treasury_bill":
                self._seeded_bills[(inst.tenor, inst.maturity_date)] = inst
                if seed.get("closing_yield") is not None:
                    self._curve[inst.maturity_date] = _CurvePoint(seed["closing_yield"])
                # Carried day ranges are applied when the bill is created.

        self._session = CALIBRATION_DATE
        self._roll_bills(CALIBRATION_DATE)
        today = now.date()
        self._start_session(max(today, CALIBRATION_DATE))
        self._last = _midnight(self._session)
        self._advance(now)

    def _load_gov(self, inst: Instrument, seed: dict, rate: float) -> _Gov:
        close_yield = seed.get("closing_yield")
        g = _Gov(inst, rate, quoted=close_yield is not None, close_yield=close_yield)
        g.day = _Day(low=seed.get("day_low"), high=seed.get("day_high"))
        if g.day.low is not None and g.day.high is not None:
            g.dispersion = min(max((g.day.high - g.day.low) / 2, DEFAULT_YIELD_DISPERSION), MAX_YIELD_DISPERSION)
        if g.quoted and seed.get("closing_price") is not None:
            # Calibrate away the pricing-convention gap for structured bonds.
            g.offset = seed["closing_price"] - clean_price(g.bond, CALIBRATION_DATE, close_yield)
        g.sbb_yield, g.sbb_price = seed.get("sbb_yield"), seed.get("sbb_price")
        return g

    # ------------------------------------------------------------ time

    def _advance(self, now: datetime) -> None:
        """Simulate from the last read up to `now`, closing and opening
        sessions at each UTC midnight in between."""
        while now.date() > self._session:
            end = _midnight(self._session + timedelta(days=1))
            self._simulate((end - self._last).total_seconds())
            self._start_session(self._session + timedelta(days=1))
            self._last = end
        if now > self._last:
            self._simulate((now - self._last).total_seconds())
            self._last = now

    def _start_session(self, session: date) -> None:
        for g in self._gov:
            g.prev_close_yield = g.close_yield
            g.day.new_session()
            g.sbb_volume = g.sbb_count = 0
            g.sbb_yield_value = g.sbb_price_value = 0.0
        self._gov = [g for g in self._gov if g.inst.maturity_date > session]
        for c in self._corp:
            c.prev_close_price = c.close_price
            c.day.new_session()
        self._corp = [c for c in self._corp if c.inst.maturity_date > session]
        for p in self._curve.values():
            p.prev_close = p.close
        for b in self._bills.values():
            b.day.new_session()
        self._session = session
        self._roll_bills(session)

    def _roll_bills(self, session: date) -> None:
        """The outstanding bills on `session`: for each tenor, one maturing
        every Monday from next week to the tenor's end."""
        week_start = session - timedelta(days=session.weekday())
        wanted = {}
        for tenor, days in BILL_TENOR_DAYS.items():
            for k in range(1, days // 7 + 1):
                maturity = week_start + timedelta(days=7 * k)
                wanted[(tenor, maturity)] = days
        # Price new maturities off the current curve before dropping
        # matured points, so the curve is never empty.
        for (tenor, maturity) in wanted:
            if maturity not in self._curve:
                self._curve[maturity] = _CurvePoint(self._interpolate(maturity, session))
        for (tenor, maturity), days in wanted.items():
            if (tenor, maturity) not in self._bills:
                self._bills[(tenor, maturity)] = self._new_bill(tenor, days, maturity)
        self._bills = {k: v for k, v in self._bills.items() if k in wanted}
        self._curve = {m: p for m, p in self._curve.items() if m > session}

    def _new_bill(self, tenor: str, days: int, maturity: date) -> _Bill:
        issue_date = maturity - timedelta(days=days)
        inst = self._seeded_bills.get((tenor, maturity))
        if inst is not None:
            seed = self._seeds.get(inst.symbol, {})
            rate = max(float(seed.get("trades", 0)), BASE_TRADES_PER_DAY["treasury_bill"])
            day = _Day(low=seed.get("day_low"), high=seed.get("day_high"))
            return _Bill(inst.symbol, inst.name, tenor, maturity, issue_date, rate, day)
        # Issued after the sample: not in the instrument master.
        symbol = f"GHMK{_BILL_SYNTHETIC_CODE[days]}{maturity:%y%m%d}0"
        return _Bill(symbol, f"GOG-BL-{maturity:%d/%m/%y}-MOCK-0", tenor, maturity, issue_date,
                     BASE_TRADES_PER_DAY["treasury_bill"])

    def _interpolate(self, maturity: date, session: date) -> float:
        points = sorted(((m - session).days, p.close) for m, p in self._curve.items())
        if not points:  # no bills in the seed at all
            return DEFAULT_BILL_YIELD
        target = (maturity - session).days
        if target <= points[0][0]:
            return points[0][1]
        for (d0, y0), (d1, y1) in zip(points, points[1:]):
            if d0 <= target <= d1:
                return y0 + (y1 - y0) * (target - d0) / (d1 - d0) if d1 != d0 else y1
        return points[-1][1]

    # ------------------------------------------------------------ trading

    def _simulate(self, seconds: float) -> None:
        if seconds <= 0:
            return
        frac = seconds / _SECONDS_PER_DAY
        for g in self._gov:
            if g.quoted:
                for _ in range(_poisson(g.rate * frac)):
                    self._trade_gov(g)
        for b in self._bills.values():
            for _ in range(_poisson(b.rate * frac)):
                self._trade_bill(b)
        for c in self._corp:
            if c.quoted:
                for _ in range(_poisson(c.rate * frac)):
                    self._trade_corp(c)
        eligible = [g for g in self._gov if g.quoted and g.inst.segment in ("new_gog", "ddep")]
        if eligible:
            per_bond = SELL_BUY_BACK_TRADES_PER_DAY / len(eligible)
            for g in eligible:
                for _ in range(_poisson(per_bond * frac)):
                    self._trade_sell_buy_back(g)

    def _trade_gov(self, g: _Gov) -> None:
        y = max(g.close_yield + random.gauss(0, g.dispersion), MIN_YIELD)
        g.day.record(y, _trade_size(g.inst.segment))
        g.close_yield += CLOSE_WEIGHT * (y - g.close_yield)

    def _trade_bill(self, b: _Bill) -> None:
        point = self._curve[b.maturity]
        y = max(point.close + random.gauss(0, DEFAULT_YIELD_DISPERSION), MIN_YIELD)
        b.day.record(bill_price(self._session, b.maturity, y), _trade_size("treasury_bill"))
        point.close += CLOSE_WEIGHT * (y - point.close)

    def _trade_corp(self, c: _Corp) -> None:
        p = c.close_price * math.exp(random.gauss(0, CORPORATE_PRICE_DISPERSION))
        c.day.record(p, _trade_size("corporate"))
        c.close_price += CLOSE_WEIGHT * (p - c.close_price)

    def _trade_sell_buy_back(self, g: _Gov) -> None:
        y = max(g.close_yield + random.gauss(0, max(g.dispersion, 0.3)), MIN_YIELD)
        size = _trade_size("sell_buy_back")
        g.sbb_volume += size
        g.sbb_count += 1
        g.sbb_yield_value += y * size
        g.sbb_price_value += g.price(y, self._session) * size
        g.sbb_yield = g.sbb_yield_value / g.sbb_volume
        g.sbb_price = g.sbb_price_value / g.sbb_volume

    # ------------------------------------------------------------ reads

    def report(self) -> FixedIncomeReport:
        self._advance(self._clock())
        sections = {
            "new_gog": self._gov_rows("new_gog"),
            "ddep": self._gov_rows("ddep"),
            "old_gog": self._gov_rows("old_gog"),
            "treasury_bill": self._bill_rows(),
            "corporate": self._corp_rows(),
            "sell_buy_back": self._sell_buy_back_rows(),
        }
        summaries = [_summarize(name, sections[name]) for name in REPORT_SECTIONS]
        summary = FixedIncomeSummary(
            report_date=self._session,
            sections=summaries,
            total_volume=sum(s.volume for s in summaries),
            total_trade_count=sum(s.trade_count for s in summaries),
        )
        return FixedIncomeReport(report_date=self._session, as_of=self._last, summary=summary, **sections)

    def _days(self, maturity: date) -> int:
        return (maturity - self._session).days

    def _gov_rows(self, segment: str) -> list[GovernmentBondQuote]:
        rows = []
        for g in self._gov:
            if g.inst.segment != segment:
                continue
            row = dict(
                symbol=g.inst.symbol, description=g.inst.name, segment=segment, tenor=g.inst.tenor,
                currency=g.inst.currency, maturity_date=g.inst.maturity_date,
                days_to_maturity=self._days(g.inst.maturity_date),
                volume=g.day.volume or None, trade_count=g.day.trade_count or None,
            )
            if g.quoted:
                row.update(
                    opening_yield=_r(g.prev_close_yield),
                    closing_yield=_r(g.close_yield),
                    closing_price=_r(g.price(g.close_yield, self._session)),
                    day_low_yield=_r(g.day.low),
                    day_high_yield=_r(g.day.high),
                )
            rows.append(GovernmentBondQuote(**row))
        return rows

    def _bill_rows(self) -> list[TreasuryBillQuote]:
        rows = []
        order = {tenor: n for n, tenor in enumerate(BILL_TENOR_DAYS)}
        for b in sorted(self._bills.values(), key=lambda b: (order[b.tenor], b.maturity)):
            days = self._days(b.maturity)
            point = self._curve[b.maturity]
            # A bill issued this session has no opening values yet.
            opening = None if b.issue_date == self._session else point.prev_close
            rows.append(TreasuryBillQuote(
                symbol=b.symbol, description=b.description, tenor=b.tenor,
                maturity_date=b.maturity, days_to_maturity=days,
                opening_price=_r(bill_price(self._session, b.maturity, opening)) if opening is not None else None,
                opening_yield=_r(opening),
                closing_price=_r(bill_price(self._session, b.maturity, point.close)),
                closing_yield=_r(point.close),
                volume=b.day.volume or None, trade_count=b.day.trade_count or None,
                day_low_price=_r(b.day.low), day_high_price=_r(b.day.high),
            ))
        return rows

    def _corp_rows(self) -> list[CorporateBondQuote]:
        rows = []
        for c in self._corp:
            row = dict(
                symbol=c.inst.symbol, description=c.inst.name, issuer=c.inst.issuer,
                maturity_date=c.inst.maturity_date, days_to_maturity=self._days(c.inst.maturity_date),
                volume=c.day.volume or None, trade_count=c.day.trade_count or None,
            )
            if c.quoted:
                row.update(
                    opening_price=_r(c.prev_close_price), closing_price=_r(c.close_price),
                    day_low_price=_r(c.day.low), day_high_price=_r(c.day.high),
                )
            rows.append(CorporateBondQuote(**row))
        return rows

    def _sell_buy_back_rows(self) -> list[SellBuyBackQuote]:
        return [
            SellBuyBackQuote(
                symbol=g.inst.symbol, description=g.inst.name, segment=g.inst.segment, tenor=g.inst.tenor,
                maturity_date=g.inst.maturity_date, days_to_maturity=self._days(g.inst.maturity_date),
                volume=g.sbb_volume or None, trade_count=g.sbb_count or None,
                yield_=_r(g.sbb_yield), weighted_average_price=_r(g.sbb_price),
            )
            for g in self._gov
            if g.inst.segment in ("new_gog", "ddep")
        ]


def _r(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 4)


def _summarize(section: str, rows: list) -> SectionSummary:
    traded = [r for r in rows if r.volume]
    largest = None
    if traded:
        top = max(traded, key=lambda r: r.volume)
        if section == "sell_buy_back":
            y, price = top.yield_, top.weighted_average_price
        else:
            y, price = getattr(top, "closing_yield", None), top.closing_price
        largest = LargestTrade(
            symbol=top.symbol, description=top.description, volume=top.volume,
            trade_count=top.trade_count, yield_=y, closing_price=price,
        )
    return SectionSummary(
        section=section,
        volume=sum(r.volume for r in traded),
        trade_count=sum(r.trade_count for r in traded),
        largest_trade=largest,
    )
