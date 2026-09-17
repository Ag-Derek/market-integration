from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.market_data import MarketData

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def make_tick():
    """Factory for a schema-valid MarketData tick, with overridable fields.

    Defaults describe an internally-consistent, fresh, in-range tick, so
    each test only needs to override the field(s) it cares about.
    """

    def _make(symbol: str = "AAPL", **overrides) -> MarketData:
        base = dict(
            symbol=symbol,
            name="Apple Inc.",
            exchange_label="TEST",
            price=250.03,
            previous_close=248.00,
            open=249.00,
            day_high=252.00,
            day_low=248.00,
            week52_high=300.00,
            week52_low=150.00,
            market_cap=2_500_000_000.0,
            beta=1.1,
            pe_ratio=30.0,
            eps=8.3,
            bid=250.00,
            bid_size=100,
            ask=250.05,
            ask_size=100,
            volume=10_000,
            avg_volume=1_000_000,
            forward_dividend=0.5,
            forward_dividend_yield=0.2,
            ex_dividend_date="2026-11-01",
            earnings_date="2026-11-15",
            target_est=270.00,
            dividend_announcement=None,
            timestamp=datetime.now(timezone.utc),
        )
        base.update(overrides)
        return MarketData(**base)

    return _make


@pytest.fixture(scope="session", autouse=True)
def _cleanup_market_data_db():
    """MarketAggregator's default db path (and app.main's, which uses it)
    is relative to the process cwd. Clean up whatever this test session
    caused to be created there, wherever pytest was invoked from."""
    yield
    for db_path in {REPO_ROOT / "market_data.db", Path.cwd() / "market_data.db"}:
        if db_path.exists():
            db_path.unlink()
