"""
Filing trade tickets with the depository (the CSD, which settles GSE
and GFIM trades).

Depository is what the ticket desk (app/tickets/desk.py) talks to:
file() either returns a FilingReceipt, accepted with the depository's
reference or rejected with its reason, or raises DepositoryUnavailable
when it couldn't be reached, in which case nothing was filed and the
ticket can be sent again.

MockDepository stands in until we have the CSD's interface. It accepts
any ticket that passes a few basic checks and numbers them
CSD-<trade date>-<sequence>. A real connector implements the same
protocol and is swapped in in app/main.py.
"""

import itertools
from dataclasses import dataclass
from typing import Optional, Protocol

from app.models.ticket import Ticket


class DepositoryUnavailable(Exception):
    """The depository couldn't be reached; nothing was filed."""


@dataclass(frozen=True)
class FilingReceipt:
    accepted: bool
    reference: Optional[str] = None   # the depository's ID for the filing, if accepted
    reason: Optional[str] = None      # why not, if rejected


class Depository(Protocol):
    name: str

    async def file(self, ticket: Ticket) -> FilingReceipt: ...


class MockDepository:
    name = "mock CSD"

    def __init__(self):
        self._sequence = itertools.count(1)
        self.filed: list[Ticket] = []

    async def file(self, ticket: Ticket) -> FilingReceipt:
        if ticket.settlement_date < ticket.trade_date:
            return FilingReceipt(False, reason="settlement date is before the trade date")
        if ticket.asset_class == "equity" and ticket.quantity != int(ticket.quantity):
            return FilingReceipt(False, reason="equity quantity must be a whole number of shares")
        self.filed.append(ticket)
        reference = f"CSD-{ticket.trade_date:%Y%m%d}-{next(self._sequence):06d}"
        return FilingReceipt(True, reference=reference)
