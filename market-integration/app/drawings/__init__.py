"""
Chart drawings, saved per user and symbol: trend lines, levels and
Fibonacci retracements on the stock page's chart (app/models/drawing.py
for their shape, store.py for storage, /drawings/me/{symbol} in main).

"Per user" is per app/identity.py's user: the X-User-Id header or the
anonymous cookie, so for now a browser, as with watchlists. The page
sends the whole set for a chart on each change; the last write wins, so
two tabs editing the same chart at once keep whichever saved last.
"""
