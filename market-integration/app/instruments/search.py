"""
Typeahead search over the instrument master, behind GET /search.

The universe is a few hundred instruments, so the index is a plain
in-memory list of pre-normalized keys, scanned per query: well under a
millisecond, with no database round trip on every keystroke. It is
rebuilt from the same instruments the store is seeded with.

Matching ignores case and punctuation ("fan milk" finds Fan Milk PLC,
"scb pref" finds SCB-PREF). Matches rank in tiers:

  0  exact symbol or ISIN
  1  symbol or ISIN prefix
  2  every query word starts a word of the name
  3  every query word starts a word of the other terms: tenor, issuer,
     maturity date (so "2027" finds bonds maturing in 2027)
  4  substring of any of the above

then active before suspended/delisted, shorter symbols (equities) before
longer ones (ISINs), earlier maturity, and symbol. A one-character query
only matches tiers 0-2; a substring of one letter would match nearly
everything.

Filters (asset class, sector) narrow the matches. An empty query matches
nothing, unless a sector is given: then it lists that sector, so the
search bar's Sector filter can be browsed without typing.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from app.models.instrument import Instrument

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _words(text: str) -> list[str]:
    return _NON_ALNUM.sub(" ", text.lower()).split()


def _compact(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower())


def _starts_words(query_words: list[str], words: list[str]) -> bool:
    return all(any(w.startswith(q) for w in words) for q in query_words)


@dataclass(frozen=True)
class _Entry:
    instrument: Instrument
    codes: tuple[str, ...]       # compact symbol and ISIN
    name_words: list[str]
    term_words: list[str]
    haystack: str                # compact codes, name and terms, for substring matches


class InstrumentSearch:
    def __init__(self, instruments: Iterable[Instrument]):
        self._entries = [self._entry(i) for i in instruments]

    @staticmethod
    def _entry(i: Instrument) -> _Entry:
        codes = tuple(dict.fromkeys(_compact(c) for c in (i.symbol, i.isin) if c))
        terms = " ".join(t for t in (
            i.tenor, i.issuer, i.maturity_date.isoformat() if i.maturity_date else None,
        ) if t)
        return _Entry(
            instrument=i,
            codes=codes,
            name_words=_words(i.name),
            term_words=_words(terms),
            haystack="\x00".join((*codes, _compact(i.name), _compact(terms))),
        )

    def _tier(self, e: _Entry, compact: str, words: list[str]) -> Optional[int]:
        if compact in e.codes:
            return 0
        if any(c.startswith(compact) for c in e.codes):
            return 1
        if _starts_words(words, e.name_words):
            return 2
        if len(compact) < 2:
            return None
        if _starts_words(words, e.term_words):
            return 3
        if compact in e.haystack:
            return 4
        return None

    def sectors(self) -> list[tuple[str, int]]:
        """Every sector with its number of instruments, by name."""
        counts: dict[str, int] = {}
        for e in self._entries:
            counts[e.instrument.sector] = counts.get(e.instrument.sector, 0) + 1
        return sorted(counts.items())

    def search(
        self,
        query: str,
        limit: Optional[int] = 10,
        asset_class: Optional[str] = None,
        sector: Optional[str] = None,
    ) -> list[Instrument]:
        """Matches for `query`, best first; `limit` None for all of them."""
        compact, words = _compact(query), _words(query)
        if not compact and sector is None:
            return []
        sector = sector.casefold() if sector is not None else None
        ranked = []
        for e in self._entries:
            i = e.instrument
            if asset_class is not None and i.asset_class != asset_class:
                continue
            if sector is not None and i.sector.casefold() != sector:
                continue
            tier = self._tier(e, compact, words) if compact else 0
            if tier is not None:
                ranked.append((
                    tier, i.status != "active", len(i.symbol),
                    i.maturity_date or date.min, i.symbol, i,
                ))
        ranked.sort(key=lambda r: r[:-1])
        return [r[-1] for r in ranked[:limit]]
