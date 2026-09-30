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

EQUITIES = {s for s, i in INSTRUMENTS.items() if i.asset_class == "equity"}


def test_checked_in_seed_covers_the_gse_and_every_equity_is_calibrated():
    assert len(EQUITIES) == 42
    assert {"MTNGH", "GCB", "SCB", "SCB-PREF", "TOTAL", "UNIL", "PBC", "GLD"} <= EQUITIES
    assert set(MOCK_SEEDS) == set(INSTRUMENTS)  # every instrument, equity or fixed income
    assert INSTRUMENTS["MTNGH"].isin == "GHEMTN051541"
    assert INSTRUMENTS["GLD"].kind == "etf"
    # Marked **ALW** / PBC** in the 28-Sep-2026 daily shares report.
    assert {s for s, i in INSTRUMENTS.items() if i.status != "active"} == {"ALW", "PBC"}
    assert {"ALW", "PBC"}.isdisjoint(config.SYMBOLS)


def test_checked_in_seed_has_every_gfim_sample_security():
    fixed = [i for i in INSTRUMENTS.values() if i.asset_class != "equity"]
    counts = {}
    for i in fixed:
        counts[i.segment] = counts.get(i.segment, 0) + 1
    # Rows in the 28-Sep-2026 GFIM report, per section.
    assert counts == {"new_gog": 2, "ddep": 29, "old_gog": 17, "treasury_bill": 91, "corporate": 22}
    assert all(i.symbol == i.isin and i.maturity_date and i.issuer for i in fixed)
    assert all(i.asset_class == "bill" and i.coupon_rate == 0 for i in fixed if i.segment == "treasury_bill")

    gfsf = INSTRUMENTS["GHGGOG072851"]  # GFSF-5-14YR
    # The report's maturity column says 2028-12-05; the description (and
    # the tenor) say 2037. See docs/data-formats.md.
    assert (gfsf.tenor, gfsf.maturity_date.isoformat(), gfsf.coupon_rate) == ("GFSF-5-14YR", "2037-11-24", 9.85)
    assert INSTRUMENTS["GHGGOG071689"].currency == "USD"  # USD-DDE-FCA-27
    assert INSTRUMENTS["GHGGOGI02055"].tenor == "182-DAY BILL"  # ISIN had a trailing space in the report
    assert INSTRUMENTS["GHCLGH075751"].coupon_rate is None  # LGH-BD-04/10/29-C0936: no coupon given


def test_fixed_income_terms_follow_the_conventions_doc():
    fixed = [i for i in INSTRUMENTS.values() if i.asset_class != "equity"]
    assert all(i.face_value == 100 for i in fixed)
    assert all(i.face_value is None and i.frequency is None for i in INSTRUMENTS.values()
               if i.asset_class == "equity")

    # Bills: zero coupon, ACT/364, issued on a Monday for their tenor.
    bill = INSTRUMENTS["GHGGOGI01925"]  # 91-day, matures 05-Oct-2026
    assert (bill.issue_date.isoformat(), bill.frequency, bill.day_count) == ("2026-07-06", 0, "ACT/364")
    days = {"91-DAY BILL": 91, "182-DAY BILL": 182, "364-DAY BILL": 364}
    for i in fixed:
        if i.segment == "treasury_bill":
            assert (i.maturity_date - i.issue_date).days == days[i.tenor]
            assert i.issue_date.weekday() == 0

    # Ordinary GoG bonds: semi-annual ACT/ACT. GFSF, USD DDE and
    # corporates need term sheets, so their terms stay unknown.
    gc3 = INSTRUMENTS["GHGGOG069931"]
    assert (gc3.frequency, gc3.day_count, gc3.issue_date) == (2, "ACT/ACT", None)
    for symbol in ("GHGGOG072851", "GHGGOG071689", "GHCLGH075744"):  # GFSF-5-14YR, USD-DDE-FCA-27, Letshego
        assert (INSTRUMENTS[symbol].frequency, INSTRUMENTS[symbol].day_count) == (None, None)


def test_an_issue_date_on_or_after_maturity_is_rejected():
    terms = dict(symbol="GHGGOGI01925", name="X", asset_class="bill", sector="Government",
                 maturity_date="2026-10-05")
    with pytest.raises(ValueError, match="issue_date"):
        Instrument(**terms, issue_date="2026-10-05")
    with pytest.raises(ValueError, match="face_value"):
        Instrument(**terms, face_value=0)


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
        "issuer": None, "segment": None, "tenor": None, "maturity_date": None, "coupon_rate": None,
        "issue_date": None, "frequency": None, "day_count": None, "face_value": None,
    }
    assert all("mock" not in i for i in body)

    bill = next(i for i in body if i["symbol"] == "GHGGOGI01883")
    assert bill == {
        "symbol": "GHGGOGI01883", "name": "GOG-BL-21/06/27-A7064-2012-0", "asset_class": "bill",
        "sector": "Government", "currency": "GHS", "isin": "GHGGOGI01883", "status": "active",
        "kind": None, "issuer": "Government of Ghana", "segment": "treasury_bill",
        "tenor": "364-DAY BILL", "maturity_date": "2027-06-21", "coupon_rate": 0.0,
        "issue_date": "2026-06-22", "frequency": 0, "day_count": "ACT/364", "face_value": 100.0,
    }


def test_filter_instruments_by_asset_class_and_sector(client):
    equities = client.get("/instruments", params={"asset_class": "equity"}).json()
    assert {i["symbol"] for i in equities} == EQUITIES
    assert len(client.get("/instruments", params={"asset_class": "bond"}).json()) == 70
    assert len(client.get("/instruments", params={"asset_class": "bill"}).json()) == 91

    gov_bills = client.get("/instruments", params={"asset_class": "bill", "sector": "government"}).json()
    assert len(gov_bills) == 91

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


def test_suspended_and_fixed_income_instruments_are_listed_but_not_streamed(client):
    assert client.get("/instruments/PBC").json()["status"] == "suspended"
    assert client.get("/instruments/GHGGOG069931").json()["tenor"] == "2023-GC-3"
    for symbol in ("PBC", "ALW", "GHGGOG069931"):
        assert client.get(f"/market/{symbol}").status_code == 404


def test_reseeding_upgrades_a_table_with_an_older_layout(tmp_path):
    import sqlite3

    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE instruments (symbol TEXT PRIMARY KEY, name TEXT)")  # pre-#11 shape
    conn.execute("INSERT INTO instruments VALUES ('OLD', 'Old row')")
    conn.commit()
    conn.close()

    store = InstrumentStore(db)
    store.seed([INSTRUMENTS["GHGGOG072851"]])
    assert [i.symbol for i in store.list()] == ["GHGGOG072851"]
    assert store.get("GHGGOG072851").maturity_date.isoformat() == "2037-11-24"


def test_unknown_symbols_are_404_everywhere(client):
    assert client.get("/instruments/NOPE").status_code == 404
    assert client.get("/market/NOPE").status_code == 404
    assert client.get("/candles", params={"symbol": "NOPE"}).status_code == 404
    assert client.get("/stock/NOPE").status_code == 404

    assert client.get("/market/mtngh").status_code == 200
