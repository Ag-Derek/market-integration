"""Per-user watchlists (#26): validation, storage that survives a
restart, and GET/PUT /watchlists/me with the anonymous identity."""

import uuid

import pytest

from app import config
from app.identity import USER_COOKIE, USER_HEADER
from app.instruments import INSTRUMENTS
from app.watchlists import DEFAULT_WATCHLIST, MAX_PINS, WatchlistError, validate
from app.watchlists.store import WatchlistStore

EQUITIES = sorted(s for s, i in INSTRUMENTS.items() if i.asset_class == "equity")


def _user():
    return {USER_HEADER: "test-" + uuid.uuid4().hex}


# ---------------------------------------------------------------- validation

def test_validate_normalizes_and_keeps_the_order():
    assert validate([" gcb", "MTNGH", "scb-pref"]) == ["GCB", "MTNGH", "SCB-PREF"]
    assert validate([]) == []
    assert validate(["PBC"]) == ["PBC"]  # suspended, but listed: may stay pinned


@pytest.mark.parametrize("symbols,match,offending", [
    (["MTNGH", "NOPE", "AAPL"], "Unknown symbols: NOPE, AAPL", ["NOPE", "AAPL"]),
    (["MTNGH", "GHGGOG069931"], "Only equities", ["GHGGOG069931"]),
    (["GCB", "MTNGH", "gcb"], "more than once: GCB", ["GCB"]),
])
def test_validate_rejects_bad_lists(symbols, match, offending):
    with pytest.raises(WatchlistError, match=match) as e:
        validate(symbols)
    assert e.value.symbols == offending


def test_validate_caps_the_number_of_pins():
    with pytest.raises(WatchlistError, match="At most 2 pins; got 3"):
        validate(["MTNGH", "GCB", "SCB"], max_pins=2)


def test_the_default_is_valid_and_within_the_cap():
    assert DEFAULT_WATCHLIST == config.WATCHLIST_DEFAULT
    assert 0 < len(DEFAULT_WATCHLIST) <= MAX_PINS == config.WATCHLIST_MAX_PINS == 12


# ---------------------------------------------------------------- storage

def test_store_replaces_the_list_and_survives_a_restart(tmp_path):
    db = tmp_path / "db.sqlite"
    store = WatchlistStore(db)
    assert store.get("alice") is None
    store.put("alice", ["MTNGH", "GCB"])
    store.put("alice", ["SCB", "MTNGH"])
    store.put("bob", ["CAL"])

    restarted = WatchlistStore(db)  # a new process opening the same database
    assert restarted.get("alice")["symbols"] == ["SCB", "MTNGH"]
    assert restarted.get("bob")["symbols"] == ["CAL"]
    assert restarted.get("alice")["updated_at"]


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

def test_a_new_user_gets_the_default(client):
    body = client.get("/watchlists/me", headers=_user()).json()
    assert body["symbols"] == DEFAULT_WATCHLIST
    assert body["is_default"] is True and body["updated_at"] is None
    assert body["max_pins"] == 12


def test_put_replaces_the_list_in_order(client):
    user = _user()
    first = client.put("/watchlists/me", headers=user, json={"symbols": ["gcb", "MTNGH", "SCB"]})
    assert first.status_code == 200
    assert first.json()["symbols"] == ["GCB", "MTNGH", "SCB"] and first.json()["is_default"] is False

    client.put("/watchlists/me", headers=user, json={"symbols": ["SCB", "CAL"]})
    body = client.get("/watchlists/me", headers=user).json()
    assert body["symbols"] == ["SCB", "CAL"] and body["is_default"] is False and body["updated_at"]

    # An empty list is a choice too, not a reset to the default.
    client.put("/watchlists/me", headers=user, json={"symbols": []})
    assert client.get("/watchlists/me", headers=user).json()["symbols"] == []


def test_each_user_has_their_own(client):
    a, b = _user(), _user()
    client.put("/watchlists/me", headers=a, json={"symbols": ["MTNGH"]})
    client.put("/watchlists/me", headers=b, json={"symbols": ["GCB"]})
    assert client.get("/watchlists/me", headers=a).json()["symbols"] == ["MTNGH"]
    assert client.get("/watchlists/me", headers=b).json()["symbols"] == ["GCB"]


@pytest.mark.parametrize("symbols,offending", [
    (["MTNGH", "NOPE"], ["NOPE"]),
    (["GHGGOG069931"], ["GHGGOG069931"]),
    (["GCB", "GCB"], ["GCB"]),
    (EQUITIES[:13], []),  # one over the cap
])
def test_bad_lists_are_rejected_and_nothing_is_saved(client, symbols, offending):
    user = _user()
    client.put("/watchlists/me", headers=user, json={"symbols": ["CAL"]})
    response = client.put("/watchlists/me", headers=user, json={"symbols": symbols})
    assert response.status_code == 422
    assert response.json()["detail"]["symbols"] == offending
    assert client.get("/watchlists/me", headers=user).json()["symbols"] == ["CAL"]


@pytest.mark.parametrize("body", [{}, {"symbols": "MTNGH"}, {"symbols": [1]}, {"symbols": [], "extra": 1}])
def test_malformed_bodies_are_rejected(client, body):
    assert client.put("/watchlists/me", headers=_user(), json=body).status_code == 422


def test_a_malformed_user_header_is_rejected(client):
    assert client.get("/watchlists/me", headers={USER_HEADER: "has spaces"}).status_code == 400
    assert client.get("/watchlists/me", headers={USER_HEADER: "x" * 129}).status_code == 400


def test_a_browser_is_identified_by_its_cookie(client):
    client.cookies.clear()
    try:
        first = client.get("/watchlists/me")
        user_id = first.cookies.get(USER_COOKIE)
        assert user_id and user_id.startswith("anon-") and first.json()["user_id"] == user_id
        assert "httponly" in first.headers["set-cookie"].lower()

        # The client sends the cookie back from now on: same user, no new cookie.
        client.put("/watchlists/me", json={"symbols": ["GOIL", "MTNGH"]})
        again = client.get("/watchlists/me")
        assert again.json()["user_id"] == user_id
        assert USER_COOKIE not in again.cookies
        assert again.json()["symbols"] == ["GOIL", "MTNGH"]
    finally:
        client.cookies.clear()


def test_a_saved_watchlist_survives_a_server_restart(client):
    from app.main import watchlist_store

    user = _user()
    client.put("/watchlists/me", headers=user, json={"symbols": ["EGH", "MTNGH"]})
    # A restarted server opens a new store on the same database.
    restarted = WatchlistStore(config.DB_PATH)
    assert restarted.get(user[USER_HEADER])["symbols"] == ["EGH", "MTNGH"]
    assert watchlist_store.get(user[USER_HEADER])["symbols"] == ["EGH", "MTNGH"]
