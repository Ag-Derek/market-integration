"""
Trade tickets: a trader writes up a trade agreed with a counterparty,
files it with the depository, and both parties are notified of the
outcome. The flow is in desk.py, storage in store.py, the depository
connector in app/connectors/depository.py.

build_ticket() turns what the trader fills in into a ticket: it looks up
the instrument, settles it on the exchange calendar and works out the
amounts. Equities: consideration = shares x price. Bills and bonds: the
invoice from app/bond_math (principal at the clean price, plus accrued
interest on bonds), so a ticket and the bond calculator agree. Bonds the
bond math can't price yet take face x price / 100 and the accrued
interest the trader enters. Fees and levies aren't on tickets yet.
"""

import uuid
from datetime import date, datetime
from typing import Mapping

from app import config
from app.bond_math import SETTLEMENT_LAG, settlement_date
from app.bond_math.bonds import Bond, supports
from app.bond_math.calculator import Security, calculate
from app.bond_math.conventions import BusinessCalendar
from app.models.instrument import Instrument
from app.models.ticket import Ticket, TicketRequest


class TicketError(ValueError):
    """A ticket request that can't be carried out; `status_code` is the
    HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def new_ticket_id() -> str:
    return "TKT-" + uuid.uuid4().hex[:12].upper()


def build_ticket(
    request: TicketRequest,
    trader: str,
    *,
    instruments: Mapping[str, Instrument],
    calendar: BusinessCalendar,
    today: date,
    now: datetime,
    equity_settlement_lag: int = config.EQUITY_SETTLEMENT_LAG,
    complete: bool = True,
) -> Ticket:
    """A new draft ticket. Raises TicketError for an unknown or
    non-trading instrument, a counterparty who is the trader, a trade
    date in the future or on a non-trading day, a fractional number of
    shares, a bill the bond math can't price, or accrued interest given
    where it is calculated or nil.

    A bond the bond math can't price takes the accrued interest from the
    request. Without one it is refused, unless `complete` is False (a
    preview): then the ticket says accrued_source="required"."""
    symbol = request.symbol.strip().upper()
    instrument = instruments.get(symbol)
    if instrument is None:
        raise TicketError(f"Unknown instrument '{symbol}'")
    if instrument.status != "active":
        raise TicketError(f"{symbol} is {instrument.status}, so it can't be traded")
    counterparty = request.counterparty.strip()
    if counterparty == trader:
        raise TicketError("The counterparty can't be the trader")

    trade_date = request.trade_date or today
    if trade_date > today:
        raise TicketError(f"Trade date {trade_date} is in the future")
    if not calendar.is_trading_day(trade_date):
        raise TicketError(f"{trade_date} isn't a trading day")

    accrued, days_accrued, accrued_source = 0.0, None, "none"
    if instrument.asset_class != "bond" and request.accrued_interest is not None:
        raise TicketError(f"{symbol} is {'an equity' if instrument.asset_class == 'equity' else 'a bill'}: no accrued interest")
    if instrument.asset_class == "equity":
        if request.quantity != int(request.quantity):
            raise TicketError("Equity quantity must be a whole number of shares")
        settle = settlement_date(trade_date, calendar, equity_settlement_lag)
        consideration = request.quantity * request.price
    else:
        settle = settlement_date(trade_date, calendar, SETTLEMENT_LAG)
        if instrument.maturity_date is not None and settle >= instrument.maturity_date:
            # A bill in its last days can't settle T+2; GFIM allows T+0 bilaterally.
            settle = trade_date
        if instrument.asset_class == "bond" and not supports(instrument):
            # Corporates, GFSF and USD DDE bonds: no term sheet to price
            # from yet, so the principal is face x price and the accrued
            # interest is what the parties agreed.
            consideration = request.quantity * request.price / 100
            if request.accrued_interest is not None:
                accrued, accrued_source = request.accrued_interest, "entered"
            elif complete:
                raise TicketError(
                    f"Accrued interest on {symbol} can't be calculated yet (no term sheet); "
                    "enter the amount agreed with the counterparty"
                )
            else:
                accrued_source = "required"
        else:
            if request.accrued_interest is not None:
                raise TicketError(f"Accrued interest on {symbol} is calculated from its terms; leave it empty")
            try:
                sec = (
                    Security(instrument.maturity_date) if instrument.asset_class == "bill"
                    else Security(instrument.maturity_date, Bond.from_instrument(instrument))
                )
                invoice = calculate(sec, settle, request.quantity, price=request.price).invoice
            except (ValueError, ArithmeticError, TypeError) as e:
                raise TicketError(f"Can't price a ticket for {symbol}: {e}")
            consideration = invoice.principal
            if sec.bond is not None:
                accrued, days_accrued, accrued_source = invoice.accrued, invoice.days_accrued, "calculated"

    consideration = round(consideration, 2)
    accrued = round(accrued, 2)
    return Ticket(
        id=new_ticket_id(),
        trader=trader,
        counterparty=counterparty,
        side=request.side,
        trader_account=request.trader_account,
        counterparty_account=request.counterparty_account,
        symbol=symbol,
        name=instrument.name,
        isin=instrument.isin,
        asset_class=instrument.asset_class,
        currency=instrument.currency,
        quantity=request.quantity,
        price=request.price,
        trade_date=trade_date,
        settlement_date=settle,
        consideration=consideration,
        accrued_interest=accrued,
        days_accrued=days_accrued,
        accrued_source=accrued_source,
        net_amount=round(consideration + accrued, 2),
        notes=request.notes,
        created_at=now,
        updated_at=now,
    )
