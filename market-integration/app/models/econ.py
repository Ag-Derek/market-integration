"""
The economic calendar's API shapes (app/econ/, /economic-calendar).
"""

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel

EventCategory = Literal["monetary_policy", "prices", "growth", "trade", "labour", "statistics", "auction", "holiday"]
Importance = Literal["high", "medium", "low"]
# done: happened (a decision announced, data released, an auction held, a
# holiday past); due: today, or inside its window; upcoming; overdue: its
# day or window has passed but the source doesn't show it done yet.
EventStatus = Literal["done", "due", "upcoming", "overdue"]


class EventFigure(BaseModel):
    """A number the event produced (or will): the policy rate, an
    auction yield. `value` is None until it's out."""
    label: str
    value: Optional[float] = None
    prior: Optional[float] = None
    unit: str = "%"
    change_bp: Optional[float] = None


class EconEvent(BaseModel):
    id: str
    date: date                   # the day, or the first day of the window
    date_end: Optional[date] = None   # the window's last day, when only a week or month is known
    precision: Literal["day", "week", "month"] = "day"
    title: str
    short_code: Optional[str] = None  # "MPC", "CPI", "QGDP", "TBILL", ...
    category: EventCategory
    importance: Importance
    status: EventStatus
    estimated: bool = False      # the date is ours, not the source's
    source: str
    source_url: Optional[str] = None
    reference_period: Optional[str] = None
    figures: list[EventFigure] = []
    note: Optional[str] = None
    link: Optional[str] = None   # a page here with more: /auctions, ...


class PolicyRateNow(BaseModel):
    rate: float
    since: date                  # the decision that set it (or last left it unchanged)
    changed_on: date             # the decision that last moved it
    change_bp: float             # that move


class EconCalendar(BaseModel):
    start: date
    end: date
    today: date
    policy_rate: Optional[PolicyRateNow] = None
    events: list[EconEvent]      # by date, most important first within a day
    sources: dict                # each source's last pull, for display


class PolicyRatePoint(BaseModel):
    date: date
    rate: float
    change_bp: Optional[float] = None
