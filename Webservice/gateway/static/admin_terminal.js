/* chudware-web /admin — shared, saved, concurrent xterm.js terminals
 *
 * Sessions are named tmux sessions that live on the host: they persist across
 * disconnects/gateway restarts and can be driven by several admins at once.
 * The picker bar lists every shared session; a tab/window is just this browser's
 * live attachment to one. Closing a tab/window detaches; the trash pill kills
 * the session.
 *
 * Desktop (>720px): floating/dockable multi-window manager via WinBox.
 * Mobile  (<=720px): tabbed single-active view (original UX).
 *
 * Reserved app keybindings (returned false so they do not reach the PTY):
 *   Ctrl+Alt+T  — new shared session
 *   Ctrl+Alt+W  — detach focused window / active tab
 * All other keys (Ctrl+C/D, arrows, Tab, F-keys, Alt-combos, etc.) pass through.
 * Alt+Arrow is re-encoded to \x1b[1;3<A-D> before send: xterm.js otherwise
 * remaps it to Ctrl+Arrow (non-Mac) / word-nav (Mac), hiding Chudcode's Alt chords.
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

  // Contract line icons (paths verbatim); decorative — the host control's
  // aria-label or title carries the meaning.
  function lineIcon(path) {
    return "<svg class='icon' viewBox='0 0 24 24' aria-hidden='true' fill='none'" +
           " stroke='currentColor' stroke-width='1.7' stroke-linecap='round'" +
           " stroke-linejoin='round'>" + path + "</svg>";
  }
  var ICON_CLOSE = lineIcon("<path d='M6.4 6.4l11.2 11.2M17.6 6.4 6.4 17.6'/>");
  var ICON_PLUS = lineIcon("<path d='M12 5v14M5 12h14'/>");

  // Parent scaffolds #wm-root + #snap-preview; guard-create if missing.
  var termPane = host.parentNode;
  var wmRoot = document.getElementById("wm-root");
  if (!wmRoot && termPane) {
    wmRoot = document.createElement("div");
    wmRoot.id = "wm-root";
    termPane.appendChild(wmRoot);
  }
  var snapPreview = document.getElementById("snap-preview");
  if (!snapPreview && wmRoot) {
    snapPreview = document.createElement("div");
    snapPreview.id = "snap-preview";
    snapPreview.hidden = true;
    wmRoot.appendChild(snapPreview);
  }

  var encoder = new TextEncoder();
  var tabs = []; // { id, name, term, fit, ws, el, tabBtn, label, wb, _closed, ... }
  var activeId = null;
  var focusedId = null; // keyboard-focus owner (desktop Ctrl+Alt+W)
  var idSeq = 1;
  var sessionsCache = [];
  var RECONNECT_MAX = 6;
  var DESKTOP_MQ = "(max-width:720px)";
  var SNAP_EDGE = 28;
  var cascadeN = 0;

  // Global pointer for snap-zone math while WinBox is dragging (body.wb-lock).
  var pointer = { x: 0, y: 0 };
  var snapState = { zone: null, tab: null, armed: false }; // title-drag snap only

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

  // ------------------------------------------------------------------ mode
  function isDesktop() {
    return !window.matchMedia(DESKTOP_MQ).matches;
  }

  function applyModeClass() {
    document.documentElement.classList.toggle("wm-desktop", isDesktop());
    document.documentElement.classList.toggle("wm-mobile", !isDesktop());
  }

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

  function findTabByWb(wb) {
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].wb === wb) return tabs[i];
    }
    return null;
  }

  function activeTab() {
    return activeId != null ? findTab(activeId) : null;
  }

  function focusedTab() {
    if (focusedId != null) {
      var t = findTab(focusedId);
      if (t) return t;
    }
    return activeTab();
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

  function fit(tab) {
    if (!tab || !tab.fit || !tab.term) return;
    try {
      tab.fit.fit();
    } catch (_) {}
    sendResize(tab);
  }

  function fitTab(tab) {
    fit(tab);
  }

  function fitAll() {
    for (var i = 0; i < tabs.length; i++) {
      var t = tabs[i];
      if (isDesktop()) {
        if (t.wb) fit(t);
      } else if (t.id === activeId) {
        fit(t);
      }
    }
  }

  function setActive(id) {
    var tab = findTab(id);
    if (!tab) return;
    activeId = id;
    focusedId = id;
    for (var i = 0; i < tabs.length; i++) {
      var t = tabs[i];
      var on = t.id === id;
      t.el.classList.toggle("active", on);
      if (t.tabBtn) t.tabBtn.classList.toggle("active", on);
    }
    requestAnimationFrame(function () {
      fit(tab);
      try {
        tab.term.focus();
      } catch (_) {}
    });
  }

  function focusTab(tab) {
    if (!tab) return;
    focusedId = tab.id;
    activeId = tab.id;
    if (!isDesktop()) {
      setActive(tab.id);
      return;
    }
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].tabBtn) tabs[i].tabBtn.classList.toggle("active", tabs[i].id === tab.id);
    }
    if (tab.wb && !tab.wb.focused) {
      try {
        tab.wb.focus();
      } catch (_) {}
    }
    requestAnimationFrame(function () {
      fit(tab);
      try {
        tab.term.focus();
      } catch (_) {}
    });
    persistLayoutSoon();
  }

  // --------------------------------------------------------- layout persistence
  // The window layout is a single shared doc on the server, so every admin opens
  // the same workspace and can see work-in-flight. Saves are debounced; a light
  // idle poll reconciles other admins' changes without a manual refresh.
  var LAYOUT_URL = "/api/admin/terminal/layout";
  var _layoutMtime = null; // newest mtime we have applied or saved
  var _saveTimer = null;
  var _saveEcho = 0; // our own last write, so the poll can ignore the echo
  var _applyingLayout = false;

  function saveLayoutNow() {
    _saveTimer = null;
    if (!isDesktop()) return;
    api(LAYOUT_URL, { method: "PUT", body: JSON.stringify({ layout: collectLayout() }) })
      .then(function (r) {
        if (r && typeof r.mtime === "number") {
          _layoutMtime = r.mtime;
          _saveEcho = r.mtime;
        }
      })
      .catch(function () {});
  }

  function persistLayoutSoon() {
    if (_applyingLayout || !isDesktop()) return;
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(saveLayoutNow, 700);
  }

  function applyServerLayout(doc) {
    if (!doc || !doc.layout) return;
    _applyingLayout = true;
    try {
      applyLayout(doc.layout);
    } catch (_) {}
    // Let the open/move settle before debounced saves resume.
    setTimeout(function () {
      _applyingLayout = false;
    }, 60);
  }

  function restoreLayout() {
    return api(LAYOUT_URL)
      .then(function (doc) {
        if (doc && typeof doc.mtime === "number") _layoutMtime = doc.mtime;
        var wins = (doc && doc.layout && doc.layout.windows) || [];
        if (!wins.length || !isDesktop()) return false;
        applyServerLayout(doc);
        for (var i = 0; i < tabs.length; i++) {
          if (tabs[i].wb) return true;
        }
        return false;
      })
      .catch(function () {
        return false;
      });
  }

  function pollLayout() {
    if (!isDesktop() || _applyingLayout) return;
    if (document.body.classList.contains("wb-lock")) return; // mid drag/resize
    api(LAYOUT_URL)
      .then(function (doc) {
        if (!doc || typeof doc.mtime !== "number") return;
        if (_layoutMtime != null && doc.mtime <= _layoutMtime) return;
        if (doc.mtime === _saveEcho) {
          _layoutMtime = doc.mtime;
          return;
        }
        _layoutMtime = doc.mtime;
        applyServerLayout(doc);
      })
      .catch(function () {});
  }

  function wmRootRect() {
    if (!wmRoot) {
      return {
        left: 0,
        top: 0,
        width: window.innerWidth,
        height: window.innerHeight,
        right: window.innerWidth,
        bottom: window.innerHeight,
      };
    }
    return wmRoot.getBoundingClientRect();
  }

  // WinBox is position:fixed against the viewport and clamps using
  // instance left/top/right/bottom insets + documentElement client size.
  // Map #wm-root's box into those insets so drag/max stay in the left pane.
  function rootBoundInsets() {
    var r = wmRootRect();
    var vw = document.documentElement.clientWidth;
    var vh = document.documentElement.clientHeight;
    return {
      left: Math.max(0, Math.round(r.left)),
      top: Math.max(0, Math.round(r.top)),
      right: Math.max(0, Math.round(vw - r.right)),
      bottom: Math.max(0, Math.round(vh - r.bottom)),
    };
  }

  function applyWbBounds(wb) {
    if (!wb) return;
    var b = rootBoundInsets();
    wb.left = b.left;
    wb.top = b.top;
    wb.right = b.right;
    wb.bottom = b.bottom;
  }

  function applyWbBoundsAll() {
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].wb) applyWbBounds(tabs[i].wb);
    }
  }

  // layout / cascade / snap math is root-relative; WinBox x/y are viewport.
  function toViewport(rect) {
    var r = wmRootRect();
    return {
      x: Math.round(r.left + (rect.x || 0)),
      y: Math.round(r.top + (rect.y || 0)),
      width: rect.width,
      height: rect.height,
      max: !!rect.max,
    };
  }

  function toRootLocal(wb) {
    var r = wmRootRect();
    return {
      x: Math.round((wb.x || 0) - r.left),
      y: Math.round((wb.y || 0) - r.top),
      width: wb.width,
      height: wb.height,
      max: !!wb.max,
      min: !!wb.min,
      index: wb.index,
    };
  }

  function cascadeDefault() {
    var r = wmRootRect();
    var w = Math.max(360, Math.min(720, (r.width * 0.62) | 0));
    var h = Math.max(240, Math.min(520, (r.height * 0.62) | 0));
    var step = 28;
    var n = cascadeN++;
    var x = 24 + (n % 8) * step;
    var y = 24 + (n % 8) * step;
    if (x + w > r.width) x = Math.max(0, r.width - w - 8);
    if (y + h > r.height) y = Math.max(0, r.height - h - 8);
    return { x: x, y: y, width: w, height: h, max: false };
  }

  function clampToRoot(wb) {
    if (!wb || wb.max || wb.min || wb.full) return;
    // maximize() adds .max before flipping wb.max; don't fight that transition.
    if (wb.g && wb.g.classList && wb.g.classList.contains("max")) return;
    applyWbBounds(wb);
    var r = wmRootRect();
    var rw = r.width;
    var rh = r.height;
    var w = wb.width;
    var h = wb.height;
    var minW = 240;
    var minH = 160;
    if (w > rw) w = Math.max(minW, rw | 0);
    if (h > rh) h = Math.max(minH, rh | 0);
    if (w < minW) w = Math.min(minW, rw | 0);
    if (h < minH) h = Math.min(minH, rh | 0);
    // Viewport coords — WinBox is position:fixed.
    var minX = r.left;
    var minY = r.top;
    var maxX = r.left + rw - w;
    var maxY = r.top + rh - h;
    var x = wb.x;
    var y = wb.y;
    if (x < minX) x = minX;
    if (y < minY) y = minY;
    if (x > maxX) x = Math.max(minX, maxX);
    if (y > maxY) y = Math.max(minY, maxY);
    var needMove = x !== wb.x || y !== wb.y;
    var needResize = w !== wb.width || h !== wb.height;
    // Only correct when out of bounds (avoids onmove re-entrancy loops).
    if (needResize) {
      try {
        wb.resize(w, h);
      } catch (_) {}
    }
    if (needMove) {
      try {
        wb.move(x, y);
      } catch (_) {}
    }
  }

  // --------------------------------------------------------------- snap zones
  // Zones are computed from pointer position relative to #wm-root, NOT the
  // viewport. EDGE (~28px) strips on left/right/top; corners win over edges.
  // top (non-corner) => maximize-to-pane; left/right => halves; corners => quads.
  function zoneFromPointer(clientX, clientY) {
    var r = wmRootRect();
    var x = clientX - r.left;
    var y = clientY - r.top;
    if (x < 0 || y < 0 || x > r.width || y > r.height) return null;
    var e = SNAP_EDGE;
    var nearL = x <= e;
    var nearR = x >= r.width - e;
    var nearT = y <= e;
    var nearB = y >= r.height - e;
    if (nearT && nearL) return "tl";
    if (nearT && nearR) return "tr";
    if (nearB && nearL) return "bl";
    if (nearB && nearR) return "br";
    if (nearL) return "left";
    if (nearR) return "right";
    if (nearT) return "top";
    // bottom edge alone is not a snap target (avoids accidental half-height)
    return null;
  }

  function zoneRect(zone) {
    var r = wmRootRect();
    var hw = (r.width / 2) | 0;
    var hh = (r.height / 2) | 0;
    var W = r.width | 0;
    var H = r.height | 0;
    switch (zone) {
      case "left":
        return { x: 0, y: 0, width: hw, height: H };
      case "right":
        return { x: hw, y: 0, width: W - hw, height: H };
      case "top":
        return { x: 0, y: 0, width: W, height: H };
      case "tl":
        return { x: 0, y: 0, width: hw, height: hh };
      case "tr":
        return { x: hw, y: 0, width: W - hw, height: hh };
      case "bl":
        return { x: 0, y: hh, width: hw, height: H - hh };
      case "br":
        return { x: hw, y: hh, width: W - hw, height: H - hh };
      default:
        return null;
    }
  }

  function showSnapPreview(zone) {
    if (!snapPreview) return;
    if (!zone) {
      snapPreview.hidden = true;
      snapPreview.style.display = "none";
      return;
    }
    var rect = zoneRect(zone);
    if (!rect) {
      snapPreview.hidden = true;
      snapPreview.style.display = "none";
      return;
    }
    snapPreview.hidden = false;
    snapPreview.style.display = "block";
    snapPreview.style.left = rect.x + "px";
    snapPreview.style.top = rect.y + "px";
    snapPreview.style.width = rect.width + "px";
    snapPreview.style.height = rect.height + "px";
  }

  function hideSnapPreview() {
    showSnapPreview(null);
  }

  function applySnap(tab, zone) {
    if (!tab || !tab.wb || !zone) return;
    var wb = tab.wb;
    applyWbBounds(wb);
    if (zone === "top") {
      try {
        if (!wb.max) wb.maximize();
      } catch (_) {}
      requestAnimationFrame(function () {
        fit(tab);
        persistLayoutSoon();
      });
      return;
    }
    var local = zoneRect(zone);
    if (!local) return;
    var rect = toViewport(local);
    try {
      if (wb.max) wb.restore();
    } catch (_) {}
    try {
      wb.resize(rect.width, rect.height).move(rect.x, rect.y);
    } catch (_) {
      try {
        wb.resize(rect.width, rect.height);
        wb.move(rect.x, rect.y);
      } catch (__) {}
    }
    clampToRoot(wb);
    requestAnimationFrame(function () {
      fit(tab);
      persistLayoutSoon();
    });
  }

  // ----------------------------------------------------------- scrollbar overlay
  // Visible thin track+grip on the right edge. tmux alt-screen over a raw PTY
  // gives no scroll-position feedback, so the grip RE-CENTERS when idle — it is
  // an affordance (drag = wheel / scrollLines), not a true position indicator.
  // Native xterm viewport scrollbar remains for the normal (non-alt) buffer.
  function bindScrollbarOverlay(tab) {
    var el = tab.el;
    if (!el || el.querySelector(".term-sb")) return;

    var sb = document.createElement("div");
    sb.className = "term-sb";
    sb.setAttribute("aria-hidden", "true");

    var track = document.createElement("div");
    track.className = "term-sb-track";
    var grip = document.createElement("div");
    grip.className = "term-sb-grip";
    track.appendChild(grip);
    sb.appendChild(track);
    el.appendChild(sb);

    var dragging = false;
    var lastY = 0;
    var accum = 0;
    var idleTimer = null;

    function brighten() {
      sb.classList.add("term-sb-active");
      clearTimeout(idleTimer);
      idleTimer = setTimeout(function () {
        if (!dragging) sb.classList.remove("term-sb-active");
      }, 900);
    }

    function reCenter() {
      grip.style.top = "";
      grip.classList.remove("term-sb-grip-drag");
    }

    function scrollByLines(lines, clientX, clientY) {
      if (!lines) return;
      var term = tab.term;
      if (mouseTrackingOn(term)) {
        sendWheel(tab, lines, { clientX: clientX, clientY: clientY });
      } else {
        try {
          term.scrollLines(lines);
        } catch (_) {}
      }
    }

    function pageLines() {
      var rows = (tab.term && tab.term.rows) || 24;
      return Math.max(1, rows - 1);
    }

    track.addEventListener("pointerdown", function (ev) {
      if (ev.button != null && ev.button !== 0) return;
      brighten();
      var gr = grip.getBoundingClientRect();
      var mid = (gr.top + gr.bottom) / 2;
      if (ev.target === grip || grip.contains(ev.target)) {
        dragging = true;
        lastY = ev.clientY;
        accum = 0;
        grip.classList.add("term-sb-grip-drag");
        try {
          track.setPointerCapture(ev.pointerId);
        } catch (_) {}
        ev.preventDefault();
        ev.stopPropagation();
        return;
      }
      // Click track above/below grip → page up/down.
      var dir = ev.clientY < mid ? -1 : 1;
      var cx = ev.clientX;
      var cy = ev.clientY;
      scrollByLines(dir * pageLines(), cx, cy);
      // Nudge grip briefly toward the click, then re-center.
      var tr = track.getBoundingClientRect();
      var gh = grip.offsetHeight || 48;
      var gy = ev.clientY - tr.top - gh / 2;
      gy = Math.max(0, Math.min(tr.height - gh, gy));
      grip.style.top = gy + "px";
      clearTimeout(idleTimer);
      idleTimer = setTimeout(reCenter, 280);
      ev.preventDefault();
      ev.stopPropagation();
    });

    track.addEventListener("pointermove", function (ev) {
      if (!dragging) return;
      brighten();
      var dy = ev.clientY - lastY;
      lastY = ev.clientY;
      // Visual follow (clamped); no true position feedback from PTY.
      var tr = track.getBoundingClientRect();
      var gh = grip.offsetHeight || 48;
      var top = ev.clientY - tr.top - gh / 2;
      top = Math.max(0, Math.min(tr.height - gh, top));
      grip.style.top = top + "px";

      accum += dy;
      var step = Math.max(6, cellHeight(tab.term) * 0.35);
      var lines = (accum / step) | 0;
      if (!lines) return;
      accum -= lines * step;
      scrollByLines(lines, ev.clientX, ev.clientY);
      ev.preventDefault();
    });

    function endDrag(ev) {
      if (!dragging) return;
      dragging = false;
      try {
        if (ev && ev.pointerId != null) track.releasePointerCapture(ev.pointerId);
      } catch (_) {}
      reCenter();
      brighten();
    }
    track.addEventListener("pointerup", endDrag);
    track.addEventListener("pointercancel", endDrag);

    sb.addEventListener("pointerenter", brighten);
    el.addEventListener(
      "wheel",
      function () {
        brighten();
      },
      { passive: true }
    );

    tab._sb = sb;
  }

  // ------------------------------------------------------------------ tabs
  function closeTab(id, fromWb) {
    var tab = findTab(id);
    if (!tab) return;
    tab._closed = true;
    unobserveWindowSize(tab);
    var idx = tabs.indexOf(tab);

    // Tear down WinBox first (suppress recursive onclose → closeTab).
    if (tab.wb) {
      var wb = tab.wb;
      tab.wb = null;
      if (!fromWb) {
        try {
          wb.close(true);
        } catch (_) {}
      }
    }

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

    if (focusedId === id) focusedId = null;
    if (activeId === id) {
      activeId = null;
      if (tabs.length) {
        var next = tabs[Math.min(idx, tabs.length - 1)];
        if (isDesktop()) focusTab(next);
        else setActive(next.id);
      }
    }
    renderPicker();
    persistLayoutSoon();
  }

  function setTabLabel(tab, name) {
    tab.name = name;
    tab.label = name;
    if (tab.labelSpan) tab.labelSpan.textContent = name;
    if (tab.tabBtn) tab.tabBtn.title = name;
    if (tab.wb) {
      try {
        tab.wb.setTitle(name || "terminal");
      } catch (_) {}
    }
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
      fit(tab);
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

    // Close codes the SERVER chose deliberately. Reconnecting through these is
    // wrong: 4408 would silently undo the idle timeout, 4429 would retry-storm
    // the session cap (each attempt can mint another session), and 4403/4404
    // can never succeed on retry. Anything else (1006 on a gateway restart,
    // network blips) is still transient and worth retrying.
    var FATAL_CLOSE = {
      4403: "not authorized for this terminal",
      4404: "terminal is disabled on this deployment",
      4408: "closed after being idle",
      4429: "too many terminal sessions open",
      1008: "rejected by the server",
      1009: "message too large",
    };

    ws.onclose = function (ev) {
      if (tab._closed) return;
      if (tab._ended) {
        renderPicker();
        return;
      }
      var fatal = ev && FATAL_CLOSE[ev.code];
      if (fatal) {
        term.writeln(
          "\r\n\x1b[2m[" + fatal + " — press the session pill to rejoin]\x1b[0m"
        );
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

  var ARROW_FINAL = { ArrowUp: "A", ArrowDown: "B", ArrowRight: "C", ArrowLeft: "D" };

  // Canonical modified cursor-key sequence (\x1b[1;<mod><final>). Used to bypass
  // xterm.js's non-Mac Alt+Arrow -> Ctrl+Arrow remap (and its Mac word-nav
  // remap) so Chudcode's Alt-based shortcuts reach the PTY as the real Alt chord.
  function modifiedArrowSeq(ev) {
    var final = ARROW_FINAL[ev.key];
    if (!final) return null;
    var mod = 1 + (ev.shiftKey ? 1 : 0) + (ev.altKey ? 2 : 0) + (ev.ctrlKey ? 4 : 0);
    if (mod === 1) return null; // unmodified — let xterm handle it
    return "\x1b[1;" + mod + final;
  }

  // Touch-swipe scrolling. xterm's screen layer sits on top of the scrollable
  // viewport and swallows touch events, so mobile swipes never scroll natively.
  // Translate a one-finger vertical drag into scrollback movement (normal
  // buffer) or SGR wheel reports (when Chudcode has mouse tracking enabled
  // on the alternate screen), giving a real swipe-to-scroll on phones.
  function cellHeight(term) {
    try {
      var c = term._core._renderService.dimensions.css.cell;
      if (c && c.height) return c.height;
    } catch (_) {}
    return 18;
  }

  function mouseTrackingOn(term) {
    var x = term.element;
    return !!(x && x.classList.contains("enable-mouse-events"));
  }

  function sendWheel(tab, lines, touch) {
    if (!tab.ws || tab.ws.readyState !== 1) return;
    var term = tab.term;
    var col = 1,
      row = 1;
    try {
      var rect = term.element.getBoundingClientRect();
      var cell = term._core._renderService.dimensions.css.cell;
      col = Math.max(1, Math.min(term.cols, (((touch.clientX - rect.left) / cell.width) | 0) + 1));
      row = Math.max(1, Math.min(term.rows, (((touch.clientY - rect.top) / cell.height) | 0) + 1));
    } catch (_) {}
    var btn = lines > 0 ? 65 : 64; // 65 = wheel down, 64 = wheel up
    var n = Math.min(Math.abs(lines), 8);
    var seq = "";
    for (var i = 0; i < n; i++) seq += "\x1b[<" + btn + ";" + col + ";" + row + "M";
    tab.ws.send(encoder.encode(seq));
  }

  function bindTouchScroll(tab) {
    var term = tab.term;
    var el = tab.el;
    var lastY = null;
    var accum = 0;
    el.addEventListener(
      "touchstart",
      function (ev) {
        if (ev.touches.length !== 1) {
          lastY = null;
          return;
        }
        lastY = ev.touches[0].clientY;
        accum = 0;
      },
      { passive: true }
    );
    el.addEventListener(
      "touchmove",
      function (ev) {
        if (lastY == null || ev.touches.length !== 1) return;
        ev.preventDefault(); // own the vertical swipe (no page/pane rubber-band)
        var t = ev.touches[0];
        accum += lastY - t.clientY;
        lastY = t.clientY;
        var step = cellHeight(term);
        var lines = (accum / step) | 0;
        if (!lines) return;
        accum -= lines * step;
        if (mouseTrackingOn(term)) sendWheel(tab, lines, t);
        else term.scrollLines(lines);
      },
      { passive: false }
    );
    function end() {
      lastY = null;
    }
    el.addEventListener("touchend", end, { passive: true });
    el.addEventListener("touchcancel", end, { passive: true });
  }

  // Build xterm + container + WS; does NOT mount into a display surface.
  function createAttachment(name) {
    var id = "t" + idSeq++;
    var label = name || "connecting…";

    var el = document.createElement("div");
    el.className = "term-session";
    el.dataset.tid = id;
    el.setAttribute("role", "tabpanel");

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
    xBtn.innerHTML = ICON_CLOSE;
    tabBtn.appendChild(xBtn);

    tabsEl.insertBefore(tabBtn, newBtn);

    tabBtn.addEventListener("click", function (ev) {
      if (ev.target === xBtn || xBtn.contains(ev.target)) {
        ev.preventDefault();
        ev.stopPropagation();
        closeTab(id);
        return;
      }
      if (isDesktop()) {
        var t = findTab(id);
        if (t) focusTab(t);
      } else {
        setActive(id);
      }
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

    var fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(el);

    var tab = {
      id: id,
      name: name || null,
      term: term,
      fit: fitAddon,
      ws: null,
      el: el,
      tabBtn: tabBtn,
      labelSpan: labelSpan,
      label: label,
      wb: null,
      _closed: false,
      _ended: false,
      _retries: 0,
      _dataBound: false,
    };

    term.attachCustomKeyEventHandler(function (ev) {
      if (ev.type !== "keydown") return true;
      if (isReservedCombo(ev)) {
        ev.preventDefault();
        var k = (ev.key || "").toLowerCase();
        if (k === "t") {
          newSession();
        } else if (k === "w") {
          var cur = isDesktop() ? focusedTab() : activeTab();
          if (cur) closeTab(cur.id);
        }
        return false;
      }
      // Rescue Alt+Arrow from xterm's remap so Chudcode gets the true Alt chord.
      if (ev.altKey && !ev.metaKey) {
        var seq = modifiedArrowSeq(ev);
        if (seq) {
          ev.preventDefault();
          if (tab.ws && tab.ws.readyState === 1) tab.ws.send(encoder.encode(seq));
          return false;
        }
      }
      return true;
    });

    tabs.push(tab);
    bindTouchScroll(tab);
    bindScrollbarOverlay(tab);

    try {
      fitAddon.fit();
    } catch (_) {}

    attachWs(tab);
    renderPicker();
    return tab;
  }

  function mountMobile(tab) {
    if (!tab || !tab.el) return;
    unobserveWindowSize(tab);
    // Detach from any WinBox without disposing the session.
    if (tab.wb) {
      var wb = tab.wb;
      tab.wb = null;
      try {
        // Unmount node before close so WinBox doesn't remove/destroy it.
        if (tab.el.parentNode) tab.el.parentNode.removeChild(tab.el);
        wb.close(true);
      } catch (_) {
        try {
          wb.close(true);
        } catch (__) {}
      }
    }
    if (tab.el.parentNode !== host) {
      host.appendChild(tab.el);
    }
    tab.el.classList.add("term-session");
    tab.el.style.width = "";
    tab.el.style.height = "";
    tab.el.style.position = "";
    tab.el.style.inset = "";
  }

  // Refit deterministically whenever a window's body actually changes size
  // (open, mount, maximize/restore after layout settles, edge-resize, snap) —
  // rAF alone races WinBox's style/layout, leaving the xterm grid stale.
  function observeWindowSize(tab) {
    if (typeof ResizeObserver === "undefined" || !tab || tab._ro || !tab.el) return;
    var timer = null;
    tab._ro = new ResizeObserver(function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        if (!tab._closed && tab.wb) fit(tab);
      }, 60);
    });
    try {
      tab._ro.observe(tab.el);
    } catch (_) {}
  }

  function unobserveWindowSize(tab) {
    if (tab && tab._ro) {
      try {
        tab._ro.disconnect();
      } catch (_) {}
      tab._ro = null;
    }
  }

  function mountWindow(tab, rect) {
    if (!tab || typeof WinBox === "undefined" || !wmRoot) {
      mountMobile(tab);
      setActive(tab.id);
      return null;
    }

    // Already has a live window — just focus / optionally re-place.
    if (tab.wb) {
      applyWbBounds(tab.wb);
      if (rect) {
        try {
          if (rect.max) {
            if (!tab.wb.max) tab.wb.maximize();
          } else {
            var vpExist = toViewport(rect);
            if (tab.wb.max) tab.wb.restore();
            if (vpExist.width && vpExist.height) {
              tab.wb.resize(vpExist.width, vpExist.height);
            }
            if (rect.x != null && rect.y != null) {
              tab.wb.move(vpExist.x, vpExist.y);
            }
            clampToRoot(tab.wb);
          }
        } catch (_) {}
      }
      focusTab(tab);
      return tab.wb;
    }

    // Ensure el is not sitting in #term-host.
    if (tab.el.parentNode) {
      try {
        tab.el.parentNode.removeChild(tab.el);
      } catch (_) {}
    }
    tab.el.classList.add("active");
    tab.el.classList.add("term-session");
    tab.el.style.width = "100%";
    tab.el.style.height = "100%";
    tab.el.style.position = "relative";
    tab.el.style.inset = "auto";
    tab.el.style.display = "block";

    // `rect` is root-relative (layout / cascade / snap). WinBox wants viewport.
    var local = rect || cascadeDefault();
    var vp = toViewport(local);
    var bounds = rootBoundInsets();
    var width = vp.width || 640;
    var height = vp.height || 420;

    var wb = new WinBox({
      root: wmRoot,
      title: tab.name || tab.label || "terminal",
      mount: tab.el,
      x: vp.x,
      y: vp.y,
      width: width,
      height: height,
      left: bounds.left,
      top: bounds.top,
      right: bounds.right,
      bottom: bounds.bottom,
      class: ["no-full", "term-wb"],
      onfocus: function () {
        focusTab(tab);
      },
      onresize: function () {
        clampToRoot(wb);
        requestAnimationFrame(function () {
          fit(tab);
        });
        persistLayoutSoon();
      },
      onmove: function () {
        // Soft clamp when not mid-gesture; during title-drag use bounds + snap.
        if (!document.body.classList.contains("wb-lock")) {
          clampToRoot(wb);
        } else {
          applyWbBounds(wb);
          if (snapState.armed && snapState.tab === tab) {
            var z = zoneFromPointer(pointer.x, pointer.y);
            snapState.zone = z;
            showSnapPreview(z);
          }
        }
        persistLayoutSoon();
      },
      onmaximize: function () {
        requestAnimationFrame(function () {
          fit(tab);
        });
        persistLayoutSoon();
      },
      onrestore: function () {
        requestAnimationFrame(function () {
          clampToRoot(wb);
          fit(tab);
        });
        persistLayoutSoon();
      },
      onminimize: function () {
        persistLayoutSoon();
      },
      onclose: function () {
        // Let WinBox remove the chrome; we own session teardown.
        if (tab.wb === wb) {
          tab.wb = null;
          closeTab(tab.id, true);
        }
        // return nothing / falsy → allow close
      },
    });

    tab.wb = wb;
    observeWindowSize(tab);

    // Fill the WinBox body.
    try {
      tab.el.style.width = "100%";
      tab.el.style.height = "100%";
      if (wb.body) {
        wb.body.style.overflow = "hidden";
      }
    } catch (_) {}

    if (local.max) {
      try {
        wb.maximize();
      } catch (_) {}
    } else {
      clampToRoot(wb);
    }

    focusedId = tab.id;
    activeId = tab.id;
    requestAnimationFrame(function () {
      fit(tab);
      try {
        tab.term.focus();
      } catch (_) {}
    });
    persistLayoutSoon();
    return wb;
  }

  function openWindowForSession(name, rect) {
    var existing = name ? findTabByName(name) : null;
    if (existing) {
      if (isDesktop()) {
        if (!existing.wb) mountWindow(existing, rect || null);
        else {
          if (rect) mountWindow(existing, rect);
          else focusTab(existing);
        }
      } else {
        mountMobile(existing);
        setActive(existing.id);
      }
      return existing;
    }

    var tab = createAttachment(name);
    if (isDesktop()) {
      mountWindow(tab, rect || null);
    } else {
      mountMobile(tab);
      setActive(tab.id);
    }
    return tab;
  }

  // --------------------------------------------------------------- sessions
  function openSession(name) {
    return openWindowForSession(name, null);
  }

  function newSession() {
    api("/api/admin/terminal/sessions", { method: "POST", body: "{}" })
      .then(function (r) {
        if (r && r.name) openWindowForSession(r.name, null);
        else openWindowForSession(null, null);
        refreshSessions();
      })
      .catch(function (e) {
        // No tmux on host → fall back to a plain (private) attach tab.
        console.warn("[admin_terminal] create failed, opening private tab:", e.message || e);
        openWindowForSession(null, null);
      });
  }

  function killSession(name) {
    if (
      !confirm(
        'Kill shared session "' +
          name +
          '"? Everyone attached loses it and unsaved work is gone.'
      )
    ) {
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
    Array.prototype.slice
      .call(pickerEl.querySelectorAll(".term-pill, .term-picker-empty"))
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
      clients.textContent = s.clients > 0 ? String(s.clients) : "idle";
      clients.title = s.clients + " watching";
      pill.appendChild(clients);

      var kill = document.createElement("span");
      kill.className = "term-pill-kill";
      kill.setAttribute("role", "button");
      kill.title = "Kill session";
      kill.setAttribute("aria-label", "Kill session " + s.name);
      kill.innerHTML = ICON_CLOSE;
      pill.appendChild(kill);

      pill.addEventListener("click", function (ev) {
        if (ev.target === kill || kill.contains(ev.target)) {
          ev.preventDefault();
          ev.stopPropagation();
          killSession(s.name);
          return;
        }
        openWindowForSession(s.name, null);
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
          if (isDesktop()) focusTab(t);
          else setActive(t.id);
        });
        pickerEl.appendChild(pill);
      }
    });

    var addPill = document.createElement("button");
    addPill.type = "button";
    addPill.className = "term-pill term-pill-new";
    addPill.innerHTML = ICON_PLUS + "<span>New session</span>";
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

  // ----------------------------------------------------------- layout collect
  function collectLayout() {
    var windows = [];
    if (!isDesktop() || typeof WinBox === "undefined") {
      return { v: 1, windows: windows };
    }
    var stack = [];
    try {
      stack = WinBox.stack() || [];
    } catch (_) {
      stack = [];
    }
    // z-order from stack; fall back to tabs order if empty.
    var ordered = [];
    var seen = {};
    for (var i = 0; i < stack.length; i++) {
      var t = findTabByWb(stack[i]);
      if (t && t.name && !seen[t.id]) {
        seen[t.id] = true;
        ordered.push(t);
      }
    }
    for (var j = 0; j < tabs.length; j++) {
      if (tabs[j].name && tabs[j].wb && !seen[tabs[j].id]) ordered.push(tabs[j]);
    }
    for (var k = 0; k < ordered.length; k++) {
      var tab = ordered[k];
      var wb = tab.wb;
      if (!wb) continue;
      // Persist root-relative geometry. While maximized, wb.x/y/width/height
      // are the pre-maximize (restore) values — that is what we want.
      var loc = toRootLocal(wb);
      windows.push({
        session: tab.name,
        x: loc.x,
        y: loc.y,
        width: loc.width,
        height: loc.height,
        max: !!wb.max,
        min: !!wb.min,
        index: wb.index != null ? wb.index : k,
      });
    }
    return { v: 1, windows: windows };
  }

  function applyLayout(layout) {
    if (!layout || !layout.windows || !layout.windows.length) return;
    if (!isDesktop()) return;
    var live = {};
    sessionsCache.forEach(function (s) {
      if (s && s.name) live[s.name] = true;
    });
    // Also allow sessions we already have attached (private/ephemeral).
    tabs.forEach(function (t) {
      if (t.name) live[t.name] = true;
    });

    for (var i = 0; i < layout.windows.length; i++) {
      var w = layout.windows[i];
      if (!w || !w.session || !live[w.session]) continue;
      var ex = findTabByName(w.session);
      if (ex && ex.wb) {
        // Reposition the existing window in place — never spawn a 2nd WinBox.
        try {
          if (w.max) {
            if (!ex.wb.max) ex.wb.maximize();
          } else {
            if (ex.wb.max) ex.wb.restore();
            var vp = toViewport({ x: w.x, y: w.y, width: w.width, height: w.height });
            ex.wb.resize(vp.width, vp.height).move(vp.x, vp.y);
            clampToRoot(ex.wb);
          }
          fit(ex);
        } catch (_) {}
      } else {
        openWindowForSession(w.session, {
          x: w.x,
          y: w.y,
          width: w.width,
          height: w.height,
          max: !!w.max,
        });
      }
    }
  }

  // Re-home every attachment between WinBox and #term-host without dropping WS.
  function rehomeAll() {
    applyModeClass();
    hideSnapPreview();
    snapState.zone = null;
    snapState.tab = null;
    if (isDesktop()) {
      for (var i = 0; i < tabs.length; i++) {
        var t = tabs[i];
        if (!t.wb) mountWindow(t, null);
      }
      fitAll();
    } else {
      for (var j = 0; j < tabs.length; j++) {
        mountMobile(tabs[j]);
      }
      if (activeId != null) setActive(activeId);
      else if (tabs.length) setActive(tabs[0].id);
    }
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
        var cur = isDesktop() ? focusedTab() : activeTab();
        if (cur) closeTab(cur.id);
      }
    },
    true
  );

  // Track pointer for snap zones (capture so we see it during WinBox drag).
  window.addEventListener(
    "mousemove",
    function (ev) {
      pointer.x = ev.clientX;
      pointer.y = ev.clientY;
      if (
        snapState.armed &&
        document.body.classList.contains("wb-lock") &&
        snapState.tab &&
        snapState.tab.wb
      ) {
        var z = zoneFromPointer(pointer.x, pointer.y);
        snapState.zone = z;
        showSnapPreview(z);
      }
    },
    true
  );

  // Arm snap only when the gesture starts on a title drag handle — not resize.
  window.addEventListener(
    "mousedown",
    function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var drag = t.closest(".wb-drag");
      if (!drag) return;
      var box = t.closest(".winbox");
      if (!box || !box.winbox) return;
      var tab = findTabByWb(box.winbox);
      if (!tab) return;
      snapState.armed = true;
      snapState.tab = tab;
      snapState.zone = null;
    },
    true
  );

  window.addEventListener(
    "mouseup",
    function () {
      if (!snapState.armed) {
        hideSnapPreview();
        snapState.zone = null;
        snapState.tab = null;
        return;
      }
      var zone = snapState.zone;
      var tab = snapState.tab;
      // Defer one tick so WinBox finishes its own mouseup move commit first.
      setTimeout(function () {
        hideSnapPreview();
        if (zone && tab && tab.wb && !tab._closed) {
          applySnap(tab, zone);
        }
        snapState.zone = null;
        snapState.tab = null;
        snapState.armed = false;
      }, 0);
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
      if (isDesktop()) {
        // Pane / viewport changed — refresh WinBox bound insets then clamp.
        applyWbBoundsAll();
        for (var i = 0; i < tabs.length; i++) {
          if (tabs[i].wb) clampToRoot(tabs[i].wb);
        }
        fitAll();
      } else {
        var tab = activeTab();
        if (tab) fit(tab);
      }
    }, 80);
  }
  window.addEventListener("resize", scheduleFit);
  if (typeof ResizeObserver !== "undefined") {
    try {
      new ResizeObserver(scheduleFit).observe(host);
    } catch (_) {}
    if (wmRoot) {
      try {
        new ResizeObserver(scheduleFit).observe(wmRoot);
      } catch (_) {}
    }
  }

  // Desktop ↔ mobile breakpoint: move DOM nodes, keep WS alive.
  var mq = window.matchMedia(DESKTOP_MQ);
  function onMqChange() {
    rehomeAll();
  }
  if (mq.addEventListener) mq.addEventListener("change", onMqChange);
  else if (mq.addListener) mq.addListener(onMqChange);

  applyModeClass();

  // Poll the shared-session list so client counts / new sessions stay current.
  setInterval(refreshSessions, 5000);
  setInterval(pollLayout, 5000);

  // Public WM integration surface (also local fns above).
  var __wm = {
    isDesktop: isDesktop,
    openWindowForSession: openWindowForSession,
    collectLayout: collectLayout,
    applyLayout: applyLayout,
    persistLayoutSoon: persistLayoutSoon,
  };
  window.__wm = __wm;

  // On load: restore the shared window layout; if none applies, join the
  // most-recently-active session, else start a fresh one.
  refreshSessions().then(function (list) {
    return restoreLayout().then(function (restored) {
      if (restored) return;
      if (list && list.length) {
        var latest = list.slice().sort(function (a, b) {
          return (b.activity || 0) - (a.activity || 0);
        })[0];
        openWindowForSession(latest.name, null);
      } else {
        newSession();
      }
    });
  });
})();
