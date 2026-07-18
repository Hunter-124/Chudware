/* billet-web /admin — shared, saved, concurrent xterm.js terminals
 *
 * Sessions are named tmux sessions that live on the host: they persist across
 * disconnects/gateway restarts and can be driven by several admins at once.
 * The picker bar lists every shared session; a tab is just this browser's live
 * attachment to one. Closing a tab detaches; the trash pill kills the session.
 *
 * Reserved app keybindings (returned false so they do not reach the PTY):
 *   Ctrl+Alt+T  — new shared session
 *   Ctrl+Alt+W  — detach active tab
 * All other keys (Ctrl+C/D, arrows, Tab, F-keys, Alt-combos, etc.) pass through.
 */
(function () {
  "use strict";

  if (typeof Terminal === "undefined" || typeof FitAddon === "undefined") {
    console.error("[admin_terminal] xterm assets missing");
    return;
  }

  var host = document.getElementById("term-host");
  var tabsEl = document.getElementById("term-tabs");
  var newBtn = document.getElementById("term-new");
  var pickerEl = document.getElementById("term-picker");
  var pickerEmpty = document.getElementById("term-picker-empty");
  if (!host || !tabsEl || !newBtn) return;

  var encoder = new TextEncoder();
  var tabs = []; // { id, name, term, fit, ws, el, tabBtn, label, _closed, _ended, _retries }
  var activeId = null;
  var idSeq = 1;
  var sessionsCache = [];
  var RECONNECT_MAX = 6;

  var THEME = {
    background: "#0b0d12",
    foreground: "#e8eaf0",
    cursor: "#42ecce",
    cursorAccent: "#0b0d12",
    selectionBackground: "rgba(66,236,206,.28)",
    black: "#0b0d12",
    red: "#ff6b7a",
    green: "#42ecce",
    yellow: "#ffcf6b",
    blue: "#6ba8ff",
    magenta: "#c98bff",
    cyan: "#5be0e0",
    white: "#e8eaf0",
    brightBlack: "#5a616e",
    brightRed: "#ff8b96",
    brightGreen: "#6ff0da",
    brightYellow: "#ffdd8b",
    brightBlue: "#8bbcff",
    brightMagenta: "#d6a8ff",
    brightCyan: "#7fecec",
    brightWhite: "#ffffff",
  };

  // ------------------------------------------------------------------ fetch
  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (opts.body && !opts.headers["Content-Type"]) {
      opts.headers["Content-Type"] = "application/json";
    }
    return fetch(path, opts).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          throw new Error(t || r.status);
        });
      }
      var ct = r.headers.get("content-type") || "";
      return ct.indexOf("application/json") >= 0 ? r.json() : r.text();
    });
  }

  function wsUrl(cols, rows, name) {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    return (
      proto +
      "://" +
      location.host +
      "/admin/terminal?cols=" +
      encodeURIComponent(cols) +
      "&rows=" +
      encodeURIComponent(rows) +
      (name ? "&name=" + encodeURIComponent(name) : "")
    );
  }

  function findTab(id) {
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].id === id) return tabs[i];
    }
    return null;
  }

  function findTabByName(name) {
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].name === name) return tabs[i];
    }
    return null;
  }

  function activeTab() {
    return activeId != null ? findTab(activeId) : null;
  }

  // ------------------------------------------------------------------ sizing
  function sendResize(tab) {
    if (!tab || !tab.ws || tab.ws.readyState !== 1) return;
    try {
      tab.ws.send(
        JSON.stringify({ type: "resize", cols: tab.term.cols, rows: tab.term.rows })
      );
    } catch (_) {}
  }

  function fitTab(tab) {
    if (!tab || !tab.fit || !tab.term) return;
    try {
      tab.fit.fit();
    } catch (_) {}
    sendResize(tab);
  }

  function setActive(id) {
    var tab = findTab(id);
    if (!tab) return;
    activeId = id;
    for (var i = 0; i < tabs.length; i++) {
      var t = tabs[i];
      var on = t.id === id;
      t.el.classList.toggle("active", on);
      if (t.tabBtn) t.tabBtn.classList.toggle("active", on);
    }
    requestAnimationFrame(function () {
      fitTab(tab);
      try {
        tab.term.focus();
      } catch (_) {}
    });
  }

  // ------------------------------------------------------------------ tabs
  function closeTab(id) {
    var tab = findTab(id);
    if (!tab) return;
    tab._closed = true;
    var idx = tabs.indexOf(tab);
    try {
      if (tab.ws) {
        tab.ws.onopen = tab.ws.onmessage = tab.ws.onclose = tab.ws.onerror = null;
        if (tab.ws.readyState === 0 || tab.ws.readyState === 1) tab.ws.close();
      }
    } catch (_) {}
    try {
      if (tab.term) tab.term.dispose();
    } catch (_) {}
    if (tab.el && tab.el.parentNode) tab.el.parentNode.removeChild(tab.el);
    if (tab.tabBtn && tab.tabBtn.parentNode) tab.tabBtn.parentNode.removeChild(tab.tabBtn);
    tabs.splice(idx, 1);

    if (activeId === id) {
      activeId = null;
      if (tabs.length) {
        var next = tabs[Math.min(idx, tabs.length - 1)];
        setActive(next.id);
      }
    }
    renderPicker();
  }

  function setTabLabel(tab, name) {
    tab.name = name;
    tab.label = name;
    if (tab.labelSpan) tab.labelSpan.textContent = name;
    if (tab.tabBtn) tab.tabBtn.title = name;
  }

  function attachWs(tab) {
    var term = tab.term;
    var ws;
    try {
      ws = new WebSocket(wsUrl(term.cols, term.rows, tab.name));
    } catch (e) {
      term.writeln("\r\n\x1b[31m[ws connect failed: " + (e.message || e) + "]\x1b[0m");
      return;
    }
    tab.ws = ws;
    ws.binaryType = "arraybuffer";

    ws.onopen = function () {
      tab._retries = 0;
      fitTab(tab);
    };

    ws.onmessage = function (ev) {
      if (ev.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(ev.data));
        return;
      }
      if (typeof ev.data === "string") {
        try {
          var msg = JSON.parse(ev.data);
          if (!msg) return;
          if (msg.type === "session" && msg.name) {
            setTabLabel(tab, msg.name);
            renderPicker();
          } else if (msg.type === "exit") {
            tab._ended = true; // session actually ended — do not resurrect it
            term.writeln(
              "\r\n\x1b[2m[session ended" +
                (msg.code != null ? " code=" + msg.code : "") +
                "]\x1b[0m"
            );
            refreshSessions();
          }
        } catch (_) {}
      }
    };

    ws.onclose = function (ev) {
      if (tab._closed) return;
      if (tab._ended) {
        renderPicker();
        return;
      }
      tab._retries = (tab._retries || 0) + 1;
      if (tab._retries > RECONNECT_MAX) {
        term.writeln(
          "\r\n\x1b[2m[disconnected" +
            (ev && ev.code ? " code=" + ev.code : "") +
            " — press the session pill to rejoin]\x1b[0m"
        );
        return;
      }
      term.writeln("\r\n\x1b[2m[reconnecting… (" + tab._retries + ")]\x1b[0m");
      setTimeout(function () {
        if (!tab._closed && !tab._ended) attachWs(tab);
      }, Math.min(1500 * tab._retries, 6000));
    };

    ws.onerror = function () {
      // onclose will follow; keep noise low
    };

    if (!tab._dataBound) {
      tab._dataBound = true;
      term.onData(function (d) {
        if (tab.ws && tab.ws.readyState === 1) tab.ws.send(encoder.encode(d));
      });
    }
  }

  function isReservedCombo(ev) {
    if (!ev.ctrlKey || !ev.altKey || ev.metaKey || ev.shiftKey) return false;
    var k = ev.key;
    if (typeof k === "string") k = k.toLowerCase();
    return k === "t" || k === "w";
  }

  function newTab(name) {
    var id = "t" + idSeq++;
    var label = name || "connecting…";

    var el = document.createElement("div");
    el.className = "term-session";
    el.dataset.tid = id;
    el.setAttribute("role", "tabpanel");
    host.appendChild(el);

    var tabBtn = document.createElement("button");
    tabBtn.type = "button";
    tabBtn.className = "term-tab";
    tabBtn.setAttribute("role", "tab");
    tabBtn.dataset.tid = id;
    tabBtn.title = label;

    var labelSpan = document.createElement("span");
    labelSpan.className = "term-tab-label";
    labelSpan.textContent = label;
    tabBtn.appendChild(labelSpan);

    var xBtn = document.createElement("span");
    xBtn.className = "term-tab-x";
    xBtn.setAttribute("role", "button");
    xBtn.setAttribute("aria-label", "Detach " + label);
    xBtn.title = "Detach (session keeps running)";
    xBtn.textContent = "\u00d7";
    tabBtn.appendChild(xBtn);

    tabsEl.insertBefore(tabBtn, newBtn);

    tabBtn.addEventListener("click", function (ev) {
      if (ev.target === xBtn || xBtn.contains(ev.target)) {
        ev.preventDefault();
        ev.stopPropagation();
        closeTab(id);
        return;
      }
      setActive(id);
    });

    var term = new Terminal({
      cursorBlink: true,
      fontFamily: "ui-monospace, Menlo, Consolas, monospace",
      fontSize: 13,
      scrollback: 5000,
      allowProposedApi: true,
      theme: THEME,
      convertEol: false,
    });

    var fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(el);

    term.attachCustomKeyEventHandler(function (ev) {
      if (ev.type !== "keydown") return true;
      if (!isReservedCombo(ev)) return true;
      ev.preventDefault();
      var k = (ev.key || "").toLowerCase();
      if (k === "t") {
        newSession();
      } else if (k === "w") {
        var cur = activeTab();
        if (cur) closeTab(cur.id);
      }
      return false;
    });

    var tab = {
      id: id,
      name: name || null,
      term: term,
      fit: fit,
      ws: null,
      el: el,
      tabBtn: tabBtn,
      labelSpan: labelSpan,
      label: label,
      _closed: false,
      _ended: false,
      _retries: 0,
      _dataBound: false,
    };
    tabs.push(tab);

    try {
      fit.fit();
    } catch (_) {}

    attachWs(tab);
    setActive(id);
    renderPicker();
    return tab;
  }

  // --------------------------------------------------------------- sessions
  function openSession(name) {
    var existing = findTabByName(name);
    if (existing) {
      setActive(existing.id);
      return existing;
    }
    return newTab(name);
  }

  function newSession() {
    api("/api/admin/terminal/sessions", { method: "POST", body: "{}" })
      .then(function (r) {
        if (r && r.name) openSession(r.name);
        refreshSessions();
      })
      .catch(function (e) {
        // No tmux on host → fall back to a plain (private) attach tab.
        console.warn("[admin_terminal] create failed, opening private tab:", e.message || e);
        newTab(null);
      });
  }

  function killSession(name) {
    if (!confirm('Kill shared session "' + name + '"? Everyone attached loses it and unsaved work is gone.')) {
      return;
    }
    var tab = findTabByName(name);
    if (tab) {
      tab._ended = true;
      closeTab(tab.id);
    }
    api("/api/admin/terminal/sessions/" + encodeURIComponent(name), { method: "DELETE" })
      .catch(function () {})
      .then(function () {
        refreshSessions();
      });
  }

  function renderPicker() {
    if (!pickerEl) return;
    // Drop everything except the fixed label.
    Array.prototype.slice.call(pickerEl.querySelectorAll(".term-pill, .term-picker-empty"))
      .forEach(function (n) {
        n.parentNode.removeChild(n);
      });

    var names = {};
    sessionsCache.forEach(function (s) {
      names[s.name] = true;
      var pill = document.createElement("button");
      pill.type = "button";
      pill.className = "term-pill";
      if (findTabByName(s.name)) pill.classList.add("open");
      pill.title = "Attach to " + s.name;

      var nm = document.createElement("span");
      nm.textContent = s.name;
      pill.appendChild(nm);

      var clients = document.createElement("span");
      clients.className = "term-pill-clients" + (s.clients > 0 ? " live" : "");
      clients.textContent = s.clients > 0 ? "\u25cf " + s.clients : "idle";
      clients.title = s.clients + " watching";
      pill.appendChild(clients);

      var kill = document.createElement("span");
      kill.className = "term-pill-kill";
      kill.setAttribute("role", "button");
      kill.title = "Kill session";
      kill.textContent = "\u00d7";
      pill.appendChild(kill);

      pill.addEventListener("click", function (ev) {
        if (ev.target === kill || kill.contains(ev.target)) {
          ev.preventDefault();
          ev.stopPropagation();
          killSession(s.name);
          return;
        }
        openSession(s.name);
      });
      pickerEl.appendChild(pill);
    });

    // Tabs attached to sessions the server no longer lists (private/ephemeral
    // fallback tabs) still deserve a pill so they read as "open".
    tabs.forEach(function (t) {
      if (t.name && !names[t.name]) {
        var pill = document.createElement("button");
        pill.type = "button";
        pill.className = "term-pill open";
        pill.textContent = t.name;
        pill.title = "Focus " + t.name;
        pill.addEventListener("click", function () {
          setActive(t.id);
        });
        pickerEl.appendChild(pill);
      }
    });

    var addPill = document.createElement("button");
    addPill.type = "button";
    addPill.className = "term-pill term-pill-new";
    addPill.textContent = "+ New session";
    addPill.title = "Start a new shared session (Ctrl+Alt+T)";
    addPill.addEventListener("click", newSession);
    pickerEl.appendChild(addPill);

    if (!sessionsCache.length && pickerEmpty) {
      var empty = document.createElement("span");
      empty.className = "term-picker-empty";
      empty.textContent = "no shared sessions yet";
      pickerEl.appendChild(empty);
    }
  }

  function refreshSessions() {
    return api("/api/admin/terminal/sessions")
      .then(function (data) {
        sessionsCache = (data && data.sessions) || [];
        renderPicker();
        return sessionsCache;
      })
      .catch(function () {
        return sessionsCache;
      });
  }

  // ------------------------------------------------------------------ wiring
  window.addEventListener(
    "keydown",
    function (ev) {
      if (!isReservedCombo(ev)) return;
      if (ev.defaultPrevented) return;
      ev.preventDefault();
      var k = (ev.key || "").toLowerCase();
      if (k === "t") {
        newSession();
      } else if (k === "w") {
        var cur = activeTab();
        if (cur) closeTab(cur.id);
      }
    },
    true
  );

  newBtn.addEventListener("click", function () {
    newSession();
  });

  var resizeTimer = null;
  function scheduleFit() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      resizeTimer = null;
      var tab = activeTab();
      if (tab) fitTab(tab);
    }, 80);
  }
  window.addEventListener("resize", scheduleFit);
  if (typeof ResizeObserver !== "undefined") {
    try {
      new ResizeObserver(scheduleFit).observe(host);
    } catch (_) {}
  }

  // Poll the shared-session list so client counts / new sessions stay current.
  setInterval(refreshSessions, 5000);

  // On load: join the most-recently-active session if any, else start one.
  refreshSessions().then(function (list) {
    if (list && list.length) {
      var latest = list.slice().sort(function (a, b) {
        return (b.activity || 0) - (a.activity || 0);
      })[0];
      openSession(latest.name);
    } else {
      newSession();
    }
  });
})();
