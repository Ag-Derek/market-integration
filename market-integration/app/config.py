"""
Central config. Values come from environment variables (see .env.example)
so nothing provider-specific is hardcoded once a real connector exists.
"""

import os
from pathlib import Path
from typing import get_args

from dotenv import load_dotenv

from app.instruments import INSTRUMENTS, streamable, streamable_symbols
from app.models.instrument import FixedIncomeSegment
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
            f"MARKET_SYMBOLS contains symbols the equity feed can't carry (only active "
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
# connector and the live display branch (processor/gateway). When it
# falls behind and its queue fills up, the oldest buffered tick is
# dropped to make room — see app/queue/market_buffer.py.
QUEUE_MAX_SIZE: int = int(os.getenv("QUEUE_MAX_SIZE", "200"))

# Queue size for the branches that must see every tick (the candle
# aggregator; alerts). When one is full anyway, the buffer pauses the
# feed up to LOSSLESS_BLOCK_SECONDS for it to make room before dropping
# the oldest tick (logged, counted in /metrics, candles flagged).
LOSSLESS_QUEUE_MAX_SIZE: int = int(os.getenv("LOSSLESS_QUEUE_MAX_SIZE", "10000"))
LOSSLESS_BLOCK_SECONDS: float = float(os.getenv("LOSSLESS_BLOCK_SECONDS", "0.5"))

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

# The fixed-income mock (GFIM). How often its stream wakes to send
# trades and due quotes, in seconds.
FI_MOCK_INTERVAL_SECONDS: float = float(os.getenv("FI_MOCK_INTERVAL_SECONDS", "0.5"))


def _quote_intervals_from_env() -> dict[str, float]:
    raw = os.getenv("FI_QUOTE_INTERVALS", "").strip()
    out: dict[str, float] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        key, _, value = (x.strip() for x in item.partition("="))
        try:
            seconds = float(value)
        except ValueError:
            raise ValueError(
                f"FI_QUOTE_INTERVALS: expected segment=seconds or SYMBOL=seconds, got {item!r}"
            ) from None
        segment = key.lower()
        out[segment if segment in get_args(FixedIncomeSegment) else key.upper()] = seconds
    return out


# Seconds between two-way quotes per security, overriding the defaults
# in app/connectors/fixed_income_connector.py, e.g.
# "treasury_bill=2,ddep=5,GHGGOG069931=0.5": a segment (new_gog, ddep,
# old_gog, treasury_bill, corporate) or a symbol (ISIN); 0 = only when it
# trades.
FI_QUOTE_INTERVALS: dict[str, float] = _quote_intervals_from_env()

# > 0 runs the fixed-income mock in burst mode for the whole process:
# every security quoted this many times a second, in session or not. For
# load and throughput tests only.
FI_BURST_RATE: float = float(os.getenv("FI_BURST_RATE", "0"))

# GFIM session hours (GFIM Rules 2022, Rule 12), GMT. Trading days and
# holidays are the equity calendar's.
FI_SESSION_OPEN: str = os.getenv("FI_SESSION_OPEN", "09:00")
FI_SESSION_CLOSE: str = os.getenv("FI_SESSION_CLOSE", "16:00")

# Placeholders for when a real provider is chosen — unused by the mock.
MARKET_PROVIDER_URL: str = os.getenv("MARKET_PROVIDER_URL", "")
MARKET_PROVIDER_API_KEY: str = os.getenv("MARKET_PROVIDER_API_KEY", "")
