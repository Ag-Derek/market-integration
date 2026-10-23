"""
Per-user watchlists (#26): the equities a user pins on the ticker page,
in their order. Stored in SQLite (store.py), behind GET and PUT
/watchlists/me; the user is whoever app.identity.current_user_id() says.

A user who has never saved one gets DEFAULT_WATCHLIST. A list may hold
up to MAX_PINS listed equities (any status: a suspended name can stay
pinned), each once; bills and bonds can't be pinned yet, as the ticker
only shows equities.

GCX commodity contracts have a list of their own, pinned on the
Commodities tab (GET and PUT /gcx/watchlist/me): the same rules for
asset class "commodity", empty until the user pins something.
"""

from typing import Iterable, Mapping, Optional

from app import config
from app.instruments import INSTRUMENTS
from app.models.instrument import Instrument

MAX_PINS = config.WATCHLIST_MAX_PINS


class WatchlistError(ValueError):
    """A watchlist that can't be saved; `symbols` are the offending ones."""

    def __init__(self, message: str, symbols: Optional[list[str]] = None):
        super().__init__(message)
        self.symbols = symbols or []


def validate(
    symbols: Iterable[str],
    max_pins: int = MAX_PINS,
    instruments: Optional[Mapping[str, Instrument]] = None,
    asset_class: str = "equity",
) -> list[str]:
    """The list as stored: symbols upper-cased and trimmed, in the order
    given. Raises WatchlistError for unknown symbols, symbols of another
    asset class than the list's, duplicates or more than `max_pins`."""
    instruments = INSTRUMENTS if instruments is None else instruments
    cleaned = [s.strip().upper() for s in symbols]
    unknown = [s for s in cleaned if s not in instruments]
    if unknown:
        raise WatchlistError("Unknown symbols: " + ", ".join(unknown), unknown)
    wrong = [s for s in cleaned if instruments[s].asset_class != asset_class]
    if wrong:
        what = {"equity": "equities", "commodity": "GCX contracts"}[asset_class]
        raise WatchlistError(f"Only {what} can be pinned here: " + ", ".join(wrong), wrong)
    duplicates = sorted({s for s in cleaned if cleaned.count(s) > 1})
    if duplicates:
        raise WatchlistError("Pinned more than once: " + ", ".join(duplicates), duplicates)
    if len(cleaned) > max_pins:
        raise WatchlistError(f"At most {max_pins} pins; got {len(cleaned)}")
    return cleaned


def _default() -> list[str]:
    try:
        return validate(config.WATCHLIST_DEFAULT)
    except WatchlistError as e:
        raise ValueError(f"WATCHLIST_DEFAULT is invalid: {e}") from None


# What a new user sees; checked at startup so a bad setting fails fast.
DEFAULT_WATCHLIST: list[str] = _default()
