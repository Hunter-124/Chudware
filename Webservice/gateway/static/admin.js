/* chudware-web /admin — account administration */
(function () {
  "use strict";

  var statusEl = document.getElementById("admin-status");
  var rows = document.getElementById("account-rows");
  var cardsEl = document.getElementById("account-cards");
  var filterEl = document.getElementById("account-filter");
  var totalEl = document.getElementById("account-total");
  var pendingEl = document.getElementById("account-pending");
  var rangeEl = document.getElementById("account-range");
  var prevBtn = document.getElementById("account-prev");
  var nextBtn = document.getElementById("account-next");

  function setStatus(msg, isErr) {
    statusEl.textContent = msg || "";
    statusEl.className = isErr ? "err" : "";
  }

  async function api(path, opts) {
    var res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
    var data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) {
      throw new Error((data && (data.detail || data.error)) || res.status + " " + res.statusText);
    }
    return data;
  }

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  /* ---- accounts: the approval queue and everyone already through it ---- */

  var PAGE_SIZE = 50;
  var PLANS = ["free", "trial", "pro", "team"];
  var STATUS_CLASS = {
    pending: "st-pending",
    active: "st-active",
    denied: "st-denied",
    suspended: "st-suspended"
  };
  var page = { status: "", offset: 0, total: 0 };

  function toDate(secs) {
    var n = Number(secs);
    if (!secs || isNaN(n)) return null;
    var d = new Date(n * 1000);
    return isNaN(d.getTime()) ? null : d;
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function fmtDate(secs) {
    var d = toDate(secs);
    return d ? d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) : "\u2014";
  }

  function fmtWhen(secs) {
    var d = toDate(secs);
    return d ? d.toLocaleString() : "";
  }

  function statusPill(status) {
    return "<span class='pill " + (STATUS_CLASS[status] || "st-unknown") + "'>" +
           esc(status || "unknown") + "</span>";
  }

  function roleHtml(role) {
    return "<span class='" + (role === "admin" ? "ac-admin" : "ac-dim") + "'>" +
           esc(role || "user") + "</span>";
  }

  // One literal per endpoint the card drives, so the API surface greps cleanly.
  var ACTION_PATH = {
    approve: "/api/admin/accounts/{email}/approve",
    deny: "/api/admin/accounts/{email}/deny",
    suspend: "/api/admin/accounts/{email}/suspend",
    plan: "/api/admin/accounts/{email}/plan"
  };

  function accountUrl(email, action) {
    return ACTION_PATH[action].replace("{email}", encodeURIComponent(email));
  }

  function applyPending(n) {
    var v = Number(n) || 0;
    pendingEl.textContent = v ? v + " pending" : "";
    pendingEl.hidden = !v;
  }

  async function act(email, action, verb) {
    setStatus(verb + " " + email + "\u2026");
    try {
      var r = await api(accountUrl(email, action), { method: "POST", body: "{}" });
      applyPending(r.pending);
      setStatus(email + " is now " + r.account.status + ".");
      await load();
    } catch (e) { setStatus(String(e.message || e), true); }
  }

  function actionBtn(label, cls, onClick) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function planSelect(a) {
    var sel = document.createElement("select");
    sel.title = "Plan for " + a.email;
    PLANS.forEach(function (p) {
      var o = document.createElement("option");
      o.value = p;
      o.textContent = p;
      if (p === a.plan) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener("change", async function () {
      var to = sel.value;
      setStatus("Moving " + a.email + " to the " + to + " plan\u2026");
      try {
        var r = await api(accountUrl(a.email, "plan"),
                          { method: "POST", body: JSON.stringify({ plan: to }) });
        applyPending(r.pending);
        setStatus(a.email + " is on the " + r.account.plan + " plan.");
        await load();
      } catch (e) {
        sel.value = a.plan;
        setStatus(String(e.message || e), true);
      }
    });
    return sel;
  }

  function fillPlan(host, a, prefix) {
    if (prefix) host.appendChild(document.createTextNode(prefix));
    if (a.status === "active") host.appendChild(planSelect(a));
    else host.appendChild(document.createTextNode(a.plan || "free"));
  }

  function fillActions(host, a) {
    if (a.status === "pending") {
      host.appendChild(actionBtn("Approve", "btn primary", function () {
        act(a.email, "approve", "Approving");
      }));
      host.appendChild(actionBtn("Deny", "rm-btn", function () {
        if (confirm("Deny " + a.email + "? They stay registered but get no access."))
          act(a.email, "deny", "Denying");
      }));
    } else if (a.status === "active") {
      host.appendChild(actionBtn("Suspend", "rm-btn", function () {
        if (confirm("Suspend " + a.email + "? Their access stops immediately."))
          act(a.email, "suspend", "Suspending");
      }));
    } else {
      host.appendChild(actionBtn("Reinstate", "btn ghost", function () {
        act(a.email, "approve", "Reinstating");
      }));
    }
  }

  function placeholder(msg) {
    rows.innerHTML = "<tr><td colspan='7' class='ac-dim'>" + esc(msg) + "</td></tr>";
    cardsEl.innerHTML = "<div class='ac-dim'>" + esc(msg) + "</div>";
  }

  function render(list) {
    rows.innerHTML = "";
    cardsEl.innerHTML = "";
    list.forEach(function (a) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td class='ac-email'>" + esc(a.email) + "</td>" +
        "<td>" + (a.display ? esc(a.display) : "<span class='ac-dim'>\u2014</span>") + "</td>" +
        "<td>" + statusPill(a.status) + "</td>" +
        "<td class='ac-plan'></td>" +
        "<td class='ac-role'>" + roleHtml(a.role) + "</td>" +
        "<td class='ac-when' title='" + esc(fmtWhen(a.created_at)) + "'>" +
          esc(fmtDate(a.created_at)) + "</td>" +
        "<td class='ac-act'><div class='ac-actions'></div></td>";
      fillPlan(tr.querySelector(".ac-plan"), a, "");
      fillActions(tr.querySelector(".ac-actions"), a);
      rows.appendChild(tr);

      var card = document.createElement("div");
      card.className = "account-card";
      card.innerHTML =
        "<div class='ac-email'>" + esc(a.email) + "</div>" +
        "<div class='ac-meta'>" + (a.display ? esc(a.display) : "No display name") + "</div>" +
        "<div class='ac-row'>" + statusPill(a.status) + roleHtml(a.role) + "</div>" +
        "<div class='ac-meta'>Created " + esc(fmtDate(a.created_at)) + "</div>" +
        "<div class='ac-plan'></div>" +
        "<div class='ac-actions'></div>";
      fillPlan(card.querySelector(".ac-plan"), a, "Plan ");
      fillActions(card.querySelector(".ac-actions"), a);
      cardsEl.appendChild(card);
    });
  }

  function fetchPage() {
    var q = "/api/admin/accounts?limit=" + PAGE_SIZE + "&offset=" + page.offset;
    if (page.status) q += "&status=" + encodeURIComponent(page.status);
    return api(q);
  }

  async function load() {
    var data = await fetchPage();
    var list = data.accounts || [];
    if (!list.length && page.offset > 0) {
      // The page we were sitting on emptied out (last pending approved, filter
      // flipped under us) — fall back to the last page that still has rows.
      var lastOffset = Math.max(0, (Math.ceil((data.total || 0) / PAGE_SIZE) - 1) * PAGE_SIZE);
      if (lastOffset !== page.offset) {
        page.offset = lastOffset;
        data = await fetchPage();
        list = data.accounts || [];
      }
    }
    page.total = data.total || 0;
    page.offset = data.offset || 0;
    applyPending(data.pending);
    totalEl.textContent = page.total === 1 ? "1 account" : page.total + " accounts";
    rangeEl.textContent = list.length
      ? (page.offset + 1) + "\u2013" + (page.offset + list.length)
      : "";
    prevBtn.disabled = page.offset <= 0;
    nextBtn.disabled = page.offset + list.length >= page.total;
    if (!list.length) {
      placeholder(page.status
        ? "No " + page.status + " accounts."
        : "No accounts yet \u2014 people show up here as they register.");
      return;
    }
    render(list);
  }

  function reload() {
    load().catch(function (e) {
      placeholder("Couldn't load accounts.");
      setStatus("Failed to load accounts: " + (e.message || e) + " (admin only)", true);
    });
  }

  filterEl.addEventListener("change", function () {
    page.status = filterEl.value;
    page.offset = 0;
    reload();
  });
  prevBtn.addEventListener("click", function () {
    page.offset = Math.max(0, page.offset - PAGE_SIZE);
    reload();
  });
  nextBtn.addEventListener("click", function () {
    if (page.offset + PAGE_SIZE >= page.total) return;
    page.offset += PAGE_SIZE;
    reload();
  });

  placeholder("Loading accounts\u2026");
  reload();

  /* ---- host folder browser (admin-only, read-only) ---- */
  var modal = document.getElementById("browse-modal");

  async function browseTo(path) {
    var data = await api("/api/admin/browse?path=" + encodeURIComponent(path || ""));
    document.getElementById("browse-path").textContent = data.path;
    var list = document.getElementById("browse-list");
    list.innerHTML = "";
    function row(label, cls, onClick) {
      var d = document.createElement("div");
      d.textContent = label;
      d.style.cssText = "padding:5px 14px;font-size:.88rem" +
                        (onClick ? ";cursor:pointer" : "") +
                        (cls === "part" ? ";color:#55d98a" : "");
      if (onClick) {
        d.addEventListener("mouseenter", function () { d.style.background = "rgba(255,255,255,.06)"; });
        d.addEventListener("mouseleave", function () { d.style.background = ""; });
        d.addEventListener("click", onClick);
      }
      list.appendChild(d);
    }
    if (data.parent) row("\u2B06 ..", "dir", function () { browseTo(data.parent).catch(showBrowseErr); });
    data.dirs.forEach(function (n) {
      row("\uD83D\uDCC1 " + n, "dir", function () {
        browseTo(data.path.replace(/\/$/, "") + "/" + n).catch(showBrowseErr);
      });
    });
    data.parts.forEach(function (n) {
      row("\u2699 " + n, "part", null);
    });
    if (!data.dirs.length && !data.parts.length) {
      var empty = document.createElement("div");
      empty.textContent = "(no folders or part files here)";
      empty.style.cssText = "padding:8px 14px;opacity:.5;font-size:.85rem";
      list.appendChild(empty);
    }
  }

  function showBrowseErr(e) { setStatus(String(e.message || e), true); }

  var btnBrowse = document.getElementById("btn-browse");
  if (btnBrowse && modal) {
    btnBrowse.addEventListener("click", function () {
      modal.style.display = "block";
      browseTo("").catch(showBrowseErr);
    });
    document.getElementById("browse-close").addEventListener("click", function () {
      modal.style.display = "none";
    });
    modal.addEventListener("click", function (ev) {
      if (ev.target === modal) modal.style.display = "none";
    });
  }

  /* ---- live ops: host health, log tail ---- */
  var hostBar = document.getElementById("host-bar");

  async function refreshOps() {
    if (!hostBar) return;
    var data = await api("/api/admin/ops");
    var h = data.host || {};
    var bits = [];
    if (h.mem_available_gib != null) bits.push(h.mem_available_gib.toFixed(1) + " GiB RAM free");
    if (h.gpu) bits.push("VRAM " + (h.gpu.vram_used_mib / 1024).toFixed(1) + "/" +
                         (h.gpu.vram_total_mib / 1024).toFixed(1) + " GiB · GPU " + h.gpu.util_pct + "%");
    if (h.load1 != null) bits.push("load " + h.load1.toFixed(1));
    hostBar.textContent = "";
    hostBar.appendChild(document.createTextNode(bits.join("  ·  ") + "  ·  "));

    // Sibling service chips — a dead tunnel means nobody can reach the site.
    Object.keys(h.units || {}).forEach(function (name) {
      var ok = h.units[name] === "active";
      var chip = document.createElement("span");
      chip.className = "pill " + (ok ? "user" : "admin");
      chip.style.marginRight = "6px";
      chip.title = "systemd unit state: " + h.units[name];
      chip.textContent = name + (ok ? "" : " " + h.units[name]);
      hostBar.appendChild(chip);
    });
    // REFUSE_NEW flag (shared with the disk monitor's disk-pressure check).
    var pause = document.createElement("button");
    pause.type = "button";
    pause.className = "rm-btn";
    pause.style.marginLeft = "8px";
    if (data.refuse_new) {
      pause.textContent = "REFUSE_NEW set — clear";
      pause.title = "Removes the REFUSE_NEW flag (the monitor re-creates it while disk is critical)";
    } else {
      pause.textContent = "set REFUSE_NEW";
      pause.title = "Touches the REFUSE_NEW flag the disk monitor uses to signal disk pressure";
    }
    pause.addEventListener("click", async function () {
      try {
        await api("/api/admin/refuse-new",
                  { method: "PUT", body: JSON.stringify({ on: !data.refuse_new }) });
        refreshOps();
      } catch (e) { setStatus(String(e.message || e), true); }
    });
    hostBar.appendChild(pause);
  }

  var logPre = document.getElementById("log-pre");
  async function refreshLog() {
    var res = await fetch("/api/admin/logs?lines=300");
    logPre.textContent = await res.text();
    logPre.scrollTop = logPre.scrollHeight;
  }
  var logBtn = document.getElementById("log-refresh");
  if (logBtn) logBtn.addEventListener("click", function () {
    refreshLog().catch(function (e) { setStatus(String(e.message || e), true); });
  });

  if (hostBar) {
    refreshOps().catch(function () { hostBar.textContent = "ops unavailable"; });
    setInterval(function () {
      refreshOps().catch(function () {});
      if (document.getElementById("log-follow") &&
          document.getElementById("log-follow").checked) refreshLog().catch(function () {});
    }, 5000);
  }

  /* ---- deploy: rebuild / redeploy ---- */
  (function initDeploy() {
    var side = document.querySelector(".admin-side");
    if (!side) return;

    var card = document.createElement("div");
    card.className = "admin-card deploy-card";
    card.innerHTML =
      "<h2>Deploy</h2>" +
      "<div class='deploy-section'>" +
        "<span class='deploy-label'>Rebuild targets</span>" +
        "<div class='deploy-targets' id='deploy-targets'>" +
          "<label><input type='checkbox' value='wasm' checked> wasm</label>" +
          "<label><input type='checkbox' value='engine'> engine</label>" +
        "</div>" +
        "<div class='deploy-actions'>" +
          "<button type='button' class='btn primary' id='deploy-rebuild'>Rebuild</button>" +
          "<button type='button' class='btn ghost' id='deploy-redeploy'>Redeploy gateway</button>" +
        "</div>" +
        "<p class='deploy-note'>Redeploy restarts chudware-gateway — this page and terminals/WS will briefly reconnect.</p>" +
      "</div>" +
      "<div class='deploy-status-row'>" +
        "<span class='dep-pill dep-idle' id='deploy-status'>idle</span>" +
      "</div>" +
      "<pre class='deploy-log' id='deploy-log'>(no deploy log yet)</pre>";

    // Insert right after the first admin-card (Host health).
    var firstCard = side.querySelector(".admin-card");
    if (firstCard && firstCard.nextSibling) {
      side.insertBefore(card, firstCard.nextSibling);
    } else if (firstCard) {
      firstCard.insertAdjacentElement("afterend", card);
    } else {
      side.appendChild(card);
    }

    var statusPill = document.getElementById("deploy-status");
    var logPre = document.getElementById("deploy-log");
    var btnRebuild = document.getElementById("deploy-rebuild");
    var btnRedeploy = document.getElementById("deploy-redeploy");
    var targetBox = document.getElementById("deploy-targets");
    var targetInputs = targetBox
      ? Array.prototype.slice.call(targetBox.querySelectorAll("input[type=checkbox]"))
      : [];

    var pollTimer = null;
    var activeKind = null;
    var uiRunning = false;

    function setControlsDisabled(disabled) {
      btnRebuild.disabled = disabled;
      btnRedeploy.disabled = disabled;
      targetInputs.forEach(function (cb) { cb.disabled = disabled; });
    }

    function setPill(cls, text) {
      statusPill.className = "dep-pill " + cls;
      statusPill.textContent = text;
    }

    function selectedTargets() {
      return targetInputs.filter(function (cb) { return cb.checked; })
                         .map(function (cb) { return cb.value; });
    }

    function stopPolling() {
      if (pollTimer != null) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function startPolling(kind) {
      if (kind) activeKind = kind;
      stopPolling();
      pollOnce();
      pollTimer = setInterval(pollOnce, 2000);
    }

    function renderLast(last) {
      if (!last) {
        setPill("dep-idle", "idle");
        return;
      }
      var ok = last.rc === 0;
      var kind = last.kind || "deploy";
      var label = "last: " + (ok ? "ok" : "fail") + " rc=" + last.rc +
                  (kind ? " · " + kind : "");
      setPill(ok ? "dep-ok" : "dep-fail", label);
    }

    async function fetchLog(kind) {
      if (!kind) return;
      try {
        var res = await fetch("/api/admin/deploy/log?kind=" +
                              encodeURIComponent(kind) + "&lines=400");
        var text = await res.text();
        if (res.ok) {
          logPre.textContent = text || "(empty log)";
          logPre.scrollTop = logPre.scrollHeight;
        }
      } catch (e) { /* ignore transient log fetch errors */ }
    }

    async function pollOnce() {
      try {
        var st = await api("/api/admin/deploy/status");
        var running = !!(st && st.running);
        var kind = (st && st.kind) || activeKind;

        if (running) {
          uiRunning = true;
          activeKind = kind || activeKind;
          setControlsDisabled(true);
          setPill("dep-run", "running " + (kind || "…"));
          await fetchLog(kind || activeKind);
          // Ensure continuous polling when we discover a live run (e.g. on load).
          if (pollTimer == null) {
            pollTimer = setInterval(pollOnce, 2000);
          }
        } else {
          var wasPolling = pollTimer != null || uiRunning;
          stopPolling();
          uiRunning = false;
          setControlsDisabled(false);
          if (st && st.last) {
            renderLast(st.last);
            // Keep showing the finished run's log.
            var logKind = (st.last && st.last.kind) || activeKind || kind;
            if (wasPolling && logKind) await fetchLog(logKind);
          } else {
            setPill("dep-idle", "idle");
          }
          activeKind = null;
        }
      } catch (e) {
        // Status endpoint blip (e.g. during redeploy restart) — keep trying.
      }
    }

    function isBusyError(e) {
      var msg = String((e && e.message) || e || "").toLowerCase();
      return msg.indexOf("busy") >= 0 || msg.indexOf("409") >= 0 ||
             msg.indexOf("already") >= 0;
    }

    btnRebuild.addEventListener("click", async function () {
      var targets = selectedTargets();
      if (!targets.length) {
        setStatus("Pick at least one rebuild target.", true);
        return;
      }
      setControlsDisabled(true);
      try {
        var r = await api("/api/admin/deploy/rebuild", {
          method: "POST",
          body: JSON.stringify({ targets: targets })
        });
        activeKind = (r && r.kind) || "rebuild";
        setPill("dep-run", "running " + activeKind);
        setStatus("Rebuild started.");
        startPolling(activeKind);
      } catch (e) {
        if (isBusyError(e)) {
          setStatus("A deploy is already running.", true);
          uiRunning = true;
          setControlsDisabled(true);
          startPolling("rebuild");
        } else {
          setControlsDisabled(false);
          setStatus(String(e.message || e), true);
        }
      }
    });

    btnRedeploy.addEventListener("click", async function () {
      if (!confirm("Restart the gateway now? Terminals/WS will briefly reconnect.")) return;
      setControlsDisabled(true);
      try {
        var r = await api("/api/admin/deploy/redeploy", {
          method: "POST",
          body: "{}"
        });
        activeKind = (r && r.kind) || "redeploy";
        setPill("dep-run", "running " + activeKind);
        setStatus("Redeploy started — this page may briefly disconnect.");
        startPolling(activeKind);
      } catch (e) {
        if (isBusyError(e)) {
          setStatus("A deploy is already running.", true);
          uiRunning = true;
          setControlsDisabled(true);
          startPolling("redeploy");
        } else {
          setControlsDisabled(false);
          setStatus(String(e.message || e), true);
        }
      }
    });

    // Reflect a run started elsewhere (shared workspace).
    pollOnce().catch(function () {});
  })();

  /* ---- releases: installer downloads for testing ---- */
  (function initReleases() {
    var side = document.querySelector(".admin-side");
    if (!side) return;

    var card = document.createElement("div");
    card.className = "admin-card releases-card";
    card.innerHTML =
      "<h2>Releases</h2>" +
      "<div class='releases-list' id='releases-list'>Loading…</div>" +
      "<div class='release-upload'>" +
        "<input type='file' id='release-file' />" +
        "<button type='button' class='btn ghost' id='release-upload-btn'>Upload</button>" +
        "<span class='release-upload-status' id='release-upload-status'></span>" +
      "</div>";
    side.appendChild(card);

    var listEl = document.getElementById("releases-list");
    var fileEl = document.getElementById("release-file");
    var uploadBtn = document.getElementById("release-upload-btn");
    var uploadStatus = document.getElementById("release-upload-status");

    function fmtSize(n) {
      if (n < 1024) return n + " B";
      var units = ["KB", "MB", "GB", "TB"];
      var v = n / 1024;
      var i = 0;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      return v.toFixed(v < 10 ? 2 : 1) + " " + units[i];
    }

    function fmtDate(iso) {
      var d = new Date(iso);
      return isNaN(d.getTime()) ? iso : d.toLocaleString();
    }

    async function refreshReleases() {
      var data = await api("/api/admin/releases");
      if (!data || !data.length) {
        listEl.innerHTML =
          "<p class='deploy-note'>No releases yet. Windows installers can't be built on this " +
          "Linux host — build one on a Windows box (scripts\\build_windows.ps1, then " +
          "installer\\build_release.py --zip, then packaging\\windows\\chudware.iss) and upload " +
          "it below, or drop it into ~/chudware-releases on the server.</p>";
        return;
      }
      listEl.innerHTML = data.map(function (r) {
        return "<div class='release-row'>" +
          "<div class='release-info'>" +
            "<span class='release-name'>" + esc(r.name) + "</span>" +
            "<span class='release-meta'>" + fmtSize(r.size) + " \xb7 " + fmtDate(r.mtime) + "</span>" +
          "</div>" +
          "<div class='release-actions'>" +
            "<a class='btn ghost' href='/api/admin/releases/download/" + encodeURIComponent(r.name) +
              "' download>Download</a>" +
            "<button type='button' class='btn ghost danger release-delete' data-name='" +
              esc(r.name) + "'>Delete</button>" +
          "</div>" +
        "</div>";
      }).join("");

      Array.prototype.forEach.call(listEl.querySelectorAll(".release-delete"), function (btn) {
        btn.addEventListener("click", async function () {
          var name = btn.getAttribute("data-name");
          if (!window.confirm("Delete " + name + "?")) return;
          btn.disabled = true;
          try {
            await api("/api/admin/releases/" + encodeURIComponent(name), { method: "DELETE" });
            await refreshReleases();
          } catch (e) {
            window.alert("Delete failed: " + (e.message || e));
            btn.disabled = false;
          }
        });
      });
    }

    uploadBtn.addEventListener("click", async function () {
      var f = fileEl.files && fileEl.files[0];
      if (!f) {
        uploadStatus.textContent = "choose a file first";
        return;
      }
      uploadBtn.disabled = true;
      uploadStatus.textContent = "uploading " + f.name + "\u2026";
      try {
        var fd = new FormData();
        fd.append("file", f, f.name);
        var res = await fetch("/api/admin/releases/upload", { method: "POST", body: fd });
        var data = null;
        try { data = await res.json(); } catch (e) { /* non-JSON */ }
        if (!res.ok) {
          throw new Error((data && (data.detail || data.error)) || res.status + " " + res.statusText);
        }
        uploadStatus.textContent = "uploaded " + f.name;
        fileEl.value = "";
        await refreshReleases();
      } catch (e) {
        uploadStatus.textContent = "upload failed: " + (e.message || e);
      } finally {
        uploadBtn.disabled = false;
      }
    });

    refreshReleases().catch(function (e) {
      listEl.innerHTML = "<p class='deploy-note'>Failed to load releases: " + esc(e.message || e) + "</p>";
    });
  })();
})();
