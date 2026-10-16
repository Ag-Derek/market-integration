"""
A ticket as a one-page A4 PDF: what GET /tickets/{id}/pdf serves for
printing, and what the email to the depository carries
(app/connectors/depository.py), so both show the same document.
"""

import io
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models.ticket import Ticket

_INK = colors.HexColor("#1A1A1A")
_MUTED = colors.HexColor("#6B6B6B")
_RULE = colors.HexColor("#D9D9D9")
_BORDER = colors.HexColor("#CFCFCF")
_PANEL = colors.HexColor("#F1F2F1")
_GREEN = colors.HexColor("#1F4D3A")

# status: (badge text, colour)
_STATUS = {
    "draft": ("DRAFT", _MUTED),
    "filing": ("FILING", colors.HexColor("#8A5A00")),
    "sent": ("SENT", colors.HexColor("#8A5A00")),
    "filed": ("FILED", _GREEN),
    "rejected": ("REJECTED", colors.HexColor("#A12A2A")),
}

_MARGIN = 16 * mm
_WIDTH = A4[0] - 2 * _MARGIN
_GAP = 5 * mm
_HALF = (_WIDTH - _GAP) / 2

_SANS, _BOLD, _MONO = "Helvetica", "Helvetica-Bold", "Courier"


def _style(name, size=9.5, font=_SANS, color=_INK, align=0, leading=None) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading or size * 1.3,
                          textColor=color, alignment=align)


_TITLE = _style("title", 24, _BOLD, leading=28)
_BIG = _style("big", 20, _BOLD, leading=24)
_BIG_LONG = _style("big-long", 15, _BOLD, leading=19)
_BIG_AMOUNT = _style("big-amount", 22, _BOLD, _GREEN, TA_RIGHT, leading=26)
_BODY = _style("body")
_BODY_R = _style("body-r", align=TA_RIGHT)
_LABEL = _style("label", color=_MUTED)
_SMALL = _style("small", 8, color=_MUTED)
_SMALL_R = _style("small-r", 8, color=_MUTED, align=TA_RIGHT)
_TINY = _style("tiny", 7.5, color=_MUTED)
_MONO_R = _style("mono-r", 10.5, _MONO, align=TA_RIGHT)
_PARTY = _style("party", 9, _MONO, leading=12)


class _Caps(Flowable):
    """A small letter-spaced capitals label, as section headings use."""

    def __init__(self, text, color=_INK, size=7.5, right=False, space=1.2):
        super().__init__()
        self.text, self.color, self.size, self.right, self.space = text.upper(), color, size, right, space

    def wrap(self, avail_width, avail_height):
        self.width = avail_width
        return avail_width, self.size + 3

    def draw(self):
        c = self.canv
        c.setFont(_BOLD, self.size)
        c.setFillColor(self.color)
        text_width = c.stringWidth(self.text, _BOLD, self.size) + self.space * (len(self.text) - 1)
        t = c.beginText(self.width - text_width if self.right else 0, 1)
        t.setCharSpace(self.space)
        t.textOut(self.text)
        c.drawText(t)


class _Badge(Flowable):
    """The status pill: a dot and the status, in a rounded outline."""

    def __init__(self, text, color):
        super().__init__()
        self.text, self.color = text, color
        self.size, self.space = 8, 1.2
        self.text_width = self._text_width()
        self.pill_width = self.text_width + 22
        self.pill_height = 16

    def _text_width(self):
        return stringWidth(self.text, _BOLD, self.size) + self.space * (len(self.text) - 1)

    def wrap(self, avail_width, avail_height):
        self.width = avail_width
        return avail_width, self.pill_height

    def draw(self):
        c = self.canv
        x = self.width - self.pill_width
        c.setStrokeColor(self.color)
        c.setFillColor(self.color)
        c.setLineWidth(1)
        c.roundRect(x, 0, self.pill_width, self.pill_height, self.pill_height / 2, stroke=1, fill=0)
        c.circle(x + 9, self.pill_height / 2, 2, stroke=0, fill=1)
        t = c.beginText(x + 15, 5)
        t.setFont(_BOLD, self.size)
        t.setCharSpace(self.space)
        t.textOut(self.text)
        c.drawText(t)


class _Stamp(Flowable):
    """A dashed box for the depository's stamp."""

    def __init__(self, width, height):
        super().__init__()
        self.width, self.height = width, height

    def draw(self):
        c = self.canv
        c.setStrokeColor(_MUTED)
        c.setLineWidth(0.5)
        c.setDash(1.5, 1.5)
        c.roundRect(0, 0, self.width, self.height, 2, stroke=1, fill=0)
        c.setDash()
        c.setFillColor(_MUTED)
        t = c.beginText(0, self.height / 2 - 2.5)
        t.setFont(_SANS, 7)
        t.setCharSpace(1.2)
        width = c.stringWidth("STAMP", _SANS, 7) + 1.2 * 4
        t.setTextOrigin((self.width - width) / 2, self.height / 2 - 2.5)
        t.textOut("STAMP")
        c.drawText(t)


def _p(value, style=_BODY) -> Paragraph:
    return Paragraph(escape("" if value in (None, "") else str(value)) or "-", style)


def _money(ticket: Ticket, amount: float) -> str:
    return f"{ticket.currency} {amount:,.2f}"


def _face(ticket: Ticket) -> str:
    """A bill or bond's face value, without pence when it's whole."""
    q = ticket.quantity
    return f"{ticket.currency} {q:,.0f}" if q == int(q) else _money(ticket, q)


def _business_days(start: date, end: date) -> int:
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        days += d.weekday() < 5
    return days


def _header(ticket: Ticket) -> list:
    badge, color = _STATUS[ticket.status]
    right = [_Badge(badge, color), Spacer(1, 3 * mm), _p(ticket.id, _MONO_R)]
    if ticket.depository_reference:
        right.append(Paragraph(
            f'Depository ref <font name="{_MONO}" color="#1A1A1A">{escape(ticket.depository_reference)}</font>',
            _SMALL_R,
        ))
    table = Table(
        [[[_Caps(f"{ticket.asset_class} · secondary market", _GREEN), Spacer(1, 2 * mm),
           Paragraph("Trade Ticket", _TITLE)], right]],
        colWidths=[_WIDTH * 0.6, _WIDTH * 0.4],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
        ("LINEBELOW", (0, 0), (-1, 0), 1.4, _INK),
    ]))
    return [table]


def _summary(ticket: Ticket) -> Table:
    if ticket.asset_class == "equity":
        amount, price = f"{ticket.quantity:,.0f}", _money(ticket, ticket.price)
    else:
        amount, price = _face(ticket), f"{ticket.price:,.4f}"
    plain = f"{amount} {ticket.symbol} @ {price}"
    headline = f'{amount} {escape(ticket.symbol)} <font color="#6B6B6B">@</font> {price}'
    fits = stringWidth(plain, _BOLD, _BIG.fontSize) < _WIDTH * 0.6 - 8 * mm
    subline = " · ".join(filter(None, [
        escape(ticket.name), ticket.isin and f'ISIN <font name="{_MONO}">{escape(ticket.isin)}</font>',
    ]))
    table = Table(
        [[[_Caps("Trade summary", _MUTED), Spacer(1, 3 * mm),
           Paragraph(headline, _BIG if fits else _BIG_LONG),
           Spacer(1, 1.5 * mm),
           Paragraph(subline, _LABEL)],
          [_Caps("Net amount", _MUTED, right=True), Spacer(1, 3 * mm),
           _p(_money(ticket, ticket.net_amount), _BIG_AMOUNT), Spacer(1, 1.5 * mm),
           _p("Buyer pays seller", _SMALL_R)]]],
        colWidths=[_WIDTH * 0.6, _WIDTH * 0.4],
        cornerRadii=[3, 3, 3, 3],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _PANEL),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 8 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 5 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 5 * mm),
        ("LEFTPADDING", (1, 0), (1, 0), 0),
    ]))
    return table


def _card(rows: list, width: float, padding=6 * mm) -> Table:
    """A rounded outlined box holding `rows` (one flowable or list per row)."""
    table = Table([[row] for row in rows], colWidths=[width], cornerRadii=[3, 3, 3, 3])
    table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, _BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), padding), ("RIGHTPADDING", (0, 0), (-1, -1), padding),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, 0), padding), ("BOTTOMPADDING", (0, -1), (-1, -1), padding),
    ]))
    return table


def _card_pair(left: list, right: list) -> Table:
    """Two cards side by side, the shorter padded to the taller's height."""
    heights = [_card(rows, _HALF).wrap(_HALF, A4[1])[1] for rows in (left, right)]
    pad = abs(heights[0] - heights[1])
    if pad:
        (left if heights[0] < heights[1] else right).append(Spacer(1, pad))
    return _side_by_side(_card(left, _HALF), _card(right, _HALF))


def _side_by_side(left, right) -> Table:
    table = Table([[left, "", right]], colWidths=[_HALF, _GAP, _HALF])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _party(ticket: Ticket, role: str) -> list:
    participant = ticket.buyer if role == "buyer" else ticket.seller
    account = ticket.buyer_account if role == "buyer" else ticket.seller_account
    heading = Table(
        [[_Caps(role, size=8), Paragraph(
            "Ticket author" if participant == ticket.trader else "",
            _style("author", 8, _BOLD, _GREEN, TA_RIGHT))]],
        colWidths=[(_HALF - 12 * mm) / 2] * 2,
    )
    heading.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    return [
        [heading, Spacer(1, 3 * mm)],
        _p("Participant", _SMALL),
        [_p(participant, _PARTY), Spacer(1, 2.5 * mm)],
        _p("Securities account", _SMALL),
        _p(account, _PARTY),
    ]


def _rows(title: str, rows: list[tuple[str, object]], width: float, last_bold=False) -> Table:
    """A titled list of label / right-aligned value rows with a rule under each."""
    data = [[_Caps(title, size=8), ""]]
    for label, value in rows:
        data.append([label if isinstance(label, Paragraph) else _p(label, _LABEL), value if isinstance(value, Paragraph) else _p(value, _BODY_R)])
    table = Table(data, colWidths=[width * 0.4, width * 0.6])
    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 1), (-1, -1), 2 * mm), ("BOTTOMPADDING", (0, 1), (-1, -1), 2 * mm),
        ("TOPPADDING", (0, 0), (-1, 0), 0), ("BOTTOMPADDING", (0, 0), (-1, 0), 2 * mm),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, _RULE),
    ]
    if last_bold:
        style += [("LINEABOVE", (0, -1), (-1, -1), 1.2, _INK), ("LINEBELOW", (0, -1), (-1, -1), 1.2, _INK)]
    table.setStyle(TableStyle(style))
    return table


def _signatures() -> Table:
    inner = _HALF - 12 * mm

    def line(label, width):
        cell = Table([[_p(label, _TINY)]], colWidths=[width], rowHeights=[None])
        cell.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.6, _INK),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 7 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5 * mm),
        ]))
        return cell

    def pair(left, right, total):
        half = (total - 4 * mm) / 2
        table = Table([[line(left, half), "", line(right, half)]], colWidths=[half, 4 * mm, half])
        table.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                   ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        return table

    broker = [_Caps("Authorised by broker", size=8), line("Signature", inner), pair("Name", "Date", inner)]

    stamp_width = 22 * mm
    fields = inner - stamp_width - 4 * mm
    body = Table([[[line("Name", fields), line("Date", fields)], "", _Stamp(stamp_width, 18 * mm)]],
                 colWidths=[fields, 4 * mm, stamp_width])
    body.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                              ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                              ("TOPPADDING", (2, 0), (2, 0), 3 * mm), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    depository = [_Caps("Received by depository", size=8), body]
    return _card_pair(broker, depository)


def render_ticket_pdf(ticket: Ticket, generated_at: Optional[datetime] = None) -> bytes:
    generated_at = generated_at or datetime.now(timezone.utc)
    equity = ticket.asset_class == "equity"
    quantity = f"{ticket.quantity:,.0f} shares" if equity else f"{_face(ticket)} face"
    price = f"{_money(ticket, ticket.price)} / share" if equity else f"{ticket.price:,.4f} per 100 face (clean)"
    lag = _business_days(ticket.trade_date, ticket.settlement_date)

    security = _rows("Security", [
        ("Symbol", Paragraph(f"<b>{escape(ticket.symbol)}</b>", _BODY_R)),
        ("Name", ticket.name),
        ("ISIN", Paragraph(f'<font name="{_MONO}">{escape(ticket.isin or "-")}</font>', _BODY_R)),
        ("Asset class", ticket.asset_class.capitalize()),
    ], _HALF)
    trade = _rows("Trade", [
        ("Quantity", quantity),
        ("Price", price),
        ("Trade date", f"{ticket.trade_date:%a %d %b %Y}"),
        ("Settlement date", Paragraph(
            f'<b>{ticket.settlement_date:%a %d %b %Y}</b> <font color="#6B6B6B">· T+{lag}</font>', _BODY_R)),
    ], _HALF)

    consideration = (f"Consideration ({ticket.quantity:,.0f} × {_money(ticket, ticket.price)})" if equity
                     else f"Consideration ({_face(ticket)} × {ticket.price:,.4f} / 100)")
    amounts = _rows("Amounts", [
        (consideration, _money(ticket, ticket.consideration)),
        *([("Accrued interest " + (
            f"({ticket.days_accrued} days)" if ticket.accrued_source == "calculated" else "(as agreed by the parties)"
        ), _money(ticket, ticket.accrued_interest))] if ticket.asset_class == "bond" else []),
        ("Brokerage fees & levies", "Not included"),
        (Paragraph("<b>Net amount · buyer pays seller</b>", _BODY),
         Paragraph(f"<b>{_money(ticket, ticket.net_amount)}</b>", _style("net", 11, align=TA_RIGHT))),
    ], _WIDTH, last_bold=True)

    if ticket.status == "rejected":
        delivery = f'<font color="#A12A2A">Rejected by the depository:</font> {escape(ticket.rejection_reason or "-")}'
    else:
        delivery = f'Sent to: <font color="#1A1A1A">{escape(ticket.sent_to or "Not yet sent")}</font>'

    story = [
        *_header(ticket),
        Spacer(1, 6 * mm),
        _summary(ticket),
        Spacer(1, 6 * mm),
        _card_pair(_party(ticket, "buyer"), _party(ticket, "seller")),
        Spacer(1, 6 * mm),
        _side_by_side(security, trade),
        Spacer(1, 6 * mm),
        amounts,
        Spacer(1, 2 * mm),
        Paragraph(delivery, _SMALL),
    ]
    if ticket.notes:
        story += [Spacer(1, 5 * mm), _Caps("Notes", size=8), Spacer(1, 1.5 * mm), _p(ticket.notes)]
    story += [Spacer(1, 7 * mm), _signatures()]

    def footer(canvas, doc):
        canvas.saveState()
        y = 10 * mm
        canvas.setStrokeColor(_RULE)
        canvas.setLineWidth(0.5)
        canvas.line(_MARGIN, y + 5 * mm, A4[0] - _MARGIN, y + 5 * mm)
        canvas.setFillColor(_MUTED)
        canvas.setFont(_SANS, 7.5)
        canvas.drawString(_MARGIN, y, f"Created {ticket.created_at:%d %b %Y %H:%M} UTC · "
                                      f"Printed {generated_at:%d %b %Y %H:%M} UTC")
        canvas.setFont(_MONO, 7.5)
        canvas.drawRightString(A4[0] - _MARGIN, y, f"{ticket.id} · Page {doc.page}")
        canvas.restoreState()

    buffer = io.BytesIO()
    SimpleDocTemplate(
        buffer, pagesize=A4, title=f"Trade ticket {ticket.id}", author="Market Integration Service",
        # the frame pads by 6pt on each side; take that out of the margins
        leftMargin=_MARGIN - 6, rightMargin=_MARGIN - 6, topMargin=14 * mm, bottomMargin=20 * mm,
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
