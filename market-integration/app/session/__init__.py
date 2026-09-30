"""
Market session awareness: the exchange's trading calendar (calendar.py)
and the status the UI's badge is driven by (status.py).
"""

from pathlib import Path

CALENDAR_PATH = Path(__file__).resolve().parents[2] / "data" / "market_calendar.json"
