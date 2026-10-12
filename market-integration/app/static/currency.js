/*
 * Display currency: a picker for the page header, and price formatting
 * in whichever currency it's set to, over GET /fx/rates (Bank of Ghana
 * interbank mid-rates). Shared by /ticker, /stock/{symbol} and the
 * search bar. Prices arrive in cedis and are converted here, at the
 * latest fix; the choice is kept per browser.
 *
 *   Currency.mount(el);               // the picker, into the page header
 *   Currency.onChange(fn);            // repaint: the currency or its rate changed
 *   Currency.ready                    // Promise: settled once the rates are in (or failed)
 *   Currency.code()                   // "USD"
 *   Currency.convert(ghs)             // a cedi amount in the display currency
 *   Currency.money(ghs)               // "US$0.5559": converted, a price
 *   Currency.price(ghs)               // "0.5559": the same without the symbol
 *   Currency.amount(ghs)              // "US$166,036.40": a sum, two places
 *   Currency.large(ghs)               // "US$6.80B"
 *   Currency.formatMoney(v), .formatPrice(v)   // v already converted (charts
 *                                     //  from /candles?currency=...)
 *
 * A price shows enough decimals to tell apart one GSE tick (GH₵0.01) in
 * the display currency, at least two: GH₵0.70 is US$0.0595, ₦79.07.
 * Until the rates load, or if they can't be had, everything is in cedis
 * and the picker says why.
 */
(function () {
  "use strict";

  const STORAGE_KEY = "display.currency";
  const REFRESH_MS = 15 * 60e3;   // BoG fixes once a day; this just notices the new one
  const RETRY_MS = 60e3;
  const GSE_TICK = 0.01;
  const BASE = { code: "GHS", name: "Ghanaian cedi", symbol: "GH₵", per_ghs: 1, date: null };

  const CSS = `
  .cur-pick { display: inline-flex; flex: none; align-items: center; gap: 6px; font-size: 11.5px; color: var(--ink-faint); white-space: nowrap; }
  .cur-pick select {
    font: inherit; font-size: 12.5px; font-weight: 600; color: var(--ink);
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 6px 8px; cursor: pointer;
  }
  .cur-pick select:hover { border-color: var(--ink-faint); }
  .cur-pick select:focus-visible { outline: 2px solid var(--teal); outline-offset: 1px; }
  .cur-pick select:disabled { cursor: default; color: var(--ink-faint); }
  .cur-note:empty { display: none; }
  @media (max-width: 620px) { .cur-note { display: none; } }
  `;

  let rates = null;            // the last /fx/rates body
  const byCode = { GHS: BASE };
  let wanted = "GHS";          // the viewer's choice, even while it has no rate
  let current = BASE;          // what prices are shown in
  let failed = false;
  const listeners = [];
  const pickers = [];
  let resolveReady;
  const ready = new Promise(function (resolve) { resolveReady = resolve; });

  try { wanted = localStorage.getItem(STORAGE_KEY) || "GHS"; } catch (e) { /* storage unavailable */ }

  // ------------------------------------------------------------ format

  // Decimals that resolve one GSE tick in the display currency.
  function priceDigits() {
    const tick = GSE_TICK * current.per_ghs;
    return Math.max(2, Math.min(6, Math.ceil(-Math.log10(tick) - 1e-9)));
  }
  function grouped(v, digits) {
    return Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  function signed(v, text) { return (v < 0 ? "−" : "") + text; }

  function convert(ghs) { return ghs * current.per_ghs; }
  function formatPrice(v) { return signed(v, grouped(Math.abs(v), priceDigits())); }
  function formatMoney(v) { return signed(v, current.symbol + grouped(Math.abs(v), priceDigits())); }
  function money(ghs) { return formatMoney(convert(ghs)); }
  function price(ghs) { return formatPrice(convert(ghs)); }
  function amount(ghs) {
    const v = convert(ghs);
    return signed(v, current.symbol + grouped(Math.abs(v), 2));
  }
  function large(ghs) {
    const v = convert(ghs), abs = Math.abs(v);
    const scaled = abs >= 1e12 ? (abs / 1e12).toFixed(2) + "T" : abs >= 1e9 ? (abs / 1e9).toFixed(2) + "B" :
      abs >= 1e6 ? (abs / 1e6).toFixed(2) + "M" : abs >= 1e3 ? (abs / 1e3).toFixed(2) + "K" : abs.toFixed(2);
    return signed(v, current.symbol + scaled);
  }

  function fmtDay(iso, short) {
    const d = new Date(iso + "T00:00:00");
    // The year only when it isn't this one (a stale rate should look it).
    const year = short && d.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: year });
  }

  // Beside the picker: "BoG 5 Oct", the source and day of the rate.
  function note() {
    if (current.code === "GHS") return failed && wanted !== "GHS" ? "Rates unavailable" : "";
    return "BoG " + fmtDay(current.date, true);
  }

  // On hover: "GH₵11.7650 = US$1 · Bank of Ghana interbank mid-rate, 5 Oct 2026".
  function detail() {
    if (current.code === "GHS") {
      if (failed) return "Exchange rates unavailable: prices are in cedis";
      return rates ? "Show prices in another currency, at Bank of Ghana rates" : "Loading exchange rates…";
    }
    return "GH₵" + (1 / current.per_ghs).toFixed(4) + " = " + current.symbol.trim() + "1 · Bank of Ghana interbank mid-rate, " +
      fmtDay(current.date);
  }

  // ------------------------------------------------------------ state

  function apply() {
    const next = byCode[wanted] || BASE;
    const changed = next.code !== current.code || next.per_ghs !== current.per_ghs || next.date !== current.date;
    current = next;
    pickers.forEach(paintPicker);
    if (changed) listeners.forEach(function (fn) { try { fn(current.code); } catch (e) { console.error(e); } });
  }

  function load() {
    fetch("/fx/rates")
      .then(function (res) { return res.ok ? res.json() : Promise.reject(new Error("HTTP " + res.status)); })
      .then(function (body) {
        rates = body;
        failed = false;
        Object.keys(byCode).forEach(function (k) { if (k !== "GHS") delete byCode[k]; });
        body.currencies.forEach(function (c) { byCode[c.code] = c; });
        pickers.forEach(fillPicker);
        apply();
        resolveReady();
        setTimeout(load, REFRESH_MS);
      })
      .catch(function () {
        failed = true;
        apply();
        resolveReady();
        setTimeout(load, RETRY_MS);
      });
  }

  function set(code) {
    wanted = code;
    try { localStorage.setItem(STORAGE_KEY, code); } catch (e) { /* storage unavailable */ }
    apply();
  }

  // Another tab picked a currency: follow it.
  window.addEventListener("storage", function (e) {
    if (e.key === STORAGE_KEY && e.newValue && e.newValue !== wanted) { wanted = e.newValue; apply(); }
  });

  // ------------------------------------------------------------ picker

  function fillPicker(p) {
    p.select.innerHTML = "";
    [BASE].concat(rates ? rates.currencies : []).forEach(function (c) {
      const o = document.createElement("option");
      // Just the code, so the picker stays narrow (a select is as wide
      // as its longest option); the name is on hover.
      o.value = c.code;
      o.textContent = c.code;
      o.title = c.name;
      p.select.appendChild(o);
    });
  }

  function paintPicker(p) {
    p.select.value = current.code;
    p.select.disabled = !rates;
    p.note.textContent = note();
    p.label.title = detail();
  }

  function mount(container) {
    if (!document.getElementById("cur-css")) {
      const style = document.createElement("style");
      style.id = "cur-css";
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    const label = document.createElement("label");
    label.className = "cur-pick";
    const noteEl = document.createElement("span");
    noteEl.className = "cur-note";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "Display currency");
    select.addEventListener("change", function () { set(select.value); });
    label.appendChild(noteEl);
    label.appendChild(select);
    container.appendChild(label);
    const p = { label: label, select: select, note: noteEl };
    pickers.push(p);
    fillPicker(p);
    paintPicker(p);
    return p;
  }

  function onChange(fn) { listeners.push(fn); }

  load();

  window.Currency = {
    ready: ready,
    mount: mount,
    onChange: onChange,
    code: function () { return current.code; },
    symbol: function () { return current.symbol; },
    note: note,
    convert: convert,
    money: money,
    price: price,
    amount: amount,
    large: large,
    formatMoney: formatMoney,
    formatPrice: formatPrice,
  };
})();
