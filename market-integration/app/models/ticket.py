"""
Trade tickets: a trade a trader has agreed with a counterparty, written
up so it can be filed with the depository (app/tickets/).

A ticket is written from the trader's side: `side` is what the trader
did, and buyer/seller follow from it. Amounts are in the instrument's
currency. For equities `quantity` is shares and `price` is per share;
for bills and bonds `quantity` is face value and `price` is the clean
price per 100 face.
"""

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.models.instrument import AssetClass

Side = Literal["buy", "sell"]
# draft:    written, not yet sent to the depository
# filing:   being handed to the depository right now
# sent:     emailed to the depository; waiting for its reply, which the
#           trader records (POST /tickets/{id}/confirmation)
# filed:    the depository accepted it (depository_reference is set)
# rejected: the depository refused it (rejection_reason says why)
TicketStatus = Literal["draft", "filing", "sent", "filed", "rejected"]
# Where a ticket's accrued interest came from. none: equities and bills;
# calculated: the bond math; entered: typed in by the trader, for a bond
# the bond math can't price; required: such a bond with none entered yet
# (previews only: such a ticket can't be saved).
AccruedSource = Literal["none", "calculated", "entered", "required"]


class TicketRequest(BaseModel):
    """POST /tickets: what the trader fills in. Everything else on the
    ticket is looked up or calculated."""
    model_config = ConfigDict(extra="forbid")

    symbol: str
    side: Side
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)
    counterparty: str = Field(min_length=1, max_length=128)  # the other party's user ID
    # Bonds the bond math can't price (corporates, GFSF and USD DDE
    # bonds): the accrued interest agreed with the counterparty. Refused
    # for anything else, where it is calculated or nil.
    accrued_interest: Optional[float] = Field(default=None, ge=0)
    # Defaults to today on the exchange calendar.
    trade_date: Optional[date] = None
    # Securities accounts at the depository, where known.
    trader_account: Optional[str] = Field(default=None, max_length=64)
    counterparty_account: Optional[str] = Field(default=None, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=500)


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: TicketStatus = "draft"

    trader: str
    counterparty: str
    side: Side
    trader_account: Optional[str] = None
    counterparty_account: Optional[str] = None

    symbol: str
    name: str
    isin: Optional[str] = None
    asset_class: AssetClass
    currency: str

    quantity: float
    price: float
    trade_date: date
    settlement_date: date
    consideration: float                  # quantity x price (per 100 face for bills and bonds)
    accrued_interest: float = 0.0         # bonds only
    days_accrued: Optional[int] = None    # bonds only, when calculated
    accrued_source: AccruedSource = "none"
    net_amount: float                     # what the buyer pays the seller

    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    sent_at: Optional[datetime] = None
    sent_to: Optional[str] = None          # where it was emailed, if it was
    filed_at: Optional[datetime] = None
    depository_reference: Optional[str] = None
    rejection_reason: Optional[str] = None

    @computed_field
    @property
    def buyer(self) -> str:
        return self.trader if self.side == "buy" else self.counterparty

    @computed_field
    @property
    def seller(self) -> str:
        return self.counterparty if self.side == "buy" else self.trader

    @property
    def buyer_account(self) -> Optional[str]:
        return self.trader_account if self.side == "buy" else self.counterparty_account

    @property
    def seller_account(self) -> Optional[str]:
        return self.counterparty_account if self.side == "buy" else self.trader_account

    def involves(self, user_id: str) -> bool:
        return user_id in (self.trader, self.counterparty)


class TicketConfirmation(BaseModel):
    """POST /tickets/{id}/confirmation: the depository's reply to an
    emailed ticket, as the trader read it. A reference if it accepted the
    filing, a reason if it refused."""
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    reference: Optional[str] = Field(default=None, min_length=1, max_length=64)
    reason: Optional[str] = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _reference_or_reason(self) -> "TicketConfirmation":
        if self.accepted and not self.reference:
            raise ValueError("an accepted filing needs the depository's reference")
        if not self.accepted and not self.reason:
            raise ValueError("a refused filing needs the depository's reason")
        return self


NotificationKind = Literal["ticket_sent", "ticket_filed", "ticket_rejected"]


class Notification(BaseModel):
    """A message to one user about a ticket they are a party to."""
    model_config = ConfigDict(extra="forbid")

    id: int
    user_id: str
    kind: NotificationKind
    ticket_id: str
    message: str
    created_at: datetime
    read: bool = False
