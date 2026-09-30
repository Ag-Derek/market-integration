"""
The exchange's trading calendar: which days it trades, its session hours
and its public holidays, and from those whether the market is open at a
given moment.

Configured by data/market_calendar.json (see load_calendar()). Holidays
are listed by the date they are *observed* -- a holiday falling on a
weekend is moved to the Monday by gazette, and the two Eids are only
announced shortly before -- so the list is kept by hand, per year,
rather than computed.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from enum import Enum
from pathlib import Path
from typing import Optional

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Far enough to cross any run of weekends and holidays; a calendar with
# no trading day inside it is misconfigured.
_SEARCH_DAYS = 60


class SessionState(str, Enum):
    OPEN = "open"
    PRE_OPEN = "pre_open"
    CLOSED = "closed"


@dataclass(frozen=True)
class MarketStatus:
    state: SessionState
    now: datetime
    # The start and end of the next session, or, while open, the end of
    # this one and the start of the next.
    next_open: datetime
    next_close: datetime
    # The end of the most recent session that has finished.
    last_close: Optional[datetime]
    # Why it's closed: "weekend", "holiday", "before_hours",
    # "after_hours" -- or "pre_open", or "override". None while open.
    reason: Optional[str] = None
    holiday: Optional[str] = None


def _parse_time(value: str, field: str, path: Path) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{path}: {field} must be HH:MM, got {value!r}") from None


class MarketCalendar:

    def __init__(
        self,
        *,
        tz: tzinfo,
        timezone_name: str,
        trading_days: set[int],
        pre_open: time,
        open: time,
        close: time,
        holidays: dict[date, str],
        exchange: str = "GSE",
        hours_source: str = "",
        override: Optional[SessionState] = None,
    ):
        """`trading_days` are weekday numbers (Monday is 0). `override`
        pins the reported state regardless of the clock -- for local
        development and tests, where "the market is closed" would mean
        the mock never trades."""
        if not pre_open <= open < close:
            raise ValueError(f"expected pre_open <= open < close, got {pre_open}, {open}, {close}")
        if not trading_days:
            raise ValueError("a calendar needs at least one trading day")
        self.tz = tz
        self.timezone_name = timezone_name
        self.trading_days = frozenset(trading_days)
        self.pre_open = pre_open
        self.open = open
        self.close = close
        self.holidays = dict(holidays)
        self.exchange = exchange
        self.hours_source = hours_source
        self.override = override

    def with_hours(self, *, open: time, close: time, exchange: str, hours_source: str = "") -> "MarketCalendar":
        """The same trading days and holidays with other session hours --
        e.g. GFIM's 09:00-16:00 beside the GSE equity session. No pre-open."""
        return MarketCalendar(
            tz=self.tz, timezone_name=self.timezone_name, trading_days=set(self.trading_days),
            pre_open=open, open=open, close=close, holidays=self.holidays,
            exchange=exchange, hours_source=hours_source, override=self.override,
        )

    # ------------------------------------------------------------ days

    def local_date(self, moment: datetime) -> date:
        return moment.astimezone(self.tz).date()

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() in self.trading_days and day not in self.holidays

    def session_bounds(self, day: date) -> tuple[datetime, datetime, datetime]:
        """(pre-open, open, close) on `day`, in UTC."""
        return tuple(
            datetime.combine(day, t, tzinfo=self.tz).astimezone(timezone.utc)
            for t in (self.pre_open, self.open, self.close)
        )

    def _next_trading_day(self, after: date) -> date:
        for n in range(1, _SEARCH_DAYS + 1):
            day = after + timedelta(days=n)
            if self.is_trading_day(day):
                return day
        raise ValueError(f"no trading day within {_SEARCH_DAYS} days after {after}")

    def _previous_trading_day(self, before: date) -> Optional[date]:
        for n in range(1, _SEARCH_DAYS + 1):
            day = before - timedelta(days=n)
            if self.is_trading_day(day):
                return day
        return None

    # ------------------------------------------------------------ status

    def status(self, now: Optional[datetime] = None) -> MarketStatus:
        now = now or datetime.now(timezone.utc)
        today = self.local_date(now)
        previous = self._previous_trading_day(today)
        last_close = self.session_bounds(previous)[2] if previous else None

        if self.is_trading_day(today):
            pre_open, open_, close = self.session_bounds(today)
            if now >= close:
                state, reason, last_close = SessionState.CLOSED, "after_hours", close
                session_day = self._next_trading_day(today)
            elif now >= open_:
                state, reason = SessionState.OPEN, None
                session_day = None
            elif now >= pre_open:
                state, reason, session_day = SessionState.PRE_OPEN, "pre_open", today
            else:
                state, reason, session_day = SessionState.CLOSED, "before_hours", today
        else:
            state = SessionState.CLOSED
            reason = "holiday" if today in self.holidays else "weekend"
            session_day = self._next_trading_day(today)

        if session_day is None:  # open: this session's close, the next one's open
            next_open = self.session_bounds(self._next_trading_day(today))[1]
            next_close = self.session_bounds(today)[2]
        else:
            _, next_open, next_close = self.session_bounds(session_day)

        if self.override is not None and self.override != state:
            state, reason = self.override, "override"

        return MarketStatus(
            state=state,
            now=now,
            next_open=next_open,
            next_close=next_close,
            last_close=last_close,
            reason=reason,
            holiday=self.holidays.get(today),
        )

    def is_open(self, now: Optional[datetime] = None) -> bool:
        return self.status(now).state is SessionState.OPEN

    def describe(self) -> dict:
        """The configured session, for API responses."""
        return {
            "timezone": self.timezone_name,
            "trading_days": [d for i, d in enumerate(_WEEKDAYS) if i in self.trading_days],
            "pre_open": self.pre_open.strftime("%H:%M"),
            "open": self.open.strftime("%H:%M"),
            "close": self.close.strftime("%H:%M"),
            "hours_source": self.hours_source,
        }


def load_calendar(path: Path, override: Optional[str] = None) -> MarketCalendar:
    """Build a MarketCalendar from a JSON config file. Raises ValueError
    naming the file on anything malformed, so a broken calendar stops
    startup rather than silently reporting the wrong session."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    days = raw.get("trading_days", [])
    unknown = [d for d in days if d not in _WEEKDAYS]
    if unknown:
        raise ValueError(f"{path}: unknown trading_days {unknown}; expected {list(_WEEKDAYS)}")

    holidays: dict[date, str] = {}
    for entry in raw.get("holidays", []):
        try:
            day = date.fromisoformat(entry["date"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{path}: holiday entry {entry!r} needs a YYYY-MM-DD date") from None
        if day in holidays:
            raise ValueError(f"{path}: holiday {day} is listed twice")
        holidays[day] = entry.get("name", "Holiday")

    offset = raw.get("utc_offset_minutes", 0)
    if not isinstance(offset, int):
        raise ValueError(f"{path}: utc_offset_minutes must be an integer")

    state = None
    if override:
        try:
            state = SessionState(override.strip().lower())
        except ValueError:
            raise ValueError(
                f"MARKET_SESSION_OVERRIDE must be one of "
                f"{', '.join(s.value for s in SessionState)}; got {override!r}"
            ) from None

    try:
        return MarketCalendar(
            # A fixed offset rather than a zoneinfo key: Ghana keeps GMT
            # all year, and Windows has no tz database without the
            # tzdata package.
            tz=timezone(timedelta(minutes=offset)),
            timezone_name=raw.get("timezone", "UTC"),
            trading_days={_WEEKDAYS.index(d) for d in days},
            pre_open=_parse_time(raw.get("pre_open", raw.get("open")), "pre_open", path),
            open=_parse_time(raw.get("open"), "open", path),
            close=_parse_time(raw.get("close"), "close", path),
            holidays=holidays,
            exchange=raw.get("exchange", ""),
            hours_source=raw.get("hours_source", ""),
            override=state,
        )
    except ValueError as e:
        if str(e).startswith(str(path)):
            raise
        raise ValueError(f"{path}: {e}") from None
