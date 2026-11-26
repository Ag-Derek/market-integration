"""
Function codes for the command bar (#100), like the Bloomberg Terminal's:
short codes that jump straight to a screen.

    MTNGH DES   ->  /stock/MTNGH#description
    MTNGH GP    ->  /stock/MTNGH

COMMANDS is the one table of codes. A new page registers its code here
and nowhere else: GET /commands serves it, and resolve() (behind
GET /commands/resolve, which the Ctrl K bar in app/static/search.js
calls) parses what the user typed against it. See docs/commands.md.

Two kinds of code:

  instrument  "<SYMBOL> <CODE>", for a symbol of one of the code's asset
              classes. The route has a {symbol} placeholder.
  screen      "<CODE>" alone, e.g. EQS for the screener. The other tabs
              register theirs as they gain one.

Anything else falls back to the instrument search.
"""

from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import quote

from app.models.instrument import Instrument


@dataclass(frozen=True)
class Command:
    code: str
    title: str
    kind: str                           # "instrument" or "screen"
    route: str                          # "{symbol}" is filled in for instrument codes
    asset_classes: tuple[str, ...] = ()  # the instruments an instrument code applies to

    def url(self, symbol: Optional[str] = None) -> str:
        if self.kind == "instrument":
            return self.route.replace("{symbol}", quote(symbol or "", safe=""))
        return self.route

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "kind": self.kind,
            "route": self.route,
            "asset_classes": list(self.asset_classes),
        }


# In the order the bar lists them for a symbol.
COMMANDS: dict[str, Command] = {c.code: c for c in (
    Command("DES", "Company description and figures", "instrument",
            "/stock/{symbol}#description", ("equity",)),
    Command("GP", "Price chart with indicators", "instrument",
            "/stock/{symbol}", ("equity",)),
    Command("EQS", "Stock screener", "screen", "/screener"),
    Command("GC", "Government yield curve", "screen", "/yield-curve"),
    Command("AUC", "T-bill and bond auctions", "screen", "/auctions"),
)}


def codes_for(instrument: Instrument, commands: Mapping[str, Command] = COMMANDS) -> list[Command]:
    """The instrument codes that apply to this instrument."""
    return [c for c in commands.values()
            if c.kind == "instrument" and instrument.asset_class in c.asset_classes]


def _option(command: Command, symbol: Optional[str] = None) -> dict:
    return {"code": command.code, "title": command.title, "url": command.url(symbol)}


def resolve(
    text: str,
    instruments: Mapping[str, Instrument],
    commands: Mapping[str, Command] = COMMANDS,
) -> dict:
    """Parse what was typed in the command bar. Case and extra spaces
    are ignored. Returns one of:

      {"kind": "instrument", "symbol", "name", "options", "url"}
          The first word is a symbol with codes. `options` are its codes
          starting with the second word (all of them if there is none);
          `url` is set when the second word is exactly one of them.
      {"kind": "screen", "options", "url"}
          A lone word that starts a screen code; `url` when it is one.
      {"kind": "search", "q"}
          Anything else: the bar shows instrument search results for q.
    """
    q = " ".join(text.split())
    words = q.upper().split(" ") if q else []

    if 1 <= len(words) <= 2:
        instrument = instruments.get(words[0])
        own = codes_for(instrument, commands) if instrument else []
        if own:
            prefix = words[1] if len(words) == 2 else ""
            matches = [c for c in own if c.code.startswith(prefix)]
            if matches:
                exact = commands.get(prefix) if prefix else None
                return {
                    "kind": "instrument",
                    "symbol": instrument.symbol,
                    "name": instrument.name,
                    "options": [_option(c, instrument.symbol) for c in matches],
                    "url": exact.url(instrument.symbol) if exact in matches else None,
                }

    if len(words) == 1:
        screens = [c for c in commands.values() if c.kind == "screen" and c.code.startswith(words[0])]
        if screens:
            exact = commands.get(words[0])
            # The exact code first, so the bar highlights it (GC before GCX).
            screens.sort(key=lambda c: c is not exact)
            return {
                "kind": "screen",
                "options": [_option(c) for c in screens],
                "url": exact.url() if exact in screens else None,
            }

    return {"kind": "search", "q": q}
