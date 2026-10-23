"""
The ticket desk: write a ticket, file it with the depository, tell both
parties.

    draft --file()--> filing --depository accepts--> filed
                             --depository refuses--> rejected
                             --emailed-------------> sent --confirm()--> filed | rejected
                             --unreachable---------> draft (send again)

A direct depository answers file() at once. One reached by email
answers by email later: the trader records its reply with confirm().

Only the trader who wrote a ticket may file or confirm it; either party
may read or print it. At each step after draft (sent, filed, rejected)
the trader and the counterparty each get a notification, stored (GET
/notifications/me) and pushed to any connection they have open
(/ws/notifications).
"""

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Callable, Mapping, Optional

from app.connectors.depository import Depository, DepositoryUnavailable
from app.bond_math.conventions import BusinessCalendar
from app.gateways.notification_gateway import NotificationGateway
from app.models.commodity import format_tonnes
from app.models.instrument import Instrument
from app.models.ticket import (
    Notification,
    NotificationKind,
    Ticket,
    TicketConfirmation,
    TicketRequest,
    TicketStatus,
)
from app.tickets import TicketError, build_ticket
from app.tickets.store import TicketStore

logger = logging.getLogger(__name__)


class TicketDesk:

    def __init__(
        self,
        store: TicketStore,
        depository: Depository,
        gateway: NotificationGateway,
        *,
        instruments: Mapping[str, Instrument],
        calendar: BusinessCalendar,
        today: Callable[[], date],
    ):
        """`today` returns the current trade date on the exchange calendar."""
        self.store = store
        self.depository = depository
        self.gateway = gateway
        self._instruments = instruments
        self._calendar = calendar
        self._today = today

    def preview(self, request: TicketRequest, trader: str) -> Ticket:
        """The ticket create() would save, without saving it: for a form
        to show the settlement date and amounts as they are typed."""
        return self._build(request, trader, complete=False)

    async def create(self, request: TicketRequest, trader: str) -> Ticket:
        return await asyncio.to_thread(self.store.add, self._build(request, trader, complete=True))

    def _build(self, request: TicketRequest, trader: str, complete: bool) -> Ticket:
        return build_ticket(
            request, trader,
            instruments=self._instruments, calendar=self._calendar,
            today=self._today(), now=datetime.now(timezone.utc), complete=complete,
        )

    async def get(self, ticket_id: str, user_id: str) -> Ticket:
        """The ticket, if the user is a party to it; otherwise 404, so
        others can't tell which IDs exist."""
        ticket = await asyncio.to_thread(self.store.get, ticket_id)
        if ticket is None or not ticket.involves(user_id):
            raise TicketError(f"No ticket '{ticket_id}'", 404)
        return ticket

    async def list(self, user_id: str, status: Optional[TicketStatus] = None) -> list[Ticket]:
        return await asyncio.to_thread(self.store.for_user, user_id, status)

    async def file(self, ticket_id: str, user_id: str) -> Ticket:
        """Send a draft ticket to the depository and record its answer:
        filed or rejected, or sent if it went by email. 403 if the user
        isn't its trader, 409 if it isn't a draft, 503 if the depository
        (or the mail server) can't be reached (the ticket stays a draft)."""
        ticket = await self.get(ticket_id, user_id)
        if ticket.trader != user_id:
            raise TicketError("Only the trader who wrote a ticket can file it", 403)
        claimed = await asyncio.to_thread(self.store.transition, ticket_id, "draft", status="filing")
        if claimed is None:
            current = await asyncio.to_thread(self.store.get, ticket_id)
            raise TicketError(f"Ticket {ticket_id} is {current.status}, not a draft", 409)

        try:
            receipt = await self.depository.file(claimed)
        except BaseException as e:
            # Nothing was filed: put it back so the trader can send it again.
            await asyncio.to_thread(self.store.transition, ticket_id, "filing", status="draft")
            if isinstance(e, DepositoryUnavailable):
                raise TicketError(f"The depository couldn't be reached; try again ({e})", 503) from e
            raise

        if receipt.outcome == "sent":
            done = await asyncio.to_thread(
                self.store.transition, ticket_id, "filing",
                status="sent", sent_at=datetime.now(timezone.utc), sent_to=receipt.sent_to,
            )
            await self._notify_parties(
                done, "ticket_sent", f"emailed to the depository ({receipt.sent_to}); awaiting its confirmation",
            )
            return done
        return await self._record_answer(
            ticket_id, "filing", receipt.outcome == "filed", receipt.reference, receipt.reason,
        )

    async def confirm(self, ticket_id: str, user_id: str, confirmation: TicketConfirmation) -> Ticket:
        """Record the depository's emailed reply to a sent ticket: filed
        with its reference, or rejected with its reason. 403 if the user
        isn't the ticket's trader, 409 if the ticket isn't waiting for a
        reply."""
        ticket = await self.get(ticket_id, user_id)
        if ticket.trader != user_id:
            raise TicketError("Only the trader who wrote a ticket can record the depository's reply", 403)
        done = await self._record_answer(
            ticket_id, "sent", confirmation.accepted, confirmation.reference, confirmation.reason,
        )
        if done is None:
            current = await asyncio.to_thread(self.store.get, ticket_id)
            raise TicketError(f"Ticket {ticket_id} is {current.status}, not waiting for the depository", 409)
        return done

    async def _record_answer(
        self, ticket_id: str, expected: TicketStatus, accepted: bool,
        reference: Optional[str], reason: Optional[str],
    ) -> Optional[Ticket]:
        """Move the ticket from `expected` to filed or rejected and tell
        both parties. None if it wasn't in `expected`."""
        if accepted:
            done = await asyncio.to_thread(
                self.store.transition, ticket_id, expected,
                status="filed", filed_at=datetime.now(timezone.utc), depository_reference=reference,
            )
            kind: NotificationKind = "ticket_filed"
            outcome = f"filed with the depository as {reference}"
        else:
            done = await asyncio.to_thread(
                self.store.transition, ticket_id, expected, status="rejected", rejection_reason=reason,
            )
            kind = "ticket_rejected"
            outcome = f"rejected by the depository: {reason}"
        if done is not None:
            await self._notify_parties(done, kind, outcome)
        return done

    async def _notify_parties(self, ticket: Ticket, kind: NotificationKind, outcome: str) -> None:
        for party in (ticket.trader, ticket.counterparty):
            await self._notify(party, kind, ticket, f"{_describe(ticket, party)}: {outcome}.")

    async def _notify(self, user_id: str, kind: NotificationKind, ticket: Ticket, message: str) -> None:
        notification: Notification = await asyncio.to_thread(
            self.store.notify, user_id, kind, ticket.id, message
        )
        self.gateway.push(notification)


def _describe(ticket: Ticket, party: str) -> str:
    """The trade from `party`'s side, e.g. "Ticket TKT-…: you bought
    1,000 MTNGH at GHS 6.54 from alice"."""
    bought = ticket.buyer == party
    other = ticket.seller if bought else ticket.buyer
    if ticket.asset_class == "equity":
        what = f"{ticket.quantity:,.0f} {ticket.symbol} at {ticket.currency} {ticket.price:,.2f}"
    elif ticket.asset_class == "commodity":
        what = (f"{format_tonnes(ticket.quantity)} of {ticket.name} ({ticket.symbol}) "
                f"at {ticket.currency} {ticket.price:,.2f} per MT")
    else:
        what = f"{ticket.currency} {ticket.quantity:,.2f} face of {ticket.name} at {ticket.price:,.4f}"
    return (
        f"Ticket {ticket.id}: you {'bought' if bought else 'sold'} {what} "
        f"{'from' if bought else 'to'} {other}, settling {ticket.settlement_date:%d %b %Y}"
    )
