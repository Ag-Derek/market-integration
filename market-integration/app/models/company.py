"""
Company information we maintain ourselves for an equity's description
page (#55): profile, officers and annual financials. Not market data,
and not in the instrument master; the source of truth is
data/company_profiles.json (see app/company/).

Every maintained value carries where it came from and as of when: a
Sourced value is {value, source, as_of}. A value without a source and
date is rejected when the seed loads; a value nobody has filled in yet
is all null, so the API always returns every field.
"""

from datetime import date
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

T = TypeVar("T")


class Sourced(BaseModel, Generic[T]):
    """One maintained value, where it came from (an annual report, the
    GSE listing, the company website...) and the date it was true."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Optional[T] = None
    source: Optional[str] = None
    as_of: Optional[date] = None

    @model_validator(mode="after")
    def _value_needs_provenance(self) -> "Sourced":
        if self.value is not None and (not self.source or self.as_of is None):
            raise ValueError("a value needs its source and as_of date")
        return self


# The profile's maintained fields, in display order.
PROFILE_FIELDS = (
    "description_short", "description_full", "sector", "industry", "website",
    "headquarters_city", "headquarters_country", "employees", "shares_outstanding",
    "listing_date", "registrar",
)


class CompanyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description_short: Sourced[str] = Sourced()
    description_full: Sourced[str] = Sourced()
    sector: Sourced[str] = Sourced()
    industry: Sourced[str] = Sourced()
    website: Sourced[str] = Sourced()
    headquarters_city: Sourced[str] = Sourced()
    headquarters_country: Sourced[str] = Sourced()
    employees: Sourced[int] = Sourced()
    shares_outstanding: Sourced[int] = Sourced()
    listing_date: Sourced[date] = Sourced()
    registrar: Sourced[str] = Sourced()

    @field_validator("employees", "shares_outstanding")
    @classmethod
    def _positive(cls, v: Sourced) -> Sourced:
        if v.value is not None and v.value <= 0:
            raise ValueError(f"must be positive, got {v.value}")
        return v


class CompanyOfficer(BaseModel):
    """A director or executive. Name and role come from one source
    (the annual report, a GSE notice), so provenance is per officer."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    display_order: int = Field(ge=0)
    source: str = Field(min_length=1)
    as_of: date


# A fiscal year's maintained fields.
FINANCIAL_FIELDS = (
    "revenue", "net_income", "eps", "book_value", "dividend_per_share", "dividend_payment_dates",
)


class FinancialYear(BaseModel):
    """One fiscal year. Amounts are in `currency` units (not thousands);
    book_value is total shareholders' equity; eps and dividend_per_share
    are per share. Dividends usually come from a separate announcement
    from the results, so each figure has its own provenance."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    fiscal_year: int = Field(ge=1900, le=2100)
    currency: str = "GHS"
    revenue: Sourced[float] = Sourced()
    net_income: Sourced[float] = Sourced()
    eps: Sourced[float] = Sourced()
    book_value: Sourced[float] = Sourced()
    dividend_per_share: Sourced[float] = Sourced()
    dividend_payment_dates: Sourced[list[date]] = Sourced()


class Company(BaseModel):
    """One seed entry: everything maintained about one listed company."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    profile: CompanyProfile = CompanyProfile()
    officers: list[CompanyOfficer] = []
    financials: list[FinancialYear] = []

    @model_validator(mode="after")
    def _one_row_per_year(self) -> "Company":
        years = [f.fiscal_year for f in self.financials]
        if len(years) != len(set(years)):
            raise ValueError(f"{self.symbol}: a fiscal year appears twice in financials")
        return self
