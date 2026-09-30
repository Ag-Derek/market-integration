"""
Central config. Values come from environment variables (see .env.example)
so nothing provider-specific is hardcoded once a real connector exists.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from app.instruments import INSTRUMENTS, streamable, streamable_symbols
from app.session import CALENDAR_PATH

load_dotenv()


def _symbols_from_env() -> list[str]:
    raw = os.getenv("MARKET_SYMBOLS", "").strip()
    if not raw:
        return streamable_symbols()
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    unknown = [s for s in symbols if s not in INSTRUMENTS]
    if unknown:
        raise ValueError(
            f"MARKET_SYMBOLS contains symbols missing from the instrument master "
            f"(data/instruments.json): {', '.join(unknown)}"
        )
    not_streamable = [s for s in symbols if not streamable(INSTRUMENTS[s])]
    if not_streamable:
        raise ValueError(
            f"MARKET_SYMBOLS contains symbols the feed can't carry (only active "
            f"equities are streamed): {', '.join(not_streamable)}"
        )
    return symbols


# Symbols the feed tracks, comma separated, e.g. "MTNGH,GCB,SCB". Unset or
# empty tracks every active equity in the instrument master
# (data/instruments.json).
SYMBOLS: list[str] = _symbols_from_env()

# SQLite database holding the instruments table and aggregated candles.
# Relative paths resolve against the directory the service is started in.
DB_PATH: Path = Path(os.getenv("MARKET_DB_PATH", "market_data.db"))

# How often the mock connector emits an update, in seconds
MOCK_INTERVAL_SECONDS: float = float(os.getenv("MOCK_INTERVAL_SECONDS", "0.5"))

# Per-subscriber queue size on the MarketDataBuffer sitting between the
# connector and its consumers (processor/gateway, aggregator). When a
# subscriber falls behind and its queue fills up, the oldest buffered
# tick for that subscriber is dropped to make room — see
# app/queue/market_buffer.py.
QUEUE_MAX_SIZE: int = int(os.getenv("QUEUE_MAX_SIZE", "200"))

# Trading calendar: session hours, trading days and public holidays.
MARKET_CALENDAR_PATH: Path = Path(os.getenv("MARKET_CALENDAR_PATH", str(CALENDAR_PATH)))

# Pins the reported session state to open, pre_open or closed regardless
# of the clock. For development outside GSE hours: the mock only trades
# while the market is open. Leave empty in production.
MARKET_SESSION_OVERRIDE: str = os.getenv("MARKET_SESSION_OVERRIDE", "").strip()

# How long the feed may go without a heartbeat (anything at all from the
# provider, trade or not) before the badge says "Delayed".
FEED_STALE_SECONDS: float = float(os.getenv("FEED_STALE_SECONDS", "15"))

# How often WebSocket clients get a "status" message (session + feed).
# Clients treat a few missed ones as a lost connection.
STATUS_INTERVAL_SECONDS: float = float(os.getenv("STATUS_INTERVAL_SECONDS", "5"))

# Plausible range for a fixed-income yield, in % a year; a bill or bond
# tick with any yield outside it is rejected. Wide on purpose: the
# 28-Sep-2026 GFIM sample has a real 58.59% close on an Old GoG bond.
FI_YIELD_MIN: float = float(os.getenv("FI_YIELD_MIN", "0"))
FI_YIELD_MAX: float = float(os.getenv("FI_YIELD_MAX", "100"))

# Placeholders for when a real provider is chosen — unused by the mock.
MARKET_PROVIDER_URL: str = os.getenv("MARKET_PROVIDER_URL", "")
MARKET_PROVIDER_API_KEY: str = os.getenv("MARKET_PROVIDER_API_KEY", "")
