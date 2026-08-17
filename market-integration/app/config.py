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
    for s in os.getenv("MARKET_SYMBOLS", "AAPL,MSFT,TSLA").split(",")
    if s.strip()
]

# How often the mock connector emits an update, in seconds
MOCK_INTERVAL_SECONDS: float = float(os.getenv("MOCK_INTERVAL_SECONDS", "0.5"))

# Bound on the internal queue between connector and processor.
# 0 = unbounded (fine for the mock; revisit before using a real,
# high-throughput provider).
QUEUE_MAX_SIZE: int = int(os.getenv("QUEUE_MAX_SIZE", "0"))

# Placeholders for when a real provider is chosen — unused by the mock.
MARKET_PROVIDER_URL: str = os.getenv("MARKET_PROVIDER_URL", "")
MARKET_PROVIDER_API_KEY: str = os.getenv("MARKET_PROVIDER_API_KEY", "")
