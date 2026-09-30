"""
Mock GFIM (Ghana Fixed Income Market) data: the market state behind both
the GFIM-style daily report (report()) and the pipeline's fixed-income
ticks (tick(), ticks()), plus invented history for the charts
(history()). MockFixedIncomeConnector (fixed_income_connector.py)
streams it.

Stands in for the real fixed-income feed until the GSE API exists. The
universe is every active bill and bond in the instrument master, and
each starts from its closing values in the 28-Sep-2026 sample report
(the "mock" block of its seed entry), which are taken as the closes of
the session before the one the market starts in.

How it moves:

  * a yield curve per currency (Nelson-Siegel, fitted to the sample's
    bill, New GoG and 2023 DDEP closes; flat for the four USD bonds)
    drifts over time, and each security sits at a spread to it that
    drifts too (yield_curve.py). Curve + spread is the security's fair
    yield;
  * two-way quotes sit either side of the fair yield, and trades print
    around it, at about each security's sample rate, so most rows on a
    given day have no volume. The ones that were blank in the sample (no
    prices at all) stay blank and never trade;
  * closing yields follow an end-of-day methodology -- they move only
    part-way toward each trade -- so they can end up outside the day's
    traded range;
  * every price is derived from its yield with app.bond_math, so price
    and yield always agree. The GFSF and USD DDE bonds, which don't
    price as plain bullets, keep a fixed offset calibrated from the
    sample (see docs/fixed-income-sources-and-conventions.md);
  * corporates are quoted by price only, as in the report, and trade
    around their last close;
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

Sessions: given a MarketCalendar, trades only happen in its sessions
(GFIM: 09:00-16:00 GMT on business days) and a new session starts at
each trading day's open. Without one (tests), or with one overridden to
"open", it trades around the clock with a session per UTC day.

History: at start() it invents the past the same way -- the curve and
spreads walked back from their starting values, and the same trade and
closing process run over them -- up to 10 years back or to the
security's issue, landing exactly on the starting closes. Live trades
extend it, so history() always ends at "now".

There is no background task: every read first advances the market to
"now", simulating whatever would have happened since the last read, so
the state is always current and tests can drive it with a fake clock.
"""

import bisect
import math
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Optional

from app.bond_math import Bond, bill_price, clean_price, supports
from app.connectors.yield_curve import OrnsteinUhlenbeck, YieldCurve, fit_curve, yield_from_factors
from app.instruments import INSTRUMENTS, MOCK_SEEDS
from app.models.candle import INTERVALS, Candle, bucket_start, resample
from app.models.fixed_income import (
    REPORT_SECTIONS,
    CorporateBondQuote,
    CurveInstrument,
    CurvePoint,
    FixedIncomeReport,
    FixedIncomeSummary,
    FixedIncomeTick,
    GovernmentBondQuote,
    LargestTrade,
    RepoTick,
    SectionSummary,
    SellBuyBackQuote,
    TreasuryBillQuote,
    GovernmentYieldCurve,
)
from app.models.instrument import Instrument

if TYPE_CHECKING:
    from app.session.calendar import MarketCalendar

# The session the seed calibration comes from.
CALIBRATION_DATE = date(2026, 9, 28)

REPORT_LABEL = "GFIM - Simulated Report"


def exchange_label(currency: str) -> str:
    return f"GFIM - Simulated Quote - {currency}"


BILL_TENOR_DAYS = {"91-DAY BILL": 91, "182-DAY BILL": 182, "364-DAY BILL": 364}
_BILL_SYNTHETIC_CODE = {91: "A", 182: "B", 364: "C"}
DEFAULT_BILL_YIELD = 8.0  # only if the seed has no bills to build a curve from

# Trades per session for a security with fewer (or none) in the sample.
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
# Standard deviation of trade yields around the fair yield, in % points.
# Bonds that traded over a wide range in the sample use half that range.
DEFAULT_YIELD_DISPERSION = 0.15
MAX_YIELD_DISPERSION = 2.0
MIN_YIELD = 0.1
CORPORATE_PRICE_DISPERSION = 0.01  # relative
MIN_PRICE = 0.01

# Curve factors (% points per sqrt(day)) and their mean reversion.
CURVE_LEVEL_VOL = 0.08
CURVE_SLOPE_VOL = 0.06
CURVE_CURVATURE_VOL = 0.10
CURVE_HALF_LIFE_DAYS = 250
# Each security's spread to the curve.
SPREAD_VOL = 0.02
SPREAD_HALF_LIFE_DAYS = 30
# Half the bid-ask spread, in yield % points. GFIM Rule 17 caps benchmark
# spreads at 50bp.
HALF_SPREAD = {"treasury_bill": 0.05, "new_gog": 0.10, "ddep": 0.15, "old_gog": 0.25}

HISTORY_YEARS = 10
# When the issue date can't be told from the tenor label.
DEFAULT_HISTORY_DAYS = 3 * 365
DDEP_SETTLEMENT = date(2023, 2, 21)
USD_DDE_ISSUE = date(2023, 9, 4)

_SECONDS_PER_DAY = 24 * 3600
_DAYS_PER_YEAR = 365.25
GOV_SEGMENTS = ("new_gog", "ddep", "old_gog")


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=timezone.utc)


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


def _years(maturity: date, t: datetime) -> float:
    return (_midnight(maturity) - t).total_seconds() / _SECONDS_PER_DAY / _DAYS_PER_YEAR


def _issue_date(inst: Instrument) -> Optional[date]:
    """Best guess at a bond's issue date from its tenor label, for how
    far back to invent history. None if the label doesn't say."""
    tenor = (inst.tenor or "").upper()
    if tenor.startswith("USD-DDE"):
        return USD_DDE_ISSUE
    if tenor.startswith("2023-"):
        return DDEP_SETTLEMENT
    m = re.search(r"(\d+)[- ]?(?:YEAR|YR)", tenor)
    if m and inst.maturity_date:
        return inst.maturity_date - timedelta(days=round(int(m.group(1)) * _DAYS_PER_YEAR))
    return None


# ---------------------------------------------------------------- sessions

class _Sessions:
    """When GFIM trades. Without a calendar, or with one overridden to
    open: around the clock, a session per UTC day. Otherwise the
    calendar's trading days and hours -- or never, if it's overridden
    to closed or pre-open."""

    def __init__(self, calendar: Optional["MarketCalendar"]):
        self._cal = calendar
        override = getattr(calendar, "override", None)
        override = getattr(override, "value", override)
        self._always = calendar is None or override == "open"
        self._never = not self._always and override is not None

    def is_trading_day(self, day: date) -> bool:
        return self._always or self._cal.is_trading_day(day)

    def bounds(self, day: date) -> tuple[datetime, datetime]:
        """The session's (open, close) on `day`, UTC."""
        if self._always:
            return _midnight(day), _midnight(day + timedelta(days=1))
        _, open_, close = self._cal.session_bounds(day)
        return open_, close

    def session_day(self, t: datetime) -> date:
        """The session current at `t`: the latest trading day that has
        opened by then."""
        for n in range(60):
            day = t.date() - timedelta(days=n)
            if self.is_trading_day(day) and self.bounds(day)[0] <= t:
                return day
        return t.date()

    def next_session_day(self, day: date) -> date:
        for n in range(1, 60):
            if self.is_trading_day(d := day + timedelta(days=n)):
                return d
        raise ValueError(f"no trading day within 60 days after {day}")

    def is_open(self, t: datetime) -> bool:
        if self._never:
            return False
        open_, close = self.bounds(t.date())
        return self.is_trading_day(t.date()) and open_ <= t < close

    def windows(self, a: datetime, b: datetime) -> list[tuple[datetime, datetime, date]]:
        """The trading hours inside [a, b), as (start, end, session day)."""
        if self._never or b <= a:
            return []
        out = []
        day = a.date() - timedelta(days=1)
        while True:
            open_, close = self.bounds(day)
            if open_ >= b:
                return out
            if self.is_trading_day(day):
                start, end = max(a, open_), min(b, close)
                if start < end:
                    out.append((start, end, day))
            day += timedelta(days=1)

    def length(self, day: date) -> float:
        open_, close = self.bounds(day)
        return (close - open_).total_seconds()


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
class _Path:
    """A closing value (yield, or price for corporates) over time: the
    value from each time until the next."""
    times: list[datetime] = field(default_factory=list)
    values: list[float] = field(default_factory=list)

    def set(self, t: datetime, value: float) -> None:
        self.times.append(t)
        self.values.append(value)


@dataclass
class _Trades:
    times: list[datetime] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)

    def add(self, t: datetime, size: int) -> None:
        self.times.append(t)
        self.sizes.append(size)


@dataclass
class _Spread:
    """A security's spread to its curve: a mean-reverting walk, stepped
    lazily to whenever it's read."""
    walk: OrnsteinUhlenbeck
    at: datetime

    def value(self, t: datetime) -> float:
        if t > self.at:
            self.walk.step((t - self.at).total_seconds() / _SECONDS_PER_DAY)
            self.at = t
        return self.walk.value


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
    spread: Optional[_Spread] = None
    path: _Path = field(default_factory=_Path)
    trades: _Trades = field(default_factory=_Trades)
    # Sell/buy-back trades; yield and price carry over from the last
    # session with trades, as in the report.
    sbb_yield: Optional[float] = None
    sbb_price: Optional[float] = None
    sbb_volume: int = 0
    sbb_count: int = 0
    sbb_yield_value: float = 0.0
    sbb_price_value: float = 0.0

    def __post_init__(self):
        self.bond = Bond(self.inst.maturity_date, self.inst.coupon_rate or 0.0)

    def price(self, yield_pct: float, settle: date) -> float:
        return clean_price(self.bond, settle, yield_pct) + self.offset


@dataclass
class _Point:
    """A bill maturity date: bills of any tenor maturing on it share one
    close (as in the sample) and one spread to the curve."""
    maturity: date
    close: float
    prev_close: Optional[float] = None
    spread: Optional[_Spread] = None
    path: _Path = field(default_factory=_Path)


@dataclass
class _Bill:
    symbol: str
    description: str
    tenor: str
    maturity: date
    issue_date: date
    rate: float
    day: _Day = field(default_factory=_Day)
    trades: _Trades = field(default_factory=_Trades)


@dataclass
class _Corp:
    inst: Instrument
    rate: float
    quoted: bool
    close_price: Optional[float] = None
    prev_close_price: Optional[float] = None
    day: _Day = field(default_factory=_Day)
    path: _Path = field(default_factory=_Path)
    trades: _Trades = field(default_factory=_Trades)


# ---------------------------------------------------------------- market

class MockFixedIncomeMarket:

    def __init__(
        self,
        instruments: Optional[dict[str, Instrument]] = None,
        mock_seeds: Optional[dict[str, dict]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        calendar: Optional["MarketCalendar"] = None,
        history: bool = True,
    ):
        """`calendar` limits trading to its sessions; `history=False`
        skips inventing the past (faster, for tests that don't chart)."""
        self._instruments = INSTRUMENTS if instruments is None else instruments
        self._seeds = MOCK_SEEDS if mock_seeds is None else mock_seeds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sessions = _Sessions(calendar)
        self._with_history = history
        self._gov: list[_Gov] = []
        # Every bond since start(), matured or not, for past curves.
        self._all_gov: list[_Gov] = []
        self._corp: list[_Corp] = []
        self._bills: dict[tuple[str, date], _Bill] = {}
        self._points: dict[date, _Point] = {}
        self._seeded_bills: dict[tuple[str, date], Instrument] = {}
        self._curves: dict[str, YieldCurve] = {}
        self._curve_at: Optional[datetime] = None
        self._session: Optional[date] = None
        self._last: Optional[datetime] = None
        # symbol -> when it last traded outright / in a sell/buy-back.
        self._last_trade: dict[str, datetime] = {}
        self._last_repo_trade: dict[str, datetime] = {}
        # What traded since the last drain_traded(), for the connector.
        self._traded: set[str] = set()
        self._repo_traded: set[str] = set()
        # Every bill ever outstanding, so a matured one's history stays
        # readable.
        self._all_bills: dict[str, _Bill] = {}
        # Likewise every bill maturity's close path, for past curves.
        self._all_points: dict[date, _Point] = {}
        self._daily_cache: dict[str, tuple[datetime, list[Candle]]] = {}
        # symbol -> the security's state, for the current universe.
        self._by_symbol: dict[str, _Gov | _Bill | _Corp] = {}

    @property
    def ready(self) -> bool:
        return self._session is not None

    # ------------------------------------------------------------ setup

    def start(self) -> None:
        """Load the calibration as the previous session's closes, open
        the current session, invent the history before it and simulate
        the session up to now."""
        now = self._clock()
        for inst in self._instruments.values():
            if inst.asset_class == "equity" or inst.status != "active":
                continue
            seed = self._seeds.get(inst.symbol, {})
            rate = max(float(seed.get("trades", 0)), BASE_TRADES_PER_DAY[inst.segment])
            if inst.segment in GOV_SEGMENTS:
                self._gov.append(self._load_gov(inst, seed, rate))
            elif inst.segment == "corporate":
                close = seed.get("closing_price")
                day = _Day(low=seed.get("day_low"), high=seed.get("day_high"))
                self._corp.append(_Corp(inst, rate, close is not None, close, day=day))
            elif inst.segment == "treasury_bill":
                self._seeded_bills[(inst.tenor, inst.maturity_date)] = inst
                if seed.get("closing_yield") is not None:
                    self._points[inst.maturity_date] = _Point(inst.maturity_date, seed["closing_yield"])
                # Carried day ranges are applied when the bill is created.

        self._session = CALIBRATION_DATE
        self._roll_bills(CALIBRATION_DATE)
        session = max(self._sessions.session_day(now), CALIBRATION_DATE)
        self._start_session(session)
        self._all_points.update(self._points)
        self._all_gov = list(self._gov)
        begin = min(self._sessions.bounds(session)[0], now)

        self._fit_curves(begin)
        for g in self._gov:
            if g.quoted:
                g.path.set(begin, g.close_yield)
        for p in self._points.values():
            p.path.set(begin, p.close)
        for c in self._corp:
            if c.quoted:
                c.path.set(begin, c.close_price)
        if self._with_history:
            self._invent_history(begin)

        self._last = begin
        self._advance(now)

    def _load_gov(self, inst: Instrument, seed: dict, rate: float) -> _Gov:
        close_yield = seed.get("closing_yield")
        g = _Gov(inst, rate, quoted=close_yield is not None, close_yield=close_yield)
        g.day = _Day(low=seed.get("day_low"), high=seed.get("day_high"))
        if g.day.low is not None and g.day.high is not None:
            g.dispersion = min(max((g.day.high - g.day.low) / 2, DEFAULT_YIELD_DISPERSION), MAX_YIELD_DISPERSION)
        if g.quoted and seed.get("closing_price") is not None and not supports(inst):
            # GFSF and USD DDE bonds don't price as plain bullets; keep the
            # sample's price-yield gap as a fixed offset. Everything else
            # prices exactly off its yield.
            g.offset = seed["closing_price"] - clean_price(g.bond, CALIBRATION_DATE, close_yield)
        g.sbb_yield, g.sbb_price = seed.get("sbb_yield"), seed.get("sbb_price")
        return g

    def _fit_curves(self, t: datetime) -> None:
        """Fit each currency's curve to the closes at `t`, and give every
        quoted security its spread to it."""
        ghs = [(_years(p.maturity, t), p.close) for p in self._points.values()]
        ghs += [
            (_years(g.inst.maturity_date, t), g.close_yield) for g in self._gov
            if g.quoted and g.inst.currency == "GHS"
            and (g.inst.segment == "new_gog" or (g.inst.tenor or "").startswith("2023-"))
        ]
        usd = [(_years(g.inst.maturity_date, t), g.close_yield) for g in self._gov
               if g.quoted and g.inst.currency == "USD"]
        vols = (CURVE_LEVEL_VOL, CURVE_SLOPE_VOL, CURVE_CURVATURE_VOL, CURVE_HALF_LIFE_DAYS)
        self._curves = {
            ccy: curve for ccy, curve in (
                ("GHS", fit_curve(ghs, *vols) or fit_curve([(1.0, DEFAULT_BILL_YIELD)], *vols, flat=True)),
                ("USD", fit_curve(usd, *vols, flat=True)),
            ) if curve is not None
        }
        self._curve_at = t
        for g in self._gov:
            if g.quoted:
                g.spread = self._new_spread(g.close_yield - self._curve_yield(g.inst.currency, g.inst.maturity_date, t), t)
        for p in self._points.values():
            p.spread = self._new_spread(p.close - self._curve_yield("GHS", p.maturity, t), t)

    @staticmethod
    def _new_spread(value: float, t: datetime) -> _Spread:
        return _Spread(OrnsteinUhlenbeck(value, value, SPREAD_VOL, SPREAD_HALF_LIFE_DAYS), t)

    # ------------------------------------------------------------ the curve

    def _curve_for(self, currency: str) -> YieldCurve:
        return self._curves.get(currency) or self._curves["GHS"]

    def _evolve_curves(self, t: datetime) -> None:
        if self._curve_at is not None and t > self._curve_at:
            days = (t - self._curve_at).total_seconds() / _SECONDS_PER_DAY
            for curve in self._curves.values():
                curve.step(days)
            self._curve_at = t

    def _curve_yield(self, currency: str, maturity: date, t: datetime) -> float:
        return self._curve_for(currency).yield_at(_years(maturity, t))

    def _fair_gov(self, g: _Gov, t: datetime) -> float:
        self._evolve_curves(t)
        return max(self._curve_yield(g.inst.currency, g.inst.maturity_date, t) + g.spread.value(t), MIN_YIELD)

    def _fair_point(self, p: _Point, t: datetime) -> float:
        self._evolve_curves(t)
        return max(self._curve_yield("GHS", p.maturity, t) + p.spread.value(t), MIN_YIELD)

    # ------------------------------------------------------------ time

    def advance(self, now: Optional[datetime] = None) -> None:
        self._advance(now or self._clock())

    def _advance(self, now: datetime) -> None:
        """Simulate from the last read up to `now`, starting a new session
        at each trading day's open in between."""
        while True:
            next_day = self._sessions.next_session_day(self._session)
            next_open = self._sessions.bounds(next_day)[0]
            if next_open > now:
                break
            self._simulate(self._last, next_open)
            self._start_session(next_day)
            self._last = max(self._last, next_open)
        if now > self._last:
            self._simulate(self._last, now)
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
        for p in self._points.values():
            p.prev_close = p.close
        for b in self._bills.values():
            b.day.new_session()
        self._session = session
        self._roll_bills(session)
        self._by_symbol = {g.inst.symbol: g for g in self._gov}
        self._by_symbol.update({b.symbol: b for b in self._bills.values()})
        self._by_symbol.update({c.inst.symbol: c for c in self._corp})

    def _roll_bills(self, session: date) -> None:
        """The outstanding bills on `session`: for each tenor, one maturing
        every Monday from next week to the tenor's end."""
        week_start = session - timedelta(days=session.weekday())
        wanted = {}
        for tenor, days in BILL_TENOR_DAYS.items():
            for k in range(1, days // 7 + 1):
                maturity = week_start + timedelta(days=7 * k)
                wanted[(tenor, maturity)] = days
        # Price new maturities off the current closes before dropping
        # matured points, so there is always something to interpolate.
        opened = self._sessions.bounds(session)[0]
        for (tenor, maturity) in wanted:
            if maturity not in self._points:
                p = _Point(maturity, self._interpolate(maturity, session))
                if self._curves:  # after start(): join the curve and the history
                    t = max(opened, self._last or opened)
                    p.spread = self._new_spread(p.close - self._curve_yield("GHS", maturity, t), t)
                    p.path.set(t, p.close)
                self._points[maturity] = p
                self._all_points[maturity] = p
        for (tenor, maturity), days in wanted.items():
            if (tenor, maturity) not in self._bills:
                bill = self._new_bill(tenor, days, maturity)
                self._bills[(tenor, maturity)] = bill
                self._all_bills[bill.symbol] = bill
        self._bills = {k: v for k, v in self._bills.items() if k in wanted}
        self._points = {m: p for m, p in self._points.items() if m > session}

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
        points = sorted(((m - session).days, p.close) for m, p in self._points.items())
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

    def _simulate(self, a: datetime, b: datetime) -> None:
        """Every trade in the trading hours between a and b, in time
        order. Rates are per session, so a partial session gets its
        share."""
        events: list[tuple[datetime, int, Callable[[datetime], None]]] = []
        n = 0

        def schedule(rate: float, frac: float, start: datetime, span: float, fn) -> None:
            nonlocal n
            for _ in range(_poisson(rate * frac)):
                events.append((start + timedelta(seconds=random.uniform(0, span)), n, fn))
                n += 1

        for start, end, day in self._sessions.windows(a, b):
            span = (end - start).total_seconds()
            frac = span / self._sessions.length(day)
            for g in self._gov:
                if g.quoted:
                    schedule(g.rate, frac, start, span, lambda t, g=g: self._trade_gov(g, t))
            for bill in self._bills.values():
                schedule(bill.rate, frac, start, span, lambda t, bill=bill: self._trade_bill(bill, t))
            for c in self._corp:
                if c.quoted:
                    schedule(c.rate, frac, start, span, lambda t, c=c: self._trade_corp(c, t))
            eligible = [g for g in self._gov if g.quoted and g.inst.segment in ("new_gog", "ddep")]
            for g in eligible:
                schedule(SELL_BUY_BACK_TRADES_PER_DAY / len(eligible), frac, start, span,
                         lambda t, g=g: self._trade_sell_buy_back(g, t))
        for t, _, fn in sorted(events, key=lambda e: (e[0], e[1])):
            fn(t)

    def _trade_gov(self, g: _Gov, t: datetime) -> None:
        y = max(self._fair_gov(g, t) + random.gauss(0, g.dispersion), MIN_YIELD)
        size = _trade_size(g.inst.segment)
        g.day.record(y, size)
        g.close_yield += CLOSE_WEIGHT * (y - g.close_yield)
        g.path.set(t, g.close_yield)
        g.trades.add(t, size)
        self._last_trade[g.inst.symbol] = t
        self._traded.add(g.inst.symbol)

    def _trade_bill(self, b: _Bill, t: datetime) -> None:
        point = self._points[b.maturity]
        y = max(self._fair_point(point, t) + random.gauss(0, DEFAULT_YIELD_DISPERSION), MIN_YIELD)
        size = _trade_size("treasury_bill")
        b.day.record(bill_price(self._session, b.maturity, y), size)
        point.close += CLOSE_WEIGHT * (y - point.close)
        point.path.set(t, point.close)
        b.trades.add(t, size)
        self._last_trade[b.symbol] = t
        # Same-maturity bills of the other tenors share the new close.
        self._traded.update(o.symbol for o in self._bills.values() if o.maturity == b.maturity)

    def _trade_corp(self, c: _Corp, t: datetime) -> None:
        p = c.close_price * math.exp(random.gauss(0, CORPORATE_PRICE_DISPERSION))
        size = _trade_size("corporate")
        c.day.record(p, size)
        c.close_price += CLOSE_WEIGHT * (p - c.close_price)
        c.path.set(t, c.close_price)
        c.trades.add(t, size)
        self._last_trade[c.inst.symbol] = t
        self._traded.add(c.inst.symbol)

    def _trade_sell_buy_back(self, g: _Gov, t: datetime) -> None:
        y = max(self._fair_gov(g, t) + random.gauss(0, max(g.dispersion, 0.3)), MIN_YIELD)
        size = _trade_size("sell_buy_back")
        g.sbb_volume += size
        g.sbb_count += 1
        g.sbb_yield_value += y * size
        g.sbb_price_value += g.price(y, self._session) * size
        g.sbb_yield = g.sbb_yield_value / g.sbb_volume
        g.sbb_price = g.sbb_price_value / g.sbb_volume
        self._last_repo_trade[g.inst.symbol] = t
        self._repo_traded.add(g.inst.symbol)

    # ------------------------------------------------------------ history

    def _invent_history(self, end: datetime) -> None:
        """The past before `end`, run through the same trade and closing
        process as the live market: curve factors and spreads walked back
        a day at a time from their values at `end`, trades at each
        security's rate in each session, and each close path nudged so it
        lands exactly on the security's close at `end`."""
        first_day = (end - timedelta(days=round(HISTORY_YEARS * _DAYS_PER_YEAR))).date()
        days = [first_day + timedelta(days=n) for n in range((end.date() - first_day).days)]
        windows = self._sessions.windows(_midnight(first_day), end)
        factors = {ccy: _walk_back(curve.factors(), days, curve) for ccy, curve in self._curves.items()}

        def fair(currency: str, maturity: date, spreads: dict[date, float], t: datetime, day: date) -> float:
            base = yield_from_factors(factors[currency].get(day, self._curve_for(currency).factors()),
                                      _years(maturity, t))
            return max(base + spreads[day], MIN_YIELD)

        for g in self._gov:
            if not g.quoted:
                continue
            issued = _issue_date(g.inst) or end.date() - timedelta(days=DEFAULT_HISTORY_DAYS)
            spreads = _walk_back_scalar(g.spread.walk, [d for d in days if d >= issued])
            ccy = g.inst.currency if g.inst.currency in factors else "GHS"
            trades = []
            for start, stop, day in windows:
                if day >= issued:
                    trades += _times(g.rate, start, stop, self._sessions.length(day))
            path, sizes = _run_closes(
                trades, g.close_yield, CLOSE_WEIGHT, MIN_YIELD,
                lambda t: fair(ccy, g.inst.maturity_date, spreads, t, t.date())
                + random.gauss(0, g.dispersion),
                lambda: _trade_size(g.inst.segment),
            )
            _prepend(g.path, g.trades, min(_midnight(max(issued, first_day)), end), path, sizes, g.close_yield)

        for p in self._points.values():
            bills = [b for b in self._bills.values() if b.maturity == p.maturity]
            issued = min(b.issue_date for b in bills)
            spreads = _walk_back_scalar(p.spread.walk, [d for d in days if d >= issued])
            trades: list[tuple[datetime, _Bill]] = []
            for start, stop, day in windows:
                for b in bills:
                    if day >= b.issue_date:
                        trades += [(t, b) for t in _times(b.rate, start, stop, self._sessions.length(day))]
            trades.sort(key=lambda e: e[0])
            path, sizes = _run_closes(
                [t for t, _ in trades], p.close, CLOSE_WEIGHT, MIN_YIELD,
                lambda t: fair("GHS", p.maturity, spreads, t, t.date()) + random.gauss(0, DEFAULT_YIELD_DISPERSION),
                lambda: _trade_size("treasury_bill"),
            )
            _prepend(p.path, _Trades(), min(_midnight(max(issued, first_day)), end), path, [], p.close)
            # The live market hasn't run yet, so each bill's trades are all history.
            for (t, b), size in zip(trades, sizes):
                b.trades.add(t, size)

        for c in self._corp:
            if not c.quoted:
                continue
            first = max(end.date() - timedelta(days=DEFAULT_HISTORY_DAYS), first_day)
            trades = []
            for start, stop, day in windows:
                if day >= first:
                    trades += _times(c.rate, start, stop, self._sessions.length(day))
            path, sizes = _run_closes(
                trades, c.close_price, CLOSE_WEIGHT, MIN_PRICE,
                None, lambda: _trade_size("corporate"), relative_dispersion=CORPORATE_PRICE_DISPERSION,
            )
            _prepend(c.path, c.trades, _midnight(first), path, sizes, c.close_price)

    def history(self, symbol: str, interval: str, start: Optional[datetime] = None) -> list[Candle]:
        """Candles of the security's closing price (with its closing yield
        alongside, for bills and government bonds), from its invented
        history and live trades up to now, oldest first. Buckets as
        app.models.candle.bucket_start()."""
        now = self._last
        if interval == "1w":
            candles = resample(self.history(symbol, "1d"), "1w")
        elif interval == "1d":
            cached = self._daily_cache.get(symbol)
            if cached is None or cached[0] != now:
                cached = (now, self._candles(symbol, "1d", None, now))
                self._daily_cache[symbol] = cached
            candles = cached[1]
        else:
            candles = self._candles(symbol, interval, start, now)
        if start is None:
            return list(candles)
        first = bucket_start(start, interval)
        return [c for c in candles if c.window_start >= first]

    def _candles(self, symbol: str, interval: str, start: Optional[datetime], now: datetime) -> list[Candle]:
        source = self._source(symbol)
        if source is None:
            return []
        path, trades, to_price, since = source
        if not path.times:
            return []
        step = INTERVALS[interval]
        bar = bucket_start(max(start or path.times[0], path.times[0], since or path.times[0]), interval)
        last_bar = bucket_start(now, interval)
        prefix = [0]
        for s in trades.sizes:
            prefix.append(prefix[-1] + s)
        prices: dict[tuple[float, date], float] = {}

        def price(value: float, day: date) -> float:
            if to_price is None:
                return value
            key = (value, day)
            if key not in prices:
                prices[key] = round(to_price(value, day), 4)
            return prices[key]

        make = Candle
        candles = []
        while bar <= last_bar:
            end = bar + step
            i = bisect.bisect_right(path.times, bar) - 1
            j = bisect.bisect_left(path.times, end)
            opening = path.values[max(i, 0)]
            values = [opening] + path.values[max(i + 1, 0):j]
            values = [round(v, 4) for v in values]
            lo, hi = bisect.bisect_left(trades.times, bar), bisect.bisect_left(trades.times, end)
            day = bar.date()
            o, c, low, high = values[0], values[-1], min(values), max(values)
            if to_price is None:  # quoted by price: no yield
                candles.append(make(
                    symbol=symbol, interval=interval, window_start=bar, window_end=end,
                    open=o, high=high, low=low, close=c, volume=prefix[hi] - prefix[lo],
                ))
            else:  # the high price is the low yield's
                candles.append(make(
                    symbol=symbol, interval=interval, window_start=bar, window_end=end,
                    open=price(o, day), high=price(low, day), low=price(high, day), close=price(c, day),
                    volume=prefix[hi] - prefix[lo],
                    yield_open=o, yield_high=high, yield_low=low, yield_close=c,
                ))
            bar = end
        return candles

    def _source(self, symbol: str):
        """(close path, trades, yield -> price on a day, first day) for a
        security, or None. The converter is None for price-quoted
        corporates."""
        sec = self._by_symbol.get(symbol) or self._all_bills.get(symbol)
        if isinstance(sec, _Gov):
            return sec.path, sec.trades, sec.price, None
        if isinstance(sec, _Bill):
            point = self._points.get(sec.maturity)
            if point is None:  # matured
                return None
            m = sec.maturity
            return (point.path, sec.trades,
                    lambda y, d: bill_price(min(d, m - timedelta(days=1)), m, y), _midnight(sec.issue_date))
        if isinstance(sec, _Corp):
            return sec.path, sec.trades, None, None
        return None

    # ------------------------------------------------------------ reads

    def symbols(self) -> list[str]:
        """Every security the market carries now, quoted or not."""
        return (
            [g.inst.symbol for g in self._gov]
            + [b.symbol for b in sorted(self._bills.values(), key=lambda b: (b.maturity, b.tenor))]
            + [c.inst.symbol for c in self._corp]
        )

    def asset_class(self, symbol: str) -> str:
        return "bill" if symbol in self._all_bills else "bond"

    def segment(self, symbol: str) -> str:
        sec = self._by_symbol.get(symbol) or self._all_bills.get(symbol)
        if isinstance(sec, _Bill):
            return "treasury_bill"
        if sec is None:
            raise KeyError(symbol)
        return sec.inst.segment

    def is_open(self, t: Optional[datetime] = None) -> bool:
        return self._sessions.is_open(t or self._clock())

    def drain_traded(self) -> tuple[set[str], set[str]]:
        """Symbols that traded outright, and in sell/buy-backs, since the
        last call."""
        traded, repos = self._traded, self._repo_traded
        self._traded, self._repo_traded = set(), set()
        return traded, repos

    def report(self) -> FixedIncomeReport:
        self._advance(self._clock())
        sections = {
            "new_gog": [self._gov_row(g) for g in self._gov if g.inst.segment == "new_gog"],
            "ddep": [self._gov_row(g) for g in self._gov if g.inst.segment == "ddep"],
            "old_gog": [self._gov_row(g) for g in self._gov if g.inst.segment == "old_gog"],
            "treasury_bill": self._bill_rows(),
            "corporate": [self._corp_row(c) for c in self._corp],
            "sell_buy_back": self._sell_buy_back_rows(),
        }
        summaries = [_summarize(name, sections[name]) for name in REPORT_SECTIONS]
        summary = FixedIncomeSummary(
            report_date=self._session,
            sections=summaries,
            total_volume=sum(s.volume for s in summaries),
            total_trade_count=sum(s.trade_count for s in summaries),
        )
        return FixedIncomeReport(report_date=self._session, as_of=self._last, summary=summary,
                                 exchange_label=REPORT_LABEL, **sections)

    def curve(self, on: Optional[date] = None) -> GovernmentYieldCurve:
        """The GHS government curve at the close of the latest session on
        or before `on` (default: the current session, so far). Each point
        is a security's closing yield then, from the same close paths
        the charts use; tenors are measured from that session's date.
        Raises ValueError for a date after the current session."""
        self._advance(self._clock())
        if on is not None and on > self._session:
            raise ValueError(f"{on} is after the current session ({self._session})")
        day = self._session if on is None else min(
            self._sessions.session_day(_midnight(on + timedelta(days=1)) - timedelta(microseconds=1)),
            self._session,
        )
        t = min(self._sessions.bounds(day)[1], self._last)

        points: list[CurvePoint] = []
        for g in self._all_gov:
            inst = g.inst
            if not g.quoted or inst.currency != "GHS" or inst.maturity_date <= day:
                continue
            if inst.segment == "ddep" and not (inst.tenor or "").startswith("2023-"):
                continue  # GFSF: structured, not on the curve
            y = _path_at(g.path, t)
            if y is None:
                continue
            points.append(_curve_point(
                day, inst.maturity_date, y, g.price(y, day), inst.segment,
                [CurveInstrument(symbol=inst.symbol, description=inst.name, tenor=inst.tenor or "")],
            ))

        by_maturity: dict[date, list[_Bill]] = {}
        order = {tenor: n for n, tenor in enumerate(BILL_TENOR_DAYS)}
        for b in self._all_bills.values():
            if b.issue_date <= day < b.maturity:
                by_maturity.setdefault(b.maturity, []).append(b)
        for maturity, bills in by_maturity.items():
            point = self._all_points.get(maturity)
            y = None if point is None else _path_at(point.path, t)
            if y is None:
                continue
            bills.sort(key=lambda b: order[b.tenor])
            points.append(_curve_point(
                day, maturity, y, bill_price(day, maturity, y), "treasury_bill",
                [CurveInstrument(symbol=b.symbol, description=b.description, tenor=b.tenor) for b in bills],
            ))

        points.sort(key=lambda p: (p.days_to_maturity, p.segment))
        return GovernmentYieldCurve(date=day, as_of=t, exchange_label=REPORT_LABEL, points=points)

    def ticks(self) -> list[FixedIncomeTick | RepoTick]:
        """Every security as a pipeline tick (with a two-way quote around
        its fair yield where it's quoted by yield), and a RepoTick per bond
        with sell/buy-back trades this session."""
        self._advance(self._clock())
        ticks: list[FixedIncomeTick | RepoTick] = [self.tick(s) for s in self.symbols()]
        ticks += [self.repo_tick(g.inst.symbol) for g in self._gov if g.sbb_count]
        return ticks

    def tick(self, symbol: str) -> FixedIncomeTick:
        """One security's quote as of the last advance."""
        t = self._last
        sec = self._by_symbol[symbol]
        if isinstance(sec, _Gov):
            row = self._gov_row(sec)
            quote = self._quote(sec.inst.segment, sec.quoted and self._fair_gov(sec, t),
                                lambda y: sec.price(y, self._session))
            return FixedIncomeTick(
                **_common(row, t), segment=row.segment, currency=row.currency,
                exchange_label=exchange_label(row.currency),
                opening_yield=row.opening_yield, closing_yield=row.closing_yield,
                day_low_yield=row.day_low_yield, day_high_yield=row.day_high_yield,
                closing_price=row.closing_price, last_trade_at=self._last_trade.get(symbol), **quote,
            )
        if isinstance(sec, _Bill):
            row = self._bill_row(sec)
            quote = self._quote("treasury_bill", self._fair_point(self._points[sec.maturity], t),
                                lambda y: bill_price(self._session, sec.maturity, y))
            return FixedIncomeTick(
                **_common(row, t), segment="treasury_bill", currency="GHS",
                exchange_label=exchange_label("GHS"),
                opening_yield=row.opening_yield, closing_yield=row.closing_yield,
                opening_price=row.opening_price, closing_price=row.closing_price,
                day_low_price=row.day_low_price, day_high_price=row.day_high_price,
                last_trade_at=self._last_trade.get(symbol), **quote,
            )
        row = self._corp_row(sec)
        return FixedIncomeTick(
            **_common(row, t), segment="corporate", currency=sec.inst.currency,
            exchange_label=exchange_label(sec.inst.currency),
            opening_price=row.opening_price, closing_price=row.closing_price,
            day_low_price=row.day_low_price, day_high_price=row.day_high_price,
            last_trade_at=self._last_trade.get(symbol),
        )

    def repo_tick(self, symbol: str) -> RepoTick:
        g = self._by_symbol[symbol]
        row = self._sell_buy_back_row(g)
        return RepoTick(
            **_common(row, self._last), segment=row.segment, currency=g.inst.currency,
            bond_yield=repo_bond_yield(row), bond_price=row.weighted_average_price,
            last_trade_at=self._last_repo_trade.get(symbol),
        )

    @staticmethod
    def _quote(segment: str, fair, to_price: Callable[[float], float]) -> dict:
        """Bid and ask either side of the fair yield; the bid is the
        higher yield, so the lower price."""
        if not fair:
            return {}
        half = HALF_SPREAD[segment]
        bid_yield, ask_yield = fair + half, max(fair - half, MIN_YIELD)
        return dict(
            bid_yield=_r(bid_yield), ask_yield=_r(ask_yield),
            bid_price=_r(to_price(bid_yield)), ask_price=_r(to_price(ask_yield)),
        )

    def _days(self, maturity: date) -> int:
        return (maturity - self._session).days

    def _gov_row(self, g: _Gov) -> GovernmentBondQuote:
        row = dict(
            symbol=g.inst.symbol, description=g.inst.name, segment=g.inst.segment, tenor=g.inst.tenor,
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
        return GovernmentBondQuote(**row)

    def _bill_rows(self) -> list[TreasuryBillQuote]:
        order = {tenor: n for n, tenor in enumerate(BILL_TENOR_DAYS)}
        return [self._bill_row(b) for b in sorted(self._bills.values(), key=lambda b: (order[b.tenor], b.maturity))]

    def _bill_row(self, b: _Bill) -> TreasuryBillQuote:
        point = self._points[b.maturity]
        # A bill issued this session has no opening values yet.
        opening = None if b.issue_date == self._session else point.prev_close
        return TreasuryBillQuote(
            symbol=b.symbol, description=b.description, tenor=b.tenor,
            maturity_date=b.maturity, days_to_maturity=self._days(b.maturity),
            opening_price=_r(bill_price(self._session, b.maturity, opening)) if opening is not None else None,
            opening_yield=_r(opening),
            closing_price=_r(bill_price(self._session, b.maturity, point.close)),
            closing_yield=_r(point.close),
            volume=b.day.volume or None, trade_count=b.day.trade_count or None,
            day_low_price=_r(b.day.low), day_high_price=_r(b.day.high),
        )

    def _corp_row(self, c: _Corp) -> CorporateBondQuote:
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
        return CorporateBondQuote(**row)

    def _sell_buy_back_rows(self) -> list[SellBuyBackQuote]:
        return [self._sell_buy_back_row(g) for g in self._gov if g.inst.segment in ("new_gog", "ddep")]

    def _sell_buy_back_row(self, g: _Gov) -> SellBuyBackQuote:
        return SellBuyBackQuote(
            symbol=g.inst.symbol, description=g.inst.name, segment=g.inst.segment, tenor=g.inst.tenor,
            maturity_date=g.inst.maturity_date, days_to_maturity=self._days(g.inst.maturity_date),
            volume=g.sbb_volume or None, trade_count=g.sbb_count or None,
            yield_=_r(g.sbb_yield), weighted_average_price=_r(g.sbb_price),
        )


# ---------------------------------------------------------------- helpers

def repo_bond_yield(row: SellBuyBackQuote) -> Optional[float]:
    """The report's sell/buy-back "Yield", or None where the column holds
    the price instead (quirk 12: 79.96 and 86.80 on the USD DDE bonds).
    Better unmapped than a price in a yield field."""
    if row.yield_ is not None and row.yield_ == row.weighted_average_price:
        return None
    return row.yield_


def _common(row, t: datetime) -> dict:
    return dict(
        symbol=row.symbol, name=row.description, maturity_date=row.maturity_date,
        volume=row.volume or 0, trade_count=row.trade_count or 0, timestamp=t,
    )


def _walk_back(end: tuple[float, float, float], days: list[date], curve: YieldCurve) -> dict[date, tuple]:
    """Curve factors for each of `days` (ascending), walked back a day at
    a time from `end` on the curve's own dynamics. An Ornstein-Uhlenbeck
    walk is time-reversible, so stepping it backwards from today is a
    fair draw of the past."""
    walks = [OrnsteinUhlenbeck(v, f.mean, f.daily_vol, f.half_life_days)
             for v, f in zip(end, (curve.level, curve.slope, curve.curvature))]
    out = {}
    for day in reversed(days):
        out[day] = tuple(w.step(1) for w in walks)
    return out


def _walk_back_scalar(walk: OrnsteinUhlenbeck, days: list[date]) -> dict[date, float]:
    w = OrnsteinUhlenbeck(walk.value, walk.mean, walk.daily_vol, walk.half_life_days)
    return {day: w.step(1) for day in reversed(days)}


def _times(rate: float, start: datetime, stop: datetime, session_seconds: float) -> list[datetime]:
    span = (stop - start).total_seconds()
    n = _poisson(rate * span / session_seconds)
    return sorted(start + timedelta(seconds=random.uniform(0, span)) for _ in range(n))


def _run_closes(
    trades: list[datetime],
    end_value: float,
    weight: float,
    floor: float,
    trade_value: Optional[Callable[[datetime], float]],
    trade_size: Callable[[], int],
    relative_dispersion: float = 0.0,
) -> tuple[list[tuple[datetime, float]], list[int]]:
    """Run the end-of-day closing process over `trades` (each moves the
    close `weight` of the way to its price or yield), then shift the
    path so it finishes exactly at `end_value`: the k-th of n closes by
    k/n of the gap, so no flat stretch tilts. Without `trade_value`
    (price-quoted) trades print around the current close."""
    if not trades:
        return [], []
    value = trade_value(trades[0]) if trade_value else end_value
    closes, sizes = [], []
    for t in trades:
        traded = trade_value(t) if trade_value else value * math.exp(random.gauss(0, relative_dispersion))
        value += weight * (max(traded, floor) - value)
        closes.append((t, value))
        sizes.append(trade_size())
    gap, n = closes[-1][1] - end_value, len(closes)
    return [(t, max(v - gap * (k + 1) / n, floor)) for k, (t, v) in enumerate(closes)], sizes


def _prepend(path: _Path, trades: _Trades, begin: datetime, closes, sizes, end_value: float) -> None:
    """Put the invented past in front of a path that starts at the live
    start. Before its first trade a security sits at its first close
    (or, if it never traded, at today's)."""
    first_value = closes[0][1] if closes else end_value
    past_times = [begin] + [t for t, _ in closes]
    past_values = [first_value] + [v for _, v in closes]
    path.times[:0] = past_times
    path.values[:0] = past_values
    if sizes:
        trades.times[:0] = [t for t, _ in closes]
        trades.sizes[:0] = sizes


def _path_at(path: _Path, t: datetime) -> Optional[float]:
    """The close in force at `t`, or None before the path starts."""
    i = bisect.bisect_right(path.times, t) - 1
    return path.values[i] if i >= 0 else None


def _curve_point(day: date, maturity: date, y: float, price: float, segment: str,
                 instruments: list[CurveInstrument]) -> CurvePoint:
    days = (maturity - day).days
    return CurvePoint(
        tenor_years=round(days / _DAYS_PER_YEAR, 4), days_to_maturity=days, yield_=_r(y),
        closing_price=_r(price), segment=segment, maturity_date=maturity, instruments=instruments,
    )


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
