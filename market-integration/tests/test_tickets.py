"""Trade tickets: building one (instrument, settlement, amounts), storage
and its status guard, filing with the depository, and the notifications
both parties get, over HTTP and /ws/notifications."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.websockets import WebSocketDisconnect

from app.bond_math.bonds import Bond, supports
from app.bond_math.calculator import Security, calculate
from app.connectors.depository import DepositoryUnavailable, FilingReceipt
from app.identity import USER_HEADER
from app.instruments import INSTRUMENTS
from app.models.ticket import TicketRequest
from app.tickets import TicketError, build_ticket
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
        return FilingReceipt(False, reason="no such securities account")

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
