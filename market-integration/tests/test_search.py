import time

import pytest

from app.instruments import INSTRUMENTS
from app.instruments.search import InstrumentSearch
from app.models.instrument import Instrument


def _eq(symbol, name, **kw):
    return Instrument(symbol=symbol, name=name, asset_class="equity", sector="Banking", **kw)


# One instrument per ranking tier for the query "gc".
UNIVERSE = [
    _eq("XYZ", "Magicgc Ltd"),                          # 4: substring of a name word
    _eq("ABC", "Gc Holdings PLC"),                      # 2: name word prefix
    _eq("GCBX", "Other Co"),                            # 1: symbol prefix
    _eq("GC", "Exactly Gc"),                            # 0: exact symbol
    Instrument(symbol="GHGGOG069931", name="GOG-BD-13/02/29-A6123-1833-9.85",
               asset_class="bond", sector="Government", isin="GHGGOG069931",
               tenor="2023-GC-3", maturity_date="2029-02-13"),  # 3: tenor word prefix
    _eq("NOPE", "Unrelated PLC"),
]


def _symbols(index, q, **kw):
    return [i.symbol for i in index.search(q, **kw)]


def test_ranking_order():
    index = InstrumentSearch(UNIVERSE)
    assert _symbols(index, "gc") == ["GC", "GCBX", "ABC", "GHGGOG069931", "XYZ"]
    assert _symbols(index, "GC", limit=2) == ["GC", "GCBX"]


def test_active_instruments_rank_above_suspended_ones_in_a_tier():
    index = InstrumentSearch([_eq("GCA", "A", status="suspended"), _eq("GCB", "B")])
    assert _symbols(index, "gc") == ["GCB", "GCA"]


def test_case_and_punctuation_are_ignored():
    index = InstrumentSearch([_eq("FML", "Fan Milk PLC"), _eq("SCB-PREF", "SCB Preference")])
    for q in ("fan milk", "FAN-MILK", "fan  milk.", "fanmilk"):
        assert _symbols(index, q)[0] == "FML", q
    assert _symbols(index, "scbpref") == ["SCB-PREF"]
    assert _symbols(index, "scb pref") == ["SCB-PREF"]


def test_every_query_word_must_match():
    index = InstrumentSearch([_eq("FML", "Fan Milk PLC"), _eq("FAN", "Fan Club")])
    assert _symbols(index, "fan mi") == ["FML"]
    assert _symbols(index, "milk fan") == ["FML"]


@pytest.mark.parametrize("q", ["", "   ", "-", "./"])
def test_empty_queries_return_nothing(q):
    assert InstrumentSearch(UNIVERSE).search(q) == []


def test_a_one_character_query_only_matches_symbols_and_name_words():
    index = InstrumentSearch(UNIVERSE)
    # "o" is in "Other Co" and "Unrelated" only as a substring or later word.
    assert _symbols(index, "o") == ["GCBX"]  # "Other"
    assert _symbols(index, "x") == ["XYZ"]   # symbol prefix, not the "x" in GCBX


def test_asset_class_filter():
    index = InstrumentSearch(UNIVERSE)
    assert _symbols(index, "gc", asset_class="bond") == ["GHGGOG069931"]
    assert _symbols(index, "gc", asset_class="bill") == []


# ---------------------------------------------------------------- real master

INDEX = InstrumentSearch(INSTRUMENTS.values())


def test_gc_suggests_gcb_first():
    assert _symbols(INDEX, "gc")[0] == "GCB"


def test_fan_milk_finds_fml():
    assert _symbols(INDEX, "fan milk")[0] == "FML"


def test_a_maturity_year_finds_bills_and_bonds_maturing_then():
    expected = {s for s, i in INSTRUMENTS.items() if i.maturity_date and i.maturity_date.year == 2027}
    found = _symbols(INDEX, "2027", limit=len(INSTRUMENTS))
    assert expected and set(found[:len(expected)]) == expected
    # Within the maturity-date tier, soonest first. (ILL-BD-09/04/2027
    # ranks above them all: "2027" starts a word of its name.)
    assert found[0] == "GHCILL074111"
    dates = [INSTRUMENTS[s].maturity_date for s in found[1:len(expected)]]
    assert dates == sorted(dates)


@pytest.mark.parametrize("isin", ["GHGGOG069931", "ghggog069931", "GHGGOGI01883"])
def test_an_isin_finds_its_bill_or_bond(isin):
    assert _symbols(INDEX, isin)[0] == isin.upper()


def test_an_equity_isin_finds_the_equity():
    assert _symbols(INDEX, "GHEMTN051541")[0] == "MTNGH"


def test_p95_latency_over_the_full_universe_is_under_50ms():
    queries = ["g", "gc", "gcb", "fan milk", "2027", "GHGGOG", "bill", "zz", "364 day", "bank"]
    timings = []
    for _ in range(20):
        for q in queries:
            start = time.perf_counter()
            INDEX.search(q)
            timings.append(time.perf_counter() - start)
    timings.sort()
    assert timings[int(len(timings) * 0.95)] < 0.050


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

def test_search_endpoint(client):
    body = client.get("/search", params={"q": "gc"}).json()
    assert len(body) <= 10
    assert body[0]["symbol"] == "GCB"
    assert set(body[0]) == {"symbol", "name", "asset_class", "price", "change"}
    assert body[0]["name"] == "GCB Bank PLC" and body[0]["asset_class"] == "equity"


def test_search_endpoint_includes_live_prices(client):
    [gcb] = client.get("/search", params={"q": "GCB", "limit": 1}).json()
    quote = client.get("/market/GCB").json()
    assert gcb["price"] is not None and gcb["change"] is not None
    assert isinstance(gcb["price"], float) and quote["vwap"] > 0

    [bond] = client.get("/search", params={"q": "GHGGOG069931", "limit": 1}).json()
    assert bond["asset_class"] == "bond"


def test_search_endpoint_filters_and_validates(client):
    bills = client.get("/search", params={"q": "2027", "asset_class": "bill", "limit": 50}).json()
    assert bills and all(r["asset_class"] == "bill" for r in bills)
    assert client.get("/search").json() == []
    assert client.get("/search", params={"q": ""}).json() == []
    assert client.get("/search", params={"q": "gc", "limit": 0}).status_code == 422
    assert client.get("/search", params={"q": "gc", "limit": 51}).status_code == 422
    assert client.get("/search", params={"q": "gc", "asset_class": "crypto"}).status_code == 422
