"""
Figures calculated from the live quote and the maintained company data,
for the description page. A placeholder set until DES 2 defines the
page's calculated figures; it owns this module.

Each figure is {value, formula, inputs_as_of}: calculated values have
no source of their own, so the dates of the inputs stand in for it.
A figure is null when an input is missing, or when it would mix
currencies (a company reporting in USD while trading in GHS).

  price            session VWAP, the GSE's official price (as the UI
                   headlines it), in GHS
  market_cap       price x shares outstanding
  pe_ratio         price / EPS of the latest fiscal year (positive EPS only)
  dividend_yield   latest dividend per share / price, in %
  price_to_book    market cap / book value (total shareholders' equity)
"""

from datetime import date, datetime
from typing import Optional

from app.models.company import Company, FinancialYear
from app.models.market_data import MarketData

QUOTE_CURRENCY = "GHS"


def _figure(value: Optional[float], formula: str, **inputs: Optional["date | datetime"]) -> dict:
    return {
        "value": None if value is None else round(value, 4),
        "formula": formula,
        "inputs_as_of": {name: as_of.isoformat() if as_of else None for name, as_of in inputs.items()},
    }


def calculated_figures(company: Optional[Company], quote: Optional[MarketData]) -> dict:
    price = quote.vwap if quote is not None else None
    priced_at = quote.timestamp if quote is not None else None
    profile = company.profile if company is not None else None
    shares = profile.shares_outstanding if profile is not None else None
    latest: Optional[FinancialYear] = (
        max(company.financials, key=lambda f: f.fiscal_year) if company and company.financials else None
    )
    # Ratios against the price only if the year is reported in its currency.
    comparable = latest is not None and latest.currency == QUOTE_CURRENCY

    market_cap = price * shares.value if price is not None and shares and shares.value else None
    eps = latest.eps if comparable else None
    dps = latest.dividend_per_share if comparable else None
    book = latest.book_value if comparable else None

    return {
        "currency": QUOTE_CURRENCY,
        "fiscal_year": latest.fiscal_year if latest else None,
        "price": _figure(price, "session VWAP (GSE closing price)", price=priced_at),
        "market_cap": _figure(market_cap, "price x shares outstanding",
                              price=priced_at, shares_outstanding=shares.as_of if shares else None),
        "pe_ratio": _figure(
            price / eps.value if price is not None and eps and eps.value and eps.value > 0 else None,
            "price / EPS (latest fiscal year)", price=priced_at, eps=eps.as_of if eps else None),
        "dividend_yield": _figure(
            dps.value / price * 100 if price and dps and dps.value is not None else None,
            "dividend per share / price x 100 (latest fiscal year)",
            price=priced_at, dividend_per_share=dps.as_of if dps else None),
        "price_to_book": _figure(
            market_cap / book.value if market_cap is not None and book and book.value and book.value > 0 else None,
            "market cap / book value (latest fiscal year)",
            price=priced_at, shares_outstanding=shares.as_of if shares else None,
            book_value=book.as_of if book else None),
    }
