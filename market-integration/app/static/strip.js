/*
 * The market strip: a Bloomberg-style band under the page header. A
 * fixed panel updated in place -- the GSE-CI, USD/GHS, the on-the-run
 * 91-, 182- and 364-day bill yields, the session and the Accra clock --
 * over a crawl that scrolls right to left. Shared by /ticker and the
 * fixed income pages (/fixed-income, /yield-curve, /bond/{symbol}).
 *
 *   MarketStrip.mount(el, { crawl: "equity" });        // GSE equities: gainers, losers, unchanged
 *   MarketStrip.mount(el, { crawl: "fixed_income" });  // bills and bonds: most active, then GoG bonds
 *
 * It opens its own /ws/market connection and needs nothing from the page
 * but render.js loaded first (Render.batch, .text, .flash). Where
 * currency.js is loaded too (/ticker), equity prices follow the display
 * currency.
 *
 * Conventions are the rest of the UI's. An equity's price is its session
 * VWAP and its change is against the previous VWAP close, as the GSE
 * reports them. A bill or bond shows its closing yield and the change on
 * the open in basis points, coloured by price: a rising yield is red.
 * Values flash green or red as they move.
 *
 * The crawl is regrouped only as it wraps around, so names don't jump
 * while they're being read; in between, values update in place. It
 * pauses under the pointer or keyboard focus. With reduced motion it
 * doesn't move at all and scrolls by hand instead.
 */
(function () {
  "use strict";

  const SPEED = 30;                   // crawl, px a second
  const MAX_FI_ITEMS = 40;
  const INDEX_REFRESH_MS = 60e3;      // /indices/gse-ci is rebuilt at most once a minute
  const FX_REFRESH_MS = 15 * 60e3;    // BoG fixes once a day; this just notices the new one
  const RETRY_MS = 60e3;
  const REGROUP_MS = 15e3;            // how often a crawl that isn't moving regroups
  const BILLS = [["91-DAY BILL", "91D"], ["182-DAY BILL", "182D"], ["364-DAY BILL", "364D"]];
  const BADGE_TEXT = { live: "Live", delayed: "Delayed", closed: "Market closed", disconnected: "Disconnected" };
  const ARROW = { up: "▲ ", down: "▼ ", flat: "" };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  const CSS = `
  .ms {
    margin: 0 0 18px; background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    overflow: hidden; font-size: 12.5px; color: var(--ink);
  }
  .ms .num { font-family: ui-monospace, "SFMono-Regular", "Cascadia Mono", Consolas, monospace; font-variant-numeric: tabular-nums; }
  .ms .up { color: var(--green); }
  .ms .down { color: var(--red); }
  .ms .flat { color: var(--ink-faint); }
  /* A static tint toggled by Render.flash, as on the tables. */
  .ms .flash-up { background: rgba(52, 197, 127, 0.22); }
  .ms .flash-down { background: rgba(229, 103, 90, 0.22); }
  .ms .flash-neutral { background: rgba(174, 182, 169, 0.14); }

  .ms-panel { display: flex; align-items: stretch; border-bottom: 1px solid var(--line); }
  /* The quotes scroll sideways where they don't fit; the session and
     clock stay put at the right. */
  .ms-quotes { flex: 1; min-width: 0; display: flex; align-items: stretch; overflow-x: auto; scrollbar-width: none; }
  .ms-quotes::-webkit-scrollbar { display: none; }
  .ms-cell {
    flex: none; display: flex; align-items: baseline; gap: 7px; padding: 7px 14px;
    white-space: nowrap; border-right: 1px solid var(--line); color: var(--ink); text-decoration: none;
  }
  a.ms-cell:hover, a.ms-cell:focus-visible { background: var(--panel-strong); outline: none; }
  .ms-name { font-size: 10.5px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ink-faint); }
  .ms-val { font-weight: 600; }
  .ms-chg { font-size: 11.5px; }
  .ms-session { border-right: 0; border-left: 1px solid var(--line); align-items: center; color: var(--ink-soft); }
  @media (max-width: 620px) { .ms-state { display: none; } }
  .ms-session[data-badge="delayed"] .ms-state { color: var(--status-delayed); }
  .ms-clock { color: var(--ink); }

  .ms-crawl { display: flex; align-items: stretch; }
  .ms-label {
    flex: none; display: flex; align-items: center; padding: 0 12px;
    font-size: 10.5px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; white-space: nowrap;
    color: var(--bg); background: var(--teal);
  }
  .ms-window {
    flex: 1; min-width: 0; overflow: hidden;
    -webkit-mask-image: linear-gradient(90deg, transparent, #000 20px, #000 calc(100% - 20px), transparent);
    mask-image: linear-gradient(90deg, transparent, #000 20px, #000 calc(100% - 20px), transparent);
  }
  .ms-window.manual { overflow-x: auto; scrollbar-width: thin; scrollbar-color: var(--line) transparent; -webkit-mask-image: none; mask-image: none; }
  .ms-track { display: flex; width: max-content; }
  .ms-track.moving { animation: ms-crawl linear infinite; }
  .ms-crawl:hover .ms-track, .ms-crawl:focus-within .ms-track { animation-play-state: paused; }
  @keyframes ms-crawl { from { transform: translateX(0); } to { transform: translateX(-50%); } }
  .ms-seg { flex: none; display: flex; align-items: center; padding-right: 12px; }
  .ms-group {
    padding: 0 4px 0 16px; font-size: 10px; font-weight: 700; letter-spacing: 0.05em;
    text-transform: uppercase; color: var(--gold); white-space: nowrap;
  }
  .ms-item {
    display: inline-flex; align-items: baseline; gap: 6px; padding: 7px 9px; border-radius: 5px;
    white-space: nowrap; color: var(--ink); text-decoration: none;
  }
  .ms-item:hover, .ms-item:focus-visible { background: var(--panel-strong); outline: none; }
  .ms-sym { font-size: 12px; font-weight: 700; }
  .ms-note { padding: 7px 14px; color: var(--ink-faint); white-space: nowrap; }
  `;

  let opts = null;
  const el = {};               // the strip's own elements
  const cells = {};            // panel key -> {el, val, chg, invert, value}
  const quotes = {};           // symbol -> latest equity or fixed-income tick
  const instruments = {};      // symbol -> instrument (bills and bonds)
  const benchmarks = {};       // bill symbol -> its panel key ("91D", ...)
  const items = {};            // symbol -> its crawl elements (two copies while it moves)
  const flashes = {};          // symbol -> direction to flash at the next paint
  let signature = "";          // the crawl's grouping as last built
  let built = false;
  let reduced = false;

  function make(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function noop() {}
  function getJSON(url) {
    return fetch(url).then(function (res) { return res.ok ? res.json() : Promise.reject(new Error("HTTP " + res.status)); });
  }

  // ------------------------------------------------------------ format

  function grouped(v, digits) {
    return Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  // An equity amount in cedis, in the display currency where there is one.
  function price(ghs) { return window.Currency ? Currency.price(ghs) : grouped(ghs, 2); }
  function signed(v, text) { return (v > 0 ? "+" : v < 0 ? "−" : "") + text; }

  // "▲ +0.32 (+5.14%)": a move rounded to `digits`, with its arrow, and
  // its direction for colour. `fmt` formats the magnitude.
  function move(v, pct, fmt, digits) {
    const r = Number(v.toFixed(digits));
    const dir = r > 0 ? "up" : r < 0 ? "down" : "flat";
    let text = ARROW[dir] + signed(r, fmt(Math.abs(r)));
    if (pct != null) text += " (" + signed(r, Math.abs(pct).toFixed(2)) + "%)";
    return { text: text, dir: dir };
  }
  // A yield move in bp: the arrow follows the yield, the colour the price.
  function yieldMove(close, open) {
    if (close == null || open == null) return null;
    const m = move((close - open) * 100, null, function (v) { return v.toFixed(1) + "bp"; }, 1);
    m.dir = m.dir === "up" ? "down" : m.dir === "down" ? "up" : "flat";
    return m;
  }
  // "17Aug27", as compact as a crawl needs.
  function shortDate(iso) {
    const p = iso.split("-");
    return p[2] + MONTHS[Number(p[1]) - 1] + p[0].slice(2);
  }
  function dayBefore(iso) {
    const d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() - 1);
    return d.toISOString().slice(0, 10);
  }

  // What a bill or bond is called on the crawl: "91D 13Jan27",
  // "7Y 29Mar33", "2023-A-1", or the issuer for a corporate.
  function fiLabel(q) {
    const i = instruments[q.symbol] || {};
    const tenor = i.tenor || "";
    const due = shortDate(q.maturity_date);
    let m = /^(\d+)-DAY BILL$/.exec(tenor);
    if (m) return m[1] + "D " + due;
    m = /^(\d+)-YEAR/.exec(tenor);
    if (m) return m[1] + "Y " + due;
    if (q.segment === "corporate") {
      const issuer = i.issuer || q.name;
      return (issuer.length > 18 ? issuer.slice(0, 17) + "…" : issuer) + " " + due;
    }
    return tenor || q.name;
  }

  // A quote's headline value and move, as the crawl shows it.
  function view(q) {
    if (q.tick_type === "equity") {
      return { text: price(q.vwap), move: move(q.change, q.change / q.previous_close * 100, price, 2) };
    }
    if (q.closing_yield != null) {
      return { text: q.closing_yield.toFixed(2) + "%", move: yieldMove(q.closing_yield, q.opening_yield) };
    }
    if (q.closing_price != null) {
      const m = q.opening_price == null ? null :
        move(q.closing_price - q.opening_price, null, function (v) { return v.toFixed(2); }, 2);
      return { text: q.closing_price.toFixed(2), move: m };
    }
    return { text: "—", move: null };
  }

  // Which way a new quote moved its headline value: a flash direction,
  // or null if it didn't.
  function flashDir(prev, q) {
    if (q.tick_type === "equity") {
      const d = Math.round(q.vwap * 100) - Math.round(prev.vwap * 100);
      return d > 0 ? "up" : d < 0 ? "down" : null;
    }
    if (q.closing_yield != null && prev.closing_yield != null) {
      const d = q.closing_yield - prev.closing_yield;
      return Math.abs(d) < 1e-9 ? null : d < 0 ? "up" : "down";
    }
    if (q.closing_price != null && prev.closing_price != null) {
      const d = q.closing_price - prev.closing_price;
      return Math.abs(d) < 1e-9 ? null : d > 0 ? "up" : "down";
    }
    return null;
  }

  // ------------------------------------------------------------ panel

  function addCell(key, name, title) {
    const c = make("span", "ms-cell");
    c.appendChild(make("span", "ms-name", name));
    const val = c.appendChild(make("span", "ms-val num", "—"));
    const chg = c.appendChild(make("span", "ms-chg num flat"));
    if (title) c.title = title;
    el.quotes.appendChild(c);
    cells[key] = { el: c, val: val, chg: chg, value: null };
  }

  // A cell that opens a page: swap its span for a link, keeping its parts.
  function linkCell(key, href) {
    const cell = cells[key];
    if (cell.el.tagName === "A") { cell.el.href = href; return; }
    const a = make("a", "ms-cell");
    a.href = href;
    a.title = cell.el.title;
    while (cell.el.firstChild) a.appendChild(cell.el.firstChild);
    cell.el.replaceWith(a);
    cell.el = a;
  }

  // `value` is what a flash compares; `invert` for yields (up is red).
  function setCell(key, value, text, m, invert) {
    const cell = cells[key];
    if (cell.value != null && value != null && Math.abs(value - cell.value) > 1e-9) {
      const up = value > cell.value;
      Render.flash(cell.el, up !== Boolean(invert) ? "up" : "down");
    }
    cell.value = value;
    Render.text(cell.val, text);
    Render.text(cell.chg, m ? m.text : "");
    Render.cls(cell.chg, "ms-chg num " + (m ? m.dir : "flat"));
  }

  function loadIndex() {
    getJSON("/indices/gse-ci")
      .then(function (body) {
        const s = body.series, last = s[s.length - 1], prev = s[s.length - 2];
        if (!last) return;
        const m = prev ? move(last.level - prev.level, (last.level - prev.level) / prev.level * 100,
          function (v) { return grouped(v, 2); }, 2) : null;
        setCell("gseci", last.level, grouped(last.level, 2), m);
        cells.gseci.el.title = body.name + " (simulated), " + last.date;
      })
      .catch(noop)
      .then(function () { setTimeout(loadIndex, INDEX_REFRESH_MS); });
  }

  function findCode(body, code) {
    return (body.currencies || []).filter(function (c) { return c.code === code; })[0] || null;
  }

  function loadFx() {
    getJSON("/fx/rates")
      .then(function (body) {
        const usd = findCode(body, "USD");
        if (!usd) return Promise.reject(new Error("no USD fix"));
        // The fix before: the one in force the day before this one's.
        return getJSON("/fx/rates?date=" + dayBefore(usd.date))
          .then(function (b) { return findCode(b, "USD"); }, function () { return null; })
          .then(function (prev) {
            const rate = usd.ghs_per_unit;
            const m = prev ? move(rate - prev.ghs_per_unit, (rate - prev.ghs_per_unit) / prev.ghs_per_unit * 100,
              function (v) { return v.toFixed(4); }, 4) : null;
            setCell("usdghs", rate, rate.toFixed(4), m);
            cells.usdghs.el.title = "Cedis per US dollar, Bank of Ghana interbank mid-rate, " + usd.date +
              (prev ? "; change on the fix of " + prev.date : "");
          });
      })
      .then(function () { setTimeout(loadFx, FX_REFRESH_MS); },
            function () { setTimeout(loadFx, RETRY_MS); });
  }

  function paintBenchmark(q) {
    const key = benchmarks[q.symbol];
    if (!key) return;
    setCell(key, q.closing_yield, q.closing_yield == null ? "—" : q.closing_yield.toFixed(2) + "%",
      yieldMove(q.closing_yield, q.opening_yield), true);
  }

  // The on-the-run bill of each tenor: the latest issued, so the one that
  // matures last.
  function pickBenchmarks() {
    BILLS.forEach(function (b) {
      let best = null;
      Object.keys(instruments).forEach(function (s) {
        const i = instruments[s];
        if (i.asset_class === "bill" && i.status === "active" && i.tenor === b[0] &&
            (!best || i.maturity_date > best.maturity_date)) best = i;
      });
      if (!best) return;
      benchmarks[best.symbol] = b[1];
      linkCell(b[1], "/bond/" + encodeURIComponent(best.symbol));
      cells[b[1]].el.title = "On-the-run " + b[0].toLowerCase() + ": " + best.name +
        ". Closing yield, change on the open in basis points.";
      if (quotes[best.symbol]) paintBenchmark(quotes[best.symbol]);
    });
  }

  function setBadge(b) {
    if (!BADGE_TEXT[b]) return;
    el.session.setAttribute("data-badge", b);
    Render.text(el.state, BADGE_TEXT[b]);
    Render.text(el.label, opts.crawl === "equity" ? (b === "closed" ? "GSE · At close" : "GSE equities")
      : (b === "closed" ? "GFIM · At close" : "GFIM bills & bonds"));
  }

  let clockFmt = null;
  try {
    clockFmt = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Africa/Accra", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  } catch (e) { /* no time zone support: the clock stays hidden */ }

  function tick() {
    if (clockFmt) Render.text(el.clock, clockFmt.format(new Date()) + " GMT");
  }

  // ------------------------------------------------------------ crawl

  function cents(v) { return Math.round(v * 100); }
  function bySymbol(a, b) { return a.symbol < b.symbol ? -1 : a.symbol > b.symbol ? 1 : 0; }

  function equityGroups() {
    const qs = Object.keys(quotes).map(function (s) { return quotes[s]; })
      .filter(function (q) { return q.tick_type === "equity"; });
    const pct = function (q) { return q.change / q.previous_close; };
    return [
      ["Gainers", qs.filter(function (q) { return cents(q.change) > 0; })
        .sort(function (a, b) { return pct(b) - pct(a) || bySymbol(a, b); })],
      ["Losers", qs.filter(function (q) { return cents(q.change) < 0; })
        .sort(function (a, b) { return pct(a) - pct(b) || bySymbol(a, b); })],
      ["Unchanged", qs.filter(function (q) { return cents(q.change) === 0; }).sort(bySymbol)],
    ];
  }

  // What traded this session by face value, then the GHS government bonds
  // with a close, by maturity. Bills only once they trade: there are too
  // many to list otherwise.
  function fixedIncomeGroups() {
    const qs = Object.keys(quotes).map(function (s) { return quotes[s]; })
      .filter(function (q) { return q.tick_type === "fixed_income"; });
    const active = qs.filter(function (q) { return q.trade_count > 0; })
      .sort(function (a, b) { return b.volume - a.volume || bySymbol(a, b); })
      .slice(0, MAX_FI_ITEMS);
    const taken = {};
    active.forEach(function (q) { taken[q.symbol] = true; });
    const bonds = qs.filter(function (q) {
      return !taken[q.symbol] && q.segment !== "treasury_bill" && q.segment !== "corporate" &&
        q.currency === "GHS" && q.closing_yield != null;
    }).sort(function (a, b) { return a.maturity_date.localeCompare(b.maturity_date) || bySymbol(a, b); })
      .slice(0, MAX_FI_ITEMS - active.length);
    return [["Most active", active], ["GoG bonds", bonds]];
  }

  function groups() {
    return (opts.crawl === "equity" ? equityGroups() : fixedIncomeGroups())
      .filter(function (g) { return g[1].length; });
  }

  function signatureOf(gs) {
    return gs.map(function (g) { return g[0] + ":" + g[1].map(function (q) { return q.symbol; }).join(","); }).join("|");
  }

  function makeItem(q, hidden) {
    const a = make("a", "ms-item");
    a.href = (q.tick_type === "equity" ? "/stock/" : "/bond/") + encodeURIComponent(q.symbol);
    a.title = q.name;
    if (hidden) a.tabIndex = -1;
    a.appendChild(make("span", "ms-sym", q.tick_type === "equity" ? q.symbol : fiLabel(q)));
    a.appendChild(make("span", "ms-val num"));
    a.appendChild(make("span", "ms-chg num"));
    return a;
  }

  function paintItem(q) {
    const els = items[q.symbol];
    if (!els) return;
    const v = view(q);
    els.forEach(function (a) {
      Render.text(a.children[1], v.text);
      Render.text(a.children[2], v.move ? v.move.text : "");
      Render.cls(a.children[2], "ms-chg num " + (v.move ? v.move.dir : "flat"));
    });
  }

  function build() {
    const gs = groups();
    const scroll = el.window.scrollLeft;
    signature = signatureOf(gs);
    Object.keys(items).forEach(function (k) { delete items[k]; });
    el.track.textContent = "";
    el.track.classList.remove("moving");
    if (!gs.length) {
      el.track.appendChild(make("span", "ms-note", built ? "Nothing to show yet" : "Waiting for quotes…"));
      return;
    }
    built = true;
    addCopy(gs, false);
    const width = el.track.firstChild.offsetWidth;
    // Moving only if it doesn't all fit, and then as two identical
    // copies, so sliding one copy's width left lands where it started.
    if (!reduced && width > el.window.clientWidth) {
      addCopy(gs, true);
      el.track.style.animationDuration = (width / SPEED).toFixed(1) + "s";
      el.track.classList.add("moving");
    }
    gs.forEach(function (g) { g[1].forEach(paintItem); });
    el.window.scrollLeft = scroll;
  }

  function addCopy(gs, hidden) {
    const seg = make("div", "ms-seg");
    if (hidden) seg.setAttribute("aria-hidden", "true");
    gs.forEach(function (g) {
      seg.appendChild(make("span", "ms-group", g[0]));
      g[1].forEach(function (q) {
        const a = makeItem(q, hidden);
        seg.appendChild(a);
        (items[q.symbol] = items[q.symbol] || []).push(a);
      });
    });
    el.track.appendChild(seg);
  }

  // Regroup if the grouping changed: as the crawl wraps around, or every
  // so often while it isn't moving.
  function regroup() {
    if (signatureOf(groups()) !== signature) build();
  }

  // ------------------------------------------------------------ feed

  let schedule = noop;   // Render.batch(flush), once mounted

  function apply(q) {
    if (q.tick_type !== "equity" && q.tick_type !== "fixed_income") return;
    const prev = quotes[q.symbol];
    quotes[q.symbol] = q;
    const dir = prev ? flashDir(prev, q) : null;
    if (dir) flashes[q.symbol] = dir;
    schedule(q.symbol);
  }

  function flush(symbols) {
    symbols.forEach(function (s) {
      const q = quotes[s];
      paintItem(q);
      paintBenchmark(q);
      const dir = flashes[s];
      if (dir) {
        delete flashes[s];
        (items[s] || []).forEach(function (a) { Render.flash(a, dir); });
      }
    });
  }

  function wanted(symbol, assetClass) {
    if (benchmarks[symbol]) return true;
    return opts.crawl === "equity" ? assetClass === "equity" : assetClass === "bill" || assetClass === "bond";
  }

  let retry = 1000;
  function connect(ready) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(proto + "://" + location.host + "/ws/market");
    ws.onopen = function () { retry = 1000; };
    ws.onmessage = function (e) {
      let m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      if (m.type === "welcome") {
        if (m.status) setBadge(m.status.badge);
        ready.then(function () {
          const classes = m.asset_classes || {};
          const symbols = m.symbols.filter(function (s) { return wanted(s, classes[s]); });
          if (symbols.length && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ action: "subscribe", symbols: symbols }));
          }
        });
      } else if (m.type === "snapshot") {
        Object.keys(m.data).forEach(function (s) { apply(m.data[s]); });
        if (!built) build();
      } else if (m.type === "ticks") {
        m.data.forEach(apply);
      } else if (m.type === "status") {
        setBadge(m.badge);
      }
    };
    ws.onclose = function () {
      setBadge("disconnected");
      setTimeout(function () { connect(ready); }, retry);
      retry = Math.min(retry * 2, 30e3);
    };
  }

  // ------------------------------------------------------------ mount

  function mount(container, options) {
    if (opts) return;   // one strip a page: it has one feed connection
    opts = { crawl: (options && options.crawl) === "fixed_income" ? "fixed_income" : "equity" };
    if (!document.getElementById("ms-css")) {
      const style = document.createElement("style");
      style.id = "ms-css";
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    try { reduced = matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { /* assume motion */ }

    const root = make("section", "ms");
    root.setAttribute("aria-label", "Market strip");
    el.panel = root.appendChild(make("div", "ms-panel"));
    el.quotes = el.panel.appendChild(make("div", "ms-quotes"));
    addCell("gseci", "GSE-CI");
    addCell("usdghs", "USD/GHS");
    BILLS.forEach(function (b) { addCell(b[1], b[1] + " bill"); });
    el.session = el.panel.appendChild(make("span", "ms-cell ms-session"));
    el.state = el.session.appendChild(make("span", "ms-state"));
    el.clock = el.session.appendChild(make("span", "ms-clock num"));
    el.session.title = "Session and feed state; the time in Accra";

    const crawl = root.appendChild(make("div", "ms-crawl"));
    el.label = crawl.appendChild(make("span", "ms-label"));
    el.window = crawl.appendChild(make("div", "ms-window" + (reduced ? " manual" : "")));
    el.track = el.window.appendChild(make("div", "ms-track"));
    el.track.addEventListener("animationiteration", regroup);
    container.appendChild(root);

    schedule = Render.batch(flush);
    setBadge("disconnected");
    build();
    tick();
    setInterval(tick, 1000);
    // Whether it needs to move depends on the width it has.
    let resizing = 0;
    window.addEventListener("resize", function () {
      clearTimeout(resizing);
      resizing = setTimeout(function () { if (built) build(); }, 200);
    });
    // A crawl that isn't moving never wraps around, so regroup on a timer.
    setInterval(function () { if (!el.track.classList.contains("moving")) regroup(); }, REGROUP_MS);
    if (window.Currency) Currency.onChange(function () { Object.keys(items).forEach(function (s) { paintItem(quotes[s]); }); });

    const classes = opts.crawl === "equity" ? ["bill"] : ["bill", "bond"];
    const ready = Promise.all(classes.map(function (c) {
      return getJSON("/instruments?asset_class=" + c).catch(function () { return []; });
    })).then(function (lists) {
      lists.forEach(function (list) { list.forEach(function (i) { instruments[i.symbol] = i; }); });
      pickBenchmarks();
    });
    connect(ready);
    loadIndex();
    loadFx();
  }

  window.MarketStrip = { mount: mount };
})();
