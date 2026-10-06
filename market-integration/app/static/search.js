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
 * /bond/{symbol}. The best-matching equity is followed by a row for its
 * Description page (/stock/{symbol}#description).
 *
 * Filters: focusing the box opens a panel with two pills, Sector and
 * Movers (Tab reaches them), each opening a menu. Sector narrows the
 * results, and with an empty box lists the whole sector. Movers
 * switches the results to today's top gainers, top losers or most
 * active equities (GET /movers), in the chosen sector if one is set;
 * typing then searches within that list.
 *
 * "+ Pin" adds an equity to the watchlist: the /ticker page's open
 * views, kept on the server per user (GET and PUT /watchlists/me, #26),
 * so a pin from any page shows up there and survives reloads and server
 * restarts. `onPin(symbol)` lets a page handle it itself (the ticker
 * opens the view at once); it returns a message to show (or a promise of
 * one), or nothing for "Pinned". The watchlist only renders equities for
 * now, so bills and bonds can't be pinned.
 *
 * The watchlist API for pages:
 *
 *   MarketSearch.loadWatchlist()      -> Promise of {symbols, max_pins, is_default}
 *   MarketSearch.readWatchlist()      -> its symbols, once loaded (else null)
 *   MarketSearch.saveWatchlist(list)  -> Promise; replaces the whole list
 */
(function () {
  "use strict";

  const DEBOUNCE_MS = 150;
  const LIMIT = 10;
  const BROWSE_LIMIT = 50;  // a whole sector, with nothing typed
  // Where pins were kept before they moved to the server; read once to
  // carry an existing list over, then cleared.
  const LEGACY_WATCHLIST_KEY = "watchlist";
  const BADGE = { equity: "Equity", bill: "Bill", bond: "Bond", description: "DES" };
  const MOVERS = [
    { key: "gainers", label: "Top gainers", none: "No gainers" },
    { key: "losers", label: "Top losers", none: "No losers" },
    { key: "active", label: "Most active", none: "Nothing traded" },
  ];

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
  .msearch-panel {
    position: absolute; z-index: 20; left: 0; top: calc(100% + 6px); width: max(100%, 600px);
    padding: 4px; background: var(--panel-strong); border: 1px solid var(--line); border-radius: 10px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
  }
  .msearch-panel[hidden] { display: none; }
  .msearch-filters { display: flex; gap: 6px; padding: 4px 4px 8px; border-bottom: 1px solid var(--line); margin-bottom: 4px; }
  .msearch-filter { position: relative; }
  .msearch-pill {
    font: 600 12px -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--ink-soft);
    background: var(--panel); border: 1px solid var(--line); border-radius: 999px;
    padding: 5px 11px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
  }
  .msearch-pill::after { content: ""; border: 4px solid transparent; border-top-color: currentColor; margin-top: 4px; }
  .msearch-pill:hover { color: var(--ink); border-color: var(--ink-faint); }
  .msearch-pill:focus-visible { outline: 2px solid var(--teal); outline-offset: 1px; }
  .msearch-pill.set { color: var(--bg); background: var(--teal); border-color: var(--teal); }
  .msearch-menu {
    position: absolute; z-index: 1; left: 0; top: calc(100% + 4px); min-width: 210px;
    max-height: 300px; overflow-y: auto; padding: 4px;
    background: var(--panel-strong); border: 1px solid var(--line); border-radius: 9px;
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.5);
  }
  .msearch-menu[hidden] { display: none; }
  .msearch-item {
    display: flex; justify-content: space-between; gap: 12px; width: 100%;
    font: 12.5px -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--ink-soft); text-align: left;
    background: transparent; border: 0; border-radius: 6px; padding: 7px 9px; cursor: pointer;
  }
  .msearch-item:hover, .msearch-item:focus { color: var(--ink); background: var(--panel); outline: none; }
  .msearch-item[aria-checked="true"] { color: var(--teal); font-weight: 600; }
  .msearch-item .count { font: 11px ui-monospace, monospace; color: var(--ink-faint); }
  .msearch-list {
    margin: 0; padding: 0; list-style: none; max-height: 400px; overflow-y: auto;
  }
  .msearch-list, .msearch-menu { scrollbar-width: thin; scrollbar-color: var(--ink-faint) transparent; }
  .msearch-list::-webkit-scrollbar, .msearch-menu::-webkit-scrollbar { width: 6px; }
  .msearch-list::-webkit-scrollbar-thumb, .msearch-menu::-webkit-scrollbar-thumb { background: var(--ink-faint); border-radius: 3px; }
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
  .msearch-badge.description { color: var(--teal); background: rgba(62, 156, 130, 0.12); }
  .msearch-opt.description .msearch-sym { color: var(--ink-soft); font-weight: 500; }
  .msearch-opt.description .msearch-pin { visibility: hidden; }
  .msearch-px { font: 12px ui-monospace, monospace; font-variant-numeric: tabular-nums; color: var(--ink); text-align: right; }
  .msearch-px small { display: block; font-size: 10.5px; color: var(--ink-faint); }
  .msearch-px small.up { color: var(--green); }
  .msearch-px small.down { color: var(--red); }
  .msearch-pin {
    font: 600 11px -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--teal);
    background: transparent; border: 1px solid var(--line); border-radius: 6px;
    padding: 4px 8px; cursor: pointer; white-space: nowrap; min-width: 74px;
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
    .msearch-panel { width: 100%; }
    .msearch-opt { grid-template-columns: auto minmax(0, 1fr) auto auto; }
    .msearch-px { display: none; }
  }`;

  // ------------------------------------------------------------ watchlist

  // The user's watchlist lives on the server; this is the page's copy.
  let watchlist = null;     // the last GET/PUT /watchlists/me body
  let loadingWatchlist = null;

  function watchlistRequest(method, body) {
    return fetch("/watchlists/me", {
      method: method,
      credentials: "same-origin",   // the anonymous-ID cookie
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (res) {
      return res.json().then(function (json) {
        if (!res.ok) {
          const err = new Error((json.detail && json.detail.message) || "HTTP " + res.status);
          err.symbols = (json.detail && json.detail.symbols) || [];   // the ones the server refused
          throw err;
        }
        watchlist = json;
        return json;
      });
    });
  }

  // Loads once per page (later calls share the result). A user still on
  // the default who has a list from before pins moved to the server gets
  // it carried over.
  function loadWatchlist() {
    if (!loadingWatchlist) {
      loadingWatchlist = watchlistRequest("GET").then(migrateLegacy).catch(function (err) {
        loadingWatchlist = null;    // retry on the next call
        throw err;
      });
    }
    return loadingWatchlist;
  }

  function migrateLegacy(current) {
    let legacy = null;
    try { legacy = JSON.parse(localStorage.getItem(LEGACY_WATCHLIST_KEY) || "null"); } catch (e) { /* storage blocked */ }
    if (!Array.isArray(legacy)) return current;
    const done = function () {
      try { localStorage.removeItem(LEGACY_WATCHLIST_KEY); } catch (e) { /* storage blocked */ }
      return watchlist;
    };
    if (!current.is_default) return done();
    const symbols = legacy.filter(function (s) { return typeof s === "string"; }).slice(0, current.max_pins);
    // A symbol since dropped from the instrument master would sink the
    // whole list; retry once without the ones the server names.
    return watchlistRequest("PUT", { symbols: symbols }).catch(function (err) {
      if (!err.symbols || !err.symbols.length) throw err;
      return watchlistRequest("PUT", {
        symbols: symbols.filter(function (s) { return err.symbols.indexOf(s.trim().toUpperCase()) === -1; }),
      });
    }).then(done, done);
  }

  function readWatchlist() {
    return watchlist ? watchlist.symbols.slice() : null;
  }

  // Replaces the whole list, in order. Rejects with the server's reason
  // (unknown symbol, too many pins).
  function saveWatchlist(symbols) {
    return watchlistRequest("PUT", { symbols: symbols });
  }

  // Pin from a page with no watchlist of its own: append to the list the
  // ticker opens with.
  function pinToServer(symbol) {
    return loadWatchlist().then(function (current) {
      if (current.symbols.indexOf(symbol) !== -1) return "Already pinned";
      if (current.symbols.length >= current.max_pins) return "Watchlist full";
      return saveWatchlist(current.symbols.concat([symbol])).then(function () { return "Pinned"; });
    }).catch(function () { return "Couldn't pin"; });
  }

  // ------------------------------------------------------------ component

  function detailUrl(r) {
    if (r.asset_class === "description") return "/stock/" + encodeURIComponent(r.symbol) + "#description";
    return (r.asset_class === "equity" ? "/stock/" : "/bond/") + encodeURIComponent(r.symbol);
  }

  // A Description row right after the best-matching equity, so a search
  // for a company also offers its profile page.
  function withDescription(rows) {
    const i = rows.findIndex(function (r) { return r.asset_class === "equity"; });
    if (i === -1) return rows;
    const des = {
      symbol: rows[i].symbol,
      name: "Description · profile, management, dividends",
      asset_class: "description",
      price: null,
    };
    return rows.slice(0, i + 1).concat([des], rows.slice(i + 1));
  }

  function fmtPrice(r) {
    if (r.price == null) return "—";
    const n = Number(r.price).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return r.asset_class === "equity" ? "GH₵" + n : n;
  }

  function signed(n, digits) {
    return (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n).toFixed(digits);
  }

  function moverOf(key) {
    return MOVERS.filter(function (m) { return m.key === key; })[0] || null;
  }

  function mount(root, options) {
    options = options || {};
    // So results can show what's already pinned.
    loadWatchlist().catch(function () { /* pins just won't show as pinned */ });
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
      '<div class="msearch-panel" hidden>' +
      '  <div class="msearch-filters" role="group" aria-label="Filters">' +
      '    <div class="msearch-filter"><button type="button" class="msearch-pill" data-filter="sector" aria-haspopup="menu" aria-expanded="false"></button>' +
      '      <div class="msearch-menu" role="menu" aria-label="Sector" hidden></div></div>' +
      '    <div class="msearch-filter"><button type="button" class="msearch-pill" data-filter="mover" aria-haspopup="menu" aria-expanded="false"></button>' +
      '      <div class="msearch-menu" role="menu" aria-label="Movers" hidden></div></div>' +
      '  </div>' +
      '  <ul class="msearch-list" id="' + listId + '" role="listbox" aria-label="Search results"></ul>' +
      '</div>';

    const input = root.querySelector(".msearch-input");
    const panel = root.querySelector(".msearch-panel");
    const list = root.querySelector(".msearch-list");
    const pills = {
      sector: root.querySelector('[data-filter="sector"]'),
      mover: root.querySelector('[data-filter="mover"]'),
    };

    const filters = { sector: null, mover: null };
    let sectors = null;     // [{sector, count}] once /sectors has loaded
    let results = [];
    let active = -1;
    let timer = null;
    let controller = null;
    let seq = 0;

    // ---------------------------------------------------------- panel

    function setOpen(open) {
      panel.hidden = !open;
      input.setAttribute("aria-expanded", open ? "true" : "false");
      if (!open) {
        closeMenus();
        input.removeAttribute("aria-activedescendant");
        return;
      }
      // The panel can be wider than the input; keep it on screen.
      panel.style.left = "0px";
      const over = panel.getBoundingClientRect().right - (document.documentElement.clientWidth - 12);
      if (over > 0) panel.style.left = -over + "px";
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
      active = -1;
      input.removeAttribute("aria-activedescendant");
      const li = document.createElement("li");
      li.className = "msearch-empty";
      li.setAttribute("role", "option");
      li.setAttribute("aria-disabled", "true");
      li.textContent = text;
      list.appendChild(li);
    }

    function emptyText(q) {
      const inSector = filters.sector ? " in " + filters.sector : "";
      const mover = moverOf(filters.mover);
      if (!q) return mover ? mover.none + inSector + " today" : "Nothing listed" + inSector;
      return "No matches for “" + q + "”" + inSector + (mover ? " among " + mover.label.toLowerCase() : "");
    }

    function pin(r, button) {
      if (r.asset_class !== "equity") return;
      if (button) button.disabled = true;
      Promise.resolve(options.onPin ? options.onPin(r.symbol) : pinToServer(r.symbol)).then(function (note) {
        note = note || "Pinned";
        if (button) button.textContent = note === "Pinned" ? "Pinned ✓" : note;
      });
    }

    function open(r) {
      location.href = detailUrl(r);
    }

    function render(q) {
      list.innerHTML = "";
      active = -1;
      if (!results.length) {
        message(emptyText(q));
        return;
      }
      const pinned = readWatchlist() || [];
      results.forEach(function (r, i) {
        const li = document.createElement("li");
        li.className = "msearch-opt" + (r.asset_class === "description" ? " description" : "");
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

        // Price, and under it what the list is about: today's volume for
        // most active, % change for gainers/losers, else the change.
        const px = document.createElement("span");
        px.className = "msearch-px";
        px.textContent = r.asset_class === "description" ? "" : fmtPrice(r);
        const sub = document.createElement("small");
        if (filters.mover === "active" && r.volume != null) {
          sub.textContent = Number(r.volume).toLocaleString() + " sh";
        } else if (r.change_percent != null) {
          sub.className = r.change_percent > 0 ? "up" : r.change_percent < 0 ? "down" : "";
          sub.textContent = signed(r.change_percent, 2) + "%";
        } else if (r.change != null && r.price != null) {
          sub.className = r.change > 0 ? "up" : r.change < 0 ? "down" : "";
          sub.textContent = signed(r.change, 2);
        }
        if (sub.textContent) px.appendChild(sub);

        const pinBtn = document.createElement("button");
        pinBtn.type = "button";
        pinBtn.className = "msearch-pin";
        pinBtn.tabIndex = -1;
        if (r.asset_class === "description") {
          pinBtn.textContent = "+ Pin";   // hidden; keeps the columns and Shift+Enter's indexing
          pinBtn.disabled = true;
        } else if (r.asset_class !== "equity") {
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
        // mousedown would take focus from the input first.
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
        "<span><kbd>Shift</kbd>+<kbd>Enter</kbd> pin</span><span><kbd>Tab</kbd> filters</span>" +
        "<span><kbd>Esc</kbd> close</span>";
      hint.addEventListener("mousedown", function (e) { e.preventDefault(); });
      list.appendChild(hint);
      setActive(0);
    }

    // ---------------------------------------------------------- fetching

    function requestUrl(q) {
      const params = new URLSearchParams();
      if (filters.mover) params.set("type", filters.mover);
      if (q) params.set("q", q);
      if (filters.sector) params.set("sector", filters.sector);
      params.set("limit", String(q || filters.mover ? LIMIT : BROWSE_LIMIT));
      return (filters.mover ? "/movers?" : "/search?") + params.toString();
    }

    function cancel() {
      clearTimeout(timer);
      if (controller) controller.abort();
      seq++;
    }

    // Fetch what the box and the filters currently ask for.
    function load() {
      cancel();
      const q = input.value.trim();
      if (!q && !filters.sector && !filters.mover) {
        results = [];
        message("Type to search, or pick a sector or a movers list.");
        return;
      }
      controller = new AbortController();
      const mine = seq;
      fetch(requestUrl(q), { signal: controller.signal })
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then(function (body) {
          if (mine !== seq) return;
          results = filters.mover ? body : withDescription(body);
          render(q);
        })
        .catch(function (err) {
          if (err.name === "AbortError" || mine !== seq) return;
          results = [];
          message("Search is unavailable right now");
        });
    }

    // ---------------------------------------------------------- filters

    function renderPills() {
      const mover = moverOf(filters.mover);
      pills.sector.textContent = filters.sector || "Sector";
      pills.sector.classList.toggle("set", !!filters.sector);
      pills.sector.setAttribute("aria-label", "Sector filter: " + (filters.sector || "all sectors"));
      pills.mover.textContent = mover ? mover.label : "Movers";
      pills.mover.classList.toggle("set", !!mover);
      pills.mover.setAttribute("aria-label", "Movers: " + (mover ? mover.label : "off"));
    }

    function menuOf(pill) { return pill.nextElementSibling; }

    function closeMenus() {
      Object.keys(pills).forEach(function (k) {
        menuOf(pills[k]).hidden = true;
        pills[k].setAttribute("aria-expanded", "false");
      });
    }

    function menuItems(kind) {
      if (kind === "mover") {
        return [{ value: null, label: "Off" }].concat(MOVERS.map(function (m) {
          return { value: m.key, label: m.label };
        }));
      }
      return [{ value: null, label: "All sectors" }].concat((sectors || []).map(function (s) {
        return { value: s.sector, label: s.sector, count: s.count };
      }));
    }

    function buildMenu(kind) {
      const menu = menuOf(pills[kind]);
      menu.innerHTML = "";
      menuItems(kind).forEach(function (item) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "msearch-item";
        b.setAttribute("role", "menuitemradio");
        b.setAttribute("aria-checked", filters[kind] === item.value ? "true" : "false");
        b.tabIndex = -1;
        b.textContent = item.label;
        if (item.count != null) {
          const c = document.createElement("span");
          c.className = "count";
          c.textContent = item.count;
          b.appendChild(c);
        }
        b.addEventListener("click", function () { choose(kind, item.value); });
        menu.appendChild(b);
      });
      if (kind === "sector" && sectors === null) {
        const note = document.createElement("div");
        note.className = "msearch-empty";
        note.textContent = "Loading sectors…";
        menu.appendChild(note);
      }
    }

    function openMenu(kind) {
      closeMenus();
      const pill = pills[kind];
      buildMenu(kind);
      menuOf(pill).hidden = false;
      pill.setAttribute("aria-expanded", "true");
      const items = menuOf(pill).querySelectorAll(".msearch-item");
      const checked = menuOf(pill).querySelector('[aria-checked="true"]') || items[0];
      checked.focus();
      if (kind === "sector" && sectors === null) loadSectors();
    }

    function loadSectors() {
      fetch("/sectors")
        .then(function (res) { return res.ok ? res.json() : Promise.reject(new Error("HTTP " + res.status)); })
        .then(function (body) {
          sectors = body;
          if (!menuOf(pills.sector).hidden) openMenu("sector");
        })
        .catch(function () { /* the menu keeps "All sectors"; retried on next open */ });
    }

    function choose(kind, value) {
      filters[kind] = value;
      closeMenus();
      renderPills();
      input.focus();
      load();
    }

    Object.keys(pills).forEach(function (kind) {
      const pill = pills[kind];
      const menu = menuOf(pill);
      pill.addEventListener("click", function () {
        if (menu.hidden) openMenu(kind); else closeMenus();
      });
      pill.addEventListener("keydown", function (e) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          openMenu(kind);
        } else if (e.key === "Escape") {
          e.preventDefault();
          input.focus();
        }
      });
      menu.addEventListener("keydown", function (e) {
        const items = Array.prototype.slice.call(menu.querySelectorAll(".msearch-item"));
        const i = items.indexOf(document.activeElement);
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          const next = (i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
          items[next].focus();
        } else if (e.key === "Home" || e.key === "End") {
          e.preventDefault();
          items[e.key === "Home" ? 0 : items.length - 1].focus();
        } else if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          closeMenus();
          pill.focus();
        } else if (e.key === "Tab") {
          closeMenus();
        }
      });
    });

    // ---------------------------------------------------------- input

    input.addEventListener("input", function () {
      cancel();
      timer = setTimeout(load, DEBOUNCE_MS);
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (panel.hidden) {
          setOpen(true);
          load();
          return;
        }
        setActive(active + (e.key === "ArrowDown" ? 1 : -1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (panel.hidden || !results.length) return;
        const r = results[active < 0 ? 0 : active];
        if (e.shiftKey) {
          const btn = list.querySelectorAll(".msearch-pin")[active < 0 ? 0 : active];
          if (!btn.disabled) pin(r, btn);
        } else {
          open(r);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        if (!panel.hidden) {
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
      if (sectors === null) loadSectors();
      if (panel.hidden) {
        setOpen(true);
        load();
      }
    });

    // Close once focus has left the whole search (input, pills, menus).
    // Checked a tick later: rebuilding an open menu removes its focused
    // item, which reports focus leaving before it lands on the new one.
    root.addEventListener("focusout", function () {
      setTimeout(function () {
        if (!root.contains(document.activeElement)) setOpen(false);
      }, 0);
    });

    // A click elsewhere in the panel closes an open menu, and keeps
    // focus in the input rather than dropping it (which would close all).
    panel.addEventListener("mousedown", function (e) {
      if (e.target.closest(".msearch-filter")) return;
      e.preventDefault();
      closeMenus();
    });

    // "/" focuses the search, unless the user is typing somewhere else.
    document.addEventListener("keydown", function (e) {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      e.preventDefault();
      input.focus();
      input.select();
    });

    renderPills();
    return { input: input };
  }

  window.MarketSearch = {
    mount: mount,
    loadWatchlist: loadWatchlist,
    readWatchlist: readWatchlist,
    saveWatchlist: saveWatchlist,
  };
})();
