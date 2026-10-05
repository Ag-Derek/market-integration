"""Per-client WebSocket subscriptions (#13), throttled and conflated (#20).

Most tests drive WebSocketGateway directly with fake sockets, so they
control exactly which ticks are broadcast and when a client is slow. The
last ones go through the real app and the live mock feed.
"""

import asyncio
import json
import time

import pytest

from app.gateways.websocket_gateway import WebSocketGateway

SYMBOLS = ["MTNGH", "GCB", "CAL", "SCB"]


class FakeSocket:
    """Records what the gateway sends. A `gate` makes it slow: every send
    waits until the gate is opened."""

    def __init__(self, gate: asyncio.Event | None = None):
        self.sent: list[dict] = []
        self.gate = gate
        self.accepted = False
        self.fail = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("connection reset")
        self.sent.append(message)

    def of_type(self, kind):
        return [m for m in self.sent if m["type"] == kind]

    def quotes(self):
        """Every quote received, flattened out of the "ticks" batches."""
        return [q for m in self.of_type("ticks") for q in m["data"]]

    def tick_symbols(self):
        return [q["symbol"] for q in self.quotes()]


async def _settle():
    """Let the per-client sender tasks drain their outboxes."""
    for _ in range(20):
        await asyncio.sleep(0)


def _gateway(latest=None, send_interval=0.0):
    latest = latest or {}
    return WebSocketGateway(SYMBOLS, snapshot=lambda syms: {s: latest[s] for s in syms if s in latest},
                            send_interval=send_interval)


async def _connect(gateway, socket=None):
    socket = socket or FakeSocket()
    client = await gateway.connect(socket)
    return socket, client


def _subscribe(gateway, client, *symbols, action="subscribe"):
    gateway.handle_message(client, json.dumps({"action": action, "symbols": list(symbols)}))


# ---------------------------------------------------------------- protocol

async def test_a_new_client_gets_the_symbol_list_and_no_market_data(make_tick):
    gateway = _gateway()
    socket, _ = await _connect(gateway)
    await gateway.broadcast(make_tick("MTNGH"))
    await _settle()

    assert socket.accepted
    assert socket.sent == [{"type": "welcome", "symbols": SYMBOLS}]


async def test_the_welcome_labels_asset_classes_and_the_universe_can_grow():
    # Fixed income joins the feed (#35): pages pick what they show by asset
    # class, and a T-bill issued after startup can be subscribed to.
    universe = ["MTNGH", "GHGGOG069931"]
    gateway = WebSocketGateway(
        lambda: universe, snapshot=lambda syms: {},
        asset_class=lambda s: "equity" if s == "MTNGH" else "bond",
    )
    socket, client = await _connect(gateway)
    await _settle()
    assert socket.sent[0] == {
        "type": "welcome", "symbols": universe,
        "asset_classes": {"MTNGH": "equity", "GHGGOG069931": "bond"},
    }

    universe.append("GHMKA2701040")
    _subscribe(gateway, client, "GHMKA2701040")
    await _settle()
    assert socket.of_type("subscribed")[0]["symbols"] == ["GHMKA2701040"]
    assert socket.of_type("error") == []


async def test_subscribe_acknowledges_and_snapshots_only_the_new_symbols(make_tick):
    latest = {s: make_tick(s) for s in SYMBOLS}
    gateway = _gateway(latest)
    socket, client = await _connect(gateway)

    _subscribe(gateway, client, "mtngh", "GCB")  # case-insensitive
    _subscribe(gateway, client, "GCB", "CAL")    # GCB already subscribed
    await _settle()

    acks = socket.of_type("subscribed")
    assert acks == [
        {"type": "subscribed", "symbols": ["MTNGH", "GCB"], "subscriptions": ["GCB", "MTNGH"]},
        {"type": "subscribed", "symbols": ["CAL"], "subscriptions": ["CAL", "GCB", "MTNGH"]},
    ]
    snapshots = socket.of_type("snapshot")
    assert [set(s["data"]) for s in snapshots] == [{"MTNGH", "GCB"}, {"CAL"}]
    assert snapshots[0]["data"]["MTNGH"]["price"] == latest["MTNGH"].price


async def test_a_client_subscribed_to_two_symbols_never_receives_other_ticks(make_tick):
    # The issue's acceptance criterion.
    gateway = _gateway()
    socket, client = await _connect(gateway)
    _subscribe(gateway, client, "MTNGH", "GCB")
    await _settle()

    for n in range(5):
        for symbol in SYMBOLS:
            await gateway.broadcast(make_tick(symbol, price=6.00 + n / 100))
            await _settle()

    assert set(socket.tick_symbols()) == {"MTNGH", "GCB"}
    assert len(socket.tick_symbols()) == 10


async def test_unsubscribe_stops_ticks_for_those_symbols_only(make_tick):
    gateway = _gateway()
    socket, client = await _connect(gateway)
    _subscribe(gateway, client, "MTNGH", "GCB")
    _subscribe(gateway, client, "GCB", "CAL", action="unsubscribe")  # CAL wasn't subscribed
    await _settle()
    await gateway.broadcast(make_tick("MTNGH"))
    await gateway.broadcast(make_tick("GCB"))
    await _settle()

    assert socket.of_type("unsubscribed") == [
        {"type": "unsubscribed", "symbols": ["GCB"], "subscriptions": ["MTNGH"]},
    ]
    assert socket.tick_symbols() == ["MTNGH"]
    assert gateway.subscribers("GCB") == 0


async def test_ticks_go_only_to_each_symbols_subscribers(make_tick):
    gateway = _gateway()
    a, ca = await _connect(gateway)
    b, cb = await _connect(gateway)
    _subscribe(gateway, ca, "MTNGH")
    _subscribe(gateway, cb, "MTNGH", "CAL")
    await gateway.broadcast(make_tick("MTNGH"))
    await gateway.broadcast(make_tick("CAL"))
    await _settle()

    assert a.tick_symbols() == ["MTNGH"]
    assert b.tick_symbols() == ["MTNGH", "CAL"]


async def test_unknown_symbols_are_reported_and_the_valid_ones_still_apply(make_tick):
    gateway = _gateway()
    socket, client = await _connect(gateway)
    _subscribe(gateway, client, "MTNGH", "NOPE", "AAPL")
    await _settle()

    [error] = socket.of_type("error")
    assert (error["code"], error["symbols"]) == ("unknown_symbols", ["NOPE", "AAPL"])
    assert socket.of_type("subscribed")[0]["symbols"] == ["MTNGH"]


@pytest.mark.parametrize("text,code", [
    ("{not json", "bad_json"),
    ('["subscribe"]', "bad_request"),
    ('{"action": "subscribe"}', "bad_request"),
    ('{"action": "buy", "symbols": ["MTNGH"]}', "bad_request"),
    ('{"action": "subscribe", "symbols": "MTNGH"}', "bad_request"),
    ('{"action": "subscribe", "symbols": [1, 2]}', "bad_request"),
])
async def test_malformed_messages_get_an_error_and_change_nothing(text, code):
    gateway = _gateway()
    socket, client = await _connect(gateway)
    gateway.handle_message(client, text)
    await _settle()

    assert [m["code"] for m in socket.of_type("error")] == [code]
    assert client.subscriptions == set()


# ---------------------------------------------------------------- cleanup

async def test_disconnect_removes_the_client_from_every_symbol(make_tick):
    gateway = _gateway()
    socket, client = await _connect(gateway)
    other, other_client = await _connect(gateway)
    _subscribe(gateway, client, "MTNGH", "GCB")
    _subscribe(gateway, other_client, "MTNGH")

    await gateway.disconnect(socket)
    await gateway.broadcast(make_tick("MTNGH"))
    await _settle()

    assert gateway.client_count == 1
    assert (gateway.subscribers("MTNGH"), gateway.subscribers("GCB")) == (1, 0)
    assert client.sender.done()
    assert socket.of_type("ticks") == []
    assert other.tick_symbols() == ["MTNGH"]
    await gateway.disconnect(socket)  # idempotent


async def test_a_client_whose_send_fails_stops_receiving(make_tick):
    gateway = _gateway()
    socket, client = await _connect(gateway)
    _subscribe(gateway, client, "MTNGH")
    await _settle()
    socket.fail = True
    await gateway.broadcast(make_tick("MTNGH"))
    await _settle()

    assert gateway.subscribers("MTNGH") == 0
    assert client.closed


# ---------------------------------------------------------------- slow clients

async def test_a_slow_client_holds_up_neither_the_feed_nor_other_clients(make_tick):
    gateway = _gateway()
    fast, fast_client = await _connect(gateway)
    gate = asyncio.Event()
    slow, slow_client = await _connect(gateway, FakeSocket(gate))  # stuck until the gate opens
    for c in (fast_client, slow_client):
        _subscribe(gateway, c, "MTNGH", "GCB")
    await _settle()

    prices = [round(6.00 + n / 100, 2) for n in range(50)]
    for price in prices:
        # broadcast() must return at once even though `slow` isn't reading.
        await asyncio.wait_for(gateway.broadcast(make_tick("MTNGH", price=price)), timeout=0.05)
        await gateway.broadcast(make_tick("GCB", price=40.0))
        await _settle()

    # The fast client (no send interval here) got every tick, in order.
    assert [q["price"] for q in fast.quotes() if q["symbol"] == "MTNGH"] == prices
    # The slow one has nothing yet, and at most one tick per symbol waiting.
    assert slow.of_type("ticks") == []
    assert set(slow_client.pending) <= {("equity", "MTNGH"), ("equity", "GCB")}
    assert slow_client.conflated > 0

    gate.set()
    await _settle()
    # Once it catches up it gets the current price, not a backlog: one
    # batch with the latest quote per symbol.
    assert len(slow.of_type("ticks")) == 1
    assert {q["symbol"]: q["price"] for q in slow.quotes()} == {"MTNGH": prices[-1], "GCB": 40.0}


# ---------------------------------------------------------------- throttling (#20)

async def test_ticks_are_batched_and_sent_at_most_once_per_interval(make_tick):
    interval = 0.05
    gateway = _gateway(send_interval=interval)
    socket, client = await _connect(gateway)
    _subscribe(gateway, client, "MTNGH", "GCB")
    await _settle()

    # A burst far faster than the interval: two symbols as fast as the
    # loop goes for 0.3 s (thousands of ticks).
    start, n = time.monotonic(), 0
    while time.monotonic() - start < 0.3:
        await gateway.broadcast(make_tick("MTNGH", price=6.00 + n))
        await gateway.broadcast(make_tick("GCB", price=40.0 + n))
        n += 1
        await asyncio.sleep(0)
    await asyncio.sleep(interval * 2)  # time for the last batch
    elapsed = time.monotonic() - start

    batches = socket.of_type("ticks")
    # Bounded by time, not by tick count: one batch per interval, plus the
    # first, which goes at once.
    assert 2 <= len(batches) <= elapsed / interval + 1
    assert 2 * n > 10 * len(batches)
    # Each batch carries at most one quote per symbol...
    for batch in batches:
        symbols = [q["symbol"] for q in batch["data"]]
        assert len(symbols) == len(set(symbols))
    # ...and the last one ends on the current prices.
    latest = {q["symbol"]: q["price"] for q in socket.quotes()}
    assert latest == {"MTNGH": 6.00 + n - 1, "GCB": 40.0 + n - 1}
    assert gateway.ticks_sent == len(socket.quotes())
    assert client.conflated == 2 * n - gateway.ticks_sent


async def test_control_messages_do_not_wait_for_the_interval(make_tick):
    latest = {"CAL": make_tick("CAL", price=0.7)}
    gateway = _gateway(latest, send_interval=10)  # a batch every 10 s
    socket, client = await _connect(gateway)
    _subscribe(gateway, client, "MTNGH")
    await gateway.broadcast(make_tick("MTNGH", price=6.0))
    await _settle()
    assert len(socket.of_type("ticks")) == 1   # the first batch goes at once

    await gateway.broadcast(make_tick("MTNGH", price=6.1))   # held for ~10 s
    _subscribe(gateway, client, "CAL")
    await gateway.broadcast_status({"status": "open"})
    await _settle()
    # The ack, snapshot and status went out; the tick is still held.
    assert socket.of_type("subscribed")[-1]["symbols"] == ["CAL"]
    assert socket.of_type("snapshot")[-1]["data"]["CAL"]["price"] == 0.7
    assert len(socket.of_type("status")) == 1
    assert len(socket.of_type("ticks")) == 1
    assert client.pending  # still waiting for the interval


async def test_a_snapshot_still_comes_before_ticks_for_its_symbols(make_tick):
    gateway = _gateway({"GCB": make_tick("GCB", price=40.0)}, send_interval=0.02)
    socket, client = await _connect(gateway)
    _subscribe(gateway, client, "GCB")
    await gateway.broadcast(make_tick("GCB", price=40.5))
    await asyncio.sleep(0.05)

    kinds = [m["type"] for m in socket.sent]
    assert kinds.index("snapshot") < kinds.index("ticks")


# ---------------------------------------------------------------- through the app

def test_live_feed_sends_only_subscribed_symbols(client):
    from app import config

    wanted = ["MTNGH", "CAL"]  # active names: trade every few seconds in the mock
    with client.websocket_connect("/ws/market") as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"action": "subscribe", "symbols": wanted})

        ack = ws.receive_json()
        assert ack == {"type": "subscribed", "symbols": wanted, "subscriptions": sorted(wanted)}
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot" and set(snapshot["data"]) == set(wanted)

        ticks = []
        while len(ticks) < 3:
            msg = ws.receive_json()
            if msg["type"] == "status":  # periodic, to every client
                continue
            assert msg["type"] == "ticks"
            ticks += [q["symbol"] for q in msg["data"]]
    assert set(ticks) <= set(wanted)
    assert len(config.SYMBOLS) > len(wanted)  # there were other symbols to leak


def test_disconnect_through_the_app_cleans_up(client):
    from app.main import gateway

    before = gateway.client_count
    with client.websocket_connect("/ws/market") as ws:
        ws.receive_json()
        ws.send_json({"action": "subscribe", "symbols": ["SCB"]})
        ws.receive_json()
        assert gateway.subscribers("SCB") >= 1
    for _ in range(50):  # the server notices the close asynchronously
        if gateway.client_count == before:
            break
        time.sleep(0.02)
    assert gateway.client_count == before
    assert gateway.subscribers("SCB") == 0
