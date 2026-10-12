"""Trade tickets: building one (instrument, settlement, amounts), storage
and its status guard, filing with the depository, and the notifications
both parties get, over HTTP and /ws/notifications."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.websockets import WebSocketDisconnect

from app.bond_math.bonds import Bond, supports
from app.bond_math.calculator import Security, calculate
from app.connectors.depository import DepositoryUnavailable, EmailDepository, FilingReceipt, OutboxSender
from app.identity import USER_HEADER
from app.instruments import INSTRUMENTS
from app.models.ticket import TicketRequest
from app.tickets import TicketError, build_ticket
from app.tickets.pdf import render_ticket_pdf
from app.tickets.store import TicketStore

NOW = datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc)
MONDAY = date(2026, 10, 12)


class Weekdays:
    """Mon-Fri, less the given holidays."""

    def __init__(self, *holidays: date):
        self.holidays = set(holidays)

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays


def _request(**overrides) -> TicketRequest:
    base = dict(symbol="MTNGH", side="buy", quantity=1000, price=6.54, counterparty="bob")
    base.update(overrides)
    return TicketRequest(**base)


def _build(request=None, *, calendar=None, today=MONDAY, lag=3):
    return build_ticket(
        request or _request(), "alice",
        instruments=INSTRUMENTS, calendar=calendar or Weekdays(), today=today, now=NOW,
        equity_settlement_lag=lag,
    )


def _supported_bond():
    return next(
        i for i in INSTRUMENTS.values()
        if i.asset_class == "bond" and i.status == "active" and supports(i) and i.maturity_date > MONDAY + timedelta(days=30)
    )


# ---------------------------------------------------------------- building

def test_an_equity_ticket():
    t = _build(_request(symbol=" mtngh", trade_date=date(2026, 10, 8)))  # a Thursday
    assert t.id.startswith("TKT-") and t.status == "draft"
    assert t.symbol == "MTNGH" and t.name == INSTRUMENTS["MTNGH"].name and t.isin == INSTRUMENTS["MTNGH"].isin
    assert t.asset_class == "equity" and t.currency == "GHS"
    assert t.settlement_date == date(2026, 10, 13)  # T+3 over the weekend
    assert t.consideration == 6540.0 and t.accrued_interest == 0 and t.net_amount == 6540.0
    assert (t.buyer, t.seller) == ("alice", "bob")
    assert (_build(_request(side="sell")).buyer, _build(_request(side="sell")).seller) == ("bob", "alice")


def test_settlement_skips_holidays_and_follows_the_lag():
    holiday = Weekdays(date(2026, 10, 13))
    assert _build(calendar=holiday).settlement_date == date(2026, 10, 16)
    assert _build(lag=2).settlement_date == date(2026, 10, 14)
    assert _build().trade_date == MONDAY  # defaults to today


def test_a_bond_ticket_carries_the_calculators_invoice():
    bond = _supported_bond()
    t = _build(_request(symbol=bond.symbol, quantity=1_000_000, price=98.5))
    assert t.asset_class == "bond" and t.settlement_date == date(2026, 10, 14)  # T+2
    invoice = calculate(
        Security(bond.maturity_date, Bond.from_instrument(bond)), t.settlement_date, 1_000_000, price=98.5
    ).invoice
    assert t.consideration == round(invoice.principal, 2) == 985_000.0
    assert t.accrued_interest == round(invoice.accrued, 2) and t.days_accrued == invoice.days_accrued
    assert t.net_amount == round(t.consideration + t.accrued_interest, 2)


@pytest.mark.parametrize("overrides,match", [
    (dict(symbol="NOPE"), "Unknown instrument 'NOPE'"),
    (dict(symbol="PBC"), "PBC is suspended"),
    (dict(counterparty="alice"), "can't be the trader"),
    (dict(trade_date=MONDAY + timedelta(days=1)), "in the future"),
    (dict(trade_date=date(2026, 10, 11)), "isn't a trading day"),
    (dict(quantity=10.5), "whole number of shares"),
])
def test_bad_tickets_are_refused(overrides, match):
    with pytest.raises(TicketError, match=match) as e:
        _build(_request(**overrides))
    assert e.value.status_code == 422


# ---------------------------------------------------------------- storage

def test_the_store_moves_a_ticket_only_from_the_expected_status(tmp_path):
    store = TicketStore(tmp_path / "db.sqlite")
    t = store.add(_build())
    assert store.transition(t.id, "draft", status="filing").status == "filing"
    assert store.transition(t.id, "draft", status="filing") is None  # someone else got there first
    filed = store.transition(t.id, "filing", status="filed", depository_reference="CSD-1")
    assert filed.depository_reference == "CSD-1" and filed.updated_at > t.updated_at

    restarted = TicketStore(tmp_path / "db.sqlite")
    assert restarted.get(t.id) == filed
    assert [x.id for x in restarted.for_user("bob")] == [t.id]
    assert restarted.for_user("bob", "draft") == [] and restarted.for_user("carol") == []


def test_notifications_are_kept_per_user_and_can_be_marked_read(tmp_path):
    store = TicketStore(tmp_path / "db.sqlite")
    first = store.notify("alice", "ticket_filed", "TKT-1", "one")
    store.notify("alice", "ticket_rejected", "TKT-2", "two")
    store.notify("bob", "ticket_filed", "TKT-1", "one")
    assert [n.message for n in store.notifications("alice")] == ["two", "one"]
    assert store.mark_read("alice", first.id) and not store.mark_read("bob", first.id)
    assert [n.message for n in store.notifications("alice", unread_only=True)] == ["two"]


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

def _user(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _as(user: str) -> dict:
    return {USER_HEADER: user}


def _trading_day() -> date:
    from app.main import calendar

    day = calendar.local_date(datetime.now(timezone.utc))
    while not calendar.is_trading_day(day):
        day -= timedelta(days=1)
    return day


def _create(client, trader, counterparty, **overrides):
    body = dict(
        symbol="MTNGH", side="buy", quantity=500, price=6.5, counterparty=counterparty,
        trade_date=_trading_day().isoformat(),
    )
    body.update(overrides)
    response = client.post("/tickets", headers=_as(trader), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_file_and_notify_both_parties(client):
    trader, counterparty, stranger = _user("trader"), _user("cpty"), _user("other")
    ticket = _create(client, trader, counterparty)
    assert ticket["status"] == "draft" and ticket["net_amount"] == 3250.0
    assert ticket["buyer"] == trader and ticket["seller"] == counterparty

    # Both parties can see it; nobody else can.
    assert client.get(f"/tickets/{ticket['id']}", headers=_as(counterparty)).json()["id"] == ticket["id"]
    assert client.get(f"/tickets/{ticket['id']}", headers=_as(stranger)).status_code == 404
    assert [t["id"] for t in client.get("/tickets/me", headers=_as(counterparty)).json()] == [ticket["id"]]

    # Only the trader files it.
    assert client.post(f"/tickets/{ticket['id']}/file", headers=_as(counterparty)).status_code == 403
    filed = client.post(f"/tickets/{ticket['id']}/file", headers=_as(trader))
    assert filed.status_code == 200
    filed = filed.json()
    assert filed["status"] == "filed" and filed["depository_reference"].startswith("CSD-") and filed["filed_at"]
    assert client.post(f"/tickets/{ticket['id']}/file", headers=_as(trader)).status_code == 409

    for user, verb in ((trader, "bought"), (counterparty, "sold")):
        notes = client.get("/notifications/me", headers=_as(user)).json()
        assert len(notes) == 1
        assert notes[0]["kind"] == "ticket_filed" and notes[0]["ticket_id"] == ticket["id"]
        assert f"you {verb} 500 MTNGH" in notes[0]["message"] and filed["depository_reference"] in notes[0]["message"]
    assert client.get("/notifications/me", headers=_as(stranger)).json() == []

    note_id = client.get("/notifications/me", headers=_as(trader)).json()[0]["id"]
    assert client.post(f"/notifications/me/{note_id}/read", headers=_as(counterparty)).status_code == 404
    assert client.post(f"/notifications/me/{note_id}/read", headers=_as(trader)).status_code == 204
    assert client.get("/notifications/me?unread=true", headers=_as(trader)).json() == []


def test_a_rejection_is_recorded_and_both_parties_hear_of_it(client, monkeypatch):
    from app.main import ticket_desk

    async def refuse(ticket):
        return FilingReceipt("rejected", reason="no such securities account")

    monkeypatch.setattr(ticket_desk.depository, "file", refuse)
    trader, counterparty = _user("trader"), _user("cpty")
    ticket = _create(client, trader, counterparty, side="sell")
    rejected = client.post(f"/tickets/{ticket['id']}/file", headers=_as(trader)).json()
    assert rejected["status"] == "rejected" and rejected["rejection_reason"] == "no such securities account"
    for user in (trader, counterparty):
        [note] = client.get("/notifications/me", headers=_as(user)).json()
        assert note["kind"] == "ticket_rejected" and "no such securities account" in note["message"]


def test_an_unreachable_depository_leaves_a_draft_to_send_again(client, monkeypatch):
    from app.main import ticket_desk

    async def down(ticket):
        raise DepositoryUnavailable("connection refused")

    trader, counterparty = _user("trader"), _user("cpty")
    ticket = _create(client, trader, counterparty)
    with monkeypatch.context() as m:
        m.setattr(ticket_desk.depository, "file", down)
        response = client.post(f"/tickets/{ticket['id']}/file", headers=_as(trader))
    assert response.status_code == 503
    assert client.get(f"/tickets/{ticket['id']}", headers=_as(trader)).json()["status"] == "draft"
    assert client.get("/notifications/me", headers=_as(trader)).json() == []

    assert client.post(f"/tickets/{ticket['id']}/file", headers=_as(trader)).json()["status"] == "filed"


@pytest.mark.parametrize("body", [
    {"symbol": "MTNGH", "side": "hold", "quantity": 1, "price": 1, "counterparty": "x"},
    {"symbol": "MTNGH", "side": "buy", "quantity": 0, "price": 1, "counterparty": "x"},
    {"symbol": "MTNGH", "side": "buy", "quantity": 1, "price": 1},
    {"symbol": "NOPE", "side": "buy", "quantity": 1, "price": 1, "counterparty": "x"},
])
def test_bad_requests_are_rejected(client, body):
    assert client.post("/tickets", headers=_as(_user("trader")), json=body).status_code == 422


def test_notifications_are_pushed_live_to_the_counterparty(client):
    trader, counterparty = _user("trader"), _user("cpty")
    ticket = _create(client, trader, counterparty)
    with client.websocket_connect("/ws/notifications", headers=_as(counterparty)) as ws:
        assert ws.receive_json() == {"type": "welcome", "user_id": counterparty, "unread": 0}
        client.post(f"/tickets/{ticket['id']}/file", headers=_as(trader))
        message = ws.receive_json()
    assert message["type"] == "notification"
    assert message["notification"]["ticket_id"] == ticket["id"]
    assert message["notification"]["user_id"] == counterparty
    assert message["notification"]["kind"] == "ticket_filed"


def test_the_notification_socket_needs_a_user(client):
    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws/notifications") as ws:
            ws.receive_json()
    assert e.value.code == 1008


# ---------------------------------------------------------------- printing and email

def test_the_pdf_is_a_titled_a4_document():
    for ticket in (_build(), _build(_request(symbol=_supported_bond().symbol, quantity=1_000_000, price=98.5))):
        pdf = render_ticket_pdf(ticket.model_copy(update={"notes": "Per call <10:12> & chat"}))
        assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
        assert f"Trade ticket {ticket.id}".encode() in pdf
        assert b"/MediaBox [ 0 0 595.2756 841.8898 ]" in pdf  # A4


def _email_depository(directory, **kwargs):
    return EmailDepository("filings@csd.example", "desk@broker.example", OutboxSender(directory), **kwargs)


async def test_the_email_carries_the_ticket_and_its_pdf(tmp_path):
    ticket = _build(_request(trader_account="ACC-A", counterparty_account="ACC-B"))
    depository = _email_depository(tmp_path, cc=("backoffice@broker.example",))
    receipt = await depository.file(ticket)
    assert receipt == FilingReceipt("sent", sent_to="filings@csd.example")

    import email
    from email import policy

    message = email.message_from_bytes((tmp_path / f"{ticket.id}.eml").read_bytes(), policy=policy.default)
    assert message["To"] == "filings@csd.example" and message["From"] == "desk@broker.example"
    assert message["Cc"] == "backoffice@broker.example"
    assert message["Subject"].startswith(f"Trade ticket {ticket.id}: MTNGH 1,000 @ 6.54")
    body = message.get_body(("plain",)).get_content()
    assert "Buyer:            alice (account ACC-A)" in body and "GHS 6,540.00" in body
    [attachment] = list(message.iter_attachments())
    assert attachment.get_filename() == f"{ticket.id}.pdf"
    assert attachment.get_content().startswith(b"%PDF-")


async def test_a_mail_failure_means_nothing_was_sent(tmp_path):
    class Broken:
        def send(self, message):
            raise ConnectionRefusedError("no mail server")

    depository = EmailDepository("filings@csd.example", "desk@broker.example", Broken())
    with pytest.raises(DepositoryUnavailable, match="no mail server"):
        await depository.file(_build())


def test_the_pdf_endpoint_is_for_the_parties_only(client):
    trader, counterparty = _user("trader"), _user("cpty")
    ticket = _create(client, trader, counterparty)
    for user in (trader, counterparty):
        response = client.get(f"/tickets/{ticket['id']}/pdf", headers=_as(user))
        assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"] == f'inline; filename="{ticket["id"]}.pdf"'
        assert response.content.startswith(b"%PDF-")
    download = client.get(f"/tickets/{ticket['id']}/pdf?download=true", headers=_as(trader))
    assert download.headers["content-disposition"].startswith("attachment;")
    assert client.get(f"/tickets/{ticket['id']}/pdf", headers=_as(_user("other"))).status_code == 404


def test_an_emailed_ticket_waits_for_the_depositorys_reply(client, monkeypatch, tmp_path):
    from app.main import ticket_desk

    monkeypatch.setattr(ticket_desk, "depository", _email_depository(tmp_path))
    trader, counterparty = _user("trader"), _user("cpty")
    ticket = _create(client, trader, counterparty)

    # Not sent yet: nothing to confirm.
    reply = {"accepted": True, "reference": "CSD/2026/0457"}
    assert client.post(f"/tickets/{ticket['id']}/confirmation", headers=_as(trader), json=reply).status_code == 409

    sent = client.post(f"/tickets/{ticket['id']}/file", headers=_as(trader)).json()
    assert sent["status"] == "sent" and sent["sent_to"] == "filings@csd.example" and sent["sent_at"]
    assert sent["depository_reference"] is None
    assert (tmp_path / f"{ticket['id']}.eml").exists()
    for user in (trader, counterparty):
        [note] = client.get("/notifications/me", headers=_as(user)).json()
        assert note["kind"] == "ticket_sent" and "awaiting its confirmation" in note["message"]

    # Only the trader records the reply, and it must say what happened.
    assert client.post(f"/tickets/{ticket['id']}/confirmation", headers=_as(counterparty), json=reply).status_code == 403
    bad = {"accepted": True}
    assert client.post(f"/tickets/{ticket['id']}/confirmation", headers=_as(trader), json=bad).status_code == 422

    filed = client.post(f"/tickets/{ticket['id']}/confirmation", headers=_as(trader), json=reply).json()
    assert filed["status"] == "filed" and filed["depository_reference"] == "CSD/2026/0457" and filed["filed_at"]
    for user in (trader, counterparty):
        latest = client.get("/notifications/me", headers=_as(user)).json()[0]
        assert latest["kind"] == "ticket_filed" and "CSD/2026/0457" in latest["message"]
    assert client.post(f"/tickets/{ticket['id']}/confirmation", headers=_as(trader), json=reply).status_code == 409


def test_the_depository_can_refuse_an_emailed_ticket(client, monkeypatch, tmp_path):
    from app.main import ticket_desk

    monkeypatch.setattr(ticket_desk, "depository", _email_depository(tmp_path))
    trader, counterparty = _user("trader"), _user("cpty")
    ticket = _create(client, trader, counterparty)
    client.post(f"/tickets/{ticket['id']}/file", headers=_as(trader))
    rejected = client.post(
        f"/tickets/{ticket['id']}/confirmation", headers=_as(trader),
        json={"accepted": False, "reason": "seller account not found"},
    ).json()
    assert rejected["status"] == "rejected" and rejected["rejection_reason"] == "seller account not found"
    latest = client.get("/notifications/me", headers=_as(counterparty)).json()[0]
    assert latest["kind"] == "ticket_rejected" and "seller account not found" in latest["message"]


# ---------------------------------------------------------------- the Trade Tickets window

def _unpriced_bond():
    return next(
        i for i in INSTRUMENTS.values()
        if i.asset_class == "bond" and i.status == "active" and not supports(i)
        and i.maturity_date and i.maturity_date > MONDAY + timedelta(days=30)
    )


def test_a_bond_without_terms_takes_the_agreed_accrued_interest():
    bond = _unpriced_bond()
    request = _request(symbol=bond.symbol, quantity=1_000_000, price=99.25)
    with pytest.raises(TicketError, match="enter the amount agreed"):
        _build(request)
    previewed = build_ticket(
        request, "alice", instruments=INSTRUMENTS, calendar=Weekdays(), today=MONDAY, now=NOW, complete=False,
    )
    assert previewed.accrued_source == "required" and previewed.consideration == 992_500.0

    t = _build(_request(symbol=bond.symbol, quantity=1_000_000, price=99.25, accrued_interest=12_345.67))
    assert t.accrued_source == "entered" and t.days_accrued is None
    assert t.accrued_interest == 12_345.67 and t.net_amount == 1_004_845.67
    assert render_ticket_pdf(t).startswith(b"%PDF-")


@pytest.mark.parametrize("symbol,match", [
    ("MTNGH", "an equity: no accrued interest"),
    (None, "calculated from its terms"),  # a bond the bond math prices
])
def test_accrued_interest_is_refused_where_it_isnt_typed_in(symbol, match):
    symbol = symbol or _supported_bond().symbol
    with pytest.raises(TicketError, match=match):
        _build(_request(symbol=symbol, accrued_interest=10))


def test_preview_works_out_a_ticket_without_saving_it(client):
    trader = _user("trader")
    body = dict(symbol="MTNGH", side="buy", quantity=100, price=6.5, counterparty="someone",
                trade_date=_trading_day().isoformat())
    preview = client.post("/tickets/preview", headers=_as(trader), json=body)
    assert preview.status_code == 200 and preview.json()["net_amount"] == 650.0
    assert client.get("/tickets/me", headers=_as(trader)).json() == []
    assert client.post("/tickets/preview", headers=_as(trader), json={**body, "symbol": "NOPE"}).status_code == 422

    bond = _unpriced_bond()
    unpriced = client.post("/tickets/preview", headers=_as(trader), json={**body, "symbol": bond.symbol, "price": 99})
    assert unpriced.status_code == 200 and unpriced.json()["accrued_source"] == "required"
    saved = client.post("/tickets", headers=_as(trader), json={**body, "symbol": bond.symbol, "price": 99})
    assert saved.status_code == 422 and "enter the amount agreed" in saved.json()["detail"]


def test_a_browser_can_choose_its_user_id(client):
    client.cookies.clear()
    try:
        first = client.get("/identity/me").json()["user_id"]
        assert first.startswith("anon-")
        assert client.put("/identity/me", json={"user_id": "has spaces"}).status_code == 400
        assert client.put("/identity/me", json={"user_id": " ama.mensah "}).json() == {"user_id": "ama.mensah"}
        assert client.get("/identity/me").json() == {"user_id": "ama.mensah"}
        assert client.get("/identity/me", headers=_as("kofi")).json() == {"user_id": "kofi"}  # a header still wins
    finally:
        client.cookies.clear()


def test_the_trade_tickets_window_is_served_and_linked(client):
    page = client.get("/tickets")
    assert page.status_code == 200 and "<title>Trade Tickets</title>" in page.text
    assert "VCON" not in page.text
    for path in ("/fixed-income", "/yield-curve", "/ticker"):
        assert 'href="/tickets">Trade Tickets</a>' in client.get(path).text
    assert '"/tickets?symbol="' in client.get("/fixed-income").text
    assert 'id="ticketBtn"' in client.get("/bond/" + _supported_bond().symbol).text
