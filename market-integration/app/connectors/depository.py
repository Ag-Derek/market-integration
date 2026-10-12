"""
Filing trade tickets with the depository (the CSD, which settles GSE
and GFIM trades).

Depository is what the ticket desk (app/tickets/desk.py) talks to.
file() returns a FilingReceipt:

  filed     the depository accepted it, with its reference
  rejected  it refused, with its reason
  sent      handed over by email; its reply comes later, by email too,
            and the trader records it (POST /tickets/{id}/confirmation)

or raises DepositoryUnavailable when it couldn't be reached, in which
case nothing was filed and the ticket can be sent again.

Two implementations, chosen by DEPOSITORY_MODE (app/config.py):

- EmailDepository: emails the ticket as a PDF (app/tickets/pdf.py) to
  DEPOSITORY_EMAIL, through SMTP, or, with no SMTP server configured,
  saves each email as an .eml file in an outbox folder to send by hand
  (it opens in Outlook and most mail programs). For when we have no
  direct link to the depository.
- MockDepository: accepts any ticket that passes a few basic checks,
  numbering them CSD-<trade date>-<sequence>. For development and tests,
  and the shape a direct (API) connector would take.
"""

import asyncio
import itertools
import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Literal, Optional, Protocol

from app.models.ticket import Ticket
from app.tickets.pdf import render_ticket_pdf

logger = logging.getLogger(__name__)


class DepositoryUnavailable(Exception):
    """The depository couldn't be reached; nothing was filed."""


@dataclass(frozen=True)
class FilingReceipt:
    outcome: Literal["filed", "rejected", "sent"]
    reference: Optional[str] = None   # the depository's ID for the filing, if filed
    reason: Optional[str] = None      # why not, if rejected
    sent_to: Optional[str] = None     # where it went, if sent


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
            return FilingReceipt("rejected", reason="settlement date is before the trade date")
        if ticket.asset_class == "equity" and ticket.quantity != int(ticket.quantity):
            return FilingReceipt("rejected", reason="equity quantity must be a whole number of shares")
        self.filed.append(ticket)
        reference = f"CSD-{ticket.trade_date:%Y%m%d}-{next(self._sequence):06d}"
        return FilingReceipt("filed", reference=reference)


# ---------------------------------------------------------------- email


class MailSender(Protocol):
    def send(self, message: EmailMessage) -> str:
        """Deliver (or store) the message; returns where it went, for the
        log. Blocking: called in a worker thread. Raises OSError or
        smtplib.SMTPException on failure."""
        ...


class SmtpSender:
    """Through an SMTP server: STARTTLS on port 587 (the default), TLS
    from the start on 465, plain otherwise."""

    def __init__(self, host: str, port: int = 587, username: str = "", password: str = "", timeout: float = 30):
        self.host, self.port, self.username, self.password, self.timeout = host, port, username, password, timeout

    def send(self, message: EmailMessage) -> str:
        context = ssl.create_default_context()
        if self.port == 465:
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        with smtp:
            if self.port == 587:
                smtp.starttls(context=context)
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)
        return f"smtp://{self.host}:{self.port}"


class OutboxSender:
    """Saves each message as <ticket id>.eml in `directory`, for someone
    to open and send from their own mailbox."""

    def __init__(self, directory: "Path | str"):
        self.directory = Path(directory)

    def send(self, message: EmailMessage) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{message['X-Ticket-Id']}.eml"
        path.write_bytes(bytes(message))
        return str(path)


class EmailDepository:
    name = "depository (by email)"

    def __init__(self, to_address: str, from_address: str, sender: MailSender, cc: tuple[str, ...] = ()):
        self.to_address = to_address
        self.from_address = from_address
        self.cc = cc
        self.sender = sender

    def message(self, ticket: Ticket) -> EmailMessage:
        """The email for `ticket`: a plain-text summary with the PDF ticket
        attached."""
        equity = ticket.asset_class == "equity"
        quantity = f"{ticket.quantity:,.0f}" if equity else f"{ticket.currency} {ticket.quantity:,.2f} face"
        msg = EmailMessage()
        msg["Subject"] = (
            f"Trade ticket {ticket.id}: {ticket.symbol} {quantity} @ {ticket.price:,.4g}, "
            f"T {ticket.trade_date:%d/%m/%Y}, S {ticket.settlement_date:%d/%m/%Y}"
        )
        msg["From"] = self.from_address
        msg["To"] = self.to_address
        if self.cc:
            msg["Cc"] = ", ".join(self.cc)
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(idstring=ticket.id)
        msg["X-Ticket-Id"] = ticket.id
        msg.set_content(
            "Dear Sir/Madam,\n\n"
            "Please find attached the trade ticket below for filing.\n\n"
            f"  Ticket ID:        {ticket.id}\n"
            f"  Security:         {ticket.symbol} - {ticket.name}\n"
            f"  ISIN:             {ticket.isin or '-'}\n"
            f"  Buyer:            {ticket.buyer} (account {ticket.buyer_account or '-'})\n"
            f"  Seller:           {ticket.seller} (account {ticket.seller_account or '-'})\n"
            f"  Quantity:         {quantity}\n"
            f"  Price:            {ticket.price:,.4f}\n"
            f"  Trade date:       {ticket.trade_date:%d %b %Y}\n"
            f"  Settlement date:  {ticket.settlement_date:%d %b %Y}\n"
            f"  Net amount:       {ticket.currency} {ticket.net_amount:,.2f}\n\n"
            f"Please quote {ticket.id} in your reply with the filing reference.\n"
        )
        msg.add_attachment(
            render_ticket_pdf(ticket, datetime.now(timezone.utc)),
            maintype="application", subtype="pdf", filename=f"{ticket.id}.pdf",
        )
        return msg

    async def file(self, ticket: Ticket) -> FilingReceipt:
        message = self.message(ticket)
        try:
            where = await asyncio.to_thread(self.sender.send, message)
        except (OSError, smtplib.SMTPException) as e:
            raise DepositoryUnavailable(f"couldn't send the email to {self.to_address}: {e}") from e
        logger.info("Ticket %s emailed to the depository (%s) via %s", ticket.id, self.to_address, where)
        return FilingReceipt("sent", sent_to=self.to_address)
