/*
 * Drawing tools for the stock page's chart: trend lines, horizontal
 * levels and Fibonacci retracements, saved per user and symbol
 * (/drawings/me/{symbol}; app/models/drawing.py).
 *
 *   const tools = ChartDrawings.mount({
 *     symbol, canvas, toolbar,          // the chart's <canvas>, a container for the buttons
 *     candles: () => [...],             // the drawn candles ({ts, close, ...}), oldest first
 *     stepMs: () => 86400e3,            // one candle's length
 *     geometry: () => g,                // drawChart's {padL, plotW, padT, priceH, xAt, yAt, priceAt}
 *     enabled: () => true,              // false while the chart isn't in cedis
 *     formatPrice: v => "4.1000",
 *     redraw: () => drawChart(),
 *   });
 *   tools.fit(min, max) -> [min, max]   // the price scale stretched to the drawings near it
 *   tools.draw(ctx)                     // paint them; call from drawChart
 *   tools.pointerDown/Move/Up(evt)      // true if the event was the drawing tools'
 *   tools.refresh()                     // after the currency changes
 *
 * Every drawing is kept as anchors of (time in ms, price in cedis), never
 * pixels, and placed through the chart's own geometry on each draw, so it
 * stays on the same candles whatever the range, scale or window size. A
 * time between two candles lands between them; one beyond the candles
 * shown is placed a candle-length per step past the edge.
 */
(function () {
  "use strict";

  // Retracement levels, as fractions of the move from its end back to its start.
  const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const COLORS = {
    trend: "#5AA9E6",
    level: "#D9A44C",
    fib: "#3E9C82",
    fibBand: "rgba(62, 156, 130, 0.08)",
    handle: "#EAE7DA",
    bg: "#0E1613",
  };
  const HIT = 7;          // px from a line that counts as on it
  const HANDLE = 5;       // handle radius, px
  const SNAP = 8;         // px: a point this close to a candle's close snaps to it
  const SAVE_DELAY = 600; // ms after the last change
  const NAMES = { trend: "Trend line", level: "Level", fib: "Fibonacci" };

  function mount(opts) {
    const s = {
      items: [],           // [{id, kind, points: [{t, p}], extend}]
      tool: null,          // "trend" | "level" | "fib" while placing
      pending: null,       // a two-point tool's first point
      preview: null,       // where its second would go
      downAt: null,        // where the placing press started (drag-to-draw)
      selected: null,      // an item's id
      hover: null,         // {id, handle}
      drag: null,          // {id, handle, x, y, orig, moved}
      loaded: false,
      max: 100,
      saveTimer: null,
      status: "",
    };

    // ------------------------------------------------------------ coordinates

    function candles() { return opts.candles() || []; }

    // A time as a (fractional) candle index on the drawn chart.
    function indexAt(t) {
      const c = candles(), n = c.length, step = opts.stepMs();
      if (t <= c[0].ts) return (t - c[0].ts) / step;
      if (t >= c[n - 1].ts) return n - 1 + (t - c[n - 1].ts) / step;
      let lo = 0, hi = n - 1;
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (c[mid].ts <= t) lo = mid; else hi = mid;
      }
      return lo + (t - c[lo].ts) / (c[hi].ts - c[lo].ts);
    }

    // The inverse: a fractional index back to a time.
    function timeAt(i) {
      const c = candles(), n = c.length, step = opts.stepMs();
      if (i <= 0) return c[0].ts + i * step;
      if (i >= n - 1) return c[n - 1].ts + (i - (n - 1)) * step;
      const k = Math.floor(i);
      return c[k].ts + (i - k) * (c[k + 1].ts - c[k].ts);
    }

    function screen(a, g) { return { x: g.xAt(indexAt(a.t)), y: g.yAt(a.p) }; }

    // A point under the pointer: on the nearest candle's close if within
    // SNAP px of it, otherwise exactly where it is.
    function anchorAt(x, y, g) {
      const c = candles(), n = c.length;
      const i = ((x - g.padL) / g.plotW) * (n - 1);
      const near = Math.max(0, Math.min(n - 1, Math.round(i)));
      if (Math.abs(g.xAt(near) - x) < SNAP && Math.abs(g.yAt(c[near].close) - y) < SNAP) {
        return { t: c[near].ts, p: c[near].close };
      }
      return { t: Math.round(timeAt(i)), p: round4(g.priceAt(y)) };
    }

    function round4(v) { return Math.round(v * 1e4) / 1e4; }

    function local(evt) {
      const r = opts.canvas.getBoundingClientRect();
      return { x: evt.clientX - r.left, y: evt.clientY - r.top };
    }

    function inPlot(x, y, g) {
      return x >= g.padL && x <= g.padL + g.plotW && y >= g.padT && y <= g.padT + g.priceH;
    }

    function usable() {
      return s.loaded && opts.enabled() && candles().length >= 2 && opts.geometry();
    }

    // ------------------------------------------------------------ the shapes

    // The prices a retracement's levels sit at, 0% at the move's end.
    function fibLevels(item) {
      const start = item.points[0].p, end = item.points[1].p;
      return FIB.map(function (r) { return { r: r, p: end - (end - start) * r }; });
    }

    // A trend line's on-screen segment, carried to the plot's right edge if it extends.
    function trendSegment(item, g) {
      const a = screen(item.points[0], g), b = screen(item.points[1], g);
      if (!item.extend || a.x === b.x) return [a, b];
      const from = a.x < b.x ? a : b, to = a.x < b.x ? b : a;
      const right = g.padL + g.plotW;
      if (to.x >= right) return [from, to];
      const slope = (to.y - from.y) / (to.x - from.x);
      return [from, { x: right, y: to.y + slope * (right - to.x) }];
    }

    function distToSegment(px, py, a, b) {
      const dx = b.x - a.x, dy = b.y - a.y;
      const len2 = dx * dx + dy * dy;
      const u = len2 ? Math.max(0, Math.min(1, ((px - a.x) * dx + (py - a.y) * dy) / len2)) : 0;
      return Math.hypot(px - (a.x + u * dx), py - (a.y + u * dy));
    }

    // The topmost drawing under (x, y): {id, handle} with handle the
    // index of a point, or null for the drawing's body.
    function hit(x, y, g) {
      for (let k = s.items.length - 1; k >= 0; k--) {
        const item = s.items[k];
        if (item.kind !== "level") {
          for (let h = 0; h < item.points.length; h++) {
            const q = screen(item.points[h], g);
            if (Math.hypot(q.x - x, q.y - y) <= HANDLE + 3) return { id: item.id, handle: h };
          }
        }
        if (item.kind === "trend") {
          const seg = trendSegment(item, g);
          if (distToSegment(x, y, seg[0], seg[1]) <= HIT) return { id: item.id, handle: null };
        } else if (item.kind === "level") {
          if (Math.abs(g.yAt(item.points[0].p) - y) <= HIT) return { id: item.id, handle: null };
        } else {
          const x0 = Math.min(screen(item.points[0], g).x, screen(item.points[1], g).x);
          if (x >= x0 - HIT && fibLevels(item).some(function (l) { return Math.abs(g.yAt(l.p) - y) <= HIT; })) {
            return { id: item.id, handle: null };
          }
        }
      }
      return null;
    }

    function valid(item) {
      if (item.kind === "trend") return item.points[0].t !== item.points[1].t;
      if (item.kind === "fib") return item.points[0].p !== item.points[1].p;
      return true;
    }

    // ------------------------------------------------------------ painting

    function draw(ctx) {
      const g = opts.geometry();
      if (!g || !opts.enabled() || candles().length < 2) return;
      const shown = s.items.slice();
      if (s.tool && s.pending && s.preview) {
        shown.push({ id: "_preview", kind: s.tool, points: [s.pending, s.preview], extend: false, preview: true });
      }
      const tags = [];   // price tags on the axis, drawn outside the clip

      ctx.save();
      ctx.beginPath();
      ctx.rect(g.padL, g.padT, g.plotW, g.priceH);
      ctx.clip();
      shown.forEach(function (item) {
        const active = item.id === s.selected || (s.hover && s.hover.id === item.id) || item.preview;
        const width = active ? 2.2 : 1.5;
        if (item.kind === "trend") {
          const seg = trendSegment(item, g);
          line(ctx, seg[0], seg[1], COLORS.trend, width, item.preview ? [5, 4] : null);
        } else if (item.kind === "level") {
          const y = g.yAt(item.points[0].p);
          line(ctx, { x: g.padL, y: y }, { x: g.padL + g.plotW, y: y }, COLORS.level, width, [6, 4]);
          tags.push({ p: item.points[0].p, y: y, color: COLORS.level });
        } else {
          drawFib(ctx, item, g, width);
        }
      });
      ctx.restore();

      // Handles on the selected (or hovered) drawing.
      shown.forEach(function (item) {
        const show = item.id === s.selected || item.preview || (s.hover && s.hover.id === item.id);
        if (!show || item.kind === "level") return;
        item.points.forEach(function (a) { handle(ctx, screen(a, g), COLORS[item.kind]); });
      });
      if (s.tool && s.pending && !s.preview) handle(ctx, screen(s.pending, g), COLORS[s.tool]);

      // Levels on the price axis; one off the scale gets a tag at the edge it's beyond.
      ctx.font = "10px ui-monospace, monospace";
      ctx.textBaseline = "middle";
      tags.forEach(function (tag) {
        const top = g.padT, bottom = g.padT + g.priceH;
        const off = tag.y < top ? "↑ " : tag.y > bottom ? "↓ " : "";
        const y = Math.max(top + 8, Math.min(bottom - 8, tag.y));
        const text = off + opts.formatPrice(tag.p);
        const w = ctx.measureText(text).width + 10;
        const x = g.padL + g.plotW + 4;
        ctx.fillStyle = COLORS.bg;
        ctx.fillRect(x, y - 8, w, 16);
        ctx.strokeStyle = tag.color;
        ctx.lineWidth = 1;
        ctx.strokeRect(x + 0.5, y - 7.5, w - 1, 15);
        ctx.fillStyle = tag.color;
        ctx.fillText(text, x + 5, y);
      });
    }

    function drawFib(ctx, item, g, width) {
      const a = screen(item.points[0], g), b = screen(item.points[1], g);
      const x0 = Math.min(a.x, b.x), x1 = g.padL + g.plotW;
      const levels = fibLevels(item);
      // The 38.2-61.8% zone, where retracements most often turn.
      const z1 = g.yAt(levels[2].p), z2 = g.yAt(levels[4].p);
      ctx.fillStyle = COLORS.fibBand;
      ctx.fillRect(x0, Math.min(z1, z2), x1 - x0, Math.abs(z2 - z1));
      line(ctx, a, b, COLORS.fib, 1, [3, 3]);
      ctx.font = "10px ui-monospace, monospace";
      ctx.textBaseline = "bottom";
      levels.forEach(function (l) {
        const y = g.yAt(l.p);
        const key = l.r === 0 || l.r === 1 || l.r === 0.5;
        line(ctx, { x: x0, y: y }, { x: x1, y: y }, COLORS.fib, key ? width : Math.max(1, width - 0.5), key ? null : [4, 3]);
        ctx.fillStyle = COLORS.fib;
        ctx.fillText((l.r * 100).toFixed(1) + "%  " + opts.formatPrice(l.p), x0 + 4, y - 2);
      });
    }

    function line(ctx, a, b, color, width, dash) {
      ctx.beginPath();
      ctx.setLineDash(dash || []);
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    function handle(ctx, q, color) {
      ctx.beginPath();
      ctx.arc(q.x, q.y, HANDLE, 0, Math.PI * 2);
      ctx.fillStyle = COLORS.bg;
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    // The price scale stretched to take in drawings near the data: within
    // one data-span above or below it. Further ones would squash the
    // chart; a level beyond gets an arrow tag at the edge instead.
    function fit(min, max) {
      if (!opts.enabled() || candles().length < 2) return [min, max];
      const c = candles();
      const t0 = c[0].ts, t1 = c[c.length - 1].ts;
      const span = (max - min) || Math.abs(max) * 0.02 || 1;
      let lo = min, hi = max;
      s.items.forEach(function (item) {
        item.points.forEach(function (a) {
          // A trend line's point counts only if it's on the chart's time span.
          if (item.kind === "trend" && (a.t < t0 || a.t > t1)) return;
          if (a.p < min - span || a.p > max + span) return;
          lo = Math.min(lo, a.p);
          hi = Math.max(hi, a.p);
        });
      });
      return [lo, hi];
    }

    // ------------------------------------------------------------ editing

    function byId(id) { return s.items.find(function (d) { return d.id === id; }) || null; }

    function newId() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

    function add(kind, points) {
      if (s.items.length >= s.max) {
        setStatus("At most " + s.max + " drawings per chart");
        return;
      }
      const item = { id: newId(), kind: kind, points: points, extend: false };
      if (!valid(item)) return;
      s.items.push(item);
      s.selected = item.id;
      changed();
    }

    function remove(id) {
      s.items = s.items.filter(function (d) { return d.id !== id; });
      if (s.selected === id) s.selected = null;
      changed();
    }

    function setTool(kind) {
      s.tool = s.tool === kind ? null : kind;
      s.pending = s.preview = s.downAt = null;
      if (s.tool) s.selected = null;
      syncToolbar();
      opts.redraw();
    }

    function finishTool() {
      s.tool = null;
      s.pending = s.preview = s.downAt = null;
      syncToolbar();
    }

    function changed() {
      syncToolbar();
      opts.redraw();
      scheduleSave();
    }

    // ------------------------------------------------------------ pointer

    function pointerDown(evt) {
      if (!usable()) return false;
      const g = opts.geometry();
      const q = local(evt);
      if (s.tool) {
        if (!inPlot(q.x, q.y, g)) return true;
        const a = anchorAt(q.x, q.y, g);
        if (s.tool === "level") {
          add("level", [a]);
          finishTool();
        } else if (!s.pending) {
          s.pending = a;
          s.downAt = q;
          capture(evt);
        } else {
          add(s.tool, [s.pending, a]);
          finishTool();
        }
        opts.redraw();
        return true;
      }
      const h = hit(q.x, q.y, g);
      if (h) {
        const item = byId(h.id);
        s.selected = h.id;
        s.drag = { id: h.id, handle: h.handle, x: q.x, y: q.y, moved: false,
                   orig: item.points.map(function (a) { return { t: a.t, p: a.p }; }) };
        capture(evt);
        syncToolbar();
        opts.redraw();
        return true;
      }
      if (s.selected) {
        s.selected = null;
        syncToolbar();
      }
      return false;
    }

    function pointerMove(evt) {
      if (!usable()) return false;
      const g = opts.geometry();
      const q = local(evt);
      if (s.drag) {
        const item = byId(s.drag.id);
        if (!item) { s.drag = null; return false; }
        if (Math.hypot(q.x - s.drag.x, q.y - s.drag.y) > 2) s.drag.moved = true;
        const before = item.points;
        if (s.drag.handle != null) {
          item.points = s.drag.orig.map(function (a, k) { return k === s.drag.handle ? anchorAt(q.x, q.y, g) : a; });
        } else {
          // The whole drawing, by the same distance in candles and in price.
          const n = candles().length;
          const di = ((q.x - s.drag.x) / g.plotW) * (n - 1);
          const dp = g.priceAt(q.y) - g.priceAt(s.drag.y);
          item.points = s.drag.orig.map(function (a) {
            return { t: Math.round(timeAt(indexAt(a.t) + di)), p: round4(a.p + dp) };
          });
        }
        if (!valid(item) || item.points.some(function (a) { return !(a.p > 0); })) item.points = before;
        opts.redraw();
        return true;
      }
      if (s.tool && s.pending) {
        s.preview = anchorAt(q.x, q.y, g);
        return false;   // the crosshair still follows
      }
      const h = s.tool ? null : hit(q.x, q.y, g);
      const was = s.hover;
      s.hover = h;
      opts.canvas.style.cursor = s.tool ? "crosshair" : h ? (h.handle != null ? "grab" : "move") : "";
      if ((was && was.id) !== (h && h.id)) opts.redraw();
      return false;
    }

    function pointerUp(evt) {
      if (s.drag) {
        const moved = s.drag.moved;
        s.drag = null;
        release(evt);
        if (moved) changed();
        return true;
      }
      // A press dragged out to the second point draws the line in one go.
      if (s.tool && s.pending && s.downAt && usable()) {
        const q = local(evt);
        release(evt);
        if (Math.hypot(q.x - s.downAt.x, q.y - s.downAt.y) > 6) {
          add(s.tool, [s.pending, anchorAt(q.x, q.y, opts.geometry())]);
          finishTool();
          opts.redraw();
          return true;
        }
        s.downAt = null;
      }
      return false;
    }

    function capture(evt) {
      try { opts.canvas.setPointerCapture(evt.pointerId); } catch (e) { /* not supported */ }
    }
    function release(evt) {
      try { opts.canvas.releasePointerCapture(evt.pointerId); } catch (e) { /* not captured */ }
    }

    document.addEventListener("keydown", function (evt) {
      const tag = (evt.target && evt.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (evt.key === "Escape" && (s.tool || s.selected)) {
        s.selected = null;
        finishTool();
        opts.redraw();
      } else if ((evt.key === "Delete" || evt.key === "Backspace") && s.selected && usable()) {
        evt.preventDefault();
        remove(s.selected);
      }
    });

    // ------------------------------------------------------------ toolbar

    const ui = {};

    function button(label, title, onClick) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "range-btn";
      b.textContent = label;
      b.title = title;
      b.addEventListener("click", onClick);
      opts.toolbar.appendChild(b);
      return b;
    }

    ui.trend = button("Trend line", "Click two points on the chart (or drag between them)", function () { setTool("trend"); });
    ui.level = button("Level", "Click a price on the chart", function () { setTool("level"); });
    ui.fib = button("Fibonacci", "Click the start of a move, then its end", function () { setTool("fib"); });
    ui.extend = button("Extend", "Carry the selected trend line on to the right edge", function () {
      const item = byId(s.selected);
      if (item && item.kind === "trend") { item.extend = !item.extend; changed(); }
    });
    ui.remove = button("Delete", "Delete the selected drawing (Delete key)", function () {
      if (s.selected) remove(s.selected);
    });
    ui.clear = button("Clear all", "Delete every drawing on this chart", function () {
      if (!s.items.length) return;
      if (!window.confirm("Delete all " + s.items.length + " drawings on " + opts.symbol + "?")) return;
      s.items = [];
      s.selected = null;
      changed();
    });
    ui.status = document.createElement("span");
    ui.status.className = "draw-status";
    opts.toolbar.appendChild(ui.status);

    function syncToolbar() {
      const on = s.loaded && opts.enabled();
      ["trend", "level", "fib"].forEach(function (k) {
        ui[k].disabled = !on;
        ui[k].classList.toggle("active", s.tool === k);
        ui[k].setAttribute("aria-pressed", String(s.tool === k));
      });
      const sel = byId(s.selected);
      ui.extend.hidden = !(on && sel && sel.kind === "trend");
      ui.extend.classList.toggle("active", !!(sel && sel.extend));
      ui.remove.hidden = !(on && sel);
      ui.clear.hidden = !(on && s.items.length);
      // Touch: while placing or editing, a drag on the chart is the tools', not a scroll.
      opts.canvas.style.touchAction = on && (s.tool || s.selected) ? "none" : "";
      if (!opts.enabled() && s.loaded) {
        ui.status.textContent = s.items.length
          ? s.items.length + " drawing" + (s.items.length === 1 ? "" : "s") + " hidden: drawings are in cedis, switch to GHS to see them"
          : "Drawing works in cedis: switch to GHS";
      } else if (s.tool) {
        ui.status.textContent = s.tool === "level" ? "Click a price" : s.pending ? "Click the second point" : "Click the first point";
      } else {
        ui.status.textContent = s.status;
      }
    }

    function setStatus(text) {
      s.status = text;
      syncToolbar();
    }

    // ------------------------------------------------------------ saving

    function toJson(item) {
      return {
        id: item.id,
        kind: item.kind,
        points: item.points.map(function (a) { return { t: new Date(a.t).toISOString(), p: a.p }; }),
        extend: item.kind === "trend" ? !!item.extend : false,
      };
    }

    function fromJson(d) {
      return {
        id: d.id,
        kind: d.kind,
        points: d.points.map(function (a) { return { t: Date.parse(a.t), p: a.p }; }),
        extend: !!d.extend,
      };
    }

    function url() { return "/drawings/me/" + encodeURIComponent(opts.symbol); }

    function load() {
      fetch(url())
        .then(function (r) {
          if (!r.ok) { const e = new Error("HTTP " + r.status); e.permanent = r.status === 404; throw e; }
          return r.json();
        })
        .then(function (body) {
          s.items = body.drawings.map(fromJson);
          s.max = body.max_drawings || s.max;
          s.loaded = true;
          s.status = s.items.length ? s.items.length + " saved" : "";
          syncToolbar();
          opts.redraw();
        })
        .catch(function (err) {
          if (err.permanent) { setStatus("Drawing isn't available for this symbol"); return; }
          setStatus("Couldn't load drawings; retrying…");
          setTimeout(load, 5000);
        });
    }

    function scheduleSave() {
      clearTimeout(s.saveTimer);
      setStatus("Saving…");
      s.saveTimer = setTimeout(save, SAVE_DELAY);
    }

    function save() {
      fetch(url(), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ drawings: s.items.map(toJson) }),
      })
        .then(function (r) {
          if (r.ok) { setStatus("Saved"); return; }
          return r.json().then(function (b) {
            const d = b && b.detail;
            const msg = Array.isArray(d) ? d[0].msg : typeof d === "string" ? d : "HTTP " + r.status;
            if (r.status === 422) { setStatus("Couldn't save: " + msg); return; }
            throw new Error(msg);
          });
        })
        .catch(function () {
          setStatus("Couldn't save; retrying…");
          clearTimeout(s.saveTimer);
          s.saveTimer = setTimeout(save, 5000);
        });
    }

    syncToolbar();
    load();

    return {
      fit: fit,
      draw: draw,
      pointerDown: pointerDown,
      pointerMove: pointerMove,
      pointerUp: pointerUp,
      refresh: function () {
        if (!opts.enabled()) { s.selected = null; finishTool(); }
        syncToolbar();
      },
      dragging: function () { return !!s.drag; },
    };
  }

  window.ChartDrawings = { mount: mount, FIB: FIB };
})();
