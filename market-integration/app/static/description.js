/*
 * The stock page's Description view (#64), Bloomberg DES-style: tabs
 * Profile, Issue Info, Ratios and Revenue & EPS over
 * GET /instruments/{symbol}/description.
 *
 *   const des = Description.mount(root, {
 *     symbol: "MTNGH",
 *     header: el,          // filled with the ISIN and classification
 *     onFullChart: fn,     // the price snapshot's "Full chart" link
 *     onLoad: fn,          // called with the /description response
 *   });
 *   des.show("profile");   // when the view opens (the default tab)
 *   des.update(quote);     // on every live tick for the symbol
 *   des.resize();          // after the layout changes width
 *
 * Maintained values (the company seed) show their "as of" date, with
 * the source on hover. Price figures follow the live quote: 1-day
 * change, 52-week range, YTD, market cap, dividend yield and 12-month
 * total return move with every tick; the rest is as loaded. Anything
 * missing reads "Not available", with the reason on hover where the
 * API gives one. On Ratios, P/E, price-to-book and dividend yield follow
 * the live price too; Revenue & EPS charts the last five fiscal years.
 * Styles itself with the page's colour variables. Needs
 * /static/render.js loaded first; call update() at most once a frame
 * (the stock page does, from its frame-batched paint).
 */
(function () {
  "use strict";

  const TABS = [
    { key: "profile", label: "Profile" },
    { key: "issue-info", label: "Issue Info" },
    { key: "ratios", label: "Ratios" },
    { key: "revenue-eps", label: "Revenue & EPS" },
  ];
  const NA = "Not available";
  const KEY_PEOPLE = 5;          // officers listed before "Full list"
  const PREVIEW_SENTENCES = 3;   // description shown before "More"...
  const MAX_UNFOLDED = 4;        // ...unless it's this short anyway
  const RETRY_MS = 3000;
  const CHART_YEARS = 5;
  // Bar colours: one series per chart, so colour only carries sign; a
  // loss also sits below the zero line. Validated (dataviz) against the
  // dark panel surface.
  const BAR_UP = "#1AA486";
  const BAR_DOWN = "#E5675A";
  const BOARDS = { main: "Main Market", gax: "Ghana Alternative Market (GAX)" };
  const KINDS = {
    ordinary: "Ordinary shares",
    preference: "Preference shares",
    depositary: "Depositary shares",
    etf: "Exchange-traded fund",
  };

  const CSS = `
  .des-tabs { display: flex; gap: 2px; flex-wrap: wrap; border-bottom: 1px solid var(--line); margin: 0 0 16px; }
  .des-tab {
    font-size: 13px; font-weight: 600; color: var(--ink-soft); text-decoration: none;
    padding: 9px 14px; border-bottom: 2px solid transparent; margin-bottom: -1px;
  }
  .des-tab:hover { color: var(--ink); }
  .des-tab[aria-selected="true"] { color: var(--ink); border-bottom-color: var(--teal); }
  .des-tab:focus-visible { outline: 2px solid var(--teal); outline-offset: -2px; }
  .des-msg { padding: 28px 4px; font-size: 13px; color: var(--ink-faint); }
  .des-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
  .des-wide { grid-column: span 2; }
  .des-full { grid-column: 1 / -1; }
  @media (max-width: 980px) {
    .des-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .des-full { grid-column: 1 / -1; }
  }
  @media (max-width: 620px) {
    .des-grid { grid-template-columns: minmax(0, 1fr); }
    .des-wide { grid-column: auto; }
  }
  .des-text { font-size: 13.5px; line-height: 1.6; color: var(--ink); margin: 0; }
  .des-text.na { color: var(--ink-faint); }
  .des-link {
    font: inherit; font-size: 12.5px; font-weight: 600; color: var(--teal);
    background: none; border: 0; padding: 0; cursor: pointer; text-decoration: none;
  }
  .des-link:hover { text-decoration: underline; }
  .des-link:focus-visible { outline: 2px solid var(--teal); outline-offset: 2px; border-radius: 2px; }
  .des-row {
    display: flex; justify-content: space-between; align-items: baseline; gap: 12px;
    padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 12.5px;
  }
  .des-row:last-child { border-bottom: 0; }
  .des-label { color: var(--ink-soft); flex: none; }
  .des-value { color: var(--ink); font-weight: 500; text-align: right; min-width: 0; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
  .des-value.na { color: var(--ink-faint); font-weight: 400; }
  .des-sub { display: block; font-size: 11px; font-weight: 400; color: var(--ink-faint); margin-top: 2px; }
  .des-asof { display: block; font-size: 11px; font-weight: 400; color: var(--ink-faint); margin-top: 2px; }
  .des-desc-asof { display: block; font-size: 11px; color: var(--ink-faint); margin-top: 10px; }
  .des-value .up { color: var(--green); }
  .des-value .down { color: var(--red); }
  .des-mini-wrap {
    background: var(--panel-strong); border: 1px solid var(--line); border-radius: 8px;
    padding: 6px; margin-bottom: 6px; position: relative;
  }
  .des-mini { display: block; width: 100%; height: 88px; }
  .des-mini-msg {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font-size: 12px; color: var(--ink-faint); pointer-events: none;
  }
  .des-mini-foot { display: flex; justify-content: space-between; font-size: 11px; color: var(--ink-faint); margin-bottom: 6px; }
  .des-people { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 4px 24px; margin: 0; padding: 0; list-style: none; }
  .des-person { padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 12.5px; }
  .des-person .who { color: var(--ink); font-weight: 600; }
  .des-person .role { display: block; color: var(--ink-soft); margin-top: 1px; }
  .des-officers { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }
  .des-officers th { text-align: left; font-weight: 600; color: var(--ink-faint); padding: 6px 8px 6px 0; border-bottom: 1px solid var(--line); }
  .des-officers td { padding: 7px 8px 7px 0; border-bottom: 1px solid var(--line); color: var(--ink-soft); vertical-align: top; }
  .des-officers td:first-child { color: var(--ink); font-weight: 500; }
  .des-panel-foot { margin-top: 10px; }
  .des-note { font-size: 12px; line-height: 1.55; color: var(--ink-soft); margin: 0 0 8px; }
  .des-note:last-child { margin-bottom: 0; }
  .des-bars-wrap { position: relative; }
  .des-bars { display: block; width: 100%; height: 190px; }
  .des-tip {
    position: absolute; top: 4px; pointer-events: none; display: none; z-index: 2; white-space: nowrap;
    background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px;
    font-size: 11.5px; color: var(--ink-soft);
  }
  .des-tip b { color: var(--ink); font-weight: 600; }
  .des-table-wrap { overflow-x: auto; }
  .des-table { width: 100%; border-collapse: collapse; font-size: 12.5px; font-variant-numeric: tabular-nums; }
  .des-table th, .des-table td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
  .des-table th:first-child, .des-table td:first-child { text-align: left; color: var(--ink-soft); font-weight: 500; }
  .des-table thead th { color: var(--ink-faint); font-weight: 600; font-size: 11px; }
  .des-table td { color: var(--ink); }
  .des-table td.na { color: var(--ink-faint); }
  .des-table td.neg { color: var(--red); }
  .des-related { margin: 0; padding: 0; list-style: none; }
  .des-related li { padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 12.5px; }
  .des-related li:last-child { border-bottom: 0; }
  .des-related .sym { font-family: ui-monospace, monospace; font-weight: 600; margin-right: 8px; }
  .des-related .kind { display: block; font-size: 11px; color: var(--ink-faint); margin-top: 2px; }
  `;

  // ------------------------------------------------------------ format

  function fmtDate(iso) {
    if (!iso) return null;
    return new Date(iso.slice(0, 10) + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }
  function fmtPrice(n) {
    return Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtMoney(n) { return "GH₵" + fmtPrice(n); }
  // Dividends per share can run to three decimals (GH₵0.305).
  function fmtDps(n) {
    return "GH₵" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 3 });
  }
  function currencySign(cur) {
    return cur === "GHS" ? "GH₵" : cur === "USD" ? "US$" : cur + " ";
  }
  // Company-sized amounts: GH₵24.43B, −GH₵593.40M; `short` for axis ticks.
  function fmtAmount(n, cur, short) {
    const abs = Math.abs(n), sign = n < 0 ? "−" : "", c = currencySign(cur || "GHS");
    // Ticks drop trailing zeros (GH₵2.5B, GH₵500M); values keep two places.
    const fix = function (x) { return short ? String(Number(x.toFixed(1))) : x.toFixed(2); };
    if (abs >= 1e12) return sign + c + fix(abs / 1e12) + "T";
    if (abs >= 1e9) return sign + c + fix(abs / 1e9) + "B";
    if (abs >= 1e6) return sign + c + fix(abs / 1e6) + "M";
    if (abs >= 1e3) return sign + c + fix(abs / 1e3) + "K";
    return sign + c + (short ? String(Number(abs.toFixed(2))) : abs.toFixed(2));
  }
  function fmtPerShare(n, cur, short) {
    const abs = Math.abs(n);
    return (n < 0 ? "−" : "") + currencySign(cur || "GHS") +
      abs.toLocaleString(undefined, { minimumFractionDigits: short ? 0 : 2, maximumFractionDigits: short ? 2 : 3 });
  }
  function fmtBig(n) {
    const abs = Math.abs(n);
    if (abs >= 1e12) return "GH₵" + (n / 1e12).toFixed(2) + "T";
    if (abs >= 1e9) return "GH₵" + (n / 1e9).toFixed(2) + "B";
    if (abs >= 1e6) return "GH₵" + (n / 1e6).toFixed(2) + "M";
    return fmtMoney(n);
  }
  function fmtPct(n, signed) {
    const s = Math.abs(n).toFixed(2) + "%";
    if (!signed) return (n < 0 ? "−" : "") + s;
    return (n > 0 ? "+" : n < 0 ? "−" : "") + s;
  }
  function dirOf(n) { return n > 0.00001 ? "up" : n < -0.00001 ? "down" : ""; }
  function host(url) { return url.replace(/^https?:\/\//, "").replace(/\/$/, ""); }

  // ------------------------------------------------------------ dom

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function panel(title, cls) {
    const s = el("section", "card des-panel" + (cls ? " " + cls : ""));
    s.appendChild(el("h2", "card-title", title));
    return s;
  }

  // A label/value row; returns the value cell to fill.
  function row(parent, label) {
    const r = el("div", "des-row");
    r.appendChild(el("span", "des-label", label));
    const v = el("span", "des-value");
    r.appendChild(v);
    parent.appendChild(r);
    return v;
  }

  // Fill a value cell: main text (or Not available), then optional
  // sub-lines. `reason` explains a missing value on hover. Live figures
  // call this on every frame with news, so a cell whose content would
  // come out the same is left alone (#22).
  function setValue(cell, text, opts) {
    opts = opts || {};
    const sourced = opts.sourced;
    const signature = JSON.stringify([text, opts.dir, opts.subs, opts.reason, opts.title,
      sourced ? [sourced.as_of, sourced.source] : null]);
    if (cell._signature === signature) return cell;
    cell._signature = signature;
    cell.textContent = "";
    cell.removeAttribute("title");
    cell.classList.toggle("na", text == null);
    if (text == null) {
      cell.textContent = NA;
      if (opts.reason) cell.title = opts.reason;
      return cell;
    }
    const main = el("span", opts.dir || "", text);
    cell.appendChild(main);
    (opts.subs || []).forEach(function (s) {
      if (s) cell.appendChild(el("span", "des-sub", s));
    });
    if (opts.sourced) appendAsOf(cell, opts.sourced);
    if (opts.title) cell.title = opts.title;
    return cell;
  }

  // "as of" line for a maintained {value, source, as_of}, source on hover.
  function appendAsOf(cell, sourced) {
    if (!sourced || !sourced.as_of) return;
    const a = el("span", "des-asof", "as of " + fmtDate(sourced.as_of));
    if (sourced.source) a.title = "Source: " + sourced.source;
    cell.appendChild(a);
  }

  function has(sourced) { return !!sourced && sourced.value != null && sourced.value !== ""; }

  // Split prose into sentences: a stop followed by a space and a capital
  // ("31.2 million" isn't a sentence end).
  function sentences(text) {
    return text.split(/(?<=[.!?])\s+(?=[A-Z])/);
  }

  // ------------------------------------------------------------ component

  function mount(root, options) {
    options = options || {};
    if (!document.getElementById("des-css")) {
      const style = document.createElement("style");
      style.id = "des-css";
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    const symbol = options.symbol;

    const tabsEl = el("nav", "des-tabs");
    tabsEl.setAttribute("role", "tablist");
    tabsEl.setAttribute("aria-label", "Description");
    const tabLinks = {};
    TABS.forEach(function (t) {
      const a = el("a", "des-tab", t.label);
      a.href = "#description/" + t.key;
      a.setAttribute("role", "tab");
      a.setAttribute("aria-selected", "false");
      tabsEl.appendChild(a);
      tabLinks[t.key] = a;
    });
    const body = el("div", "des-body");
    root.appendChild(tabsEl);
    root.appendChild(body);

    let data = null;         // the /description response
    let quote = null;        // latest live MarketData
    let tab = null;
    let visible = false;
    let live = null;         // value cells the quote updates, once built
    const mini = { closes: [], canvas: null, msg: null, loaded: false };

    // -------------------------------------------------------- loading

    function load() {
      fetch("/instruments/" + encodeURIComponent(symbol) + "/description")
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then(function (body) {
          data = body;
          renderHeader();
          if (options.onLoad) options.onLoad(data);
          if (visible) render();
        })
        .catch(function () {
          if (visible && !data) message("Couldn't load the description. Retrying…");
          setTimeout(load, RETRY_MS);
        });
    }

    function loadMini() {
      if (mini.loaded) return;
      mini.loaded = true;
      fetch("/candles?symbol=" + encodeURIComponent(symbol) + "&range=1Y")
        .then(function (res) { return res.ok ? res.json() : Promise.reject(new Error("HTTP " + res.status)); })
        .then(function (body) {
          mini.closes = body.candles.map(function (c) { return c.close; });
          drawMini();
        })
        .catch(function () {
          mini.loaded = false;
          if (mini.msg) mini.msg.textContent = "Chart unavailable";
        });
    }

    function message(text) {
      body.textContent = "";
      live = null;
      body.appendChild(el("p", "des-msg", text));
    }

    // -------------------------------------------------------- header

    // ISIN and classification, in the page's quote header.
    function renderHeader() {
      const h = options.header;
      if (!h || !data) return;
      const inst = data.instrument, p = data.profile;
      h.textContent = "";
      h.appendChild(document.createTextNode("ISIN " + (inst.isin || NA)));
      h.appendChild(document.createTextNode(" · "));
      const cls = el("span");
      if (has(p.sector)) {
        cls.textContent = p.sector.value + (has(p.industry) ? " › " + p.industry.value : "");
        cls.title = "Classification as of " + fmtDate(p.sector.as_of) + (p.sector.source ? "\nSource: " + p.sector.source : "");
      } else {
        // The instrument master's sector until a classification is recorded.
        cls.textContent = inst.sector || NA;
      }
      h.appendChild(cls);
      if (inst.status && inst.status !== "active") {
        h.appendChild(document.createTextNode(" · " + inst.status.charAt(0).toUpperCase() + inst.status.slice(1)));
      }
    }

    // -------------------------------------------------------- tabs

    function show(key) {
      visible = true;
      tab = TABS.some(function (t) { return t.key === key; }) ? key : "profile";
      Object.keys(tabLinks).forEach(function (k) {
        tabLinks[k].setAttribute("aria-selected", k === tab ? "true" : "false");
      });
      render();
    }

    function hide() { visible = false; }

    function render() {
      if (!data) { message("Loading description…"); return; }
      if (tab === "issue-info") renderIssueInfo();
      else if (tab === "ratios") renderRatios();
      else if (tab === "revenue-eps") renderRevenueEps();
      else renderProfile();
    }

    // -------------------------------------------------------- profile

    function renderProfile() {
      body.textContent = "";
      ratios = null;
      bars = [];
      const grid = el("div", "des-grid");
      body.appendChild(grid);
      live = {};
      grid.appendChild(descriptionPanel());
      grid.appendChild(corporatePanel());
      grid.appendChild(pricePanel());
      grid.appendChild(dividendPanel());
      grid.appendChild(returnsPanel());
      grid.appendChild(managementPanel());
      renderLive();
      loadMini();
      drawMini();
    }

    function descriptionPanel() {
      const s = panel("Description", "des-wide");
      const p = data.profile;
      const full = has(p.description_full) ? p.description_full : has(p.description_short) ? p.description_short : null;
      if (!full) {
        s.appendChild(el("p", "des-text na", NA));
        return s;
      }
      const parts = sentences(full.value);
      const folded = parts.length > MAX_UNFOLDED;
      const text = el("p", "des-text", folded ? parts.slice(0, PREVIEW_SENTENCES).join(" ") : full.value);
      s.appendChild(text);
      if (folded) {
        const more = el("button", "des-link", "More");
        more.type = "button";
        more.setAttribute("aria-expanded", "false");
        more.addEventListener("click", function () {
          const open = more.getAttribute("aria-expanded") !== "true";
          text.textContent = open ? full.value : parts.slice(0, PREVIEW_SENTENCES).join(" ");
          more.textContent = open ? "Less" : "More";
          more.setAttribute("aria-expanded", open ? "true" : "false");
        });
        text.appendChild(document.createTextNode(" "));
        s.appendChild(more);
      }
      const asof = el("span", "des-desc-asof", "as of " + fmtDate(full.as_of));
      if (full.source) asof.title = "Source: " + full.source;
      s.appendChild(asof);
      return s;
    }

    function corporatePanel() {
      const s = panel("Corporate info");
      const p = data.profile;
      const site = row(s, "Website");
      if (has(p.website)) {
        site.classList.remove("na");
        const a = el("a", "des-link", host(p.website.value));
        a.href = p.website.value;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        site.appendChild(a);
        appendAsOf(site, p.website);
      } else {
        setValue(site, null);
      }
      const hqParts = [p.headquarters_city, p.headquarters_country].filter(has).map(function (x) { return x.value; });
      setValue(row(s, "Headquarters"), hqParts.length ? hqParts.join(", ") : null,
        { sourced: has(p.headquarters_city) ? p.headquarters_city : p.headquarters_country });
      setValue(row(s, "Employees"), has(p.employees) ? Number(p.employees.value).toLocaleString() : null,
        { sourced: p.employees });
      return s;
    }

    function pricePanel() {
      const s = panel("Price snapshot");
      const wrap = el("div", "des-mini-wrap");
      mini.canvas = el("canvas", "des-mini");
      mini.canvas.setAttribute("aria-label", "12-month price chart");
      mini.canvas.setAttribute("role", "img");
      mini.msg = el("div", "des-mini-msg", mini.closes.length ? "" : "Loading chart…");
      wrap.appendChild(mini.canvas);
      wrap.appendChild(mini.msg);
      s.appendChild(wrap);
      const foot = el("div", "des-mini-foot");
      foot.appendChild(el("span", null, "12 months"));
      const full = el("a", "des-link", "Full chart →");
      full.href = "#overview";
      full.addEventListener("click", function (e) {
        if (!options.onFullChart) return;
        e.preventDefault();
        options.onFullChart();
      });
      foot.appendChild(full);
      s.appendChild(foot);

      live.oneDay = row(s, "1 day");
      live.high = row(s, "52-week high");
      live.low = row(s, "52-week low");
      live.ytd = row(s, "YTD");
      live.cap = row(s, "Market cap");
      const p = data.profile;
      setValue(row(s, "Shares outstanding"),
        has(p.shares_outstanding) ? Number(p.shares_outstanding.value).toLocaleString() : null,
        { sourced: p.shares_outstanding });
      return s;
    }

    function dividendPanel() {
      const s = panel("Dividend");
      const c = data.calculated;
      live.yield = row(s, "Indicated gross yield");

      const last = c.last_dividend;
      const lastCell = row(s, "Last cash dividend");
      if (last) {
        const when = last.paid_on ? "paid " + fmtDate(last.paid_on)
          : last.payable_on ? "payable " + fmtDate(last.payable_on) : "payment date not available";
        setValue(lastCell, fmtDps(last.dividend_per_share) + " / share", {
          subs: ["FY" + last.fiscal_year + " total · " + when],
          sourced: financialField(last.fiscal_year, "dividend_per_share"),
        });
      } else {
        setValue(lastCell, null, { reason: "no cash dividend recorded" });
      }

      const g = c.dividend_growth;
      setValue(row(s, "5-year dividend growth"), g.value == null ? null : fmtPct(g.value, true) + " / yr", {
        reason: g.reason,
        dir: g.value == null ? "" : dirOf(g.value),
        subs: g.value == null ? [] : ["FY" + g.from_year + "–FY" + g.to_year + ", compound (" + g.years + " yrs)"],
      });
      return s;
    }

    function returnsPanel() {
      const s = panel("Returns");
      live.total = row(s, "12-month total return");
      const b = data.calculated.beta;
      setValue(row(s, "Beta vs GSE-CI"), b ? b.value.toFixed(2) : null, {
        reason: "fewer than 30 days of returns in the last year",
        subs: b ? [b.index + " · " + b.observations + " days"] : [],
      });
      return s;
    }

    function managementPanel() {
      const s = panel("Management", "des-full");
      const officers = data.officers.slice().sort(function (a, b) { return a.display_order - b.display_order; });
      if (!officers.length) {
        s.appendChild(el("p", "des-text na", NA));
        return s;
      }
      const list = el("ul", "des-people");
      officers.slice(0, KEY_PEOPLE).forEach(function (o) {
        const li = el("li", "des-person");
        li.appendChild(el("span", "who", o.name));
        li.appendChild(el("span", "role", o.role));
        const asof = el("span", "des-asof", "as of " + fmtDate(o.as_of));
        asof.title = "Source: " + o.source;
        li.appendChild(asof);
        list.appendChild(li);
      });
      s.appendChild(list);

      // The full list, with each officer's source.
      const foot = el("div", "des-panel-foot");
      const toggle = el("button", "des-link", "Full list (" + officers.length + ")");
      toggle.type = "button";
      toggle.setAttribute("aria-expanded", "false");
      const table = el("table", "des-officers");
      table.hidden = true;
      const head = el("tr");
      ["Name", "Role", "As of", "Source"].forEach(function (h) { head.appendChild(el("th", null, h)); });
      table.appendChild(head);
      officers.forEach(function (o) {
        const tr = el("tr");
        [o.name, o.role, fmtDate(o.as_of), o.source].forEach(function (v) { tr.appendChild(el("td", null, v)); });
        table.appendChild(tr);
      });
      toggle.addEventListener("click", function () {
        table.hidden = !table.hidden;
        toggle.setAttribute("aria-expanded", table.hidden ? "false" : "true");
        toggle.textContent = table.hidden ? "Full list (" + officers.length + ")" : "Hide full list";
      });
      foot.appendChild(toggle);
      s.appendChild(foot);
      s.appendChild(table);
      return s;
    }

    function financialField(year, field) {
      const f = data.financials.filter(function (x) { return x.fiscal_year === year; })[0];
      return f ? f[field] : null;
    }

    // -------------------------------------------------------- issue info

    function renderIssueInfo() {
      body.textContent = "";
      live = null;
      ratios = null;
      bars = [];
      const grid = el("div", "des-grid");
      body.appendChild(grid);
      const inst = data.instrument, p = data.profile;

      const listing = panel("Listing");
      setValue(row(listing, "Listing date"), has(p.listing_date) ? fmtDate(p.listing_date.value) : null,
        { sourced: p.listing_date });
      setValue(row(listing, "Board"), BOARDS[inst.board] || null,
        { title: inst.board ? "Source: GSE listed-companies page" : null });
      setValue(row(listing, "Security type"), KINDS[inst.kind] || null);
      setValue(row(listing, "ISIN"), inst.isin || null);
      grid.appendChild(listing);

      const shares = panel("Shares");
      setValue(row(shares, "Shares outstanding"),
        has(p.shares_outstanding) ? Number(p.shares_outstanding.value).toLocaleString() : null,
        { sourced: p.shares_outstanding });
      setValue(row(shares, "Registrar"), has(p.registrar) ? p.registrar.value : null, { sourced: p.registrar });
      grid.appendChild(shares);

      // Other listed lines of the issuer; each links back here (the
      // instrument master requires the relation both ways).
      const related = panel("Related securities");
      if (data.related.length) {
        const list = el("ul", "des-related");
        data.related.forEach(function (r) {
          const li = el("li");
          const a = el("a", "des-link");
          a.href = "/stock/" + encodeURIComponent(r.symbol) + "#description/issue-info";
          a.appendChild(el("span", "sym", r.symbol));
          a.appendChild(document.createTextNode(r.name));
          li.appendChild(a);
          li.appendChild(el("span", "kind", (KINDS[r.kind] || "Security type not available") +
            (r.status !== "active" ? " · " + r.status : "")));
          list.appendChild(li);
        });
        related.appendChild(list);
      } else {
        related.appendChild(el("p", "des-text na", "None listed"));
      }
      grid.appendChild(related);
    }

    // -------------------------------------------------------- ratios

    let ratios = null;       // value cells the quote updates on the Ratios tab

    function renderRatios() {
      body.textContent = "";
      live = null;
      bars = [];
      const grid = el("div", "des-grid");
      body.appendChild(grid);
      const c = data.calculated;

      const s = panel("Ratios", "des-wide");
      ratios = {
        pe: row(s, "P/E"),
        pb: row(s, "Price-to-book"),
      };
      const epsYear = c.fiscal_years.eps;
      const eps = epsYear != null ? financialField(epsYear, "eps") : null;
      setValue(row(s, "EPS (trailing 12 months)"), eps ? fmtPerShare(eps.value, currencyOf(epsYear)) : null, {
        reason: "no eps recorded",
        dir: eps ? dirOf(eps.value) : "",
        subs: eps ? [basis("FY" + epsYear + " annual EPS", eps.as_of)] : [],
        title: eps && eps.source ? "Source: " + eps.source : null,
      });
      ratios.dy = row(s, "Dividend yield");
      const roe = c.return_on_equity;
      setValue(row(s, "Return on equity"), roe.value == null ? null : fmtPct(roe.value), {
        reason: roe.reason,
        dir: roe.value == null ? "" : dirOf(roe.value),
        subs: roe.value == null ? [] : [basis("FY" + c.fiscal_years.return_on_equity + " net income ÷ book value",
          roe.inputs_as_of.net_income)],
      });
      grid.appendChild(s);

      const how = panel("How these are calculated");
      [
        "Price is the session VWAP, the GSE's official closing price, and follows the live feed.",
        "Each ratio uses the latest fiscal year that reports its input. Only annual results are " +
          "recorded, so trailing EPS is the latest full year's EPS.",
        "Book value is total shareholders' equity. P/E reads n/a after a loss-making year.",
      ].forEach(function (t) { how.appendChild(el("p", "des-note", t)); });
      grid.appendChild(how);
      renderRatiosLive();
    }

    function basis(what, asOf) {
      return what + (asOf ? " · financials as of " + fmtDate(asOf) : "");
    }

    function renderRatiosLive() {
      if (!ratios || !data) return;
      const c = data.calculated;
      const price = quote ? quote.vwap : c.price.value;

      const epsYear = c.fiscal_years.eps;
      const eps = epsYear != null ? financialField(epsYear, "eps") : null;
      if (eps && eps.value <= 0) {
        setValue(ratios.pe, "n/a", { subs: [basis("Negative FY" + epsYear + " EPS", eps.as_of)] });
      } else if (eps && price) {
        setValue(ratios.pe, (price / eps.value).toFixed(2), { subs: [basis("Price ÷ FY" + epsYear + " EPS", eps.as_of)] });
      } else {
        setValue(ratios.pe, null, { reason: c.pe_ratio.reason });
      }

      const bookYear = c.fiscal_years.book_value;
      const book = bookYear != null ? financialField(bookYear, "book_value") : null;
      const cap = quote && quote.market_cap != null ? quote.market_cap : c.market_cap.value;
      if (cap != null && book && book.value > 0) {
        setValue(ratios.pb, (cap / book.value).toFixed(2), {
          subs: [basis("Market cap ÷ FY" + bookYear + " book value", book.as_of)],
        });
      } else {
        setValue(ratios.pb, null, { reason: c.price_to_book.reason });
      }

      const dpsYear = c.fiscal_years.dividend_per_share;
      const dps = dpsYear != null ? financialField(dpsYear, "dividend_per_share") : null;
      if (dps && price) {
        setValue(ratios.dy, fmtPct(dps.value / price * 100), {
          subs: [basis("FY" + dpsYear + " dividend " + fmtDps(dps.value) + " ÷ price", dps.as_of)],
        });
      } else {
        setValue(ratios.dy, null, { reason: c.dividend_yield.reason });
      }
    }

    // -------------------------------------------------------- revenue & eps

    const MEASURES = [
      { key: "revenue", label: "Revenue", fmt: fmtAmount },
      { key: "net_income", label: "Net income", fmt: fmtAmount },
      { key: "eps", label: "EPS", fmt: fmtPerShare },
    ];
    let bars = [];           // drawn bar charts, redrawn on resize

    function currencyOf(year) {
      const f = data.financials.filter(function (x) { return x.fiscal_year === year; })[0];
      return f ? f.currency : "GHS";
    }

    function renderRevenueEps() {
      body.textContent = "";
      live = null;
      ratios = null;
      bars = [];
      const grid = el("div", "des-grid");
      body.appendChild(grid);

      // The last five fiscal years, oldest first.
      const years = data.financials.slice()
        .sort(function (a, b) { return b.fiscal_year - a.fiscal_year; })
        .slice(0, CHART_YEARS).reverse();
      if (!years.length) {
        const s = panel("Revenue & EPS", "des-full");
        s.appendChild(el("p", "des-text na", NA + ": no financials recorded."));
        grid.appendChild(s);
        return;
      }

      // One currency per chart: years reported in another are table-only.
      const currency = years[years.length - 1].currency;
      const charted = years.filter(function (y) { return y.currency === currency; });

      MEASURES.forEach(function (m) {
        const s = panel(m.label + " (" + currency + (m.key === "eps" ? " per share" : "") + ")");
        const wrap = el("div", "des-bars-wrap");
        const canvas = el("canvas", "des-bars");
        canvas.setAttribute("role", "img");
        const tip = el("div", "des-tip");
        wrap.appendChild(canvas);
        wrap.appendChild(tip);
        s.appendChild(wrap);
        const points = charted.map(function (y) {
          const v = y[m.key];
          return { year: y.fiscal_year, value: v && v.value != null ? v.value : null };
        });
        canvas.setAttribute("aria-label", m.label + " by fiscal year: " + points.map(function (p) {
          return "FY" + p.year + " " + (p.value == null ? "not available" : m.fmt(p.value, currency));
        }).join(", "));
        const chart = { canvas: canvas, tip: tip, points: points, fmt: m.fmt, currency: currency, hover: null };
        canvas.addEventListener("pointermove", function (e) { hoverBar(chart, e); });
        canvas.addEventListener("pointerdown", function (e) { hoverBar(chart, e); });
        canvas.addEventListener("pointerleave", function () {
          chart.hover = null;
          tip.style.display = "none";
          drawBars(chart);
        });
        bars.push(chart);
        grid.appendChild(s);
      });

      // The same figures as a table, with each value's source on hover.
      const t = panel("By fiscal year", "des-full");
      const wrap = el("div", "des-table-wrap");
      const table = el("table", "des-table");
      const thead = el("thead");
      const head = el("tr");
      head.appendChild(el("th", null, ""));
      years.forEach(function (y) {
        head.appendChild(el("th", null, "FY" + y.fiscal_year + (y.currency !== currency ? " (" + y.currency + ")" : "")));
      });
      thead.appendChild(head);
      table.appendChild(thead);
      const tbody = el("tbody");
      MEASURES.forEach(function (m) {
        const tr = el("tr");
        tr.appendChild(el("td", null, m.label));
        years.forEach(function (y) {
          const v = y[m.key];
          const td = el("td");
          if (v && v.value != null) {
            td.textContent = m.fmt(v.value, y.currency);
            if (v.value < 0) td.className = "neg";
            td.title = "As of " + fmtDate(v.as_of) + (v.source ? "\nSource: " + v.source : "");
          } else {
            td.textContent = NA;
            td.className = "na";
          }
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      t.appendChild(wrap);
      const notes = [];
      if (years.length < CHART_YEARS) {
        notes.push(years.length + " fiscal year" + (years.length === 1 ? "" : "s") + " recorded; up to " +
          CHART_YEARS + " are shown.");
      }
      if (charted.length < years.length) notes.push("Years reported in another currency are in the table only.");
      notes.push("Hover a figure for its source and date.");
      const foot = el("div", "des-panel-foot");
      notes.forEach(function (n) { foot.appendChild(el("p", "des-note", n)); });
      t.appendChild(foot);
      grid.appendChild(t);
      requestAnimationFrame(function () { bars.forEach(drawBars); });
    }

    function niceStep(span, target) {
      const raw = span / target;
      const mag = Math.pow(10, Math.floor(Math.log10(raw)));
      const norm = raw / mag;
      return (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    }

    // Columns from a zero baseline: at most 24px wide, 4px rounded at the
    // data end and square at the baseline; only the latest year labelled.
    function drawBars(chart) {
      const canvas = chart.canvas;
      if (!canvas.isConnected) return;
      const dpr = window.devicePixelRatio || 1;
      const W = canvas.clientWidth, H = canvas.clientHeight || 190;
      if (!W) return;
      canvas.width = W * dpr;
      canvas.height = H * dpr;
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      const style = getComputedStyle(document.documentElement);
      const ink = function (name, fallback) { return (style.getPropertyValue(name) || fallback).trim(); };
      const axisInk = ink("--ink-faint", "#6F766D"), softInk = ink("--ink-soft", "#AEB6A9");
      const gridInk = ink("--line", "#26362F");

      const vals = chart.points.map(function (p) { return p.value; }).filter(function (v) { return v != null; });
      let lo = Math.min.apply(null, [0].concat(vals)), hi = Math.max.apply(null, [0].concat(vals));
      if (lo === hi) hi = 1;
      const step = niceStep(hi - lo, 4);
      lo = Math.floor(lo / step) * step;
      hi = Math.ceil(hi / step) * step;
      // Room under the deepest loss for its label, clear of the year labels.
      if (lo < 0 && Math.min.apply(null, vals) - lo < (hi - lo) * 0.15) lo -= step;

      const padL = 56, padR = 6, padT = 18, padB = 22;
      const plotW = W - padL - padR, plotH = H - padT - padB;
      const y = function (v) { return padT + (hi - v) / (hi - lo) * plotH; };
      const slot = plotW / chart.points.length;
      const barW = Math.max(4, Math.min(24, slot - 2));
      chart.geometry = { padL: padL, slot: slot, W: W };

      // Recessive grid and tick labels; the zero line a step stronger.
      ctx.font = "10px ui-monospace, monospace";
      ctx.textBaseline = "middle";
      ctx.textAlign = "right";
      for (let v = lo; v <= hi + step / 2; v += step) {
        const tick = Math.abs(v) < step / 1e6 ? 0 : v;
        const yy = Math.round(y(tick)) + 0.5;
        ctx.strokeStyle = gridInk;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(padL, yy);
        ctx.lineTo(W - padR, yy);
        ctx.stroke();
        ctx.fillStyle = axisInk;
        ctx.fillText(chart.fmt(tick, chart.currency, true), padL - 6, yy);
      }
      const zero = Math.round(y(0)) + 0.5;
      ctx.strokeStyle = axisInk;
      ctx.beginPath();
      ctx.moveTo(padL, zero);
      ctx.lineTo(W - padR, zero);
      ctx.stroke();

      ctx.textAlign = "center";
      ctx.textBaseline = "alphabetic";
      chart.points.forEach(function (p, i) {
        const cx = padL + slot * (i + 0.5);
        ctx.fillStyle = chart.hover === i ? softInk : axisInk;
        ctx.fillText("FY" + p.year, cx, H - 6);
        if (p.value == null) {
          ctx.fillText("n/a", cx, zero - 6);
          return;
        }
        const top = y(Math.max(p.value, 0)), bottom = y(Math.min(p.value, 0));
        const h = Math.max(1, bottom - top);
        const r = Math.min(4, h / 2, barW / 2);
        const x0 = cx - barW / 2;
        ctx.globalAlpha = chart.hover == null || chart.hover === i ? 1 : 0.55;
        ctx.fillStyle = p.value < 0 ? BAR_DOWN : BAR_UP;
        ctx.beginPath();
        if (p.value >= 0) {           // rounded top, square at the baseline
          ctx.moveTo(x0, bottom);
          ctx.lineTo(x0, top + r);
          ctx.arcTo(x0, top, x0 + r, top, r);
          ctx.lineTo(x0 + barW - r, top);
          ctx.arcTo(x0 + barW, top, x0 + barW, top + r, r);
          ctx.lineTo(x0 + barW, bottom);
        } else {                      // rounded bottom, square at the baseline
          ctx.moveTo(x0, top);
          ctx.lineTo(x0, bottom - r);
          ctx.arcTo(x0, bottom, x0 + r, bottom, r);
          ctx.lineTo(x0 + barW - r, bottom);
          ctx.arcTo(x0 + barW, bottom, x0 + barW, bottom - r, r);
          ctx.lineTo(x0 + barW, top);
        }
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha = 1;
      });

      // Direct label on the latest year's cap only; the tooltip and the
      // table carry the rest. Kept inside the canvas at the right edge.
      const lastI = chart.points.length - 1, last = chart.points[lastI];
      if (last && last.value != null) {
        const cx = padL + slot * (lastI + 0.5);
        const label = chart.fmt(last.value, chart.currency);
        ctx.font = "600 10.5px -apple-system, 'Segoe UI', Roboto, sans-serif";
        ctx.fillStyle = softInk;
        const half = ctx.measureText(label).width / 2;
        const lx = Math.min(cx, W - padR - half);
        ctx.textAlign = "center";
        if (last.value >= 0) {
          ctx.textBaseline = "bottom";
          ctx.fillText(label, lx, y(last.value) - 3);
        } else {
          ctx.textBaseline = "top";
          ctx.fillText(label, lx, y(last.value) + 3);
        }
      }
    }

    function hoverBar(chart, e) {
      const g = chart.geometry;
      if (!g) return;
      const x = e.clientX - chart.canvas.getBoundingClientRect().left;
      const i = Math.floor((x - g.padL) / g.slot);
      if (i < 0 || i >= chart.points.length) {
        if (chart.hover != null) { chart.hover = null; drawBars(chart); }
        chart.tip.style.display = "none";
        return;
      }
      if (chart.hover !== i) { chart.hover = i; drawBars(chart); }
      const p = chart.points[i];
      chart.tip.textContent = "";
      chart.tip.appendChild(el("b", null, "FY" + p.year));
      chart.tip.appendChild(document.createTextNode(" · " + (p.value == null ? NA : chart.fmt(p.value, chart.currency))));
      chart.tip.style.display = "block";
      const cx = g.padL + g.slot * (i + 0.5);
      const w = chart.tip.offsetWidth;
      chart.tip.style.left = Math.max(0, Math.min(g.W - w, cx - w / 2)) + "px";
    }

    // -------------------------------------------------------- live figures

    // The latest trade price on the daily-close basis performance uses.
    function lastTrade() { return quote && quote.volume > 0 ? quote.price : null; }

    function renderLive() {
      if (!live || !data) return;
      const c = data.calculated, perf = c.performance;

      // 1 day: the GSE headline, VWAP against the previous VWAP close.
      if (quote) {
        const pct = quote.previous_close ? quote.change / quote.previous_close * 100 : 0;
        setValue(live.oneDay, fmtMoney(quote.vwap), {
          subs: [(quote.change >= 0 ? "+" : "−") + fmtPrice(Math.abs(quote.change)) + " (" + fmtPct(pct, true) + ")"],
        });
        live.oneDay.lastChild.className = "des-sub " + dirOf(quote.change);
      } else if (perf.one_day) {
        setValue(live.oneDay, fmtMoney(perf.one_day.to_price), {
          subs: [fmtPct(perf.one_day.percent, true) + " · last close " + fmtDate(perf.one_day.to)],
        });
      } else {
        setValue(live.oneDay, null, { reason: "no quote or daily history" });
      }

      // 52-week range, extended by today's trades.
      let wk = perf.week52 ? Object.assign({}, perf.week52) : null;
      if (quote && quote.volume > 0) {
        const today = quote.timestamp.slice(0, 10);
        if (!wk) wk = { high: quote.day_high, high_date: today, low: quote.day_low, low_date: today };
        if (quote.day_high > wk.high) { wk.high = quote.day_high; wk.high_date = today; }
        if (quote.day_low < wk.low) { wk.low = quote.day_low; wk.low_date = today; }
      }
      setValue(live.high, wk ? fmtMoney(wk.high) : null, { subs: wk ? [fmtDate(wk.high_date)] : [], reason: "no trades in the last 52 weeks" });
      setValue(live.low, wk ? fmtMoney(wk.low) : null, { subs: wk ? [fmtDate(wk.low_date)] : [], reason: "no trades in the last 52 weeks" });

      // YTD, against last year's final close.
      const ytd = perf.ytd;
      if (ytd) {
        const px = lastTrade() != null ? lastTrade() : ytd.to_price;
        const diff = px - ytd.from_price;
        setValue(live.ytd, fmtPct(diff / ytd.from_price * 100, true), {
          dir: dirOf(diff),
          subs: [(diff >= 0 ? "+" : "−") + fmtMoney(Math.abs(diff)) + " since " + fmtDate(ytd.from)],
        });
      } else {
        setValue(live.ytd, null, { reason: "no close from the end of last year" });
      }

      const cap = quote && quote.market_cap != null ? quote.market_cap : c.market_cap.value;
      setValue(live.cap, cap != null ? fmtBig(cap) : null, { reason: c.market_cap.reason, title: cap != null ? "GH₵" + Math.round(cap).toLocaleString() : null });

      // Indicated gross yield: the latest year's dividend over the price.
      const dpsYear = c.fiscal_years.dividend_per_share;
      const dps = dpsYear != null ? financialField(dpsYear, "dividend_per_share") : null;
      const price = quote ? quote.vwap : c.price.value;
      if (dps && dps.value != null && price) {
        setValue(live.yield, fmtPct(dps.value / price * 100), {
          subs: ["FY" + dpsYear + " dividend " + fmtDps(dps.value) + " ÷ price"],
        });
      } else {
        setValue(live.yield, null, { reason: c.dividend_yield.reason });
      }

      // 12-month total return: price change plus dividends paid.
      const tr = perf.total_return_12m;
      if (tr) {
        const px = lastTrade() != null ? lastTrade() : tr.to_price;
        const priceRet = (px - tr.from_price) / tr.from_price * 100;
        const total = (px - tr.from_price + tr.dividends) / tr.from_price * 100;
        const subs = ["Price " + fmtPct(priceRet, true) + (tr.dividends ? " + dividends " + fmtDps(tr.dividends) : "") +
          " since " + fmtDate(tr.from)];
        setValue(live.total, fmtPct(total, true), {
          dir: dirOf(total), subs: subs,
          title: tr.dividend_amounts_estimated ? "Dividend amounts per payment are estimated: only each year's total is recorded" : null,
        });
      } else {
        setValue(live.total, null, { reason: "less than a year of daily history" });
      }
    }

    // -------------------------------------------------------- mini chart

    function drawMini() {
      const canvas = mini.canvas;
      if (!canvas || !canvas.isConnected) return;
      const closes = mini.closes.slice();
      if (closes.length && lastTrade() != null) closes[closes.length - 1] = lastTrade();
      if (mini.msg) {
        mini.msg.textContent = closes.length >= 2 ? "" : mini.loaded ? (mini.msg.textContent || "No history yet") : "Loading chart…";
      }
      // Resized only when its size changed (render.js).
      const surface = Render.canvas(canvas, 88);
      if (!surface) return;
      const ctx = surface.ctx, W = surface.w, H = surface.h;
      if (closes.length < 2) return;

      let min = Math.min.apply(null, closes), max = Math.max.apply(null, closes);
      const pad = (max - min) * 0.1 || max * 0.01 || 1;
      min -= pad; max += pad;
      const x = function (i) { return 2 + (i / (closes.length - 1)) * (W - 4); };
      const y = function (v) { return 4 + (1 - (v - min) / (max - min)) * (H - 8); };
      const up = closes[closes.length - 1] >= closes[0];
      const style = getComputedStyle(document.documentElement);
      const color = (style.getPropertyValue(up ? "--green" : "--red") || (up ? "#34C57F" : "#E5675A")).trim();

      ctx.beginPath();
      closes.forEach(function (v, i) { if (i) ctx.lineTo(x(i), y(v)); else ctx.moveTo(x(i), y(v)); });
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      ctx.lineJoin = "round";
      ctx.stroke();
      ctx.lineTo(x(closes.length - 1), H);
      ctx.lineTo(x(0), H);
      ctx.closePath();
      ctx.globalAlpha = 0.12;
      ctx.fillStyle = color;
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    // -------------------------------------------------------- api

    let drawPending = false;
    function update(q) {
      quote = q;
      if (!visible) return;
      if (tab === "ratios") { renderRatiosLive(); return; }
      if (tab !== "profile") return;
      renderLive();
      // The chart only moves with the last point; redraw once a frame.
      if (!drawPending) {
        drawPending = true;
        requestAnimationFrame(function () { drawPending = false; drawMini(); });
      }
    }

    load();
    function resize() {
      drawMini();
      bars.forEach(drawBars);
    }

    return { show: show, hide: hide, update: update, resize: resize };
  }

  window.Description = { mount: mount, TABS: TABS.map(function (t) { return t.key; }) };
})();
