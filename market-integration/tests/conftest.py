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

    def _make(symbol: str = "MTNGH", **overrides) -> MarketData:
        base = dict(
            symbol=symbol,
            name="Scancom PLC (MTN Ghana)",
            exchange_label="TEST",
            price=6.54,
            previous_close=6.50,
            open=6.52,
            day_high=6.60,
            day_low=6.48,
            week52_high=7.15,
            week52_low=4.20,
            market_cap=80_000_000_000.0,
            beta=0.8,
            pe_ratio=9.0,
            eps=0.73,
            bid=6.54,
            bid_size=100,
            ask=6.55,
            ask_size=100,
            volume=298_817,
            avg_volume=300_000,
            forward_dividend=0.35,
            forward_dividend_yield=5.35,
            ex_dividend_date="2026-11-01",
            earnings_date="2026-11-15",
            target_est=7.50,
            dividend_announcement=None,
            timestamp=datetime.now(timezone.utc),
        )
        base.update(overrides)
        return MarketData(**base)

    return _make


@pytest.fixture(scope="session")
def client():
    """One running app for the whole session. app.main wires its pipeline
    as module-level singletons, and the connector's stream can't be
    restarted once shutdown has closed it -- so a second startup of the
    same app in one process would come up with a dead buffer."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def _cleanup_market_data_db():
    """MarketAggregator's default db path (and app.main's, which uses it)
    is relative to the process cwd. Clean up whatever this test session
    caused to be created there, wherever pytest was invoked from."""
    yield
    for db_path in {REPO_ROOT / "market_data.db", Path.cwd() / "market_data.db"}:
        if db_path.exists():
            db_path.unlink()
