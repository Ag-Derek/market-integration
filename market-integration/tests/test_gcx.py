"""Ghana Commodity Exchange (GCX) spot contracts: the instrument terms,
the seed, the mock connector's GCX semantics, validation, charting,
tickets, the world-benchmark comparison and the API."""

import random
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from app.aggregation.market_aggregator import _mark
from app.commodities import COMMODITIES
from app.commodities.gcx import BENCHMARKS, Benchmark, benchmark_code
from app.connectors.depository import EmailDepository, OutboxSender
from app.connectors.fixed_income_mock import MockFixedIncomeMarket
from app.connectors.gcx_mock import MockCommodityConnector
from app.instruments import INSTRUMENTS, MOCK_SEEDS
from app.instruments.search import InstrumentSearch
from app.models.commodity import CommodityTick, format_tonnes
from app.models.instrument import COMMODITY_NAMES, Instrument
from app.models.ticket import TicketRequest
from app.tickets import TicketError, build_ticket
from app.tickets.desk import _describe
from app.tickets.pdf import render_ticket_pdf
from app.validation.market_validator import validate_tick

COMMODITIES_SEEDED = {s: i for s, i in INSTRUMENTS.items() if i.asset_class == "commodity"}
# GCX's symbol scheme: "G", the warehouse, the crop, the grade.
CROP_CODES = {"WM": "white_maize", "YM": "yellow_maize", "SB": "soya_bean", "SS": "sesame", "SG": "sorghum", "RC": "rice"}
NOW = datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc)
MONDAY = date(2026, 10, 12)


def _commodity(**overrides) -> Instrument:
    base = dict(symbol="GTEST2", name="White Maize, Grade 2, Test", asset_class="commodity", sector="Agriculture",
                commodity="white_maize", grade=2, delivery_centre="Test", unit="MT")
    base.update(overrides)
    return Instrument(**base)


def _tick(**overrides) -> CommodityTick:
    """A fresh, internally consistent quote for a contract that has traded."""
    base = dict(
        symbol="GEJWM2", name="White Maize, Grade 2, Ejura", commodity="white_maize", grade=2,
        delivery_centre="Ejura", price=3010.0, previous_close=3000.0, change=10.0,
        open=2995.0, day_high=3020.0, day_low=2990.0, week52_high=4000.0, week52_low=2500.0,
        bid=3005.0, bid_size=2000, ask=3015.0, ask_size=1000,
        volume=12_500, value_traded=37_600.0, trade_count=4, avg_volume=30_000,
        timestamp=datetime.now(timezone.utc), last_trade_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return CommodityTick(**base)


# ---------------------------------------------------------------- instruments

def test_a_commodity_needs_its_contract_terms():
    assert _commodity().unit == "MT"
    with pytest.raises(ValueError, match="needs grade, delivery_centre"):
        _commodity(grade=None, delivery_centre=None)
    with pytest.raises(ValueError, match="grade"):
        _commodity(grade=7)


def test_contract_terms_are_for_commodities_only():
    with pytest.raises(ValueError, match="commodity, grade are for commodities, not a equity"):
        Instrument(symbol="XYZ", name="X", asset_class="equity", sector="Banking", commodity="sesame", grade=2)


def test_the_seed_has_the_gcx_contracts():
    assert len(COMMODITIES_SEEDED) == 17
    assert {i.commodity for i in COMMODITIES_SEEDED.values()} == set(COMMODITY_NAMES)
    for symbol, i in COMMODITIES_SEEDED.items():
        m = re.fullmatch(r"G([A-Z]{2})([A-Z]{2})([1-5])", symbol)
        assert m, symbol
        assert CROP_CODES[m.group(2)] == i.commodity and int(m.group(3)) == i.grade
        assert i.name == f"{COMMODITY_NAMES[i.commodity]}, Grade {i.grade}, {i.delivery_centre}"
        assert (i.sector, i.currency, i.unit, i.isin, i.status) == ("Agriculture", "GHS", "MT", None, "active")
        # Each says where its price and symbol come from.
        assert MOCK_SEEDS[symbol]["price"] > 0 and "gcx.com.gh" in MOCK_SEEDS[symbol]["source"]


def test_search_finds_contracts_by_crop_and_centre():
    search = InstrumentSearch(INSTRUMENTS.values())
    assert [i.symbol for i in search.search("maize tamale")] == ["GTAYM2"]
    assert {i.symbol for i in search.search("sesame", asset_class="commodity")} == {"GTASS2", "GSASS2"}
    assert search.search("GEJWM2")[0].symbol == "GEJWM2"


def test_the_fixed_income_market_leaves_commodities_alone():
    market = MockFixedIncomeMarket(calendar=None)
    market.start()
    assert not set(market.symbols()) & set(COMMODITIES_SEEDED)


# ---------------------------------------------------------------- connector

@pytest.fixture(scope="module")
def connector():
    import asyncio

    random.seed(7)
    c = MockCommodityConnector(["GEJWM2", "GTASS2"], interval_seconds=0.01)
    asyncio.run(c.connect())
    return c


def test_the_connector_only_carries_commodities():
    with pytest.raises(ValueError, match="not commodities: MTNGH"):
        MockCommodityConnector(["GEJWM2", "MTNGH"])


def test_its_first_quotes_are_valid(connector):
    for symbol in connector.symbols:
        tick = connector.normalize(connector._quote(symbol, connector._profiles[symbol]))
        assert tick.tick_type == "commodity" and tick.unit == "MT" and tick.currency == "GHS"
        assert tick.delivery_centre == INSTRUMENTS[symbol].delivery_centre
        assert validate_tick(tick), validate_tick(tick).errors
        assert tick.change == pytest.approx(tick.price - tick.previous_close)


def test_a_new_session_is_blank_until_the_first_trade(connector):
    symbol, profile = "GEJWM2", connector._profiles["GEJWM2"]
    last = round(connector._prices[symbol], 2)
    connector._new_session(symbol)
    blank = connector.normalize(connector._quote(symbol, profile))
    # GCX's close is the last trade, so it becomes the previous close.
    assert blank.previous_close == last and blank.price == last and blank.change == 0
    assert (blank.open, blank.day_high, blank.day_low, blank.volume, blank.trade_count) == (None, None, None, 0, 0)
    assert validate_tick(blank)

    connector._trade(symbol, profile, profile["trade_gap"])
    first = connector.normalize(connector._quote(symbol, profile))
    # The trade alone sets the day's range: the previous close isn't in it.
    assert first.open == first.day_high == first.day_low == first.price
    assert first.trade_count == 1 and first.volume > 0
    # Turnover: cedis per tonne times tonnes.
    assert first.value_traded == pytest.approx(first.price * first.volume / 1000, abs=0.01)
    assert validate_tick(first)

    for _ in range(5):
        connector._trade(symbol, profile, profile["trade_gap"])
    later = connector.normalize(connector._quote(symbol, profile))
    assert later.open == first.open and later.trade_count == 6
    assert later.day_low <= later.price <= later.day_high
    assert validate_tick(later)


def test_history_is_in_cedis_per_tonne(connector):
    import asyncio

    daily = asyncio.run(connector.fetch_history("GTASS2", "1d"))
    assert len(daily) > 365
    assert daily[-1].close == connector._prices["GTASS2"] == MOCK_SEEDS["GTASS2"]["price"]


# ---------------------------------------------------------------- validation and charting

def test_a_consistent_quote_is_valid():
    assert validate_tick(_tick())


@pytest.mark.parametrize("overrides, rule", [
    (dict(bid=3015.0, ask=3015.0), "crossed_book"),
    (dict(open=None), "missing_day_range"),
    (dict(volume=0, last_trade_at=None), "day_range_without_trade"),
    (dict(day_low=3030.0), "day_range_inverted"),
    (dict(price=3050.0, change=50.0), "outside_day_range"),
    (dict(week52_high=3005.0), "outside_52_week_range"),
])
def test_validation_rules(overrides, rule):
    result = validate_tick(_tick(**overrides))
    assert not result and rule in result.rules


def test_an_untraded_session_has_no_day_range():
    blank = _tick(open=None, day_high=None, day_low=None, volume=0, value_traded=0, trade_count=0,
                  price=3000.0, change=0.0, last_trade_at=None)
    assert validate_tick(blank)


def test_candles_chart_the_last_trade():
    assert _mark(_tick()) == (3010.0, None)


def test_tonnes_are_written_as_gcx_writes_them():
    assert format_tonnes(10.7826) == "10.7826 MT"
    assert format_tonnes(1500) == "1,500 MT"
    assert format_tonnes(2.5) == "2.5 MT"


# ---------------------------------------------------------------- benchmarks

def test_benchmarks_are_per_tonne_and_offered():
    assert set(BENCHMARKS.values()) <= set(COMMODITIES)
    for crop in BENCHMARKS:
        assert COMMODITIES[benchmark_code(crop)].unit == "$/mt"
    # No Pink Sheet sesame; its sorghum series ended in August 2020.
    assert benchmark_code("sesame") is None and benchmark_code("sorghum") is None


def test_the_gcx_premium_to_the_benchmark():
    b = Benchmark("MAIZE", "Maize", date(2026, 9, 1), 200.0, 2400.0)
    assert b.basis_percent(3000.0) == pytest.approx(25.0)
    assert b.basis_percent(1800.0) == pytest.approx(-25.0)
    assert Benchmark("MAIZE", "Maize", date(2026, 9, 1), 200.0, None).basis_percent(3000.0) is None


# ---------------------------------------------------------------- tickets

class Weekdays:
    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5


def _ticket(**overrides):
    base = dict(symbol="GEJWM2", side="buy", quantity=10.7826, price=3010.0, counterparty="bob")
    base.update(overrides)
    return build_ticket(TicketRequest(**base), "alice", instruments=INSTRUMENTS, calendar=Weekdays(),
                        today=MONDAY, now=NOW)


def test_a_commodity_ticket_is_tonnes_at_a_price_per_tonne_settling_t_plus_1():
    t = _ticket(trade_date=date(2026, 10, 9))  # a Friday
    assert t.asset_class == "commodity" and t.name == "White Maize, Grade 2, Ejura" and t.isin is None
    assert t.settlement_date == date(2026, 10, 12)  # T+1 over the weekend
    assert t.consideration == round(10.7826 * 3010.0, 2) == t.net_amount
    assert t.accrued_interest == 0 and t.accrued_source == "none"


def test_commodity_ticket_rules():
    with pytest.raises(TicketError, match="4 decimal places"):
        _ticket(quantity=1.00001)
    with pytest.raises(TicketError, match="is a commodity: no accrued interest"):
        _ticket(accrued_interest=5.0)


def test_commodity_tickets_read_in_tonnes():
    t = _ticket()
    assert "you bought 10.7826 MT of White Maize, Grade 2, Ejura (GEJWM2) at GHS 3,010.00 per MT" in _describe(t, "alice")
    assert render_ticket_pdf(t, NOW).startswith(b"%PDF")
    message = EmailDepository("csd@example.com", "desk@example.com", OutboxSender("unused")).message(t)
    assert "GEJWM2 10.7826 MT @ 3,010" in message["Subject"]
    assert "Price:            3,010.0000 GHS per MT" in message.get_body(("plain",)).get_content()


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

def test_the_contracts_endpoint(client):
    body = client.get("/gcx/contracts").json()
    assert (body["exchange"], body["currency"], body["unit"]) == ("GCX", "GHS", "MT")
    assert body["status"]["badge"] in ("live", "delayed", "closed", "disconnected")
    rows = body["contracts"]
    assert {r["symbol"] for r in rows} == set(COMMODITIES_SEEDED)
    # By crop, in COMMODITY_NAMES order.
    order = list(COMMODITY_NAMES)
    assert [order.index(r["commodity"]) for r in rows] == sorted(order.index(r["commodity"]) for r in rows)
    for r in rows:
        if r["price"] is not None:
            assert r["price_per_bag"] == pytest.approx(r["price"] / 20, abs=0.01)
        if r["commodity"] in ("sesame", "sorghum"):
            assert r["benchmark"] is None


def test_contracts_carry_their_benchmark_in_cedis(client, monkeypatch):
    from app import main
    from app.commodities import Price
    from app.models.fx import FxRate

    sep = date(2026, 9, 1)
    held = {"MAIZE": Price("worldbank", "Maize", "$/mt", sep, 200.0)}
    monkeypatch.setattr(main.commodity_prices, "latest", lambda code: held.get(code))
    monkeypatch.setattr(main.fx_rates, "has", lambda code: code == "USD")
    fixes = [FxRate(currency="USD", date=date(2026, 9, d), mid=12.0, buying=11.99, selling=12.01, source_pair="USDGHS")
             for d in (1, 2)]
    monkeypatch.setattr(main.fx_rates, "history", lambda code, since=None: fixes)

    rows = {r["symbol"]: r for r in client.get("/gcx/contracts").json()["contracts"]}
    maize = rows["GEJWM2"]["benchmark"]
    assert maize == {
        "code": "MAIZE", "name": "Maize", "month": "2026-09", "usd_per_mt": 200.0, "ghs_per_mt": 2400.0,
        "basis_percent": pytest.approx((rows["GEJWM2"]["price"] / 2400.0 - 1) * 100),
    }
    assert rows["GTAYM2"]["benchmark"]["code"] == "MAIZE"   # yellow maize too
    assert rows["GTASB2"]["benchmark"] is None              # soybeans not held here
    assert rows["GTASS2"]["benchmark"] is None              # sesame has none


def test_one_contract_and_its_page(client):
    assert client.get("/gcx/contracts/gsass2").json()["delivery_centre"] == "Sandema"
    assert client.get("/gcx/contracts/MTNGH").status_code == 404
    page = client.get("/commodity/GSASS2")
    assert page.status_code == 200 and "/gcx/contracts/" in page.text
    assert client.get("/commodity/MTNGH").status_code == 404


def test_search_offers_contracts(client):
    rows = client.get("/search", params={"q": "soya", "asset_class": "commodity"}).json()
    assert {r["symbol"] for r in rows} == {"GTASB2", "GWASB2", "GKUSB2"}
    assert all(r["asset_class"] == "commodity" for r in rows)


def test_candles_and_the_graph_are_in_cedis_per_tonne(client):
    candles = client.get("/candles", params={"symbol": "GEJWM2", "range": "1M"}).json()
    assert candles["currency"] == "GHS" and candles["candles"]
    series = client.get("/graph/series", params={"series": "GEJWM2", "range": "1M"}).json()["series"][0]
    assert (series["kind"], series["unit"]) == ("spot", "GHS per MT")


def test_status_has_the_gcx_session(client):
    with client.websocket_connect("/ws/market") as ws:
        welcome = ws.receive_json()
    assert welcome["status"]["commodities"]["exchange"] == "GCX"
    assert {welcome["asset_classes"][s] for s in COMMODITIES_SEEDED} == {"commodity"}


# ---------------------------------------------------------------- pinned contracts

def _user():
    import uuid

    from app.identity import USER_HEADER

    return {USER_HEADER: "test-" + uuid.uuid4().hex}


def test_the_commodity_list_is_validated_like_the_equity_one():
    from app.watchlists import WatchlistError, validate

    assert validate([" gtass2", "GEJWM2"], asset_class="commodity") == ["GTASS2", "GEJWM2"]
    with pytest.raises(WatchlistError, match="Only GCX contracts can be pinned here: MTNGH"):
        validate(["GEJWM2", "MTNGH"], asset_class="commodity")
    with pytest.raises(WatchlistError, match="Only equities can be pinned here: GEJWM2"):
        validate(["MTNGH", "GEJWM2"])


def test_the_commodity_list_has_its_own_table(tmp_path):
    from app.watchlists.store import WatchlistStore

    db = tmp_path / "w.db"
    WatchlistStore(db).put("u", ["MTNGH"])
    WatchlistStore(db, table="commodity_watchlists").put("u", ["GEJWM2"])
    assert WatchlistStore(db).get("u")["symbols"] == ["MTNGH"]
    assert WatchlistStore(db, table="commodity_watchlists").get("u")["symbols"] == ["GEJWM2"]
    with pytest.raises(ValueError, match="unknown watchlist table"):
        WatchlistStore(db, table="users")


def test_pinning_contracts_over_http(client):
    user = _user()
    empty = client.get("/gcx/watchlist/me", headers=user).json()
    assert empty["symbols"] == [] and empty["updated_at"] is None and empty["max_pins"] > 0

    saved = client.put("/gcx/watchlist/me", headers=user, json={"symbols": ["gtass2", "GEJWM2"]})
    assert saved.status_code == 200 and saved.json()["symbols"] == ["GTASS2", "GEJWM2"]
    assert client.get("/gcx/watchlist/me", headers=user).json()["symbols"] == ["GTASS2", "GEJWM2"]

    # An equity is refused, and nothing is saved.
    refused = client.put("/gcx/watchlist/me", headers=user, json={"symbols": ["GEJWM2", "MTNGH"]})
    assert refused.status_code == 422 and refused.json()["detail"]["symbols"] == ["MTNGH"]
    assert client.get("/gcx/watchlist/me", headers=user).json()["symbols"] == ["GTASS2", "GEJWM2"]

    # The ticker's equity watchlist is a separate list.
    assert "GEJWM2" not in client.get("/watchlists/me", headers=user).json()["symbols"]


def test_a_commodity_ticket_over_http(client):
    preview = client.post("/tickets/preview", json={
        "symbol": "GEJWM2", "side": "buy", "quantity": 2.5, "price": 3000, "counterparty": "bob",
    })
    assert preview.status_code == 200
    t = preview.json()
    assert (t["asset_class"], t["consideration"]) == ("commodity", 7500.0)
    trade_date = date.fromisoformat(t["trade_date"])
    assert date.fromisoformat(t["settlement_date"]) > trade_date
    assert date.fromisoformat(t["settlement_date"]) - trade_date <= timedelta(days=4)  # one business day
