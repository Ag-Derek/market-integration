"""
The candle fidelity benchmark (benchmarks/candle_fidelity.py), short: at
the assumed benchmark target rate the aggregator and alerts branches
drop nothing and every candle matches the reference built from the raw
tick log; a stalled lossless subscriber doesn't hold the others up.
"""

from benchmarks.candle_fidelity import TARGET_RATE, run


async def test_at_the_target_rate_lossless_branches_drop_nothing_and_candles_match(tmp_path):
    result = await run(duration=3, rate=TARGET_RATE, db_path=tmp_path / "c.db")

    aggregator, alerts = result.branches["aggregator"], result.branches["alerts"]
    assert aggregator.received == alerts.received == result.generated > 0
    assert aggregator.dropped == alerts.dropped == 0
    assert result.candles > 0
    assert result.ok, result.report()


async def test_a_stalled_lossless_subscriber_does_not_hold_the_rest_up(tmp_path):
    # Alerts stops reading 0.5 s in, for good: its queue fills, the feed
    # waits for it once, then it alone loses ticks.
    result = await run(
        duration=3, rate=TARGET_RATE, stall_alerts_at=0.5, db_path=tmp_path / "c.db"
    )

    alerts, aggregator = result.branches["alerts"], result.branches["aggregator"]
    assert alerts.depth_end == alerts.capacity  # full, never read again
    assert alerts.dropped > 0
    assert alerts.received + alerts.dropped + alerts.depth_end == result.generated
    assert alerts.blocked_seconds < 1.0  # one wait, not one per tick
    # Everyone else carried on: every tick reached the aggregator and the
    # display, and the candles are exact.
    assert aggregator.received == result.generated and aggregator.dropped == 0
    display = result.branches["processor"]
    assert display.received + display.dropped + display.depth_end == result.generated
    assert result.ok, result.report()


async def test_with_the_old_drop_oldest_policy_every_wrong_candle_is_flagged(tmp_path):
    # The opening snapshot alone (~200 ticks at once) overflows a 200-tick
    # queue, so this always loses ticks; each candle it got wrong must
    # say so.
    result = await run(duration=3, rate=TARGET_RATE, lossless=False, db_path=tmp_path / "c.db")

    assert result.branches["aggregator"].dropped > 0
    assert result.incomplete_candles > 0
    assert result.unflagged_mismatches == 0, result.report()
