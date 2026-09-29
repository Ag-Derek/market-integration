"""
Central config. Values come from environment variables (see .env.example)
so nothing provider-specific is hardcoded once a real connector exists.
"""

import os

from dotenv import load_dotenv

from app.instruments import INSTRUMENTS, all_symbols

load_dotenv()


def _symbols_from_env() -> list[str]:
    raw = os.getenv("MARKET_SYMBOLS", "").strip()
    if not raw:
        return all_symbols()
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    unknown = [s for s in symbols if s not in INSTRUMENTS]
    if unknown:
        raise ValueError(
            f"MARKET_SYMBOLS contains symbols missing from the instrument master "
            f"(app/instruments/): {', '.join(unknown)}"
        )
    return symbols


# Symbols to track, comma separated, e.g. "MTNGH,GCB,SCB". Unset or empty
# tracks every instrument in the instrument master (app/instruments/).
SYMBOLS: list[str] = _symbols_from_env()

# How often the mock connector emits an update, in seconds
MOCK_INTERVAL_SECONDS: float = float(os.getenv("MOCK_INTERVAL_SECONDS", "0.5"))

# Per-subscriber queue size on the MarketDataBuffer sitting between the
# connector and its consumers (processor/gateway, aggregator). When a
# subscriber falls behind and its queue fills up, the oldest buffered
# tick for that subscriber is dropped to make room — see
# app/queue/market_buffer.py.
QUEUE_MAX_SIZE: int = int(os.getenv("QUEUE_MAX_SIZE", "200"))

# Placeholders for when a real provider is chosen — unused by the mock.
MARKET_PROVIDER_URL: str = os.getenv("MARKET_PROVIDER_URL", "")
MARKET_PROVIDER_API_KEY: str = os.getenv("MARKET_PROVIDER_API_KEY", "")
