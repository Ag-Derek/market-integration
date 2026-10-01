"""
What the market-status badge should say, and why.

Two separate questions feed it:

- Is the exchange in session? (the calendar -- see calendar.py)
- Is the feed alive? Judged by the connector's heartbeat, i.e. when we
  last heard *anything* from the provider, not when a symbol last
  traded. An illiquid name can go hours without a trade on a perfectly
  healthy feed, so last-trade time says nothing about freshness.

The badge never looks at prices, so its colour can't follow price
direction.
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from app.session.calendar import MarketCalendar, SessionState


class FeedState(str, Enum):
    OK = "ok"              # heartbeat within the staleness window
    DELAYED = "delayed"    # connected, but the heartbeat is late
    DOWN = "down"          # not connected, or never heard from
    RECONNECTING = "reconnecting"  # down, and retrying (connectors/supervisor.py)


class Badge(str, Enum):
    LIVE = "live"
    DELAYED = "delayed"
    CLOSED = "closed"
    DISCONNECTED = "disconnected"


def feed_state(
    running: bool,
    last_heartbeat: Optional[datetime],
    now: datetime,
    stale_after: timedelta,
    reconnecting: bool = False,
) -> FeedState:
    if not running or last_heartbeat is None:
        return FeedState.RECONNECTING if reconnecting else FeedState.DOWN
    if now - last_heartbeat > stale_after:
        return FeedState.DELAYED
    return FeedState.OK


def badge(session: SessionState, feed: FeedState) -> Badge:
    # A dead feed wins even out of hours: "Market closed" would hide that
    # nothing will arrive when it opens. Reconnecting is still
    # disconnected; feed.state tells the UI it's being retried.
    if feed in (FeedState.DOWN, FeedState.RECONNECTING):
        return Badge.DISCONNECTED
    if session is not SessionState.OPEN:
        return Badge.CLOSED
    if feed is FeedState.DELAYED:
        return Badge.DELAYED
    return Badge.LIVE


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


def market_status(
    calendar: MarketCalendar,
    *,
    running: bool,
    last_heartbeat: Optional[datetime],
    now: datetime,
    stale_after: timedelta,
    reconnect: Optional[dict] = None,
) -> dict:
    """The body of GET /market/status and of the WebSocket "status"
    message. `reconnect` is the supervisor's describe() while it retries
    the feed (None otherwise); it is passed through as feed.reconnect."""
    session = calendar.status(now)
    feed = feed_state(running, last_heartbeat, now, stale_after, reconnecting=reconnect is not None)
    return {
        "exchange": calendar.exchange,
        "status": session.state.value,
        "reason": session.reason,
        "holiday": session.holiday,
        "now": _iso(now),
        "next_open": _iso(session.next_open),
        "next_close": _iso(session.next_close),
        "last_close": _iso(session.last_close),
        "session": calendar.describe(),
        "feed": {
            "state": feed.value,
            "last_heartbeat": _iso(last_heartbeat),
            "stale_after_seconds": stale_after.total_seconds(),
            "reconnect": reconnect if feed is FeedState.RECONNECTING else None,
        },
        "badge": badge(session.state, feed).value,
    }
