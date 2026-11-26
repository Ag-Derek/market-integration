# Command bar and function codes

Press **Ctrl K** (Cmd K on a Mac) to open the command bar on any page.
Type a short function code, like those on the Bloomberg Terminal, to
jump straight to a screen (#100).

The bar is installed by `app/static/search.js`, which every page loads.
A new page must load it too
(`<script src="/static/search.js"></script>`), or the browser's own
Ctrl K (the address bar) takes over there. `tests/test_commands.py`
lists the pages it checks.

| You type | What happens |
|---|---|
| `MTNGH DES` | Opens MTN Ghana's Description page |
| `MTNGH GP` | Opens MTN Ghana's price chart |
| `MTNGH` or `MTNGH D` | Lists MTN Ghana's codes that start with what you typed |
| `EQS` | Opens the stock screener |
| anything else | Shows the instrument search results, as in the header search |

Case and extra spaces don't matter. Use ↑/↓ to move, Tab to complete
the highlighted row, Enter to go and Esc to close. If you type a code
exactly, Enter follows it. Otherwise Enter opens the highlighted row.

## The code table

| Code | Kind | Applies to | Screen | Route |
|---|---|---|---|---|
| `DES` | instrument | equity | Company description and figures | `/stock/{symbol}#description` |
| `GP` | instrument | equity | Price chart with indicators | `/stock/{symbol}` |
| `EQS` | screen | | Stock screener | `/screener` |

There are two kinds of code:

- **instrument** codes follow a symbol (`<SYMBOL> <CODE>`). They apply
  only to their asset classes, so `<bond> DES` falls back to search,
  which opens the bond's own page.
- **screen** codes stand alone (`<CODE>`), like `EQS` for the
  screener. `GC` (yield curve), `AUC` (auctions), `ECO` (economic
  calendar), `FXC` (FX) and `GCX` (commodities) will be added here.

Planned instrument codes include `DCF` (once the DCF panel ships) and
`NEWS` (once there is a headline feed). A code goes into the table only
when its screen exists, so the bar never links to a missing page.

## Adding a code

Add one `Command` to `COMMANDS` in `app/commands.py`, then add a row to
the table above. Nothing else needs to change:

- `GET /commands` serves the table.
- `GET /commands/resolve?q=` parses what was typed against it. The bar
  calls this endpoint, so it has no code list of its own.
- `tests/test_commands.py` checks the table's shape, and checks that
  every instrument code opens a page for a listed equity.

`GET /commands/resolve` returns one of:

```json
{"kind": "instrument", "symbol": "MTNGH", "name": "...", "options": [{"code", "title", "url"}], "url": "/stock/MTNGH#description"}
{"kind": "screen", "options": [...], "url": "/screener"}
{"kind": "search", "q": "fan milk"}
```

`url` is `null` until the typed code is exact. If a word is both a
symbol and a screen code, the symbol wins.
