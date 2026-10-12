"""
A ticket as a one-page A4 PDF: what GET /tickets/{id}/pdf serves for
printing, and what the email to the depository carries
(app/connectors/depository.py), so both show the same document.
"""

import io
from datetime import datetime, timezone
from typing import Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models.ticket import Ticket

_STATUS = {
    "draft": "DRAFT - not filed",
    "filing": "Submitted for filing",
    "sent": "Submitted for filing - awaiting depository confirmation",
    "filed": "FILED",
    "rejected": "REJECTED by the depository",
}

_styles = getSampleStyleSheet()
_TITLE = ParagraphStyle("title", parent=_styles["Title"], fontSize=18, spaceAfter=2 * mm)
_HEADING = ParagraphStyle("heading", parent=_styles["Heading3"], spaceBefore=4 * mm, spaceAfter=1.5 * mm)
_CELL = ParagraphStyle("cell", parent=_styles["BodyText"], fontSize=9.5, leading=12)
_LABEL = ParagraphStyle("label", parent=_CELL, textColor=colors.HexColor("#555555"))
_SMALL = ParagraphStyle("small", parent=_CELL, fontSize=8, textColor=colors.HexColor("#666666"))


def _p(value, style=_CELL) -> Paragraph:
    return Paragraph(escape("" if value is None else str(value)), style)


def _rows(rows: list[tuple[str, object]]) -> Table:
    table = Table(
        [[_p(label, _LABEL), _p(value if value not in (None, "") else "-")] for label, value in rows],
        colWidths=[55 * mm, 115 * mm],
    )
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BBBBBB")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F2F2")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return table


def _money(ticket: Ticket, amount: float) -> str:
    return f"{ticket.currency} {amount:,.2f}"


def render_ticket_pdf(ticket: Ticket, generated_at: Optional[datetime] = None) -> bytes:
    generated_at = generated_at or datetime.now(timezone.utc)
    equity = ticket.asset_class == "equity"
    quantity = f"{ticket.quantity:,.0f} shares" if equity else f"{_money(ticket, ticket.quantity)} face value"
    price = f"{_money(ticket, ticket.price)} per share" if equity else f"{ticket.price:,.4f} per 100 face (clean)"

    story = [
        Paragraph("TRADE TICKET", _TITLE),
        _rows([
            ("Ticket ID", ticket.id),
            ("Status", _STATUS[ticket.status]),
            ("Depository reference", ticket.depository_reference),
            ("Rejection reason", ticket.rejection_reason) if ticket.status == "rejected" else ("Sent to", ticket.sent_to),
        ]),
        Paragraph("Parties", _HEADING),
        _rows([
            ("Buyer", ticket.buyer),
            ("Buyer securities account", ticket.buyer_account),
            ("Seller", ticket.seller),
            ("Seller securities account", ticket.seller_account),
            ("Ticket written by", ticket.trader),
        ]),
        Paragraph("Security", _HEADING),
        _rows([
            ("Symbol", ticket.symbol),
            ("Name", ticket.name),
            ("ISIN", ticket.isin),
            ("Asset class", ticket.asset_class.capitalize()),
        ]),
        Paragraph("Trade", _HEADING),
        _rows([
            ("Quantity", quantity),
            ("Price", price),
            ("Trade date", f"{ticket.trade_date:%d %b %Y}"),
            ("Settlement date", f"{ticket.settlement_date:%d %b %Y}"),
        ]),
        Paragraph("Amounts", _HEADING),
        _rows([
            ("Consideration", _money(ticket, ticket.consideration)),
            *([("Accrued interest", f"{_money(ticket, ticket.accrued_interest)} " + (
                f"({ticket.days_accrued} days)" if ticket.accrued_source == "calculated" else "(as agreed by the parties)"
            ))] if ticket.asset_class == "bond" else []),
            ("Net amount (buyer pays seller)", _money(ticket, ticket.net_amount)),
        ]),
    ]
    if ticket.notes:
        story += [Paragraph("Notes", _HEADING), _p(ticket.notes)]

    signatures = Table(
        [[_p("Authorised signature (broker)"), "", _p("Received by (depository)")],
         ["", "", ""],
         [_p("Name / date:", _SMALL), "", _p("Name / date / stamp:", _SMALL)]],
        colWidths=[78 * mm, 14 * mm, 78 * mm], rowHeights=[None, 16 * mm, None],
    )
    signatures.setStyle(TableStyle([
        ("LINEBELOW", (0, 1), (0, 1), 0.6, colors.black),
        ("LINEBELOW", (2, 1), (2, 1), 0.6, colors.black),
    ]))
    story += [
        Spacer(1, 8 * mm),
        signatures,
        Spacer(1, 6 * mm),
        _p(
            f"Ticket created {ticket.created_at:%d %b %Y %H:%M} UTC. "
            f"Printed {generated_at:%d %b %Y %H:%M} UTC. Amounts exclude brokerage fees and levies.",
            _SMALL,
        ),
    ]

    buffer = io.BytesIO()
    SimpleDocTemplate(
        buffer, pagesize=A4, title=f"Trade ticket {ticket.id}", author="Market Integration Service",
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
    ).build(story)
    return buffer.getvalue()
