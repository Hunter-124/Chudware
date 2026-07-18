/* billet-web /admin — tabbed xterm.js terminal client
 *
 * Reserved app keybindings (returned false so they do not reach the PTY):
 *   Ctrl+Alt+T  — new tab
 *   Ctrl+Alt+W  — close active tab
 * All other keys (Ctrl+C/D, arrows, Tab, F-keys, Alt-combos, etc.) pass through to omp.
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
  if (!host || !tabsEl || !newBtn) return;

  var encoder = new TextEncoder();
  var tabs = []; // { id, n, term, fit, ws, el, tabBtn, label }
  var activeId = null;
  var nextN = 1;
  var idSeq = 1;

  var THEME = {
    background: "#0b0d12",
    foreground: "#e8eef5",
    cursor: "#42ecce",
    cursorAccent: "#0b0d12",
    selectionBackground: "rgba(66, 236, 206, 0.28)",
    black: "#0b0d12",
    red: "#ff6b7a",
    green: "#3dd68c",
    yellow: "#f0b429",
    blue: "#6aa8ff",
    magenta: "#c792ea",
    cyan: "#30d8be",
    white: "#e8eef5",
    brightBlack: "#6d788c",
    brightRed: "#ff8a96",
    brightGreen: "#5ee0a0",
    brightYellow: "#f5c65a",
    brightBlue: "#8bbcff",
    brightMagenta: "#d4a8f0",
    brightCyan: "#42ecce",
    brightWhite: "#ffffff",
  };

  function wsUrl(cols, rows) {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    return (
      proto +
      "://" +
      location.host +
      "/admin/terminal?cols=" +
      encodeURIComponent(cols) +
      "&rows=" +
      encodeURIComponent(rows)
    );
  }

  function findTab(id) {
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].id === id) return tabs[i];
    }
    return null;
  }

  function activeTab() {
    return activeId != null ? findTab(activeId) : null;
  }

  function sendResize(tab) {
    if (!tab || !tab.ws || tab.ws.readyState !== 1) return;
    try {
      tab.ws.send(
        JSON.stringify({
          type: "resize",
          cols: tab.term.cols,
          rows: tab.term.rows,
        })
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
    // Fit after layout paints so dimensions are real.
    requestAnimationFrame(function () {
      fitTab(tab);
      try {
        tab.term.focus();
      } catch (_) {}
    });
  }

  function closeTab(id) {
    var tab = findTab(id);
    if (!tab) return;
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
  }

  function attachWs(tab) {
    var term = tab.term;
    var url = wsUrl(term.cols, term.rows);
    var ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      term.writeln("\r\n\x1b[31m[ws connect failed: " + (e.message || e) + "]\x1b[0m");
      return;
    }
    tab.ws = ws;
    ws.binaryType = "arraybuffer";

    ws.onopen = function () {
      // Re-fit in case the container settled after open.
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
          if (msg && msg.type === "exit") {
            term.writeln(
              "\r\n\x1b[2m[process exited" +
                (msg.code != null ? " code=" + msg.code : "") +
                "]\x1b[0m"
            );
          }
        } catch (_) {
          // ignore non-JSON text frames
        }
      }
    };

    ws.onclose = function (ev) {
      if (tab._closed) return;
      term.writeln(
        "\r\n\x1b[2m[disconnected" +
          (ev && ev.code ? " code=" + ev.code : "") +
          "]\x1b[0m"
      );
    };

    ws.onerror = function () {
      // onclose will follow; keep noise low
    };

    term.onData(function (d) {
      if (ws.readyState === 1) {
        ws.send(encoder.encode(d));
      }
    });
  }

  function isReservedCombo(ev) {
    // Ctrl+Alt+T = new tab; Ctrl+Alt+W = close tab
    if (!ev.ctrlKey || !ev.altKey || ev.metaKey || ev.shiftKey) return false;
    var k = ev.key;
    if (typeof k === "string") k = k.toLowerCase();
    return k === "t" || k === "w";
  }

  function newTab() {
    var id = "t" + idSeq++;
    var n = nextN++;
    var label = "omp " + n;

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
    xBtn.setAttribute("aria-label", "Close " + label);
    xBtn.title = "Close";
    xBtn.textContent = "\u00d7";
    tabBtn.appendChild(xBtn);

    // Insert before the + button
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
      // Only intercept keydown for reserved combos; let everything else through.
      if (ev.type !== "keydown") return true;
      if (!isReservedCombo(ev)) return true;
      ev.preventDefault();
      var k = (ev.key || "").toLowerCase();
      if (k === "t") {
        newTab();
      } else if (k === "w") {
        var cur = activeTab();
        if (cur) closeTab(cur.id);
      }
      return false;
    });

    var tab = {
      id: id,
      n: n,
      term: term,
      fit: fit,
      ws: null,
      el: el,
      tabBtn: tabBtn,
      label: label,
      _closed: false,
    };
    tabs.push(tab);

    // Initial fit before connecting so cols/rows are sane.
    try {
      fit.fit();
    } catch (_) {}

    attachWs(tab);
    setActive(id);
    return tab;
  }

  // Global key handler as a safety net when focus is outside xterm
  // (e.g. on the tab bar). Still only the reserved combos.
  window.addEventListener(
    "keydown",
    function (ev) {
      if (!isReservedCombo(ev)) return;
      // If xterm already handled it via attachCustomKeyEventHandler, this still
      // fires but we de-dupe by checking defaultPrevented.
      if (ev.defaultPrevented) return;
      ev.preventDefault();
      var k = (ev.key || "").toLowerCase();
      if (k === "t") newTab();
      else if (k === "w") {
        var cur = activeTab();
        if (cur) closeTab(cur.id);
      }
    },
    true
  );

  newBtn.addEventListener("click", function () {
    newTab();
  });

  // Resize handling: window + host size changes.
  var resizeTimer = null;
  function scheduleFit() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      resizeTimer = null;
      var tab = activeTab();
      if (tab) fitTab(tab);
    }, 50);
  }

  window.addEventListener("resize", scheduleFit);

  if (typeof ResizeObserver !== "undefined") {
    var ro = new ResizeObserver(function () {
      scheduleFit();
    });
    ro.observe(host);
  }

  // Open the first tab on load.
  newTab();
})();
