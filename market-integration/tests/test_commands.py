"""Command bar function codes (#100): the code table, the parser and
the endpoints the Ctrl K bar in search.js calls."""

import pytest

from app.commands import COMMANDS, Command, resolve
from app.models.instrument import Instrument

INSTRUMENTS = {
    "MTNGH": Instrument(symbol="MTNGH", name="Scancom PLC (MTN Ghana)", asset_class="equity", sector="Telecoms"),
    "GCB": Instrument(symbol="GCB", name="GCB Bank PLC", asset_class="equity", sector="Banking"),
    "GHGGOG069931": Instrument(symbol="GHGGOG069931", name="GOG-BD-13/02/29-A6123-1833-9.85",
                               asset_class="bond", sector="Government"),
}


def _codes(result):
    return [o["code"] for o in result["options"]]


# ------------------------------------------------------------- parser

@pytest.mark.parametrize("text, url", [
    ("MTNGH DES", "/stock/MTNGH#description"),
    ("MTNGH GP", "/stock/MTNGH"),
    ("mtngh des", "/stock/MTNGH#description"),
    ("  MTNGH    gp  ", "/stock/MTNGH"),
    ("GCB DES", "/stock/GCB#description"),
])
def test_symbol_and_code_resolve_to_the_screen(text, url):
    result = resolve(text, INSTRUMENTS)
    assert result["kind"] == "instrument"
    assert result["url"] == url


def test_a_symbol_alone_lists_its_codes_in_table_order():
    result = resolve("MTNGH", INSTRUMENTS)
    assert result["kind"] == "instrument"
    assert result["symbol"] == "MTNGH" and result["name"] == "Scancom PLC (MTN Ghana)"
    assert _codes(result) == ["DES", "GP"]
    assert result["url"] is None
    assert result["options"][0]["url"] == "/stock/MTNGH#description"


def test_a_partial_code_narrows_the_list_without_going_anywhere():
    result = resolve("MTNGH D", INSTRUMENTS)
    assert _codes(result) == ["DES"]
    assert result["url"] is None


@pytest.mark.parametrize("text", ["MTNGH XYZ", "MTNGH DESX", "MTNGH DES EXTRA"])
def test_an_unknown_code_falls_back_to_search(text):
    assert resolve(text, INSTRUMENTS) == {"kind": "search", "q": text}


def test_codes_only_apply_to_their_asset_classes():
    # DES and GP are equity screens; a bond has no codes yet, so its
    # symbol goes to the search, which opens /bond/{symbol}.
    assert resolve("GHGGOG069931 DES", INSTRUMENTS)["kind"] == "search"
    assert resolve("GHGGOG069931", INSTRUMENTS)["kind"] == "search"


@pytest.mark.parametrize("text", ["eqs", "EQS", "  Eqs "])
def test_eqs_opens_the_screener(text):
    assert resolve(text, INSTRUMENTS) == {
        "kind": "screen",
        "options": [{"code": "EQS", "title": "Stock screener", "url": "/screener"}],
        "url": "/screener",
    }


def test_a_partial_screen_code_lists_without_going():
    result = resolve("EQ", INSTRUMENTS)
    assert result["kind"] == "screen" and _codes(result) == ["EQS"] and result["url"] is None


@pytest.mark.parametrize("text", ["gc", "GC", " Gc "])
def test_gc_opens_the_yield_curve(text):
    result = resolve(text, INSTRUMENTS)
    assert result["kind"] == "screen" and result["url"] == "/yield-curve"
    assert result["options"][0] == {"code": "GC", "title": "Government yield curve", "url": "/yield-curve"}


@pytest.mark.parametrize("text", ["auc", "AUC", " Auc "])
def test_auc_opens_the_auctions(text):
    assert resolve(text, INSTRUMENTS) == {
        "kind": "screen",
        "options": [{"code": "AUC", "title": "T-bill and bond auctions", "url": "/auctions"}],
        "url": "/auctions",
    }


def test_a_partial_auc_lists_it_without_going():
    result = resolve("AU", INSTRUMENTS)
    assert result["kind"] == "screen" and _codes(result) == ["AUC"] and result["url"] is None


@pytest.mark.parametrize("text", ["eco", "ECO", " Eco "])
def test_eco_opens_the_economic_calendar(text):
    assert resolve(text, INSTRUMENTS) == {
        "kind": "screen",
        "options": [{"code": "ECO", "title": "Economic calendar and policy rate", "url": "/calendar"}],
        "url": "/calendar",
    }


def test_e_lists_both_e_codes_in_table_order():
    result = resolve("E", INSTRUMENTS)
    assert _codes(result) == ["EQS", "ECO"] and result["url"] is None


def test_gc_does_not_take_over_gcb():
    # Symbols match whole: GC is the yield curve, GCB is still GCB Bank,
    # with its own codes.
    result = resolve("GCB", INSTRUMENTS)
    assert result["kind"] == "instrument" and result["symbol"] == "GCB"
    assert resolve("GCB GP", INSTRUMENTS)["url"] == "/stock/GCB"


def test_a_screen_code_after_a_symbol_is_not_an_instrument_code():
    assert resolve("MTNGH EQS", INSTRUMENTS)["kind"] == "search"


@pytest.mark.parametrize("text", ["", "   ", "fan milk", "NOPE", "NOPE DES", "DES", "GP", "EQS SCREEN", "GC CURVE", "AUC BILLS", "ECO CPI"])
def test_anything_else_falls_back_to_search(text):
    assert resolve(text, INSTRUMENTS) == {"kind": "search", "q": " ".join(text.split())}


def test_screen_codes_stand_alone():
    commands = dict(COMMANDS, EQS=Command("EQS", "Stock screener", "screen", "/screener"),
                    ECO=Command("ECO", "Economic calendar", "screen", "/calendar"))
    assert resolve("eqs", INSTRUMENTS, commands) == {
        "kind": "screen",
        "options": [{"code": "EQS", "title": "Stock screener", "url": "/screener"}],
        "url": "/screener",
    }
    partial = resolve("E", INSTRUMENTS, commands)
    assert _codes(partial) == ["EQS", "ECO"] and partial["url"] is None
    # An exact code is listed first, whatever the table order.
    longer_first = dict(commands, GCX=Command("GCX", "GCX", "screen", "/commodities"))
    longer_first = {"GCX": longer_first["GCX"], **{k: v for k, v in longer_first.items() if k != "GCX"}}
    assert _codes(resolve("GC", INSTRUMENTS, longer_first)) == ["GC", "GCX"]
    # A symbol wins over a screen code of the same spelling.
    assert resolve("GCB", INSTRUMENTS, dict(commands, GCB=Command("GCB", "x", "screen", "/x")))["kind"] == "instrument"


def test_the_symbol_is_escaped_in_the_url():
    odd = {"SCB-PREF": Instrument(symbol="SCB-PREF", name="SCB Preference", asset_class="equity", sector="Banking")}
    assert resolve("scb-pref gp", odd)["url"] == "/stock/SCB-PREF"
    assert Command("X", "x", "instrument", "/stock/{symbol}").url("A/B") == "/stock/A%2FB"


def test_the_table_is_well_formed():
    for code, c in COMMANDS.items():
        assert code == c.code and code.isupper() and " " not in code
        assert c.kind in ("instrument", "screen")
        if c.kind == "instrument":
            assert "{symbol}" in c.route and c.asset_classes
        else:
            assert "{symbol}" not in c.route


# ------------------------------------------------------------- endpoints

def test_commands_endpoint_serves_the_table(client):
    body = client.get("/commands").json()
    assert [c["code"] for c in body] == list(COMMANDS)
    des = body[0]
    assert des == {
        "code": "DES",
        "title": "Company description and figures",
        "kind": "instrument",
        "route": "/stock/{symbol}#description",
        "asset_classes": ["equity"],
    }


def test_resolve_endpoint_uses_the_instrument_master(client):
    body = client.get("/commands/resolve", params={"q": "mtngh des"}).json()
    assert body["kind"] == "instrument" and body["url"] == "/stock/MTNGH#description"
    assert client.get("/commands/resolve", params={"q": "MTNGH XYZ"}).json() == {"kind": "search", "q": "MTNGH XYZ"}
    assert client.get("/commands/resolve").json() == {"kind": "search", "q": ""}


@pytest.mark.parametrize("code", [c for c, cmd in COMMANDS.items() if cmd.kind == "instrument"])
def test_every_instrument_code_opens_a_page_for_a_listed_equity(client, code):
    url = client.get("/commands/resolve", params={"q": f"MTNGH {code}"}).json()["url"]
    assert client.get(url.split("#")[0]).status_code == 200


@pytest.mark.parametrize("code", [c for c, cmd in COMMANDS.items() if cmd.kind == "screen"])
def test_every_screen_code_opens_a_page(client, code):
    url = client.get("/commands/resolve", params={"q": code}).json()["url"]
    assert url and client.get(url.split("#")[0]).status_code == 200


@pytest.mark.parametrize("path", [
    "/ticker", "/stock/MTNGH", "/screener", "/auctions", "/bond/GHGGOG069931", "/calendar",
    "/commodities", "/commodity/GEJWM2", "/fixed-income", "/fx", "/graph", "/hedging",
    "/tickets", "/yield-curve",
])
def test_every_page_loads_the_command_bar(client, path):
    # Ctrl K works on a page only if it loads search.js; without it the
    # browser's own Ctrl K (the address bar) takes over.
    assert '<script src="/static/search.js"></script>' in client.get(path).text


def test_the_search_script_installs_the_command_bar(client):
    script = client.get("/static/search.js").text
    assert "openCommandBar" in script and "/commands/resolve" in script
