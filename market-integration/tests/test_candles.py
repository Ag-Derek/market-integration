import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import config
from app.aggregation.market_aggregator import MarketAggregator
from app.aggregation.ranges import RANGES
from app.connectors.market_connector import MockMarketConnector
from app.models.candle import Candle, bucket_start, resample
from app.validation.market_validator import validate_candle, validate_tick


async def _no_feed():
    return
    yield  # pragma: no cover - makes this an async generator


def _candle(symbol, interval, start, o, h, l, c, v, tick_count=0):
    return Candle(
        symbol=symbol, interval=interval, window_start=start,
        window_end=start + timedelta(minutes=5), open=o, high=h, low=l, close=c,
        volume=v, tick_count=tick_count,
    )


# ---------------------------------------------------------------- time grid

def test_bucket_start_aligns_to_the_interval_grid():
    ts = datetime(2026, 9, 23, 10, 7, 42, tzinfo=timezone.utc)  # a Wednesday

    assert bucket_start(ts, "5m") == datetime(2026, 9, 23, 10, 5, tzinfo=timezone.utc)
    assert bucket_start(ts, "15m") == datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    assert bucket_start(ts, "1h") == datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    assert bucket_start(ts, "1d") == datetime(2026, 9, 23, tzinfo=timezone.utc)
    # Weeks start on Monday, not on the epoch's Thursday.
    assert bucket_start(ts, "1w") == datetime(2026, 9, 21, tzinfo=timezone.utc)


def test_resample_rolls_fine_candles_into_coarser_ones():
    t0 = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    fine = [
        _candle("MTNGH", "5m", t0, 10, 12, 9, 11, 100),
        _candle("MTNGH", "5m", t0 + timedelta(minutes=5), 11, 15, 10, 14, 50),
        _candle("MTNGH", "5m", t0 + timedelta(minutes=10), 14, 14, 8, 9, 25),
        _candle("MTNGH", "5m", t0 + timedelta(minutes=15), 9, 10, 9, 10, 5),
    ]

    coarse = resample(fine, "15m")

    assert len(coarse) == 2
    first = coarse[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (10, 15, 8, 9, 175)
    assert first.window_end == t0 + timedelta(minutes=15)
    assert coarse[1].window_start == t0 + timedelta(minutes=15)


# ---------------------------------------------------------------- validation

def test_validate_candle_accepts_a_well_formed_candle():
    t0 = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    assert validate_candle(_candle("GCB", "5m", t0, 40.04, 40.10, 39.98, 40.02, 13_263))


@pytest.mark.parametrize("o,h,l,c,v,tick_count,expected", [
    (10, 12, 9, 13, 1, 0, "close (13.0) outside"),
    (8, 12, 9, 11, 1, 0, "open (8.0) outside"),
    (10, 9, 12, 11, 1, 0, "low (12.0) > high (9.0)"),
    (10, 12, 0, 11, 1, 0, "prices must be positive"),
    (10, 12, 9, 11, -1, 0, "volume (-1) is negative"),
    (10, 12, 9, 11, 1, -1, "tick_count (-1) is negative"),
])
def test_validate_candle_rejects_broken_ohlcv(o, h, l, c, v, tick_count, expected):
    t0 = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    result = validate_candle(_candle("GCB", "5m", t0, o, h, l, c, v, tick_count))
    assert not result
    assert any(expected in e for e in result.errors)


def test_validate_candle_rejects_windows_off_the_grid():
    t0 = datetime(2026, 9, 18, 9, 31, tzinfo=timezone.utc)
    result = validate_candle(_candle("GCB", "5m", t0, 10, 12, 9, 11, 1))
    assert not result
    assert any("not aligned" in e for e in result.errors)

    t0 = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    wrong_end = _candle("GCB", "15m", t0, 10, 12, 9, 11, 1)  # helper sets a 5m window_end
    assert any("window_end" in e for e in validate_candle(wrong_end).errors)


async def test_aggregator_skips_invalid_candles_when_flushing(tmp_path):
    db_path = tmp_path / "c.db"
    aggregator = MarketAggregator(_no_feed(), db_path=db_path, intervals=("5m",))
    t0 = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    aggregator._pending = [
        _candle("GCB", "5m", t0, 10, 12, 9, 11, 1, tick_count=3),
        _candle("GCB", "5m", t0 + timedelta(minutes=5), 11, 10, 12, 11, 1, tick_count=3),
    ]

    await aggregator._flush()

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT window_start FROM market_candles").fetchall()
    finally:
        conn.close()
    assert rows == [(t0.isoformat(),)]


# ---------------------------------------------------------------- aggregator

def test_live_candles_count_every_tick(make_tick, tmp_path):
    aggregator = MarketAggregator(_no_feed(), db_path=tmp_path / "c.db", intervals=("15m",))
    t0 = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    for i in range(4):
        aggregator._record(make_tick(price=100.0 + i, volume=1_000 + i, timestamp=t0 + timedelta(minutes=i)))

    live = aggregator._windows[("MTNGH", "15m")].to_candle("MTNGH", "15m")
    assert live.tick_count == 4
    assert validate_candle(live)


def test_aggregator_closes_windows_on_bucket_boundaries_without_losing_volume(make_tick, tmp_path):
    aggregator = MarketAggregator(_no_feed(), db_path=tmp_path / "c.db", intervals=("5m", "1h"))
    t0 = datetime(2026, 9, 23, 10, 3, tzinfo=timezone.utc)

    aggregator._record(make_tick(price=100.0, volume=1_000, timestamp=t0))
    aggregator._record(make_tick(price=102.0, volume=1_200, timestamp=t0 + timedelta(minutes=1)))
    # Crosses into the 10:05 five-minute bucket; same hourly bucket.
    aggregator._record(make_tick(price=101.0, volume=1_500, timestamp=t0 + timedelta(minutes=3)))

    [closed] = aggregator._pending
    assert closed.interval == "5m"
    assert closed.window_start == datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    assert (closed.open, closed.high, closed.close, closed.volume) == (100.0, 102.0, 102.0, 200)

    # Volume traded between the last tick of one window and the first
    # tick of the next is credited to the new window, not dropped.
    assert aggregator._windows[("MTNGH", "5m")].volume == 300
    hour = aggregator._windows[("MTNGH", "1h")]
    assert (hour.open, hour.close, hour.volume, hour.tick_count) == (100.0, 101.0, 500, 3)


async def test_get_candles_overlays_unflushed_windows_on_stored_ones(make_tick, tmp_path):
    aggregator = MarketAggregator(_no_feed(), db_path=tmp_path / "c.db", intervals=("5m",))
    now = datetime.now(timezone.utc)
    current = bucket_start(now, "5m")
    earlier = current - timedelta(minutes=5)
    aggregator._replace_rows("MTNGH", "5m", [
        _candle("MTNGH", "5m", earlier, 90, 95, 89, 94, 10),
        _candle("MTNGH", "5m", current, 94, 96, 93, 95, 10),  # stale copy of the open window
    ])

    aggregator._record(make_tick(price=97.0, volume=10, timestamp=now))

    candles = await aggregator.get_candles("MTNGH", "5m", earlier)
    assert [c.window_start for c in candles] == [earlier, current]
    assert candles[0].close == 94
    assert candles[1].close == 97.0  # in-memory window wins over the stored row


class _HistoryConnector:
    def __init__(self, symbols, history):
        self.symbols = symbols
        self._history = history

    async def fetch_history(self, symbol, interval, start=None):
        return self._history.get((symbol, interval), [])


async def test_backfill_replaces_stored_history_and_seeds_the_open_window(tmp_path):
    db_path = tmp_path / "c.db"
    aggregator = MarketAggregator(_no_feed(), db_path=db_path, intervals=("5m",))
    now = datetime.now(timezone.utc)
    current = bucket_start(now, "5m")
    earlier = current - timedelta(minutes=5)

    # A previous run's candle for the same window, with a different price path.
    aggregator._replace_rows("MTNGH", "5m", [_candle("MTNGH", "5m", earlier, 500, 500, 500, 500, 1)])

    history = [
        _candle("MTNGH", "5m", earlier, 90, 95, 89, 94, 10),
        _candle("MTNGH", "5m", current, 94, 99, 93, 95, 20),
    ]
    stored = await aggregator.backfill(_HistoryConnector(["MTNGH"], {("MTNGH", "5m"): history}), now=now)

    assert stored == 2
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT window_start, close FROM market_candles WHERE symbol = 'MTNGH' ORDER BY window_start"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(earlier.isoformat(), 94), (current.isoformat(), 95)]

    seeded = aggregator._windows[("MTNGH", "5m")]
    assert (seeded.window_start, seeded.open, seeded.high, seeded.volume) == (current, 94, 99, 20)


# ---------------------------------------------------------------- mock history

async def test_mock_history_is_continuous_and_matches_the_live_quote():
    connector = MockMarketConnector(symbols=["MTNGH"], interval_seconds=0.01)
    await connector.connect()
    try:
        price = connector._prices["MTNGH"]
        profile = connector._profiles["MTNGH"]
        session = connector._session["MTNGH"]

        five = await connector.fetch_history("MTNGH", "5m")
        daily = await connector.fetch_history("MTNGH", "1d")
        weekly = await connector.fetch_history("MTNGH", "1w")

        # Every interval ends exactly at the live starting price...
        for candles in (five, daily, weekly):
            assert candles[-1].close == price
        # ...and is one unbroken path (each bar opens where the last closed).
        assert all(b.open == a.close for a, b in zip(five, five[1:]))
        assert all(b.open == a.close for a, b in zip(daily, daily[1:]))
        assert all(c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close) for c in five)
        # Every interval of the synthetic history passes the OHLCV checks;
        # it came from no ticks, so tick_count is 0 throughout.
        for interval in ("5m", "15m", "1h", "1d", "1w"):
            candles = await connector.fetch_history("MTNGH", interval)
            assert all(validate_candle(c) for c in candles), interval
            assert all(c.tick_count == 0 for c in candles)

        midnight = bucket_start(datetime.now(timezone.utc), "1d")
        # Previous close is yesterday's VWAP (MTNGH trades all day), so it
        # lies within yesterday's range; the open is that reference price,
        # as in the GSE report.
        assert daily[-2].low <= profile["previous_close"] <= daily[-2].high
        assert session["open"] == profile["previous_close"]
        assert daily[-1].window_start == midnight
        # The year range is this calendar year's and the 52-week range
        # the trailing year's; both cover the live price and the
        # carried-over close.
        this_year = [c for c in daily if c.window_start.year == midnight.year]
        assert profile["year_low"] <= min(c.low for c in this_year)
        assert profile["year_high"] >= max(c.high for c in this_year)
        for key in ("year", "week52"):
            assert profile[f"{key}_low"] <= price <= profile[f"{key}_high"]
            assert profile[f"{key}_low"] <= profile["previous_close"] <= profile[f"{key}_high"]

        since = await connector.fetch_history("MTNGH", "1h", midnight)
        assert since[0].window_start == midnight
    finally:
        await connector.disconnect()


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

@pytest.mark.parametrize("range_", list(RANGES))
def test_candles_api_serves_every_range(client, range_):
    response = client.get("/candles", params={"symbol": "mtngh", "range": range_})

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "MTNGH"
    assert body["interval"] == RANGES[range_].interval
    candles = body["candles"]
    assert len(candles) > 1
    times = [c["time"] for c in candles]
    assert times == sorted(times)

    latest = client.get("/market/MTNGH").json()
    # The newest candle is the live one, not the last backfilled bar.
    assert candles[-1]["close"] == pytest.approx(latest["price"], abs=0.5)
    assert all(c["yield"] is None for c in candles)  # equities have no yield


def test_candles_api_rejects_unknown_inputs(client):
    assert client.get("/candles", params={"symbol": "NOPE"}).status_code == 404
    assert client.get("/candles", params={"symbol": "MTNGH", "range": "2D"}).status_code == 400
    assert client.get("/candles", params={"symbol": "MTNGH", "interval": "3m"}).status_code == 400


def test_csv_export_still_serves_15m_candles(client):
    response = client.get("/candles/export", params={"symbol": "MTNGH"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0] == (
        "symbol,interval,window_start,window_end,open,high,low,close,volume,tick_count,"
        "yield_open,yield_high,yield_low,yield_close,possibly_incomplete"
    )
    assert len(lines) > 1
    assert all(line.startswith("MTNGH,15m,") for line in lines[1:])


def test_stock_page_is_served_for_tracked_symbols_only(client):
    assert client.get("/stock/mtngh").status_code == 200
    assert client.get("/stock/SCB-PREF").status_code == 200
    assert client.get("/stock/NOPE").status_code == 404
    first = config.SYMBOLS[0]
    assert client.get("/stock", follow_redirects=False).headers["location"] == f"/stock/{first}"


# ---------------------------------------------------------------- GSE universe

async def test_dormant_names_stay_flat_and_quote_a_thin_book():
    # PBC: no trades and a 0.02-0.02 year range on 28-Sep-2026, no bid
    # or offer. ACCESS: bid only.
    connector = MockMarketConnector(symbols=["PBC", "ACCESS"], interval_seconds=0.01)
    await connector.connect()
    try:
        daily = await connector.fetch_history("PBC", "1d")
        assert {(c.open, c.high, c.low, c.close) for c in daily} == {(0.02, 0.02, 0.02, 0.02)}
        # Most days had no trade at all: flat, zero-volume bars.
        assert sum(c.volume == 0 for c in daily) > len(daily) / 2

        # The stream opens with one snapshot per symbol, whether or not
        # it trades.
        stream = connector.stream()
        first = {}
        async for tick in stream:
            first[tick.symbol] = tick
            if len(first) == 2:
                break
        await stream.aclose()

        pbc, access = first["PBC"], first["ACCESS"]
        assert (pbc.price, pbc.bid, pbc.ask, pbc.bid_size, pbc.ask_size) == (0.02, None, None, 0, 0)
        assert access.price == 20.67
        assert access.bid is not None and access.ask is None and access.ask_size == 0
        assert validate_tick(pbc) and validate_tick(access)
    finally:
        await connector.disconnect()


async def test_mock_emits_the_gse_report_fields_consistently():
    # MTNGH trades every few seconds; PBC and AGA are dormant (no trades on
    # 28-Sep-2026) and ACCESS quotes a bid only.
    symbols = ["MTNGH", "PBC", "AGA", "ACCESS"]
    connector = MockMarketConnector(symbols=symbols, interval_seconds=0.01)
    await connector.connect()
    try:
        # The opening snapshot (one tick per symbol), then one forced live
        # MTNGH trade, so both the backfilled and live turnover are covered.
        latest = {}
        stream = connector.stream()
        async for tick in stream:
            latest[tick.symbol] = tick
            if len(latest) == len(symbols):
                break
        await stream.aclose()
        profile = connector._profiles["MTNGH"]
        before = latest["MTNGH"].value_traded
        connector._trade("MTNGH", profile, profile["trade_gap"])
        latest["MTNGH"] = connector.normalize(connector._quote("MTNGH", profile))
        assert latest["MTNGH"].value_traded > before
    finally:
        await connector.disconnect()

    for t in latest.values():
        assert validate_tick(t), (t.symbol, validate_tick(t).errors)
        assert t.open == t.previous_close  # the report's opening price is a reference price
        assert t.change == pytest.approx(t.vwap - t.previous_close, abs=0.005)
        if t.volume == 0:
            # No trades this session: VWAP carries over, nothing changed hands.
            assert (t.vwap, t.change, t.value_traded) == (t.previous_close, 0, 0)
        else:
            assert t.vwap == pytest.approx(t.value_traded / t.volume, abs=0.005)

    mtn = latest["MTNGH"]
    assert mtn.volume > 0 and mtn.value_traded > 0
    assert mtn.day_low - 0.01 <= mtn.vwap <= mtn.day_high + 0.01


def test_websocket_welcome_lists_every_tracked_symbol_without_prices(client):
    # Per-client subscriptions (#13): on connect a client learns what it can
    # subscribe to, but gets no market data until it subscribes.
    with client.websocket_connect("/ws/market") as ws:
        msg = ws.receive_json()

    # ...plus the market status, so its badge is right from the start (#14).
    status = msg.pop("status")
    assert set(msg) == {"type", "symbols", "asset_classes"}
    # Equities first, then every bill and bond (#35), each labelled so a
    # page can show only what it handles.
    equities = msg["symbols"][:len(config.SYMBOLS)]
    assert equities == config.SYMBOLS
    classes = msg["asset_classes"]
    assert set(classes) == set(msg["symbols"])
    assert {classes[s] for s in equities} == {"equity"}
    assert {classes[s] for s in msg["symbols"][len(equities):]} == {"bill", "bond"}
    assert status["badge"] in ("live", "delayed", "closed", "disconnected")
