/* billet-web portal — vanilla session card + shoutbox client */
(function () {
  "use strict";

  var LS_CHAT = "billet.portal.chatOpen";
  var LS_UNREAD = "billet.portal.unread";

  var state = {
    user: { email: "", display: "" },
    session: { status: "idle", sid: null, stream_path: null, error: null, message: "" },
    chatOpen: localStorage.getItem(LS_CHAT) !== "0",
    unread: parseInt(localStorage.getItem(LS_UNREAD) || "0", 10) || 0,
    pollTimer: null,
    ws: null,
    wsBackoff: 800,
    presence: [],
    seenMsgIds: Object.create(null),
    streamAttached: false,
  };

  var el = {
    userName: document.getElementById("user-name"),
    userChip: document.getElementById("user-chip"),
    sessionCard: document.getElementById("session-card"),
    sessionStatus: document.getElementById("session-status"),
    sessionDetail: document.getElementById("session-detail"),
    sessionError: document.getElementById("session-error"),
    btnStart: document.getElementById("btn-start"),
    btnReconnect: document.getElementById("btn-reconnect"),
    btnStop: document.getElementById("btn-stop"),
    btnStopStream: document.getElementById("btn-stop-stream"),
    btnPopout: document.getElementById("btn-popout"),
    streamWrap: document.getElementById("stream-wrap"),
    streamFrame: document.getElementById("stream-frame"),
    streamLabel: document.getElementById("stream-label"),
    chatSidebar: document.getElementById("chat-sidebar"),
    chatToggle: document.getElementById("chat-toggle"),
    chatCollapse: document.getElementById("chat-collapse"),
    chatRailBtn: document.getElementById("chat-rail-btn"),
    chatBadge: document.getElementById("chat-badge"),
    railBadge: document.getElementById("rail-badge"),
    presenceList: document.getElementById("presence-list"),
    presenceCount: document.getElementById("presence-count"),
    chatMessages: document.getElementById("chat-messages"),
    chatForm: document.getElementById("chat-form"),
    chatInput: document.getElementById("chat-input"),
  };

  function $(sel) { return document.querySelector(sel); }

  function fmtTime(ts) {
    try {
      var d = new Date((ts || 0) * 1000);
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      return "";
    }
  }

  function setChatOpen(open) {
    state.chatOpen = !!open;
    document.body.classList.toggle("chat-collapsed", !state.chatOpen);
    localStorage.setItem(LS_CHAT, state.chatOpen ? "1" : "0");
    if (state.chatOpen) {
      state.unread = 0;
      localStorage.setItem(LS_UNREAD, "0");
      renderBadges();
      el.chatInput && el.chatInput.focus();
    }
  }

  function renderBadges() {
    var n = state.unread > 99 ? "99+" : String(state.unread);
    var show = state.unread > 0 && !state.chatOpen;
    [el.chatBadge, el.railBadge].forEach(function (b) {
      if (!b) return;
      b.textContent = n;
      b.classList.toggle("hidden", !show);
    });
  }

  function setStatusPill(status) {
    var s = (status || "idle").toLowerCase();
    el.sessionStatus.textContent = s === "ready" ? "running" : s;
    el.sessionStatus.className = "status-pill status-" + (
      s === "ready" ? "running" :
      s === "starting" ? "starting" :
      s === "running" ? "running" :
      s === "error" || s === "refused" ? "error" :
      "idle"
    );
  }

  function showError(msg) {
    if (!msg) {
      el.sessionError.classList.add("hidden");
      el.sessionError.textContent = "";
      return;
    }
    el.sessionError.textContent = msg;
    el.sessionError.classList.remove("hidden");
  }

  function streamUrl(sess) {
    if (!sess) return null;
    var base = sess.stream_url || sess.stream_path ||
      (sess.sid ? "/stream/" + encodeURIComponent(sess.sid) + "/" : null);
    if (!base) return null;
    if (base.charAt(base.length - 1) !== "/") base += "/";
    // noVNC resolves its websocket `path` against the host root, not the page
    // directory — pass the proxied prefix explicitly and autoconnect.
    return base + "vnc.html?autoconnect=1&resize=scale&reconnect=1&path=" +
      encodeURIComponent(base.slice(1) + "websockify");
  }

  function isLive(status) {
    status = (status || "").toLowerCase();
    return status === "running" || status === "ready" || status === "healthy";
  }

  function isStarting(status) {
    status = (status || "").toLowerCase();
    return status === "starting" || status === "booting" || status === "pending";
  }

  function renderSession() {
    var s = state.session;
    var status = (s.status || "idle").toLowerCase();
    setStatusPill(status);

    var detail = s.message || "";
    if (s.sid && isLive(status)) detail = (detail ? detail + " · " : "") + "sid " + s.sid;
    if (isStarting(status)) detail = detail || "Starting container and waiting for stream healthcheck…";
    if (liveNeedsReconnect(status)) {
      detail = detail || "Session still running — reconnect to the same seat.";
    }
    el.sessionDetail.textContent = detail;

    if (s.error || status === "refused" || status === "error") {
      showError(s.error || s.message || "Session request was refused.");
    } else {
      showError("");
    }

    var live = isLive(status);
    var starting = isStarting(status);
    var attached = state.streamAttached && live;

    el.btnStart.classList.toggle("hidden", live || starting);
    el.btnReconnect.classList.toggle("hidden", !(live && !attached));
    if (live || starting) el.btnStop.classList.remove("hidden");
    else el.btnStop.classList.add("hidden");

    el.btnStart.disabled = starting;
    el.btnReconnect.disabled = starting;
    el.btnStop.disabled = false;

    if (attached) {
      var url = streamUrl(s);
      el.sessionCard.classList.add("hidden");
      el.streamWrap.classList.remove("hidden");
      if (url && el.streamFrame.getAttribute("src") !== url) {
        el.streamFrame.src = url;
      }
      el.streamLabel.textContent = "Session live" + (s.sid ? " · " + s.sid : "");
    } else {
      el.sessionCard.classList.remove("hidden");
      el.streamWrap.classList.add("hidden");
      if (!live) {
        el.streamFrame.removeAttribute("src");
        state.streamAttached = false;
      }
    }
  }

  function liveNeedsReconnect(status) {
    return isLive(status) && !state.streamAttached;
  }

  async function api(path, opts) {
    opts = opts || {};
    var res = await fetch(path, {
      method: opts.method || "GET",
      headers: Object.assign({ "Accept": "application/json" }, opts.headers || {}),
      body: opts.body,
      credentials: "same-origin",
    });
    var data = null;
    var text = await res.text();
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
    if (!res.ok) {
      var err = new Error((data && (data.detail || data.error || data.message)) || (res.status + " " + res.statusText));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function normalizeSession(data) {
    data = data || {};
    // Accept a few shapes from GatewayCore while the contract settles.
    var sess = data.session || data;
    var status = (sess.status || data.status || "idle").toLowerCase();
    if (status === "none" || status === "stopped" || status === "absent") status = "idle";
    return {
      status: status,
      sid: sess.sid || sess.id || data.sid || null,
      stream_path: sess.stream_path || sess.stream_url || data.stream_path || data.stream_url || null,
      stream_url: sess.stream_url || data.stream_url || null,
      error: sess.error || data.error || null,
      message: sess.message || data.message || "",
      reason: sess.reason || data.reason || null,
    };
  }

  async function refreshSession() {
    try {
      var data = await api("/api/sessions");
      state.session = normalizeSession(data);
      // Prefer nested me/user if present
      if (data.user) {
        state.user.email = data.user.email || state.user.email;
        state.user.display = data.user.display || state.user.display;
      }
      if (data.me) {
        state.user.email = data.me.email || state.user.email;
        state.user.display = data.me.display || state.user.display;
      }
      renderUser();
      renderSession();
      return state.session;
    } catch (err) {
      if (err.status === 401 || err.status === 403) {
        showError("You’re not signed in or not on the allowlist. Ask an admin to add your email.");
        setStatusPill("error");
      } else {
        el.sessionDetail.textContent = "Could not reach session API: " + err.message;
      }
      return null;
    }
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(async function () {
      var s = await refreshSession();
      if (!s) return;
      if (isLive(s.status)) {
        state.streamAttached = true;
        renderSession();
        stopPolling();
      } else if (s.status === "idle" || s.status === "error" || s.status === "refused") {
        stopPolling();
      }
    }, 1500);
  }

  async function startSession() {
    showError("");
    setStatusPill("starting");
    el.sessionDetail.textContent = "Requesting seat…";
    el.btnStart.disabled = true;
    try {
      var data = await api("/api/sessions", { method: "POST" });
      state.session = normalizeSession(data);
      if (isLive(state.session.status)) {
        state.streamAttached = true;
      }
      renderSession();
      if (isStarting(state.session.status)) {
        startPolling();
      } else if (state.session.error || state.session.status === "refused") {
        showError(friendlyRefuse(state.session));
      }
    } catch (err) {
      var msg = friendlyRefuse(err.data) || err.message || "Start failed";
      state.session = {
        status: "refused",
        sid: null,
        stream_path: null,
        error: msg,
        message: msg,
      };
      renderSession();
      showError(msg);
    } finally {
      el.btnStart.disabled = false;
    }
  }

  function friendlyRefuse(data) {
    if (!data) return "";
    var d = data.detail || data.error || data.message || data.reason || "";
    if (typeof d === "object" && d !== null) {
      d = d.message || d.error || JSON.stringify(d);
    }
    d = String(d || "");
    var low = d.toLowerCase();
    if (low.indexOf("ram") >= 0 || low.indexOf("memory") >= 0 || low.indexOf("headroom") >= 0) {
      return "Host is protecting free RAM — new sessions are refused right now.\n" + d;
    }
    if (low.indexOf("seat") >= 0 || low.indexOf("capacity") >= 0 || low.indexOf("max_session") >= 0 || low.indexOf("full") >= 0) {
      return "All seats are taken. Try again when someone stops, or ask an admin.\n" + d;
    }
    if (low.indexOf("heavy") >= 0 || low.indexOf("gpu") >= 0) {
      return "No heavy/GPU seat available.\n" + d;
    }
    if (low.indexOf("refuse") >= 0) {
      return "New sessions are temporarily refused.\n" + d;
    }
    return d;
  }

  async function stopSession() {
    el.btnStop.disabled = true;
    el.btnStopStream.disabled = true;
    try {
      await api("/api/sessions", { method: "DELETE" });
    } catch (err) {
      // 404 = already gone — fine
      if (err.status !== 404) {
        showError("Stop failed: " + err.message);
      }
    } finally {
      stopPolling();
      state.streamAttached = false;
      state.session = { status: "idle", sid: null, stream_path: null, error: null, message: "" };
      el.streamFrame.removeAttribute("src");
      renderSession();
      el.btnStop.disabled = false;
      el.btnStopStream.disabled = false;
    }
  }

  function reconnectSession() {
    state.streamAttached = true;
    var url = streamUrl(state.session);
    if (!url) {
      refreshSession().then(function () {
        state.streamAttached = true;
        renderSession();
      });
      return;
    }
    renderSession();
  }

  function renderUser() {
    var name = state.user.display || state.user.email || "Signed in";
    el.userName.textContent = name;
    el.userChip.title = state.user.email || name;
  }

  async function loadWhoAmI() {
    // Prefer /api/sessions payload; fall back to optional /api/me if present.
    try {
      var me = await api("/api/me");
      if (me) {
        state.user.email = me.email || me.user_email || state.user.email;
        state.user.display = me.display || me.user_display || me.name || state.user.display;
        renderUser();
        if (me.role === "admin" && !document.getElementById("admin-link")) {
          var chip = document.getElementById("user-chip");
          var a = document.createElement("a");
          a.id = "admin-link";
          a.href = "/admin";
          a.textContent = "Admin";
          a.className = "btn ghost";
          a.style.marginRight = "8px";
          chip.parentNode.insertBefore(a, chip);
        }
        return;
      }
    } catch (_) { /* optional */ }
  }

  /* -------------------- chat -------------------- */

  function renderPresence(users) {
    state.presence = users || [];
    el.presenceList.innerHTML = "";
    state.presence.forEach(function (u) {
      var li = document.createElement("li");
      li.textContent = u.display || u.email || "?";
      li.title = u.email || "";
      el.presenceList.appendChild(li);
    });
    var n = state.presence.length;
    el.presenceCount.textContent = n + " online";
  }

  function appendMessage(msg, opts) {
    opts = opts || {};
    if (!msg) return;
    if (msg.id != null) {
      var key = String(msg.id);
      if (state.seenMsgIds[key]) return;
      state.seenMsgIds[key] = true;
    }
    var div = document.createElement("div");
    div.className = "chat-msg";
    div.innerHTML =
      '<div class="meta"><span class="who"></span><span class="when"></span></div>' +
      '<div class="body"></div>';
    div.querySelector(".who").textContent = msg.display || msg.user || "user";
    div.querySelector(".when").textContent = fmtTime(msg.ts);
    div.querySelector(".body").textContent = msg.text || "";
    el.chatMessages.appendChild(div);
    if (!opts.noScroll) {
      el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
    }
  }

  function appendSystem(text) {
    var div = document.createElement("div");
    div.className = "chat-sys";
    div.textContent = text;
    el.chatMessages.appendChild(div);
    el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
  }

  async function loadHistory() {
    try {
      var data = await api("/api/chat/history?limit=100");
      var msgs = (data && data.messages) || [];
      el.chatMessages.innerHTML = "";
      state.seenMsgIds = Object.create(null);
      msgs.forEach(function (m) { appendMessage(m, { noScroll: true }); });
      el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
    } catch (err) {
      appendSystem("History unavailable: " + err.message);
    }
  }

  function wsUrl() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + location.host + "/ws/chat";
  }

  function connectChat() {
    if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    var ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      appendSystem("Chat connect failed");
      scheduleReconnect();
      return;
    }
    state.ws = ws;
    ws.onopen = function () {
      state.wsBackoff = 800;
    };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      var type = msg.type || "message";
      if (type === "presence") {
        renderPresence(msg.users || []);
        return;
      }
      if (type === "join") {
        appendSystem((msg.display || msg.user) + " joined");
        // refresh list opportunistically
        if (msg.users) renderPresence(msg.users);
        else upsertPresence(msg.user, msg.display, true);
        return;
      }
      if (type === "leave") {
        appendSystem((msg.display || msg.user) + " left");
        upsertPresence(msg.user, msg.display, false);
        return;
      }
      if (type === "message") {
        appendMessage(msg);
        if (!state.chatOpen) {
          state.unread += 1;
          localStorage.setItem(LS_UNREAD, String(state.unread));
          renderBadges();
        }
      }
    };
    ws.onclose = function () {
      state.ws = null;
      scheduleReconnect();
    };
    ws.onerror = function () {
      try { ws.close(); } catch (_) {}
    };
  }

  function scheduleReconnect() {
    var delay = state.wsBackoff;
    state.wsBackoff = Math.min(state.wsBackoff * 1.6, 10000);
    setTimeout(connectChat, delay);
  }

  function upsertPresence(email, display, online) {
    email = (email || "").toLowerCase();
    var list = state.presence.slice();
    var idx = -1;
    for (var i = 0; i < list.length; i++) {
      if ((list[i].email || "").toLowerCase() === email) { idx = i; break; }
    }
    if (online) {
      if (idx >= 0) list[idx] = { email: email, display: display || list[idx].display };
      else list.push({ email: email, display: display || email });
    } else if (idx >= 0) {
      list.splice(idx, 1);
    }
    renderPresence(list);
  }

  function sendChat(text) {
    text = (text || "").trim();
    if (!text || !state.ws || state.ws.readyState !== WebSocket.OPEN) return false;
    state.ws.send(JSON.stringify({ text: text }));
    return true;
  }

  /* -------------------- wire up -------------------- */

  el.btnStart.addEventListener("click", startSession);
  el.btnReconnect.addEventListener("click", reconnectSession);
  el.btnStop.addEventListener("click", stopSession);
  el.btnStopStream.addEventListener("click", stopSession);
  el.btnPopout.addEventListener("click", function () {
    var url = streamUrl(state.session) || el.streamFrame.getAttribute("src");
    if (url) window.open(url, "_blank", "noopener");
  });
  var btnFullscreen = document.getElementById("btn-fullscreen");
  if (btnFullscreen) {
    btnFullscreen.addEventListener("click", function () {
      // Native fullscreen on the stream iframe: full-screen CAD control.
      // Esc (or the button again) returns to the portal with browser chrome + shoutbox.
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else if (el.streamFrame && el.streamFrame.getAttribute("src")) {
        (el.streamFrame.requestFullscreen ? el.streamFrame.requestFullscreen()
          : Promise.reject()).catch(function () {});
      }
    });
    document.addEventListener("fullscreenchange", function () {
      btnFullscreen.textContent = document.fullscreenElement ? "Exit fullscreen" : "Fullscreen";
    });
  }
  var btnPaste = document.getElementById("btn-paste");
  var pasteModal = document.getElementById("paste-modal");
  if (btnPaste && pasteModal) {
    var pasteText = document.getElementById("paste-text");
    var pasteSend = document.getElementById("paste-send");
    function closePaste() { pasteModal.style.display = "none"; pasteText.value = ""; }
    btnPaste.addEventListener("click", function () {
      if (!isLive(state.status)) { setStatusPill("error", "Start a session first"); return; }
      pasteModal.style.display = "block";
      // Best effort: pre-fill from the browser clipboard when permitted.
      if (navigator.clipboard && navigator.clipboard.readText) {
        navigator.clipboard.readText().then(function (t) {
          if (t && !pasteText.value) pasteText.value = t;
        }).catch(function () {});
      }
      pasteText.focus();
    });
    document.getElementById("paste-cancel").addEventListener("click", closePaste);
    pasteModal.addEventListener("click", function (ev) {
      if (ev.target === pasteModal) closePaste();
    });
    pasteSend.addEventListener("click", function () {
      var text = pasteText.value;
      if (!text) { closePaste(); return; }
      pasteSend.disabled = true;
      pasteSend.textContent = "Typing…";
      fetch("/api/sessions/paste", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      }).then(function (res) {
        if (!res.ok) return res.json().then(function (d) {
          throw new Error((d && d.detail) || res.status);
        });
        closePaste();
      }).catch(function (e) {
        setStatusPill("error", "Paste failed: " + (e.message || e));
      }).finally(function () {
        pasteSend.disabled = false;
        pasteSend.textContent = "Type it";
      });
    });
  }
  var btnImport = document.getElementById("btn-import");
  var importFile = document.getElementById("import-file");
  if (btnImport && importFile) {
    btnImport.addEventListener("click", function () {
      if (!isLive(state.status)) { setStatusPill("error", "Start a session first"); return; }
      importFile.click();
    });
    importFile.addEventListener("change", async function () {
      var f = importFile.files && importFile.files[0];
      importFile.value = "";
      if (!f) return;
      if (f.size > 256 * 1024 * 1024) { alert("File too large (256 MB max)."); return; }
      btnImport.disabled = true;
      btnImport.textContent = "Uploading…";
      try {
        var fd = new FormData();
        fd.append("file", f, f.name);
        var r = await fetch("/transfer?path=" + encodeURIComponent("Projects/imports/" + f.name),
                            { method: "POST", body: fd, credentials: "same-origin" });
        if (!r.ok) throw new Error((await r.json().catch(function () { return {}; })).detail || ("upload failed (" + r.status + ")"));
        btnImport.textContent = "Imported \u2713";
        alert("Uploaded " + f.name + ".\n\nIn the app: File \u2192 Open \u2192 imports/" + f.name);
      } catch (e) {
        alert("Import failed: " + (e.message || e));
        btnImport.textContent = "Import part\u2026";
      } finally {
        btnImport.disabled = false;
        setTimeout(function () { btnImport.textContent = "Import part\u2026"; }, 4000);
      }
    });
  }
  el.chatToggle.addEventListener("click", function () { setChatOpen(!state.chatOpen); });
  el.chatCollapse.addEventListener("click", function () { setChatOpen(false); });
  el.chatRailBtn.addEventListener("click", function () { setChatOpen(true); });
  el.chatForm.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = el.chatInput.value;
    if (sendChat(text)) el.chatInput.value = "";
  });

  setChatOpen(state.chatOpen);
  renderBadges();
  renderUser();
  renderSession();

  loadWhoAmI()
    .then(function () { return refreshSession(); })
    .then(loadHistory)
    .then(connectChat)
    .catch(function () { connectChat(); });
})();
