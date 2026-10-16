"""
Pulls the Ghana Statistical Service's release calendar for the year.

GSS's Release Calendar page is a Next.js app: the table shows a page of
it at a time, but the whole calendar (107 releases for 2026) is in the
page's React Server Components payload, the `self.__next_f.push([1,
"..."])` scripts. We join those strings, find the JSON array of release
objects in them and read it. Each has GSS's id, year, month, week of the
month, title, short code, frequency, category, reference period, status
("Released"/"Upcoming") and a needsVerification flag.

This is the least stable of our sources: a redesign of the page would
break it, and the error then says so. Blocking (urllib).
"""

import json
import re
import urllib.request

from app.bog import TIMEOUT_SECONDS, USER_AGENT
from app.econ import GssRelease

PAGE_URL = "https://statsghana.gov.gh/news-and-events/release-calendar"
SOURCE = "Ghana Statistical Service release calendar"

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)
_REQUIRED = {"id", "year", "month", "week", "title", "status"}


class GssError(Exception):
    """GSS's site didn't give us the calendar."""


def parse_calendar_page(page: str) -> list[GssRelease]:
    """Every release in the page's payload. Raises GssError if there is
    none."""
    try:
        payload = "".join(json.loads('"' + p + '"') for p in _PUSH.findall(page))
    except ValueError as e:
        raise GssError(f"page payload didn't decode: {e}") from e
    decoder = json.JSONDecoder()
    for m in re.finditer(r'\[\{"id":"', payload):
        try:
            items, _ = decoder.raw_decode(payload, m.start())
        except ValueError:
            continue
        if items and all(isinstance(i, dict) and _REQUIRED <= i.keys() for i in items):
            return [_release(i) for i in items]
    raise GssError(f"no release list in {PAGE_URL}; has the page changed?")


def _release(i: dict) -> GssRelease:
    """One release. Raises GssError for an impossible month or week; a
    missing week is allowed (GSS has one, "no week stated")."""
    try:
        month = int(i["month"])
        week = None if i["week"] is None else int(i["week"])
        year = int(i["year"])
    except (TypeError, ValueError) as e:
        raise GssError(f"release {i.get('id')!r}: {e}") from e
    if not (1 <= month <= 12 and (week is None or 1 <= week <= 5)):
        raise GssError(f"release {i.get('id')!r} has month {month}, week {week}")
    return GssRelease(
        id=str(i["id"]), year=year, month=month, week=week, title=str(i["title"]).strip(),
        short_code=i.get("shortCode") or None, frequency=str(i.get("frequency") or ""),
        category=str(i.get("category") or ""), reference_period=i.get("referencePeriod") or None,
        status=str(i["status"]), needs_verification=bool(i.get("needsVerification")),
        source_note=i.get("sourceNote") or None,
    )


class GssCalendarClient:
    def __init__(self, timeout: float = TIMEOUT_SECONDS):
        self._timeout = timeout

    def fetch(self) -> list[GssRelease]:
        req = urllib.request.Request(PAGE_URL, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as res:
                page = res.read().decode("utf-8", errors="replace")
        except OSError as e:
            raise GssError(f"{PAGE_URL}: {e}") from e
        return parse_calendar_page(page)
