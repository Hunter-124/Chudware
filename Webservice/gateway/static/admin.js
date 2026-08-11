/* chudware-web /admin — allowlist management */
(function () {
  "use strict";

  var rows = document.getElementById("user-rows");
  var form = document.getElementById("add-form");
  var statusEl = document.getElementById("admin-status");

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

  var state = { defaultProject: "", defaultDoc: "" };

  function baseName(p) { var i = p.lastIndexOf("/"); return i < 0 ? p : p.slice(i + 1); }

  function renderDocRow() {
    var row = document.getElementById("default-doc-row");
    if (!row) return;
    row.textContent = state.defaultDoc
      ? "Auto-opens for everyone: " + state.defaultDoc
      : "Auto-opens the most recently modified part in the folder. Use Browse host… to pick a specific one.";
  }

  var defForm = document.getElementById("default-form");
  if (defForm) {
    defForm.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      var p = document.getElementById("f-default-project").value.trim();
      setStatus(p ? "Setting default project to " + p + "…" : "Clearing default project…");
      try {
        await api("/api/admin/default-project",
                  { method: "PUT", body: JSON.stringify({ project: p, doc: p ? state.defaultDoc : "" }) });
        setStatus(p ? "Default project set: everyone without an explicit project now opens " +
                      (state.defaultDoc ? state.defaultDoc + " from " : "") + baseName(p) +
                      " on their next session start." : "Default project cleared.");
        refresh();
      } catch (e) { setStatus(String(e.message || e), true); }
    });
  }

  /* ---- host folder browser (admin-only) ---- */
  var modal = document.getElementById("browse-modal");
  var browse = { path: "" };

  async function browseTo(path) {
    var data = await api("/api/admin/browse?path=" + encodeURIComponent(path || ""));
    browse.path = data.path;
    document.getElementById("browse-path").textContent = data.path;
    var list = document.getElementById("browse-list");
    list.innerHTML = "";
    function row(label, cls, onClick) {
      var d = document.createElement("div");
      d.textContent = label;
      d.style.cssText = "padding:5px 14px;cursor:pointer;font-size:.88rem" +
                        (cls === "part" ? ";color:#55d98a" : "");
      d.addEventListener("mouseenter", function () { d.style.background = "rgba(255,255,255,.06)"; });
      d.addEventListener("mouseleave", function () { d.style.background = ""; });
      d.addEventListener("click", onClick);
      list.appendChild(d);
    }
    if (data.parent) row("\u2B06 ..", "dir", function () { browseTo(data.parent).catch(showBrowseErr); });
    data.dirs.forEach(function (n) {
      row("\uD83D\uDCC1 " + n, "dir", function () {
        browseTo(data.path.replace(/\/$/, "") + "/" + n).catch(showBrowseErr);
      });
    });
    data.parts.forEach(function (n) {
      row("\u2699 " + n + " — click to auto-open this part", "part", function () {
        state.defaultDoc = n;
        pickFolder(data.path);
      });
    });
    if (!data.dirs.length && !data.parts.length) {
      var empty = document.createElement("div");
      empty.textContent = "(no folders or part files here)";
      empty.style.cssText = "padding:8px 14px;opacity:.5;font-size:.85rem";
      list.appendChild(empty);
    }
  }

  function showBrowseErr(e) { setStatus(String(e.message || e), true); }

  function pickFolder(path) {
    document.getElementById("f-default-project").value = path;
    modal.style.display = "none";
    renderDocRow();
    setStatus("Folder picked — hit \u201CSet for all\u201D to apply.");
  }

  var btnBrowse = document.getElementById("btn-browse");
  if (btnBrowse && modal) {
    btnBrowse.addEventListener("click", function () {
      state.defaultDoc = "";
      modal.style.display = "block";
      var cur = document.getElementById("f-default-project").value.trim();
      browseTo(cur.indexOf("/") === 0 ? cur : "").catch(showBrowseErr);
    });
    document.getElementById("browse-cancel").addEventListener("click", function () {
      modal.style.display = "none";
    });
    document.getElementById("browse-use").addEventListener("click", function () {
      state.defaultDoc = "";
      pickFolder(browse.path);
    });
    modal.addEventListener("click", function (ev) {
      if (ev.target === modal) modal.style.display = "none";
    });
  }
  async function refresh() {
    var data = await api("/api/admin/users");
    var dl = document.getElementById("project-list");
    if (dl && data.projects) {
      dl.innerHTML = "";
      data.projects.forEach(function (p) {
        var o = document.createElement("option");
        o.value = p;
        dl.appendChild(o);
      });
    }
    var defInput = document.getElementById("f-default-project");
    if (defInput && document.activeElement !== defInput) defInput.value = data.default_project || "";
    state.defaultProject = data.default_project || "";
    state.defaultDoc = data.default_doc || "";
    renderDocRow();
    rows.innerHTML = "";
    var cards = document.getElementById("user-cards");
    if (cards) cards.innerHTML = "";
    data.users.forEach(function (u) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + esc(u.email) + "</td>" +
        "<td>" + esc(u.display) + "</td>" +
        "<td><span class='pill " + (u.role === "admin" ? "admin" : "user") + " edit-role' style='cursor:pointer' title='Click to toggle admin/user'>" + esc(u.role) + "</span>" +
        "<span class='pill heavy edit-heavy' style='cursor:pointer" + (u.heavy ? "" : ";opacity:.35") + "' title='Click to toggle the GPU seat'>GPU</span></td>" +
        "<td class='edit-project' style='cursor:pointer' title='Click to change their project'>" +
        (u.project ? esc(baseName(u.project))
                 : state.defaultProject ? "<span style='opacity:.6'>" + esc(baseName(state.defaultProject)) + " (default)</span>"
                 : "—") + "</td>" +
        "<td></td>";
      async function patch(fields, label) {
        setStatus("Updating " + u.email + "…");
        try {
          await api("/api/admin/users/" + encodeURIComponent(u.email),
                    { method: "PATCH", body: JSON.stringify(fields) });
          setStatus(label + " Applies on their next session start.");
          refresh();
        } catch (e) { setStatus(String(e.message || e), true); }
      }
      function bindRole(el) {
        el.addEventListener("click", function () {
          var to = u.role === "admin" ? "user" : "admin";
          if (confirm("Make " + u.email + " " + (to === "admin" ? "an ADMIN (full ops + allowlist control)?" : "a regular user?")))
            patch({ role: to }, u.email + " is now " + to + ".");
        });
      }
      function bindHeavy(el) {
        el.addEventListener("click", function () {
          patch({ heavy: !u.heavy }, u.email + (u.heavy ? " lost" : " got") + " the GPU seat.");
        });
      }
      function bindProject(el) {
        el.addEventListener("click", function () {
          var v = prompt("Project for " + u.email + " (PLM name or host folder path; empty = inherit site default):",
                         u.project || "");
          if (v !== null) patch({ project: v.trim() }, "Project updated.");
        });
      }
      function makeRemoveBtn() {
        var btn = document.createElement("button");
        btn.className = "rm-btn";
        btn.textContent = "remove";
        btn.addEventListener("click", async function () {
          if (!confirm("Remove " + u.email + " from the allowlist?")) return;
          setStatus("Removing " + u.email + "…");
          try {
            var r = await api("/api/admin/users/" + encodeURIComponent(u.email), { method: "DELETE" });
            setStatus("Removed. Access policy: " + (r.access_policy.synced ? "synced" : "SYNC FAILED — " + r.access_policy.detail),
                      !r.access_policy.synced);
            refresh();
          } catch (e) { setStatus(String(e.message || e), true); }
        });
        return btn;
      }
      bindRole(tr.querySelector(".edit-role"));
      bindHeavy(tr.querySelector(".edit-heavy"));
      bindProject(tr.querySelector(".edit-project"));
      tr.lastElementChild.appendChild(makeRemoveBtn());
      rows.appendChild(tr);

      if (cards) {
        var card = document.createElement("div");
        card.className = "user-card";
        var projectLabel = u.project ? esc(baseName(u.project))
          : state.defaultProject ? esc(baseName(state.defaultProject)) + " (default)"
          : "—";
        card.innerHTML =
          "<div class='uc-email'>" + esc(u.email) + "</div>" +
          "<div class='uc-meta'>" + (u.display ? esc(u.display) : "No display name") + "</div>" +
          "<div class='uc-row'>" +
            "<span class='pill " + (u.role === "admin" ? "admin" : "user") + " edit-role' style='cursor:pointer'>" + esc(u.role) + "</span>" +
            "<span class='pill heavy edit-heavy' style='cursor:pointer" + (u.heavy ? "" : ";opacity:.35") + "'>GPU</span>" +
          "</div>" +
          "<div class='uc-meta edit-project' style='cursor:pointer'>Project: " + projectLabel + "</div>" +
          "<div class='uc-actions'></div>";
        bindRole(card.querySelector(".edit-role"));
        bindHeavy(card.querySelector(".edit-heavy"));
        bindProject(card.querySelector(".edit-project"));
        card.querySelector(".uc-actions").appendChild(makeRemoveBtn());
        cards.appendChild(card);
      }
    });
  }

  form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    var email = document.getElementById("f-email").value.trim();
    if (!email) return;
    setStatus("Adding " + email + "…");
    try {
      var r = await api("/api/admin/users", {
        method: "POST",
        body: JSON.stringify({
          email: email,
          display: document.getElementById("f-display").value.trim(),
          role: document.getElementById("f-admin").checked ? "admin" : "user",
          heavy: document.getElementById("f-heavy").checked,
          project: document.getElementById("f-project").value.trim(),
        }),
      });
      setStatus("Added " + r.user.email + ". Access policy: " +
                (r.access_policy.synced ? "synced — they can log in now." : "SYNC FAILED — " + r.access_policy.detail),
                !r.access_policy.synced);
      form.reset();
      refresh();
    } catch (e) { setStatus(String(e.message || e), true); }
  });

  refresh().catch(function (e) {
    setStatus("Failed to load users: " + (e.message || e) + " (admin only)", true);
  });

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
