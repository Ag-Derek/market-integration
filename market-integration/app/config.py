"""
Central config. Values come from environment variables (see .env.example)
so nothing provider-specific is hardcoded once a real connector exists.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Symbols to track — comma separated, e.g. "AAPL,MSFT,TSLA"
SYMBOLS: list[str] = [
    s.strip()
    for s in os.getenv("MARKET_SYMBOLS", "AAPL,MSFT,TSLA,NVDA,SNAP,META").split(",")
    if s.strip()
]

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
