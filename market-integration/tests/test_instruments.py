import json

import pytest

from app import config
from app.connectors.market_connector import MockMarketConnector
from app.instruments import INSTRUMENTS, MOCK_SEEDS, SEED_PATH, load_seed, streamable_symbols
from app.instruments.store import InstrumentStore
from app.models.instrument import Instrument, isin_format_ok


def _write_seed(tmp_path, entries):
    path = tmp_path / "instruments.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _seed_entries():
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- seed file

def test_checked_in_seed_covers_the_gse_and_every_entry_is_calibrated():
    assert len(INSTRUMENTS) == 42
    assert {"MTNGH", "GCB", "SCB", "SCB-PREF", "TOTAL", "UNIL", "PBC", "GLD"} <= set(INSTRUMENTS)
    assert all(i.asset_class == "equity" and i.status == "active" for i in INSTRUMENTS.values())
    assert set(MOCK_SEEDS) == set(INSTRUMENTS)
    assert INSTRUMENTS["MTNGH"].isin == "GHEMTN051541"
    assert INSTRUMENTS["GLD"].kind == "etf"


@pytest.mark.parametrize("isin,ok", [
    ("GHEMTN051541", True),
    ("GB0001500809", True),
    # As published by the GSE; fails the ISO 6166 check digit, which is
    # deliberately not enforced -- the exchange is authoritative.
    ("GH0000000118", True),
    ("GH00000001233", False),  # 13 characters
    ("GHEAB043726", False),    # 11 characters
    ("gh0000000094", False),   # lower case
])
def test_isin_format_validation(isin, ok):
    assert isin_format_ok(isin) is ok


def test_mac_carries_its_gse_published_isin():
    assert INSTRUMENTS["MAC"].isin == "GH0000000118"


@pytest.mark.parametrize("bad,match", [
    ({"isin": "GH00000001233"}, "invalid ISIN"),
    ({"asset_class": "crypto"}, "asset_class"),
    ({"status": "halted"}, "status"),
    ({"symbol": "mtngh"}, "upper-case"),
    ({"ticker": "X"}, "ticker"),  # unknown field
])
def test_bad_seed_entries_fail_loudly(tmp_path, bad, match):
    entry = {"symbol": "NEWCO", "name": "New Co PLC", "asset_class": "equity", "sector": "Banking"}
    with pytest.raises(ValueError, match=match):
        load_seed(_write_seed(tmp_path, [{**entry, **bad}]))


def test_duplicate_symbols_in_seed_are_rejected(tmp_path):
    entry = {"symbol": "NEWCO", "name": "New Co PLC", "asset_class": "equity", "sector": "Banking"}
    with pytest.raises(ValueError, match="duplicate"):
        load_seed(_write_seed(tmp_path, [entry, entry]))


def test_an_isin_shared_by_two_entries_is_rejected(tmp_path):
    base = {"name": "X", "asset_class": "equity", "sector": "Banking", "isin": "GH0000001183"}
    with pytest.raises(ValueError, match="GH0000001183 is on both 'MAC' and 'SAMBA'"):
        load_seed(_write_seed(tmp_path, [{**base, "symbol": "MAC"}, {**base, "symbol": "SAMBA"}]))


# ---------------------------------------------------------------- "no code change"

async def test_a_symbol_added_to_the_seed_reaches_the_feed_and_the_store(tmp_path):
    # Minimal entries: no ISIN and no mock calibration at all.
    entries = _seed_entries() + [
        {"symbol": "NEWCO", "name": "New Co PLC", "asset_class": "equity", "sector": "Banking"},
        {"symbol": "GOG-BD-TEST", "name": "Test bond", "asset_class": "bond", "sector": "Government"},
        {"symbol": "OLDCO", "name": "Old Co PLC", "asset_class": "equity", "sector": "Banking",
         "status": "delisted"},
    ]
    instruments, mock_seeds = load_seed(_write_seed(tmp_path, entries))

    # Feed: active equities only.
    symbols = streamable_symbols(instruments.values())
    assert "NEWCO" in symbols
    assert "GOG-BD-TEST" not in symbols and "OLDCO" not in symbols

    connector = MockMarketConnector(
        ["NEWCO"], interval_seconds=0.01, instruments=instruments, mock_seeds=mock_seeds
    )
    await connector.connect()
    try:
        stream = connector.stream()
        tick = await stream.__anext__()
        await stream.aclose()
    finally:
        await connector.disconnect()
    assert (tick.symbol, tick.name) == ("NEWCO", "New Co PLC")

    # API: everything in the master, streamed or not.
    store = InstrumentStore(tmp_path / "db.sqlite")
    store.seed(instruments.values())
    assert store.get("NEWCO").name == "New Co PLC"
    assert store.get("GOG-BD-TEST").asset_class == "bond"
    assert store.get("OLDCO").status == "delisted"


def test_reseeding_replaces_the_table_so_removed_entries_disappear(tmp_path):
    store = InstrumentStore(tmp_path / "db.sqlite")
    a = Instrument(symbol="AAA", name="A", asset_class="equity", sector="Banking")
    b = Instrument(symbol="BBB", name="B", asset_class="equity", sector="Banking")
    store.seed([a, b])
    store.seed([a])
    assert [i.symbol for i in store.list()] == ["AAA"]
    assert store.get("BBB") is None


def test_market_symbols_env_is_validated_against_the_master(monkeypatch):
    monkeypatch.setenv("MARKET_SYMBOLS", "MTNGH, gcb")
    assert config._symbols_from_env() == ["MTNGH", "GCB"]

    monkeypatch.setenv("MARKET_SYMBOLS", "MTNGH,NOPE")
    with pytest.raises(ValueError, match="NOPE"):
        config._symbols_from_env()

    bond = Instrument(symbol="GOG-BD-TEST", name="Test bond", asset_class="bond", sector="Government")
    monkeypatch.setattr(config, "INSTRUMENTS", {**INSTRUMENTS, bond.symbol: bond})
    monkeypatch.setenv("MARKET_SYMBOLS", "MTNGH,GOG-BD-TEST")
    with pytest.raises(ValueError, match="GOG-BD-TEST"):
        config._symbols_from_env()

    monkeypatch.setenv("MARKET_SYMBOLS", "")
    assert config._symbols_from_env() == streamable_symbols()


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

def test_list_instruments(client):
    response = client.get("/instruments")

    assert response.status_code == 200
    body = response.json()
    assert [i["symbol"] for i in body] == sorted(INSTRUMENTS)
    mtn = next(i for i in body if i["symbol"] == "MTNGH")
    assert mtn == {
        "symbol": "MTNGH", "name": "Scancom PLC (MTN Ghana)", "asset_class": "equity",
        "sector": "Telecommunications", "currency": "GHS", "isin": "GHEMTN051541",
        "status": "active", "kind": "ordinary",
    }
    assert all("mock" not in i for i in body)


def test_filter_instruments_by_asset_class_and_sector(client):
    equities = client.get("/instruments", params={"asset_class": "equity"}).json()
    assert len(equities) == len(INSTRUMENTS)
    assert client.get("/instruments", params={"asset_class": "bond"}).json() == []

    banks = client.get("/instruments", params={"sector": "banking"}).json()  # case-insensitive
    expected = sorted(s for s, i in INSTRUMENTS.items() if i.sector == "Banking")
    assert [i["symbol"] for i in banks] == expected
    assert {"GCB", "SCB", "CAL"} <= set(expected)

    both = client.get("/instruments", params={"asset_class": "equity", "sector": "Energy"}).json()
    assert {i["symbol"] for i in both} == {"GOIL", "TLW", "TOTAL", "ZEN"}

    assert client.get("/instruments", params={"sector": "Nope"}).json() == []
    assert client.get("/instruments", params={"asset_class": "crypto"}).status_code == 422


def test_get_one_instrument(client):
    response = client.get("/instruments/scb-pref")
    assert response.status_code == 200
    assert response.json()["kind"] == "preference"


def test_unknown_symbols_are_404_everywhere(client):
    assert client.get("/instruments/NOPE").status_code == 404
    assert client.get("/market/NOPE").status_code == 404
    assert client.get("/candles", params={"symbol": "NOPE"}).status_code == 404
    assert client.get("/stock/NOPE").status_code == 404

    assert client.get("/market/mtngh").status_code == 200
