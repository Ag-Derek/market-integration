import json

import pytest

from app.company import SEED_PATH, load_seed
from app.company.figures import calculated_figures
from app.company.store import CompanyStore
from app.models.company import FINANCIAL_FIELDS, PROFILE_FIELDS, Company

SRC = {"source": "Test fixture", "as_of": "2025-12-31"}


def sourced(value, **kw):
    return {"value": value, **SRC, **kw}


# Fixture data, not facts about these companies.
FULL = {
    "symbol": "MTNGH",
    "profile": {
        "description_short": sourced("Mobile network operator."),
        "description_full": sourced("Mobile network operator. Longer text."),
        "sector": sourced("Telecommunications"),
        "industry": sourced("Wireless telecommunications"),
        "website": sourced("https://example.com"),
        "headquarters_city": sourced("Accra"),
        "headquarters_country": sourced("Ghana"),
        "employees": sourced(1000),
        "shares_outstanding": sourced(10_000_000_000, as_of="2026-06-30"),
        "listing_date": sourced("2018-09-05"),
        "registrar": sourced("Test Registrar Ltd"),
    },
    "officers": [
        {"name": "B. Second", "role": "Chief Financial Officer", "display_order": 2, **SRC},
        {"name": "A. First", "role": "Chief Executive Officer", "display_order": 1, **SRC},
    ],
    "financials": [
        {"fiscal_year": 2024, "revenue": sourced(9.0e9), "net_income": sourced(3.0e9), "eps": sourced(0.30)},
        {"fiscal_year": 2025, "revenue": sourced(1.0e10), "net_income": sourced(4.0e9), "eps": sourced(0.40),
         "book_value": sourced(2.0e10), "dividend_per_share": sourced(0.30, as_of="2026-03-01"),
         "dividend_payment_dates": sourced(["2026-04-15", "2026-10-15"], as_of="2026-03-01")},
    ],
}
# Only a couple of fields filled in; no officers or financials.
PARTIAL = {"symbol": "GCB", "profile": {"website": sourced("https://example.org")}}
# Reports in USD while trading in GHS.
USD = {"symbol": "AGA", "profile": {"shares_outstanding": sourced(500_000_000)},
       "financials": [{"fiscal_year": 2025, "currency": "USD", "eps": sourced(2.5),
                       "book_value": sourced(5.0e9), "dividend_per_share": sourced(1.0)}]}


def _write(tmp_path, entries):
    path = tmp_path / "companies.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


# ---------------------------------------------------------------- seed

def test_checked_in_seed_loads():
    load_seed(SEED_PATH)


def test_seed_round_trips_through_the_store(tmp_path):
    companies = load_seed(_write(tmp_path, [FULL, PARTIAL]))
    store = CompanyStore(tmp_path / "db.sqlite")
    assert store.seed(companies.values()) == 2

    full = store.get("MTNGH")
    assert full.profile == companies["MTNGH"].profile
    assert [o.display_order for o in full.officers] == [1, 2]
    assert [f.fiscal_year for f in full.financials] == [2025, 2024]
    assert full.financials[0].dividend_payment_dates.value[1].isoformat() == "2026-10-15"
    assert store.get("GCB").officers == [] and store.get("GCB").financials == []
    assert store.get("SCB") is None


def test_reseeding_replaces_everything(tmp_path):
    store = CompanyStore(tmp_path / "db.sqlite")
    store.seed(load_seed(_write(tmp_path, [FULL, PARTIAL])).values())
    store.seed(load_seed(_write(tmp_path, [PARTIAL])).values())
    assert store.get("MTNGH") is None


@pytest.mark.parametrize("entry,match", [
    ({"symbol": "MTNGH", "profile": {"employees": {"value": 10}}}, "source"),
    ({"symbol": "MTNGH", "profile": {"employees": {"value": 10, "source": "x"}}}, "source"),
    ({"symbol": "MTNGH", "profile": {"employees": sourced(0)}}, "positive"),
    ({"symbol": "MTNGH", "profile": {"ceo": sourced("x")}}, "ceo"),
    ({"symbol": "MTNGH", "officers": [{"name": "X", "role": "CEO", "display_order": 1}]}, "source"),
    ({"symbol": "MTNGH", "financials": [{"fiscal_year": 2025}, {"fiscal_year": 2025}]}, "twice"),
    ({"symbol": "NOPE"}, "not in the instrument master"),
    ({"symbol": "GHGGOG069931"}, "not an equity"),
])
def test_bad_entries_fail_loudly(tmp_path, entry, match):
    with pytest.raises(ValueError, match=match):
        load_seed(_write(tmp_path, [entry]))


def test_duplicate_companies_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        load_seed(_write(tmp_path, [PARTIAL, PARTIAL]))


# ---------------------------------------------------------------- figures

def test_calculated_figures(make_tick):
    company = Company(**FULL)
    quote = make_tick("MTNGH", vwap=6.0, price=6.0, day_low=5.9, day_high=6.1)
    figures = calculated_figures(company, quote)
    assert figures["fiscal_years"]["eps"] == 2025
    assert figures["price"]["value"] == 6.0
    assert figures["market_cap"]["value"] == 6.0e10
    assert figures["pe_ratio"]["value"] == 15.0               # 6.0 / 0.40, latest year
    assert figures["dividend_yield"]["value"] == 5.0          # 0.30 / 6.0
    assert figures["price_to_book"]["value"] == 3.0           # 6e10 / 2e10
    assert figures["market_cap"]["inputs_as_of"]["shares_outstanding"] == "2026-06-30"
    assert figures["dividend_yield"]["inputs_as_of"]["dividend_per_share"] == "2026-03-01"


def test_figures_never_mix_currencies_or_guess(make_tick):
    usd = calculated_figures(Company(**USD), make_tick("AGA", vwap=6.0, price=6.0, day_low=5.9, day_high=6.1))
    assert usd["market_cap"]["value"] == 3.0e9               # price and shares: both fine
    assert [usd[k]["value"] for k in ("pe_ratio", "dividend_yield", "price_to_book")] == [None] * 3

    nothing = calculated_figures(None, None)
    assert all(nothing[k]["value"] is None for k in ("price", "market_cap", "pe_ratio"))


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

@pytest.fixture
def seeded(tmp_path):
    """The running app's store seeded with the fixtures; the real seed
    is put back afterwards."""
    from app.company import COMPANIES
    from app.main import company_store

    company_store.seed(load_seed(_write(tmp_path, [FULL, PARTIAL, USD])).values())
    yield
    company_store.seed(COMPANIES.values())


def _all_sourced(obj, fields):
    return all(set(obj[f]) == {"value", "source", "as_of"} for f in fields)


def test_description_returns_everything_in_one_response(client, seeded):
    body = client.get("/instruments/mtngh/description").json()

    assert set(body) == {"symbol", "instrument", "related", "profile", "officers", "financials", "calculated"}
    assert body["instrument"]["isin"] == "GHEMTN051541"
    assert body["profile"]["employees"] == {"value": 1000, "source": "Test fixture", "as_of": "2025-12-31"}
    assert body["profile"]["listing_date"]["value"] == "2018-09-05"
    assert _all_sourced(body["profile"], PROFILE_FIELDS)
    assert [o["role"] for o in body["officers"]] == ["Chief Executive Officer", "Chief Financial Officer"]
    assert all(o["source"] and o["as_of"] for o in body["officers"])
    assert [f["fiscal_year"] for f in body["financials"]] == [2025, 2024]
    assert all(_all_sourced(f, FINANCIAL_FIELDS) for f in body["financials"])
    assert body["financials"][0]["dividend_payment_dates"]["value"] == ["2026-04-15", "2026-10-15"]

    calc = body["calculated"]
    price = calc["price"]["value"]
    assert price is not None  # MTNGH is in the live feed
    assert calc["market_cap"]["value"] == pytest.approx(price * 10_000_000_000, rel=1e-6)
    assert calc["pe_ratio"]["value"] == pytest.approx(price / 0.40, rel=1e-3)
    assert calc["last_dividend"]["fiscal_year"] == 2025 and calc["last_dividend"]["dividend_per_share"] == 0.30
    assert calc["dividend_growth"]["reason"] == "fewer than two years with a dividend recorded"


def test_missing_fields_are_null_not_omitted(client, seeded):
    body = client.get("/instruments/GCB/description").json()
    assert _all_sourced(body["profile"], PROFILE_FIELDS)
    assert body["profile"]["website"]["value"] == "https://example.org"
    assert body["profile"]["employees"] == {"value": None, "source": None, "as_of": None}
    assert body["officers"] == [] and body["financials"] == []
    assert body["calculated"]["market_cap"]["value"] is None  # no shares outstanding
    assert body["calculated"]["price"]["value"] is not None


def test_an_equity_with_no_entry_at_all_is_all_null(client, seeded):
    body = client.get("/instruments/SCB/description").json()
    assert all(body["profile"][f] == {"value": None, "source": None, "as_of": None} for f in PROFILE_FIELDS)
    assert body["instrument"]["symbol"] == "SCB"
    # A suspended equity has no quote, so nothing to calculate from.
    pbc = client.get("/instruments/PBC/description").json()
    assert pbc["calculated"]["price"]["value"] is None


def test_stock_page_has_the_description_view_for_every_listed_equity(client):
    page = client.get("/stock/MTNGH").text
    assert '<script src="/static/description.js"></script>' in page and 'id="description"' in page
    script = client.get("/static/description.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    # Suspended: not on the feed, but listed, so its description is reachable.
    assert client.get("/stock/PBC").status_code == 200
    assert client.get("/instruments/PBC/description").status_code == 200


def test_description_names_related_securities_both_ways(client):
    aga = client.get("/instruments/AGA/description").json()
    aads = client.get("/instruments/AADS/description").json()
    assert aga["related"] == [{"symbol": "AADS", "name": "AngloGold Ashanti Depositary Shares",
                               "kind": "depositary", "status": "active"}]
    assert [r["symbol"] for r in aads["related"]] == ["AGA"]
    assert client.get("/instruments/MTNGH/description").json()["related"] == []


def test_unknown_symbols_and_non_equities_are_404(client):
    assert client.get("/instruments/NOPE/description").status_code == 404
    bond = client.get("/instruments/GHGGOG069931/description")
    assert bond.status_code == 404 and "equities" in bond.json()["detail"]
    assert client.get("/instruments/GHGGOGI01883/description").status_code == 404  # a bill
