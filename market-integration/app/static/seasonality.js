/*
 * The stock page's Seasonality view (Bloomberg SEAG) over
 * GET /seasonality/{symbol}: how the stock has tended to do in each
 * calendar month, and this year's path against past years'.
 *
 *   const sea = Seasonality.mount(root, { symbol: "MTNGH" });
 *   sea.show();     // when the view opens (loads on first show)
 *   sea.hide();
 *   sea.resize();   // after the layout changes width
 *
 * Three parts, each readable on its own:
 *   - Average return by month: one bar per month (mean or median),
 *     coloured by sign, with how many of those months were up.
 *   - Year by year: each year's cumulative return from 1 January. This
 *     year is the highlighted line, the finished years' average dashed,
 *     and past years recede in grey; hovering picks out the nearest one.
 *     Ten-odd years can't each get a colour, so identity is on hover.
 *   - Monthly returns: the table behind both, years down, months across,
 *     tinted green/red by size; also the accessible view of the charts.
 *
 * Returns are in cedis whatever the page's display currency: a currency
 * move would otherwise read as the stock's season. The current month
 * and year are to date and not in the averages (the API flags them).
 * Styles itself with the page's colour variables; needs
 * /static/render.js loaded first.
 */
(function () {
  "use strict";

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // Day of year each month starts on (non-leap), for the year chart's axis.
  const MONTH_START = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335];
  const RETRY_MS = 3000;
  // Sign colours, as the Description view's bars (validated against the
  // dark panel there); the year chart's highlight is the page's teal.
  const UP = "#1AA486";
  const DOWN = "#E5675A";
  const THIS_YEAR = "#3E9C82";
  const AVERAGE = "#AEB6A9";
  const PAST = "rgba(174, 182, 169, 0.22)";
  const PAST_HOVER = "#EAE7DA";
  const GRID = "rgba(38, 54, 47, 0.9)";
  const AXIS = "#6F766D";
  const ZERO = "rgba(174, 182, 169, 0.45)";
  // A table cell is fully tinted at this monthly move (percent).
  const TINT_AT = 10;

  const CSS = `
  .sea-note { font-size: 12px; color: var(--ink-faint); margin: 0 0 14px; line-height: 1.55; }
  .sea-msg { padding: 28px 4px; font-size: 13px; color: var(--ink-faint); }
  .sea-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
  @media (max-width: 980px) { .sea-grid { grid-template-columns: minmax(0, 1fr); } }
  .sea-card-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin: 0 0 10px; }
  .sea-card-head .card-title { margin: 0; }
  .sea-toggle { display: flex; gap: 2px; }
  .sea-toggle button {
    font: inherit; font-size: 11.5px; font-weight: 600; color: var(--ink-soft);
    background: transparent; border: 1px solid transparent; border-radius: 6px;
    padding: 4px 9px; cursor: pointer;
  }
  .sea-toggle button:hover { color: var(--ink); background: var(--panel-strong); }
  .sea-toggle button[aria-pressed="true"] { color: var(--ink); background: var(--panel-strong); border-color: var(--line); }
  .sea-toggle button:focus-visible { outline: 2px solid var(--teal); outline-offset: 1px; }
  .sea-wrap { position: relative; }
  .sea-chart { display: block; width: 100%; height: 250px; touch-action: pan-y; }
  .sea-legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 11.5px; color: var(--ink-soft); margin: 8px 0 0; }
  .sea-legend span { display: inline-flex; align-items: center; gap: 6px; }
  .sea-key { width: 16px; height: 0; border-top: 2px solid; }
  .sea-tip {
    position: absolute; top: 8px; pointer-events: none; display: none; z-index: 2;
    background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
    padding: 7px 9px; font-size: 11.5px; line-height: 1.5; color: var(--ink-soft); white-space: nowrap;
  }
  .sea-tip .t-head { color: var(--ink); font-weight: 600; margin-bottom: 2px; }
  .sea-tip .t-row { display: flex; justify-content: space-between; gap: 14px; }
  .sea-tip .t-row span:last-child { color: var(--ink); }
  .sea-table-wrap { overflow-x: auto; margin-top: 16px; }
  .sea-table { width: 100%; border-collapse: separate; border-spacing: 2px; font-size: 12px; }
  .sea-table th { font-weight: 600; color: var(--ink-faint); font-size: 11px; padding: 4px 6px; text-align: right; }
  .sea-table th:first-child, .sea-table td:first-child {
    text-align: left; position: sticky; left: 0; background: var(--panel); z-index: 1;
  }
  .sea-table td { padding: 5px 6px; text-align: right; color: var(--ink); border-radius: 4px; }
  .sea-table td.na { color: var(--ink-faint); }
  .sea-table td.partial { font-style: italic; }
  .sea-table tr.sea-stat td { color: var(--ink-soft); }
  .sea-table tr.sea-stat.first td { border-top: 1px solid var(--line); }
  .sea-table .yr { color: var(--ink-soft); font-weight: 600; }
  .sea-table td.total { font-weight: 600; }
  `;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function pct(v, digits) {
    if (v == null) return "—";
    const d = digits == null ? 1 : digits;
    const s = Math.abs(v).toFixed(d);
    if (Number(s) === 0) return (0).toFixed(d) + "%";
    return (v > 0 ? "+" : "−") + s + "%";
  }

  function niceStep(span, target) {
    const raw = span / target;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    return (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  }

  // "12 Mar" for a day of the year, on a non-leap calendar.
  function dayLabel(day) {
    let m = 11;
    while (m > 0 && MONTH_START[m] > day) m--;
    return (day - MONTH_START[m] + 1) + " " + MONTHS[m];
  }

  // A path's value on `day`: its last point on or before it, carried
  // over weekends and holidays. Null before its first point.
  function valueAt(points, day) {
    let lo = 0, hi = points.length - 1, best = null;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (points[mid][0] <= day) { best = points[mid][1]; lo = mid + 1; } else hi = mid - 1;
    }
    return best;
  }

  function fmtMonthYear(iso) {
    return new Date(iso + "T00:00:00Z").toLocaleDateString(undefined, { month: "short", year: "numeric", timeZone: "UTC" });
  }

  function tipRows(tip, head, rows) {
    tip.textContent = "";
    tip.appendChild(el("div", "t-head", head));
    rows.forEach(function (r) {
      const row = el("div", "t-row");
      row.appendChild(el("span", null, r[0]));
      row.appendChild(el("span", null, r[1]));
      tip.appendChild(row);
    });
    tip.style.display = "block";
  }

  function placeTip(tip, x, width) {
    const w = tip.offsetWidth;
    tip.style.left = Math.max(4, x + 14 + w > width ? x - 14 - w : x + 14) + "px";
  }

  // A bar whose data end (away from zero) is rounded, anchored square on
  // the zero line.
  function bar(ctx, x, y0, w, y1) {
    const top = Math.min(y0, y1), h = Math.abs(y1 - y0);
    const r = Math.min(4, w / 2, h);
    ctx.beginPath();
    if (ctx.roundRect && h > 0) {
      ctx.roundRect(x, top, w, h, y1 < y0 ? [r, r, 0, 0] : [0, 0, r, r]);
    } else {
      ctx.rect(x, top, w, h);
    }
    ctx.fill();
  }

  // ------------------------------------------------------------ component

  function mount(root, options) {
    if (!document.getElementById("sea-css")) {
      const style = document.createElement("style");
      style.id = "sea-css";
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    const symbol = options.symbol;

    let data = null;
    let loading = false;
    let visible = false;
    let built = false;
    let stat = "mean";
    const bars = { canvas: null, tip: null, hover: null, geo: null };
    const years = { canvas: null, tip: null, hover: null, geo: null };

    function message(text) {
      root.textContent = "";
      built = false;
      root.appendChild(el("p", "sea-msg", text));
    }

    function load() {
      if (data || loading) return;
      loading = true;
      fetch("/seasonality/" + encodeURIComponent(symbol))
        .then(function (res) { return res.ok ? res.json() : Promise.reject(new Error("HTTP " + res.status)); })
        .then(function (body) {
          data = body;
          loading = false;
          if (visible) render();
        })
        .catch(function () {
          loading = false;
          if (visible) message("Couldn't load seasonality. Retrying…");
          setTimeout(function () { if (visible) load(); }, RETRY_MS);
        });
    }

    function show() {
      visible = true;
      if (!data) { message("Loading seasonality…"); load(); return; }
      if (!built) render(); else requestAnimationFrame(redraw);
    }

    function hide() { visible = false; }

    function resize() { if (visible && built) redraw(); }

    function redraw() { drawBars(); drawYears(); }

    // -------------------------------------------------------- layout

    function render() {
      root.textContent = "";
      built = true;
      if (!data.months.length) {
        root.appendChild(el("p", "sea-msg", "No daily history for " + symbol + " yet."));
        return;
      }
      const finished = data.by_month.reduce(function (n, m) { return Math.max(n, m.count); }, 0);
      root.appendChild(el("p", "sea-note",
        "From daily closes, " + fmtMonthYear(data.from) + " – " + fmtMonthYear(data.to) +
        ", in cedis whatever the display currency. Averages are over up to " + finished +
        " finished months each; the current month and year are to date and left out of them. " +
        "Past patterns are no forecast."));

      const grid = el("div", "sea-grid");
      grid.appendChild(barsCard());
      grid.appendChild(yearsCard());
      root.appendChild(grid);
      root.appendChild(tableCard());
      requestAnimationFrame(redraw);
    }

    function chartBox(target, label) {
      const wrap = el("div", "sea-wrap");
      target.canvas = el("canvas", "sea-chart");
      target.canvas.setAttribute("role", "img");
      target.canvas.setAttribute("aria-label", label);
      target.tip = el("div", "sea-tip num");
      wrap.appendChild(target.canvas);
      wrap.appendChild(target.tip);
      return wrap;
    }

    function hoverable(target, onMove, draw) {
      function move(e) {
        const r = target.canvas.getBoundingClientRect();
        onMove(e.clientX - r.left, e.clientY - r.top);
        draw();
      }
      target.canvas.addEventListener("pointermove", move);
      target.canvas.addEventListener("pointerdown", move);
      target.canvas.addEventListener("pointerleave", function () {
        target.hover = null;
        target.tip.style.display = "none";
        draw();
      });
    }

    function barsCard() {
      const card = el("section", "card");
      const head = el("div", "sea-card-head");
      head.appendChild(el("h2", "card-title", "Average return by month"));
      const toggle = el("div", "sea-toggle");
      toggle.setAttribute("role", "group");
      toggle.setAttribute("aria-label", "Average");
      [["mean", "Mean"], ["median", "Median"]].forEach(function (s) {
        const b = el("button", null, s[1]);
        b.type = "button";
        b.setAttribute("aria-pressed", stat === s[0] ? "true" : "false");
        b.addEventListener("click", function () {
          stat = s[0];
          toggle.querySelectorAll("button").forEach(function (o) {
            o.setAttribute("aria-pressed", o === b ? "true" : "false");
          });
          drawBars();
        });
        toggle.appendChild(b);
      });
      head.appendChild(toggle);
      card.appendChild(head);
      card.appendChild(chartBox(bars, "Average return by calendar month: " + data.by_month.map(function (m) {
        return MONTHS[m.month - 1] + " " + pct(m.mean) + ", up " + m.positive + " of " + m.count;
      }).join("; ")));
      card.appendChild(el("p", "sea-note", "Under each month: how many of its years it rose in."));
      hoverable(bars, function (x) {
        const g = bars.geo;
        if (!g) return;
        const i = Math.floor((x - g.left) / g.slot);
        bars.hover = i >= 0 && i < 12 ? i : null;
      }, drawBars);
      return card;
    }

    function yearsCard() {
      const card = el("section", "card");
      const head = el("div", "sea-card-head");
      head.appendChild(el("h2", "card-title", "Year by year, from 1 January"));
      card.appendChild(head);
      const current = data.paths[data.paths.length - 1];
      card.appendChild(chartBox(years, "Cumulative return through the year: " + current.year + " so far " +
        pct(current.points[current.points.length - 1][1]) + "; average of " + data.average_years +
        " years " + pct(data.average.length ? data.average[data.average.length - 1][1] : null) + " by year end"));
      const legend = el("div", "sea-legend");
      function key(color, dashed, text) {
        const s = el("span");
        const k = el("span", "sea-key");
        k.style.borderTopColor = color;
        if (dashed) k.style.borderTopStyle = "dashed";
        s.appendChild(k);
        s.appendChild(document.createTextNode(text));
        legend.appendChild(s);
      }
      key(THIS_YEAR, false, String(current.year) + " (to date)");
      if (data.average_years) key(AVERAGE, true, "Average of " + data.average_years + " years");
      key("rgba(174, 182, 169, 0.5)", false, "Past years (hover to pick one)");
      card.appendChild(legend);
      hoverable(years, function (x, y) {
        const g = years.geo;
        if (!g) return;
        const day = Math.round(g.dayAt(x));
        if (day < 1 || day > 366) { years.hover = null; return; }
        // The past year whose line is nearest the pointer at that day.
        let best = null, bestD = Infinity;
        data.paths.slice(0, -1).forEach(function (p) {
          const v = valueAt(p.points, day);
          if (v == null) return;
          const d = Math.abs(g.yAt(v) - y);
          if (d < bestD) { bestD = d; best = p; }
        });
        years.hover = { day: day, year: best && bestD < 24 ? best : null };
      }, drawYears);
      return card;
    }

    function tableCard() {
      const card = el("section", "card");
      card.style.marginTop = "16px";
      card.appendChild(el("h2", "card-title", "Monthly returns"));
      const wrap = el("div", "sea-table-wrap");
      const table = el("table", "sea-table num");
      const thead = el("thead");
      const hr = el("tr");
      hr.appendChild(el("th", null, "Year"));
      MONTHS.forEach(function (m) { hr.appendChild(el("th", null, m)); });
      hr.appendChild(el("th", null, "Full year"));
      thead.appendChild(hr);
      table.appendChild(thead);

      const byKey = {};
      data.months.forEach(function (m) { byKey[m.year + "-" + m.month] = m; });
      const annual = {};
      data.annual.forEach(function (a) { annual[a.year] = a; });

      function cell(value, partial, total) {
        const td = el("td", value == null ? "na" : "", pct(value));
        if (value != null) td.style.background = tint(value);
        if (partial) { td.classList.add("partial"); td.title = "To date"; }
        if (total) td.classList.add("total");
        return td;
      }

      const tbody = el("tbody");
      data.annual.slice().reverse().forEach(function (a) {
        const tr = el("tr");
        tr.appendChild(el("td", "yr", String(a.year)));
        for (let m = 1; m <= 12; m++) {
          const r = byKey[a.year + "-" + m];
          tr.appendChild(r ? cell(r.return, r.partial) : el("td", "na", ""));
        }
        tr.appendChild(cell(a.return, a.partial, true));
        tbody.appendChild(tr);
      });
      [["Mean", function (s) { return pct(s.mean); }],
       ["Median", function (s) { return pct(s.median); }],
       ["Up", function (s) { return s.count ? s.positive + "/" + s.count : "—"; }]].forEach(function (row, i) {
        const tr = el("tr", i ? "sea-stat" : "sea-stat first");
        tr.appendChild(el("td", "yr", row[0]));
        data.by_month.forEach(function (s) { tr.appendChild(el("td", null, row[1](s))); });
        tr.appendChild(el("td"));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      card.appendChild(wrap);
      card.appendChild(el("p", "sea-note", "Italic: to date. Blank: before the history starts, or no previous month to measure from."));
      return card;
    }

    // Diverging tint: green up, red down, transparent at zero.
    function tint(v) {
      const a = Math.min(1, Math.abs(v) / TINT_AT) * 0.42 + 0.04;
      return v >= 0 ? "rgba(26, 164, 134, " + a.toFixed(3) + ")" : "rgba(229, 103, 90, " + a.toFixed(3) + ")";
    }

    // -------------------------------------------------------- bars

    function drawBars() {
      if (!bars.canvas || !bars.canvas.isConnected) return;
      const surface = Render.canvas(bars.canvas, 250);
      if (!surface) return;
      const ctx = surface.ctx, W = surface.w, H = surface.h;
      const padL = 46, padR = 8, padT = 12, padB = 38;
      const plotW = W - padL - padR, plotH = H - padT - padB;
      const values = data.by_month.map(function (m) { return m[stat]; });

      let lo = 0, hi = 0;
      values.forEach(function (v) { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
      if (hi === lo) { hi = 1; lo = -1; }
      const span = hi - lo;
      hi += span * 0.08; lo -= lo < 0 ? span * 0.08 : 0;
      function yAt(v) { return padT + (1 - (v - lo) / (hi - lo)) * plotH; }
      const slot = plotW / 12;
      bars.geo = { left: padL, slot: slot };

      ctx.font = "10px ui-monospace, monospace";
      ctx.textBaseline = "middle";
      ctx.textAlign = "right";
      const step = niceStep(hi - lo, 4);
      for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
        const y = Math.round(yAt(v)) + 0.5;
        ctx.strokeStyle = Math.abs(v) < step / 1000 ? ZERO : GRID;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
        ctx.fillStyle = AXIS;
        ctx.fillText(pct(v, step < 1 ? 1 : 0), padL - 6, y);
      }

      const barW = Math.max(4, Math.min(28, slot * 0.58));
      const y0 = yAt(0);
      const short = slot < 30;
      values.forEach(function (v, i) {
        const x = padL + i * slot + (slot - barW) / 2;
        const dim = bars.hover != null && bars.hover !== i;
        if (v != null) {
          ctx.fillStyle = v >= 0 ? UP : DOWN;
          ctx.globalAlpha = dim ? 0.45 : 1;
          bar(ctx, x, y0, barW, yAt(v));
          ctx.globalAlpha = 1;
        }
        const cx = padL + i * slot + slot / 2;
        ctx.textAlign = "center";
        ctx.fillStyle = bars.hover === i ? "#EAE7DA" : "#AEB6A9";
        ctx.font = "10.5px ui-monospace, monospace";
        ctx.fillText(short ? MONTHS[i].charAt(0) : MONTHS[i], cx, H - padB + 12);
        const m = data.by_month[i];
        ctx.fillStyle = AXIS;
        ctx.font = "9.5px ui-monospace, monospace";
        ctx.fillText(m.count ? m.positive + "/" + m.count : "—", cx, H - padB + 27);
      });

      if (bars.hover != null) {
        const m = data.by_month[bars.hover];
        const rows = [["Mean", pct(m.mean, 2)], ["Median", pct(m.median, 2)], ["Up", m.positive + " of " + m.count]];
        if (m.best) rows.push(["Best", pct(m.best.return) + " (" + m.best.year + ")"]);
        if (m.worst) rows.push(["Worst", pct(m.worst.return) + " (" + m.worst.year + ")"]);
        tipRows(bars.tip, MONTHS[bars.hover], rows);
        placeTip(bars.tip, padL + bars.hover * slot + slot / 2, W);
      }
    }

    // -------------------------------------------------------- years

    function drawYears() {
      if (!years.canvas || !years.canvas.isConnected) return;
      const surface = Render.canvas(years.canvas, 250);
      if (!surface) return;
      const ctx = surface.ctx, W = surface.w, H = surface.h;
      const padL = 46, padR = 64, padT = 12, padB = 24;
      const plotW = W - padL - padR, plotH = H - padT - padB;
      const paths = data.paths;
      const current = paths[paths.length - 1];
      const past = paths.slice(0, -1);

      let lo = 0, hi = 0;
      paths.concat([{ points: data.average }]).forEach(function (p) {
        p.points.forEach(function (pt) { if (pt[1] < lo) lo = pt[1]; if (pt[1] > hi) hi = pt[1]; });
      });
      if (hi === lo) { hi = 1; lo = -1; }
      const pad = (hi - lo) * 0.06;
      lo -= pad; hi += pad;
      function xAt(day) { return padL + ((day - 1) / 365) * plotW; }
      function yAt(v) { return padT + (1 - (v - lo) / (hi - lo)) * plotH; }
      years.geo = { yAt: yAt, dayAt: function (x) { return 1 + ((x - padL) / plotW) * 365; } };

      ctx.font = "10px ui-monospace, monospace";
      ctx.textBaseline = "middle";
      ctx.textAlign = "right";
      const step = niceStep(hi - lo, 5);
      for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
        const y = Math.round(yAt(v)) + 0.5;
        ctx.strokeStyle = Math.abs(v) < step / 1000 ? ZERO : GRID;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
        ctx.fillStyle = AXIS;
        ctx.fillText(pct(v, 0), padL - 6, y);
      }
      ctx.textAlign = "center";
      const every = plotW < 360 ? 3 : 1;
      MONTH_START.forEach(function (d, i) {
        if (i % every) return;
        ctx.fillStyle = AXIS;
        ctx.fillText(MONTHS[i], xAt(d + 14), H - 8);
      });

      function line(points, color, width, dash) {
        if (!points.length) return;
        ctx.beginPath();
        points.forEach(function (pt, i) {
          if (i) ctx.lineTo(xAt(pt[0]), yAt(pt[1])); else ctx.moveTo(xAt(pt[0]), yAt(pt[1]));
        });
        ctx.strokeStyle = color;
        ctx.lineWidth = width;
        ctx.lineJoin = "round";
        ctx.setLineDash(dash || []);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      const picked = years.hover && years.hover.year;
      past.forEach(function (p) { if (p !== picked) line(p.points, PAST, 1); });
      line(data.average, AVERAGE, 1.5, [5, 4]);
      if (picked) line(picked.points, PAST_HOVER, 1.5);
      line(current.points, THIS_YEAR, 2);

      // Direct labels at the line ends: this year and the average.
      const last = current.points[current.points.length - 1];
      ctx.beginPath();
      ctx.arc(xAt(last[0]), yAt(last[1]), 4, 0, Math.PI * 2);
      ctx.fillStyle = THIS_YEAR;
      ctx.fill();
      ctx.strokeStyle = "#16211B";
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.textAlign = "left";
      ctx.font = "10.5px ui-monospace, monospace";
      const labels = [{ y: yAt(last[1]), text: String(current.year), color: "#EAE7DA", x: xAt(last[0]) + 8 }];
      if (data.average.length) {
        const a = data.average[data.average.length - 1];
        labels.push({ y: yAt(a[1]), text: "Avg", color: "#AEB6A9", x: padL + plotW + 6 });
      }
      if (labels.length === 2 && Math.abs(labels[0].y - labels[1].y) < 12 && labels[0].x > padL + plotW - 30) {
        labels[1].y += labels[1].y >= labels[0].y ? 12 : -12;
      }
      labels.forEach(function (l) { ctx.fillStyle = l.color; ctx.fillText(l.text, Math.min(l.x, W - 34), l.y); });

      if (years.hover) {
        const day = years.hover.day;
        const x = Math.round(xAt(day)) + 0.5;
        ctx.strokeStyle = "rgba(174, 182, 169, 0.55)";
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + plotH); ctx.stroke();
        ctx.setLineDash([]);
        const rows = [[String(current.year), day <= last[0] ? pct(valueAt(current.points, day), 2) : "—"]];
        if (data.average.length) rows.push(["Average", pct(valueAt(data.average, day), 2)]);
        if (picked) rows.push([String(picked.year), pct(valueAt(picked.points, day), 2)]);
        tipRows(years.tip, dayLabel(day), rows);
        placeTip(years.tip, xAt(day), W);
      }
    }

    return { show: show, hide: hide, resize: resize };
  }

  window.Seasonality = { mount: mount };
})();
