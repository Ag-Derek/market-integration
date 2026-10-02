import asyncio

import pytest

from app.connectors.load_connector import LoadConnector
from app.validation.market_validator import validate_tick


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


async def _run(load: LoadConnector, clock: FakeClock, seconds: float, step: float = 0.01) -> list:
    """Drive the stream on a fake clock: `seconds` of stream time."""
    ticks = []
    stream = load.stream()
    # The snapshot comes first, all at once.
    for _ in load.symbols:
        ticks.append(await stream.__anext__())
    snapshot = len(ticks)

    async def pull():
        async for t in stream:
            ticks.append(t)

    task = asyncio.ensure_future(pull())
    while clock.now < seconds:
        clock.now = round(clock.now + step, 6)
        await asyncio.sleep(0)  # the stream's sleep(step_seconds=0) yields here
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return ticks[snapshot:]


def _load(clock, **kw):
    return LoadConnector(step_seconds=0, clock=clock, **kw)


async def test_overall_rate_is_emitted_round_robin():
    clock = FakeClock()
    load = _load(clock, instruments=4, ticks_per_second=200)
    await load.connect()
    ticks = await _run(load, clock, seconds=2)

    assert 390 <= len(ticks) <= 400  # 200/s for 2 s
    assert [t.symbol for t in ticks[:8]] == ["LOAD0001", "LOAD0002", "LOAD0003", "LOAD0004"] * 2
    assert load.emitted == len(ticks)


async def test_per_instrument_rate_when_no_overall_rate():
    load = LoadConnector(instruments=50, ticks_per_instrument=2.0)
    assert load.base_rate == 100
    assert LoadConnector(instruments=50, ticks_per_second=7, ticks_per_instrument=2.0).base_rate == 7


def test_burst_windows():
    once = LoadConnector(ticks_per_second=100, burst_multiplier=10, burst_seconds=30, burst_after=60)
    assert [once.rate(t) for t in (0, 59.9, 60, 89.9, 90, 500)] == [100, 100, 1000, 1000, 100, 100]

    every = LoadConnector(ticks_per_second=100, burst_multiplier=5, burst_seconds=10, burst_after=0, burst_every=60)
    assert [every.in_burst(t) for t in (0, 9.9, 10, 59, 60, 65, 70)] == [True, True, False, False, True, True, False]

    assert not LoadConnector(ticks_per_second=100).in_burst(1000)  # burst_seconds=0: never


async def test_a_burst_multiplies_the_rate_while_it_lasts():
    clock = FakeClock()
    load = _load(clock, instruments=10, ticks_per_second=100, burst_multiplier=10, burst_seconds=1, burst_after=1)
    await load.connect()
    ticks = await _run(load, clock, seconds=3)
    # 1 s at 100, 1 s at 1,000, 1 s at 100 (give or take a clock step).
    assert 1150 <= len(ticks) <= 1220


async def test_ticks_pass_validation():
    load = LoadConnector(instruments=20, ticks_per_second=1000)
    await load.connect()
    ticks = [load._tick(s, trade=False) for s in load.symbols]
    ticks += [load._tick(load.symbols[i % 20], trade=True) for i in range(2000)]
    rejected = [(t.symbol, validate_tick(t).errors) for t in ticks if not validate_tick(t)]
    assert rejected == []


async def test_a_loop_too_slow_to_keep_up_skips_rather_than_floods():
    clock = FakeClock()
    load = _load(clock, instruments=1, ticks_per_second=100)
    await load.connect()
    stream = load.stream()
    await stream.__anext__()  # snapshot
    batch = []
    task = asyncio.ensure_future(_collect(stream, batch))
    for _ in range(3):        # running at t=0: nothing due yet
        await asyncio.sleep(0)
    assert batch == []
    clock.now = 10.0          # then the loop stalls for 10 s
    for _ in range(3):
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(batch) <= 100          # at most a second's worth at once
    assert load.skipped >= 899


async def _collect(stream, into):
    async for t in stream:
        into.append(t)


def test_bad_settings_are_rejected():
    with pytest.raises(ValueError):
        LoadConnector(instruments=0)
    with pytest.raises(ValueError):
        LoadConnector(ticks_per_second=0)
    with pytest.raises(ValueError):
        LoadConnector(burst_seconds=30, burst_every=10)
