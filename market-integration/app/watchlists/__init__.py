"""
Per-user watchlists (#26): the equities a user pins on the ticker page,
in their order. Stored in SQLite (store.py), behind GET and PUT
/watchlists/me; the user is whoever app.identity.current_user_id() says.

A user who has never saved one gets DEFAULT_WATCHLIST. A list may hold
up to MAX_PINS listed equities (any status: a suspended name can stay
pinned), each once; bills and bonds can't be pinned yet, as the ticker
only shows equities.
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
) -> list[str]:
    """The list as stored: symbols upper-cased and trimmed, in the order
    given. Raises WatchlistError for unknown symbols, non-equities,
    duplicates or more than `max_pins`."""
    instruments = INSTRUMENTS if instruments is None else instruments
    cleaned = [s.strip().upper() for s in symbols]
    unknown = [s for s in cleaned if s not in instruments]
    if unknown:
        raise WatchlistError("Unknown symbols: " + ", ".join(unknown), unknown)
    not_equities = [s for s in cleaned if instruments[s].asset_class != "equity"]
    if not_equities:
        raise WatchlistError("Only equities can be pinned: " + ", ".join(not_equities), not_equities)
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
