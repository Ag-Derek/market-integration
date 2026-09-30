/*
 * Header search bar with autocomplete, over GET /search. Shared by
 * /ticker and /stock/{symbol}; styles itself with the page's colour
 * variables.
 *
 *   MarketSearch.mount(document.getElementById("search"), { onPin: fn });
 *
 * Keyboard: "/" focuses, Up/Down move, Enter opens the highlighted (or
 * first) result, Shift+Enter pins it, Esc closes (a second Esc clears
 * and leaves the box). Equities open /stock/{symbol}, bills and bonds
 * /bond/{symbol}.
 *
 * "+ Pin" adds an equity to the watchlist: the /ticker page's open
 * views, kept in localStorage so a pin from any page shows up there.
 * `onPin(symbol)` lets a page handle it itself (the ticker opens the
 * view at once); it returns a message to show, or nothing for "Pinned".
 * The watchlist only renders equities for now, so bills and bonds can't
 * be pinned.
 */
(function () {
  "use strict";

  const DEBOUNCE_MS = 150;
  const LIMIT = 10;
  const WATCHLIST_KEY = "watchlist";
  const WATCHLIST_MAX = 6;
  const BADGE = { equity: "Equity", bill: "Bill", bond: "Bond" };

  const CSS = `
  .msearch { position: relative; flex: 1 1 280px; max-width: 440px; min-width: 200px; }
  .msearch-input {
    width: 100%; font: inherit; font-size: 13px; color: var(--ink);
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 8px 34px 8px 32px; outline: none;
  }
  .msearch-input::placeholder { color: var(--ink-faint); }
  .msearch-input:focus { border-color: var(--teal); }
  .msearch-input::-webkit-search-cancel-button { display: none; }
  .msearch-icon {
    position: absolute; left: 11px; top: 50%; width: 12px; height: 12px; margin-top: -7px;
    border: 1.6px solid var(--ink-faint); border-radius: 50%; pointer-events: none;
  }
  .msearch-icon::after {
    content: ""; position: absolute; width: 5px; height: 1.6px; background: var(--ink-faint);
    right: -5px; bottom: -2px; transform: rotate(45deg);
  }
  .msearch-key {
    position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
    font: 11px ui-monospace, monospace; color: var(--ink-faint);
    border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; pointer-events: none;
  }
  .msearch-input:focus ~ .msearch-key { display: none; }
  .msearch-list {
    position: absolute; z-index: 20; left: 0; top: calc(100% + 6px); width: max(100%, 600px);
    margin: 0; padding: 4px; list-style: none; max-height: 420px; overflow-y: auto;
    background: var(--panel-strong); border: 1px solid var(--line); border-radius: 10px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
    scrollbar-width: thin; scrollbar-color: var(--ink-faint) transparent;
  }
  .msearch-list::-webkit-scrollbar { width: 6px; }
  .msearch-list::-webkit-scrollbar-thumb { background: var(--ink-faint); border-radius: 3px; }
  .msearch-list[hidden] { display: none; }
  .msearch-opt {
    display: grid; grid-template-columns: 96px minmax(0, 1fr) auto 76px auto; align-items: center;
    gap: 10px; padding: 8px 8px 8px 10px; border-radius: 7px; cursor: pointer;
  }
  .msearch-opt.active { background: var(--panel); box-shadow: inset 2px 0 0 var(--teal); }
  .msearch-sym { font: 600 12.5px ui-monospace, monospace; color: var(--ink); }
  .msearch-name { font-size: 12.5px; color: var(--ink-soft); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .msearch-badge {
    font-size: 10px; font-weight: 600; letter-spacing: 0.03em;
    text-transform: uppercase; padding: 1px 6px; border-radius: 4px;
    color: var(--ink-soft); background: rgba(174, 182, 169, 0.1);
  }
  .msearch-badge.bill { color: var(--gold); background: var(--gold-bg); }
  .msearch-badge.bond { color: var(--status-live, #5AA9E6); background: rgba(90, 169, 230, 0.12); }
  .msearch-px { font: 12px ui-monospace, monospace; font-variant-numeric: tabular-nums; color: var(--ink); text-align: right; }
  .msearch-px .up { color: var(--green); }
  .msearch-px .down { color: var(--red); }
  .msearch-px small { display: block; font-size: 10.5px; color: var(--ink-faint); }
  .msearch-pin {
    font: 600 11px -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--teal);
    background: transparent; border: 1px solid var(--line); border-radius: 6px;
    padding: 4px 8px; cursor: pointer; white-space: nowrap;
  }
  .msearch-pin:hover:not(:disabled) { border-color: var(--teal); }
  .msearch-pin:disabled { color: var(--ink-faint); cursor: default; }
  .msearch-empty { padding: 14px 10px; font-size: 12.5px; color: var(--ink-faint); cursor: default; }
  .msearch-hint {
    display: flex; gap: 12px; flex-wrap: wrap; padding: 7px 10px 4px; margin-top: 4px;
    border-top: 1px solid var(--line); font-size: 10.5px; color: var(--ink-faint); cursor: default;
  }
  .msearch-hint kbd { font: 10px ui-monospace, monospace; border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; }
  @media (max-width: 620px) {
    .msearch { max-width: none; flex-basis: 100%; order: 10; }
    .msearch-list { width: 100%; }
    .msearch-opt { grid-template-columns: auto minmax(0, 1fr) auto auto; }
    .msearch-px { display: none; }
  }`;

  // ------------------------------------------------------------ watchlist

  function readWatchlist() {
    try {
      const v = JSON.parse(localStorage.getItem(WATCHLIST_KEY) || "null");
      return Array.isArray(v) ? v.filter(function (s) { return typeof s === "string"; }) : null;
    } catch (e) {
      return null;
    }
  }

  function writeWatchlist(symbols) {
    try { localStorage.setItem(WATCHLIST_KEY, JSON.stringify(symbols)); } catch (e) { /* storage unavailable */ }
  }

  // Pin from a page with no watchlist of its own: append to the stored
  // list the ticker opens with.
  function pinToStorage(symbol) {
    const list = readWatchlist() || [];
    if (list.indexOf(symbol) !== -1) return "Already pinned";
    if (list.length >= WATCHLIST_MAX) return "Watchlist full";
    list.push(symbol);
    writeWatchlist(list);
    return "Pinned";
  }

  // ------------------------------------------------------------ component

  function detailUrl(r) {
    return (r.asset_class === "equity" ? "/stock/" : "/bond/") + encodeURIComponent(r.symbol);
  }

  function fmtPrice(r) {
    if (r.price == null) return "—";
    const n = Number(r.price).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return r.asset_class === "equity" ? "GH₵" + n : n;
  }

  function mount(root, options) {
    options = options || {};
    if (!document.getElementById("msearch-css")) {
      const style = document.createElement("style");
      style.id = "msearch-css";
      style.textContent = CSS;
      document.head.appendChild(style);
    }

    const listId = "msearch-list-" + Math.random().toString(36).slice(2, 8);
    root.classList.add("msearch");
    root.setAttribute("role", "search");
    root.innerHTML =
      '<span class="msearch-icon" aria-hidden="true"></span>' +
      '<input class="msearch-input" type="search" autocomplete="off" spellcheck="false"' +
      ' placeholder="Search symbol, name, ISIN or maturity" aria-label="Search instruments"' +
      ' role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="' + listId + '">' +
      '<kbd class="msearch-key" aria-hidden="true">/</kbd>' +
      '<ul class="msearch-list" id="' + listId + '" role="listbox" aria-label="Search results" hidden></ul>';

    const input = root.querySelector(".msearch-input");
    const list = root.querySelector(".msearch-list");

    let results = [];
    let active = -1;
    let query = "";
    let timer = null;
    let controller = null;
    let seq = 0;

    function setOpen(open) {
      list.hidden = !open;
      input.setAttribute("aria-expanded", open ? "true" : "false");
      if (!open) {
        input.removeAttribute("aria-activedescendant");
        return;
      }
      // The list can be wider than the input; keep it on screen.
      list.style.left = "0px";
      const over = list.getBoundingClientRect().right - (document.documentElement.clientWidth - 12);
      if (over > 0) list.style.left = -over + "px";
    }

    function setActive(i) {
      const opts = list.querySelectorAll(".msearch-opt");
      if (!opts.length) { active = -1; return; }
      active = (i + opts.length) % opts.length;
      opts.forEach(function (o, j) {
        o.classList.toggle("active", j === active);
        o.setAttribute("aria-selected", j === active ? "true" : "false");
      });
      input.setAttribute("aria-activedescendant", opts[active].id);
      opts[active].scrollIntoView({ block: "nearest" });
    }

    function message(text) {
      list.innerHTML = "";
      const li = document.createElement("li");
      li.className = "msearch-empty";
      li.setAttribute("role", "option");
      li.setAttribute("aria-disabled", "true");
      li.textContent = text;
      list.appendChild(li);
      setOpen(true);
    }

    function pin(r, button) {
      if (r.asset_class !== "equity") return;
      const note = (options.onPin ? options.onPin(r.symbol) : pinToStorage(r.symbol)) || "Pinned";
      if (button) {
        button.textContent = note === "Pinned" ? "Pinned ✓" : note;
        button.disabled = true;
      }
    }

    function open(r) {
      location.href = detailUrl(r);
    }

    function render() {
      list.innerHTML = "";
      active = -1;
      if (!results.length) {
        message("No matches for “" + query + "”");
        return;
      }
      const pinned = readWatchlist() || [];
      results.forEach(function (r, i) {
        const li = document.createElement("li");
        li.className = "msearch-opt";
        li.id = listId + "-" + i;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");

        const sym = document.createElement("span");
        sym.className = "msearch-sym";
        sym.textContent = r.symbol;

        const name = document.createElement("span");
        name.className = "msearch-name";
        name.title = r.name;
        name.textContent = r.name;
        const badge = document.createElement("span");
        badge.className = "msearch-badge " + r.asset_class;
        badge.textContent = BADGE[r.asset_class] || r.asset_class;

        const px = document.createElement("span");
        px.className = "msearch-px";
        px.textContent = fmtPrice(r);
        if (r.change != null && r.price != null) {
          const chg = document.createElement("small");
          const dir = r.change > 0 ? "up" : r.change < 0 ? "down" : "";
          chg.className = dir;
          chg.textContent = (r.change > 0 ? "+" : r.change < 0 ? "−" : "") + Math.abs(r.change).toFixed(2);
          px.appendChild(chg);
        }

        const pinBtn = document.createElement("button");
        pinBtn.type = "button";
        pinBtn.className = "msearch-pin";
        pinBtn.tabIndex = -1;
        if (r.asset_class !== "equity") {
          pinBtn.textContent = "+ Pin";
          pinBtn.disabled = true;
          pinBtn.title = "The watchlist holds equities for now";
        } else if (pinned.indexOf(r.symbol) !== -1) {
          pinBtn.textContent = "Pinned ✓";
          pinBtn.disabled = true;
        } else {
          pinBtn.textContent = "+ Pin";
          pinBtn.title = "Add to the watchlist (Shift+Enter)";
        }
        // mousedown would blur the input and close the list first.
        pinBtn.addEventListener("mousedown", function (e) { e.preventDefault(); });
        pinBtn.addEventListener("click", function (e) {
          e.stopPropagation();
          pin(r, pinBtn);
        });

        li.appendChild(sym);
        li.appendChild(name);
        li.appendChild(badge);
        li.appendChild(px);
        li.appendChild(pinBtn);
        li.addEventListener("mousedown", function (e) { e.preventDefault(); });
        li.addEventListener("mousemove", function () { if (active !== i) setActive(i); });
        li.addEventListener("click", function () { open(r); });
        list.appendChild(li);
      });

      const hint = document.createElement("li");
      hint.className = "msearch-hint";
      hint.setAttribute("aria-hidden", "true");
      hint.innerHTML = "<span><kbd>↑</kbd> <kbd>↓</kbd> move</span><span><kbd>Enter</kbd> open</span>" +
        "<span><kbd>Shift</kbd>+<kbd>Enter</kbd> pin</span><span><kbd>Esc</kbd> close</span>";
      hint.addEventListener("mousedown", function (e) { e.preventDefault(); });
      list.appendChild(hint);
      setOpen(true);
      setActive(0);
    }

    function fetchResults(q) {
      if (controller) controller.abort();
      controller = new AbortController();
      const mine = ++seq;
      fetch("/search?q=" + encodeURIComponent(q) + "&limit=" + LIMIT, { signal: controller.signal })
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then(function (body) {
          if (mine !== seq || document.activeElement !== input) return;
          results = body;
          query = q;
          render();
        })
        .catch(function (err) {
          if (err.name === "AbortError" || mine !== seq) return;
          message("Search is unavailable right now");
        });
    }

    function cancel() {
      clearTimeout(timer);
      if (controller) controller.abort();
      seq++;
    }

    input.addEventListener("input", function () {
      const q = input.value.trim();
      cancel();
      if (!q) {
        results = [];
        setOpen(false);
        return;
      }
      timer = setTimeout(function () { fetchResults(q); }, DEBOUNCE_MS);
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (list.hidden) {
          if (results.length) render();
          return;
        }
        setActive(active + (e.key === "ArrowDown" ? 1 : -1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (list.hidden || !results.length) return;
        const r = results[active < 0 ? 0 : active];
        if (e.shiftKey) {
          const btn = list.querySelectorAll(".msearch-pin")[active < 0 ? 0 : active];
          if (!btn.disabled) pin(r, btn);
        } else {
          open(r);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        if (!list.hidden) {
          setOpen(false);
        } else {
          cancel();
          input.value = "";
          results = [];
          input.blur();
        }
      }
    });

    input.addEventListener("focus", function () {
      if (input.value.trim() && results.length) render();
    });
    input.addEventListener("blur", function () { setOpen(false); });

    // "/" focuses the search, unless the user is typing somewhere else.
    document.addEventListener("keydown", function (e) {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      e.preventDefault();
      input.focus();
      input.select();
    });

    return { input: input };
  }

  window.MarketSearch = {
    mount: mount,
    readWatchlist: readWatchlist,
    writeWatchlist: writeWatchlist,
    WATCHLIST_MAX: WATCHLIST_MAX,
  };
})();
