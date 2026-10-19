"""
European FX options against the cedi, priced with QuantLib's
Garman-Kohlhagen model (Black-Scholes for currencies: the foreign
currency's interest rate plays the part of a dividend yield).

A call is the right to buy the foreign currency at the strike (cedis per
unit) on the expiry date; a put, the right to sell it. Rates are set up
exactly as the synthetic forwards on /fx (app/fx/forwards.py) so the two
agree: the cedi rate simple on ACT/364, the foreign rate simple on its
benchmark's day count. The forward this module implies is therefore the
/fx forward for the same day.

Volatility is the one input with no market source: Ghana has no traded
cedi options, so no implied volatility is published. historical_vol()
measures how much the Bank of Ghana fix has actually moved, which the
caller offers as a default the user can override.

QuantLib's evaluation date is process-wide state. Each function sets it
and prices synchronously, with no await in between, so on the event
loop's single thread one calculation can't see another's date.
"""

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional, Sequence

import QuantLib as ql

from app.fx.forwards import GHS_BASIS

Kind = Literal["call", "put"]

# BoG fixes on Ghana business days, about 252 a year.
FIXES_PER_YEAR = 252
# Historical windows, in fixes, by the label the page shows.
VOL_WINDOWS: dict[str, int] = {"1M": 21, "3M": 63, "6M": 126, "1Y": 252}
# Implied volatility is searched for between these (annual, as decimals).
MIN_VOL, MAX_VOL = 1e-4, 5.0


class OptionError(ValueError):
    """Inputs that can't be priced, or a premium no volatility explains."""


@dataclass(frozen=True)
class Market:
    """Everything an FX option's price depends on besides its terms."""
    spot: float            # cedis per unit of the foreign currency
    vol: float             # % a year
    ghs_rate: float        # % a year, simple, ACT/364
    foreign_rate: float    # % a year, simple, ACT/foreign_basis
    foreign_basis: int     # 360 or 365


@dataclass(frozen=True)
class Valuation:
    """Per unit of the foreign currency, in cedis. Greeks are scaled to
    the moves the page talks about: vega per 1 point of volatility, theta
    per calendar day, rho per 1 percentage point of rate."""
    forward: float
    premium: float
    delta: float           # change in premium per 1 cedi rise in spot
    gamma: float           # change in delta per 1 cedi rise in spot
    vega: float
    theta: float
    rho_ghs: float
    rho_foreign: float


def _date(d: date) -> ql.Date:
    return ql.Date(d.day, d.month, d.year)


def _check(strike: float, valuation_date: date, expiry: date, market: Market) -> None:
    if expiry <= valuation_date:
        raise OptionError("The expiry must be after the valuation date")
    if strike <= 0 or market.spot <= 0:
        raise OptionError("The strike and spot must be positive")
    if market.vol <= 0:
        raise OptionError("Volatility must be above zero")


def _process(valuation_date: date, market: Market, vol: Optional[float] = None) -> ql.GarmanKohlagenProcess:
    today = _date(valuation_date)
    ql.Settings.instance().evaluationDate = today
    ghs = ql.FlatForward(today, market.ghs_rate / 100, ql.Actual364(), ql.Simple, ql.Annual)
    dc = ql.Actual360() if market.foreign_basis == 360 else ql.Actual365Fixed()
    foreign = ql.FlatForward(today, market.foreign_rate / 100, dc, ql.Simple, ql.Annual)
    sigma = ql.BlackConstantVol(today, ql.NullCalendar(), (market.vol if vol is None else vol) / 100,
                                ql.Actual365Fixed())
    return ql.GarmanKohlagenProcess(
        ql.QuoteHandle(ql.SimpleQuote(market.spot)),
        ql.YieldTermStructureHandle(foreign),   # the "dividend" curve
        ql.YieldTermStructureHandle(ghs),       # the domestic, risk-free curve
        ql.BlackVolTermStructureHandle(sigma),
    )


def _option(kind: Kind, strike: float, expiry: date, process) -> ql.VanillaOption:
    payoff = ql.PlainVanillaPayoff(ql.Option.Call if kind == "call" else ql.Option.Put, strike)
    option = ql.VanillaOption(payoff, ql.EuropeanExercise(_date(expiry)))
    option.setPricingEngine(ql.AnalyticEuropeanEngine(process))
    return option


def forward(valuation_date: date, expiry: date, market: Market) -> float:
    """Cedis per unit for delivery on `expiry`, by covered interest parity:
    the same number /fx/forwards gives for that day."""
    days = (expiry - valuation_date).days
    return (market.spot * (1 + market.ghs_rate / 100 * days / GHS_BASIS)
            / (1 + market.foreign_rate / 100 * days / market.foreign_basis))


def value(kind: Kind, strike: float, valuation_date: date, expiry: date, market: Market) -> Valuation:
    _check(strike, valuation_date, expiry, market)
    option = _option(kind, strike, expiry, _process(valuation_date, market))
    return Valuation(
        forward=forward(valuation_date, expiry, market),
        premium=option.NPV(),
        delta=option.delta(),
        gamma=option.gamma(),
        vega=option.vega() / 100,
        theta=option.thetaPerDay(),
        rho_ghs=option.rho() / 100,
        rho_foreign=option.dividendRho() / 100,
    )


def implied_vol(kind: Kind, strike: float, valuation_date: date, expiry: date, market: Market,
                premium: float) -> float:
    """The volatility (% a year) at which the option is worth `premium`
    cedis per unit. OptionError if no volatility gets there: a premium
    below what the option is worth with no uncertainty at all, or above
    the most it can be worth."""
    _check(strike, valuation_date, expiry, market)
    process = _process(valuation_date, market)
    option = _option(kind, strike, expiry, process)
    try:
        return option.impliedVolatility(premium, process, 1e-7, 500, MIN_VOL, MAX_VOL) * 100
    except RuntimeError as e:
        floor = _option(kind, strike, expiry, _process(valuation_date, market, vol=MIN_VOL * 100)).NPV()
        if premium <= floor:
            raise OptionError(
                f"That price is at or below what the option is worth with no uncertainty at all "
                f"(GH₵{floor:,.4f} per unit), so no volatility explains it") from e
        raise OptionError("No volatility between 0.01% and 500% gives that price") from e


def values_across(kind: Kind, strike: float, valuation_date: date, expiry: date, market: Market,
                  spots: Sequence[float]) -> list[float]:
    """The premium per unit if spot were each of `spots` today, all else equal."""
    _check(strike, valuation_date, expiry, market)
    quote = ql.SimpleQuote(market.spot)
    process = _process(valuation_date, market)
    process = ql.GarmanKohlagenProcess(ql.QuoteHandle(quote), process.dividendYield(), process.riskFreeRate(),
                                       process.blackVolatility())
    option = _option(kind, strike, expiry, process)
    out = []
    for s in spots:
        quote.setValue(s)
        out.append(option.NPV())
    return out


def likely_range(fwd: float, vol: float, days: int, z: float = 1.645) -> tuple[float, float]:
    """Where the rate is likely to be in `days`, under the same model the
    premium uses: lognormal around the forward with volatility `vol` (%).
    The default z gives the middle 90%: a 1-in-20 chance of each end
    being passed."""
    s = vol / 100 * math.sqrt(days / 365)
    centre = math.log(fwd) - s * s / 2
    return math.exp(centre - z * s), math.exp(centre + z * s)


def historical_vol(mids: Sequence[float], window: int) -> Optional[float]:
    """How much the fix has moved over the last `window` fixes: the
    standard deviation of daily log changes, annualised on 252 fixes a
    year, in %. None if there are fewer than `window` changes."""
    if len(mids) < window + 1:
        return None
    recent = mids[-(window + 1):]
    changes = [math.log(b / a) for a, b in zip(recent, recent[1:])]
    mean = sum(changes) / len(changes)
    variance = sum((c - mean) ** 2 for c in changes) / (len(changes) - 1)
    return math.sqrt(variance * FIXES_PER_YEAR) * 100


def window_for(days: int) -> str:
    """The historical window that best matches an option's life: the
    closest of 1M, 3M, 6M and 1Y by calendar days."""
    lengths = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365}
    return min(lengths, key=lambda k: abs(lengths[k] - days))
