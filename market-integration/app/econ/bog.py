"""
Pulls the Bank of Ghana's policy rate history: every MPC decision since
November 2002 and the rate it set.

BoG has no table or download for it. Its Policy Rate Trends page draws a
wpDataTables (Highcharts) chart, and the chart's options are in the page
itself: `xAxis.categories` holds the decision dates ("24 Sep 2026") and
the series named "BOG Policy Rate" the rates, one for one. We read both
out of the HTML.

Quirk (checked 16-Oct-2026): the chart has decisions on both 17 and 18
Sep 2025, at 21.5% and 18%. Kept as published.

Blocking (urllib), so call it from a thread; see tracker.py.
"""

import json
import re
from datetime import datetime

from app.bog import BogError, request
from app.econ import PolicyDecision

PAGE_URL = "https://www.bog.gov.gh/monetary-policy/policy-rate/"
SOURCE = "Bank of Ghana policy rate"

_CATEGORIES = re.compile(r'"categories":(\[[^\]]*\])')
_SERIES = re.compile(r'"name":"BOG Policy Rate"[^{}]*?"data":(\[[^\]]*\])')


def parse_policy_page(page: str) -> list[PolicyDecision]:
    """The decisions in the page's chart, oldest first. Raises BogError if
    the chart isn't there or doesn't add up."""
    cats, series = _CATEGORIES.search(page), _SERIES.search(page)
    if not cats or not series:
        raise BogError(f"no policy rate chart data on {PAGE_URL}; has the page changed?")
    try:
        dates = [datetime.strptime(d.strip(), "%d %b %Y").date() for d in json.loads(cats.group(1))]
        rates = [float(r) for r in json.loads(series.group(1))]
    except (ValueError, TypeError) as e:
        raise BogError(f"policy rate chart data didn't parse: {e}") from e
    if len(dates) != len(rates) or not dates:
        raise BogError(f"policy rate chart has {len(dates)} dates but {len(rates)} rates")
    if any(not 0 < r < 100 for r in rates):
        raise BogError("policy rate chart has a rate outside 0-100%")
    return sorted((PolicyDecision(d, r) for d, r in zip(dates, rates)), key=lambda p: p.date)


class BogPolicyRateClient:
    def fetch(self) -> list[PolicyDecision]:
        return parse_policy_page(request(PAGE_URL).decode("utf-8", errors="replace"))
