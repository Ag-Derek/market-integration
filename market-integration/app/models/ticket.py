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

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.instrument import AssetClass

Side = Literal["buy", "sell"]
# draft:    written, not yet sent to the depository
# filing:   sent; waiting for the depository's answer
# filed:    the depository accepted it (depository_reference is set)
# rejected: the depository refused it (rejection_reason says why)
TicketStatus = Literal["draft", "filing", "filed", "rejected"]


class TicketRequest(BaseModel):
    """POST /tickets: what the trader fills in. Everything else on the
    ticket is looked up or calculated."""
    model_config = ConfigDict(extra="forbid")

    symbol: str
    side: Side
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)
    counterparty: str = Field(min_length=1, max_length=128)  # the other party's user ID
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
    days_accrued: Optional[int] = None    # bonds only
    net_amount: float                     # what the buyer pays the seller

    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime
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

    def involves(self, user_id: str) -> bool:
        return user_id in (self.trader, self.counterparty)


NotificationKind = Literal["ticket_filed", "ticket_rejected"]


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
