"""Chart drawings saved per user and symbol: /drawings/me/{symbol},
their validation (app/models/drawing.py) and storage
(app/drawings/store.py)."""

import uuid

import pytest

from app.drawings.store import DrawingStore
from app.identity import USER_COOKIE, USER_HEADER
from app.instruments import INSTRUMENTS
from app.models.drawing import MAX_DRAWINGS

STOCK = "MTNGH"
OTHER_STOCK = next(s for s, i in INSTRUMENTS.items() if i.asset_class == "equity" and s != STOCK)
NOT_A_STOCK = next(s for s, i in INSTRUMENTS.items() if i.asset_class != "equity")


def _user():
    return {USER_HEADER: f"draw-{uuid.uuid4().hex[:8]}"}


def trend(id_="t1", extend=False):
    return {"id": id_, "kind": "trend", "extend": extend, "points": [
        {"t": "2026-03-02T00:00:00Z", "p": 4.10}, {"t": "2026-07-14T00:00:00Z", "p": 5.30}]}


def level(id_="l1", p=5.0):
    return {"id": id_, "kind": "level", "points": [{"t": "2026-09-01T00:00:00Z", "p": p}]}


def fib(id_="f1"):
    return {"id": id_, "kind": "fib", "points": [
        {"t": "2026-01-05T00:00:00Z", "p": 3.80}, {"t": "2026-06-30T00:00:00Z", "p": 5.60}]}


def _put(client, user, drawings, symbol=STOCK):
    return client.put(f"/drawings/me/{symbol}", json={"drawings": drawings}, headers=user)


def test_a_new_user_has_no_drawings(client):
    body = client.get(f"/drawings/me/{STOCK}", headers=_user()).json()
    assert body == {"symbol": STOCK, "currency": "GHS", "drawings": [], "updated_at": None,
                    "max_drawings": MAX_DRAWINGS}


def test_drawings_round_trip(client):
    user = _user()
    res = _put(client, user, [trend(extend=True), level(), fib()])
    assert res.status_code == 200 and res.json()["updated_at"]
    saved = client.get(f"/drawings/me/{STOCK.lower()}", headers=user).json()
    assert saved["symbol"] == STOCK
    assert [(d["id"], d["kind"]) for d in saved["drawings"]] == [("t1", "trend"), ("l1", "level"), ("f1", "fib")]
    t = saved["drawings"][0]
    assert t["extend"] is True and t["points"][1]["p"] == 5.30
    assert t["points"][0]["t"].startswith("2026-03-02T00:00:00")
    assert saved["drawings"][1]["extend"] is False


def test_times_are_kept_in_utc(client):
    user = _user()
    d = level()
    d["points"][0]["t"] = "2026-09-01T09:30:00+01:00"
    _put(client, user, [d])
    t = client.get(f"/drawings/me/{STOCK}", headers=user).json()["drawings"][0]["points"][0]["t"]
    assert t.replace("Z", "+00:00") == "2026-09-01T08:30:00+00:00"


def test_drawings_are_per_user_and_per_symbol(client):
    alice, bob = _user(), _user()
    _put(client, alice, [level("a")])
    _put(client, alice, [level("x"), level("y", 6)], symbol=OTHER_STOCK)
    assert client.get(f"/drawings/me/{STOCK}", headers=bob).json()["drawings"] == []
    assert [d["id"] for d in client.get(f"/drawings/me/{STOCK}", headers=alice).json()["drawings"]] == ["a"]
    charts = client.get("/drawings/me", headers=alice).json()["charts"]
    assert charts == {STOCK: 1, OTHER_STOCK: 2}


def test_a_put_replaces_and_an_empty_one_clears(client):
    user = _user()
    _put(client, user, [trend(), level()])
    _put(client, user, [fib()])
    assert [d["id"] for d in client.get(f"/drawings/me/{STOCK}", headers=user).json()["drawings"]] == ["f1"]
    cleared = _put(client, user, []).json()
    assert cleared["drawings"] == []
    assert client.get("/drawings/me", headers=user).json()["charts"] == {}


def test_a_browser_gets_its_own_anonymous_user(client):
    # As test_watchlists.py: the session client, with its cookies cleared
    # before and after, is a new browser.
    client.cookies.clear()
    try:
        first = client.put(f"/drawings/me/{STOCK}", json={"drawings": [level()]})
        assert first.status_code == 200 and client.cookies.get(USER_COOKIE, "").startswith("anon-")
        assert len(client.get(f"/drawings/me/{STOCK}").json()["drawings"]) == 1
        client.cookies.clear()
        assert client.get(f"/drawings/me/{STOCK}").json()["drawings"] == []   # another browser
    finally:
        client.cookies.clear()


@pytest.mark.parametrize("drawing", [
    {**level(), "points": level()["points"] * 2},                                          # a level is one point
    {**trend(), "points": trend()["points"][:1]},                                          # a trend line is two
    {**trend(), "points": [{"t": "2026-03-02T00:00:00Z", "p": 4.1}, {"t": "2026-03-02T00:00:00Z", "p": 5}]},  # vertical
    {**fib(), "points": [{"t": "2026-01-05T00:00:00Z", "p": 4}, {"t": "2026-06-30T00:00:00Z", "p": 4}]},    # flat
    {**level(), "extend": True},                                                            # only trend lines extend
    {**level(), "points": [{"t": "2026-09-01T00:00:00Z", "p": -1}]},                        # negative price
    {**level(), "points": [{"t": "2026-09-01T00:00:00", "p": 5}]},                          # no time zone
    {**level(), "id": "has spaces"},
    {**level(), "kind": "circle"},
    {**level(), "colour": "red"},                                                           # unknown field
])
def test_malformed_drawings_are_refused(client, drawing):
    user = _user()
    _put(client, user, [level("keep")])
    assert _put(client, user, [drawing]).status_code == 422
    # Nothing was saved: the earlier drawings are still there.
    assert [d["id"] for d in client.get(f"/drawings/me/{STOCK}", headers=user).json()["drawings"]] == ["keep"]


def test_ids_must_be_unique_and_the_count_is_capped(client):
    user = _user()
    assert _put(client, user, [level("same"), level("same", 6)]).status_code == 422
    assert _put(client, user, [level(f"l{i}") for i in range(MAX_DRAWINGS)]).status_code == 200
    assert _put(client, user, [level(f"l{i}") for i in range(MAX_DRAWINGS + 1)]).status_code == 422


@pytest.mark.parametrize("symbol", [NOT_A_STOCK, "NOSUCH"])
def test_only_stock_charts_take_drawings(client, symbol):
    user = _user()
    assert client.get(f"/drawings/me/{symbol}", headers=user).status_code == 404
    assert _put(client, user, [level()], symbol=symbol).status_code == 404


def test_the_store_keeps_drawings_across_restarts(tmp_path):
    db = tmp_path / "d.db"
    DrawingStore(db).put("u", STOCK, [level()])
    restarted = DrawingStore(db)
    assert restarted.get("u", STOCK)["drawings"] == [level()]
    assert restarted.get("u", OTHER_STOCK) is None and restarted.get("v", STOCK) is None
    assert restarted.symbols("u") == {STOCK: 1}


def test_the_stock_page_loads_the_drawing_tools(client):
    page = client.get(f"/stock/{STOCK}").text
    assert '<script src="/static/drawings.js"></script>' in page and 'id="drawTools"' in page
    script = client.get("/static/drawings.js")
    assert script.status_code == 200 and "ChartDrawings" in script.text
