/*
 * Rendering helpers for pages that take live updates (#22).
 *
 * The feed can deliver many quotes at once (a batched "ticks" message
 * carries one per changed symbol), so pages apply each quote to their
 * state straight away and draw at most once per animation frame:
 *
 *   const schedule = Render.batch(function (symbols) {
 *     symbols.forEach(paintRow);          // each changed key once, however many quotes
 *   });
 *   ws.onmessage = ... state[q.symbol] = q; schedule(q.symbol);
 *
 * and touch only what changed when they do:
 *
 *   Render.text(td, "12.34")      writes textContent only if it differs
 *   Render.cls(td, "num up")      sets className only if it differs
 *   Render.flash(td, "up")        highlights a cell for FLASH_MS
 *   Render.canvas(canvas, 110)    sizes a canvas only when its size changed
 *
 * Render.flash() is a class toggle, not an animation: it adds
 * "flash-up" (or -down, -neutral), which the page styles as a static
 * tint, and one shared sweep removes it FLASH_MS later, batched into a
 * frame. A CSS animation would make the browser restyle and repaint
 * every flashing cell on every frame; with a whole table moving at once
 * that, not the script, was what dropped frames. A toggle costs two
 * style updates per flash, and flashing a cell again just extends it.
 */
(function () {
  "use strict";

  // Calls flush(keys) once per animation frame with the keys scheduled
  // since the last one (a Set, in first-scheduled order). schedule()
  // with no key just asks for a frame (flush gets an empty Set).
  function batch(flush) {
    let pending = new Set();
    let requested = false;
    function run() {
      requested = false;
      const keys = pending;
      pending = new Set();
      flush(keys);
    }
    return function schedule(key) {
      if (key !== undefined) pending.add(key);
      if (!requested) {
        requested = true;
        requestAnimationFrame(run);
      }
    };
  }

  function text(el, value) {
    const v = value == null ? "" : String(value);
    if (el._renderText !== v) {
      el._renderText = v;
      el.textContent = v;
    }
  }

  function cls(el, className) {
    if (el.className !== className) el.className = className;
  }

  const FLASH_MS = 800;
  const FLASH_CLASSES = ["flash-up", "flash-down", "flash-neutral"];
  const flashing = new Map();   // element -> time its flash ends
  let sweepAt = 0;              // when the pending sweep runs (0: none)

  function flash(el, dir) {
    const name = "flash-" + dir;
    if (!el.classList.contains(name)) {
      FLASH_CLASSES.forEach(function (c) { if (c !== name) el.classList.remove(c); });
      el.classList.add(name);
    }
    const until = performance.now() + FLASH_MS;
    flashing.set(el, until);
    if (!sweepAt) scheduleSweep(until);
  }

  function scheduleSweep(at) {
    sweepAt = at;
    setTimeout(function () { requestAnimationFrame(sweep); }, Math.max(0, at - performance.now()));
  }

  // Ends every flash that's due, in one frame; then waits for the next.
  function sweep(now) {
    sweepAt = 0;
    let next = Infinity;
    flashing.forEach(function (until, el) {
      if (until <= now) {
        FLASH_CLASSES.forEach(function (c) { el.classList.remove(c); });
        flashing.delete(el);
      } else if (until < next) {
        next = until;
      }
    });
    if (next !== Infinity) scheduleSweep(next);
  }

  // Backing-store size for a canvas at its CSS size and the device pixel
  // ratio, set only when it changed (setting width/height reallocates
  // and clears the canvas even when the value is the same). Returns the
  // context, cleared, scaled to CSS pixels, or null while it has no width.
  function canvas(el, fallbackHeight) {
    const dpr = window.devicePixelRatio || 1;
    const w = el.clientWidth;
    const h = el.clientHeight || fallbackHeight;
    if (!w) return null;
    const bw = Math.round(w * dpr), bh = Math.round(h * dpr);
    if (el.width !== bw) el.width = bw;
    if (el.height !== bh) el.height = bh;
    const ctx = el.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx: ctx, w: w, h: h };
  }

  window.Render = { batch: batch, text: text, cls: cls, flash: flash, canvas: canvas };
})();
