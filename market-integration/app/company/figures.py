"""
Figures calculated for an equity's description page (#62): valuation
from the price and the maintained financials, and performance from the
stored daily candles (performance.py). Nothing here is maintained by
hand; it all follows the price data.

Valuation uses the GSE's official price, the session VWAP, as the quote
cards headline it and as the GSE computes market capitalisation; with
no live quote, the latest daily close stands in. Ratios use the latest
fiscal year that has the input:

  market_cap        price x shares outstanding
  pe_ratio          price / trailing EPS (latest fiscal year)
  price_to_book     market cap / book value (total shareholders' equity)
  dividend_yield    latest dividend per share / price, in %
  return_on_equity  net income / book value, in %
  dividend_growth   compound annual growth of dividend per share over
                    up to 5 fiscal years, in %

plus the last cash dividend: the latest fiscal year's dividend per
share and when it was (or will be) paid.

Each is {value, formula, inputs_as_of, reason}: calculated values have
no source, so their inputs' dates stand in for one, and `reason` says
why a value is null ("negative earnings" for a loss-making company's
P/E, which the page shows as "n/a"). Ratios are never taken between
different currencies: a company reporting in USD while trading in GHS
gets nulls rather than a mixed figure.
"""

from datetime import date, datetime
from typing import AsyncIterator, Callable, Optional, Sequence

from app.company import performance
from app.company.performance import Day
from app.models.company import Company, FinancialYear
from app.models.market_data import MarketData
from app.models.tick import Tick

QUOTE_CURRENCY = "GHS"
INDEX_NAME = "GSE-CI (simulated)"


def _figure(value: Optional[float], formula: str, reason: Optional[str] = None,
            **inputs: Optional["date | datetime"]) -> dict:
    return {
        "value": None if value is None else round(value, 4),
        "formula": formula,
        "inputs_as_of": {name: as_of.isoformat() if as_of else None for name, as_of in inputs.items()},
        "reason": None if value is not None else reason,
    }


def market_cap(price: Optional[float], shares: Optional[int]) -> Optional[float]:
    return round(price * shares, 2) if price is not None and shares else None


def with_market_cap(feed: AsyncIterator[Tick], shares_for: Callable[[str], Optional[int]]) -> AsyncIterator[Tick]:
    """Set each equity tick's market_cap to VWAP x shares outstanding
    (null where shares outstanding isn't known), before it reaches the
    processor and the quote cards. Other ticks pass through."""
    async def enriched():
        async for tick in feed:
            if isinstance(tick, MarketData):
                cap = market_cap(tick.vwap, shares_for(tick.symbol))
                if cap != tick.market_cap:
                    tick = tick.model_copy(update={"market_cap": cap})
            yield tick
    return enriched()


def _latest_with(financials: Sequence[FinancialYear], field: str) -> Optional[FinancialYear]:
    """The latest fiscal year in the quote's currency that has `field`."""
    years = [f for f in financials if f.currency == QUOTE_CURRENCY and getattr(f, field).value is not None]
    return max(years, key=lambda f: f.fiscal_year) if years else None


def _missing(field: str, financials: Sequence[FinancialYear]) -> str:
    if any(getattr(f, field).value is not None for f in financials):
        return f"{field.replace('_', ' ')} only reported in another currency"
    return f"no {field.replace('_', ' ')} recorded"


def dividends_paid(financials: Sequence[FinancialYear]) -> tuple[list[tuple[date, float]], bool]:
    """(payment date, amount per share) for every recorded dividend in
    the quote's currency, and whether any amount had to be estimated: a
    year's dividend per share paid on several dates is split equally
    between them, since only the year's total is recorded."""
    paid, estimated = [], False
    for f in financials:
        dps, dates = f.dividend_per_share.value, f.dividend_payment_dates.value
        if f.currency != QUOTE_CURRENCY or not dps or not dates:
            continue
        estimated = estimated or len(dates) > 1
        paid += [(on, dps / len(dates)) for on in dates]
    return paid, estimated


def last_dividend(financials: Sequence[FinancialYear], today: date) -> Optional[dict]:
    """The latest fiscal year that paid a cash dividend: its dividend per
    share (the year's total; interim and final aren't recorded apart),
    the last of its payment dates on or before `today` and the next one
    after it. None if no year in the quote's currency paid one."""
    years = [f for f in financials if f.currency == QUOTE_CURRENCY and f.dividend_per_share.value]
    if not years:
        return None
    year = max(years, key=lambda f: f.fiscal_year)
    dates = sorted(year.dividend_payment_dates.value or [])
    paid = [d for d in dates if d <= today]
    due = [d for d in dates if d > today]
    return {
        "fiscal_year": year.fiscal_year,
        "dividend_per_share": year.dividend_per_share.value,
        "paid_on": paid[-1].isoformat() if paid else None,
        "payable_on": due[0].isoformat() if due else None,
        "as_of": year.dividend_per_share.as_of.isoformat(),
    }


DIVIDEND_GROWTH_YEARS = 5


def dividend_growth(financials: Sequence[FinancialYear]) -> dict:
    """Compound annual growth of dividend per share, from the earliest
    year that paid one in the DIVIDEND_GROWTH_YEARS before the latest
    paying year, to that latest year. Shorter spans are allowed (and
    reported in `years`); growth from no dividend isn't defined."""
    paying = sorted(
        (f for f in financials if f.currency == QUOTE_CURRENCY and f.dividend_per_share.value),
        key=lambda f: f.fiscal_year,
    )
    formula = "(latest / earliest dividend per share) ^ (1 / years) - 1"
    end = paying[-1] if paying else None
    start = next((f for f in paying if end and end.fiscal_year - f.fiscal_year <= DIVIDEND_GROWTH_YEARS), None)
    if end is None or start is end:
        reason = "fewer than two years with a dividend recorded"
        return {**_figure(None, formula, reason), "from_year": None, "to_year": None, "years": None}
    years = end.fiscal_year - start.fiscal_year
    growth = ((end.dividend_per_share.value / start.dividend_per_share.value) ** (1 / years) - 1) * 100
    return {
        **_figure(growth, formula, None,
                  from_dividend=start.dividend_per_share.as_of, to_dividend=end.dividend_per_share.as_of),
        "from_year": start.fiscal_year, "to_year": end.fiscal_year, "years": years,
    }


def calculated_figures(
    company: Optional[Company],
    quote: Optional[MarketData],
    daily: Sequence[Day] = (),
    index: Sequence[Day] = (),
    today: Optional[date] = None,
) -> dict:
    """Everything calculated for the description page. `daily` is the
    symbol's daily candles and `index` the GSE-CI's, oldest first."""
    today = today or (quote.timestamp.date() if quote else date.today())
    financials = list(company.financials) if company else []
    shares = company.profile.shares_outstanding if company else None

    if quote is not None:
        price, priced_at, basis = quote.vwap, quote.timestamp, "session VWAP (GSE closing price)"
    elif daily:
        price, priced_at, basis = daily[-1].close, daily[-1].on, "latest daily close (no live quote)"
    else:
        price, priced_at, basis = None, None, "no price"
    no_price = "no price"

    cap = market_cap(price, shares.value if shares else None)
    cap_reason = no_price if price is None else "no shares outstanding recorded"

    eps_year = _latest_with(financials, "eps")
    eps = eps_year.eps.value if eps_year else None
    if price is None:
        pe, pe_reason = None, no_price
    elif eps is None:
        pe, pe_reason = None, _missing("eps", financials)
    elif eps <= 0:
        pe, pe_reason = None, "negative earnings"
    else:
        pe, pe_reason = price / eps, None

    book_year = _latest_with(financials, "book_value")
    book = book_year.book_value.value if book_year else None
    if cap is None:
        pb, pb_reason = None, cap_reason
    elif book is None:
        pb, pb_reason = None, _missing("book_value", financials)
    elif book <= 0:
        pb, pb_reason = None, "negative book value"
    else:
        pb, pb_reason = cap / book, None

    dps_year = _latest_with(financials, "dividend_per_share")
    dps = dps_year.dividend_per_share.value if dps_year else None
    if price is None:
        dy, dy_reason = None, no_price
    elif dps is None:
        dy, dy_reason = None, _missing("dividend_per_share", financials)
    else:
        dy, dy_reason = dps / price * 100, None

    roe_year = next(
        (f for f in sorted(financials, key=lambda f: -f.fiscal_year)
         if f.net_income.value is not None and f.book_value.value is not None), None,
    )
    if roe_year is None:
        roe, roe_reason = None, "no year with both net income and book value"
    elif roe_year.book_value.value <= 0:
        roe, roe_reason = None, "negative book value"
    else:
        roe, roe_reason = roe_year.net_income.value / roe_year.book_value.value * 100, None

    dividends, estimated = dividends_paid(financials)
    total_return = performance.total_return_12m(daily, today, dividends)
    if total_return is not None:
        total_return["dividends_recorded"] = bool(dividends)
        total_return["dividend_amounts_estimated"] = estimated

    beta = performance.beta(daily, index, today)
    if beta is not None:
        beta["index"] = INDEX_NAME

    def as_of(year: Optional[FinancialYear], field: str):
        return getattr(year, field).as_of if year else None

    return {
        "currency": QUOTE_CURRENCY,
        "price": _figure(price, basis, no_price, price=priced_at),
        "market_cap": _figure(cap, "price x shares outstanding", cap_reason,
                              price=priced_at, shares_outstanding=shares.as_of if shares else None),
        "pe_ratio": _figure(pe, "price / trailing EPS", pe_reason,
                            price=priced_at, eps=as_of(eps_year, "eps")),
        "price_to_book": _figure(pb, "market cap / book value", pb_reason,
                                 price=priced_at, shares_outstanding=shares.as_of if shares else None,
                                 book_value=as_of(book_year, "book_value")),
        "dividend_yield": _figure(dy, "dividend per share / price x 100", dy_reason,
                                  price=priced_at, dividend_per_share=as_of(dps_year, "dividend_per_share")),
        "return_on_equity": _figure(roe, "net income / book value x 100", roe_reason,
                                    net_income=as_of(roe_year, "net_income"),
                                    book_value=as_of(roe_year, "book_value")),
        "dividend_growth": dividend_growth(financials),
        "last_dividend": last_dividend(financials, today),
        "fiscal_years": {
            "eps": eps_year.fiscal_year if eps_year else None,
            "book_value": book_year.fiscal_year if book_year else None,
            "dividend_per_share": dps_year.fiscal_year if dps_year else None,
            "return_on_equity": roe_year.fiscal_year if roe_year else None,
        },
        "performance": {
            "basis": "daily closes (last trade of each day)",
            "one_day": performance.one_day_change(daily),
            "ytd": performance.ytd_change(daily, today),
            "week52": performance.week52_range(daily, today),
            "total_return_12m": total_return,
        },
        "beta": beta,
    }
