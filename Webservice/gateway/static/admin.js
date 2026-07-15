/* billet-web /admin — allowlist management */
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
      tr.querySelector(".edit-role").addEventListener("click", function () {
        var to = u.role === "admin" ? "user" : "admin";
        if (confirm("Make " + u.email + " " + (to === "admin" ? "an ADMIN (full ops + allowlist control)?" : "a regular user?")))
          patch({ role: to }, u.email + " is now " + to + ".");
      });
      tr.querySelector(".edit-heavy").addEventListener("click", function () {
        patch({ heavy: !u.heavy }, u.email + (u.heavy ? " lost" : " got") + " the GPU seat.");
      });
      tr.querySelector(".edit-project").addEventListener("click", function () {
        var v = prompt("Project for " + u.email + " (PLM name or host folder path; empty = inherit site default):",
                       u.project || "");
        if (v !== null) patch({ project: v.trim() }, "Project updated.");
      });
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
      tr.lastElementChild.appendChild(btn);
      rows.appendChild(tr);
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

  /* ---- live ops: sessions, host health, log tail ---- */
  var opsRows = document.getElementById("ops-rows");
  var opsEmpty = document.getElementById("ops-empty");
  var hostBar = document.getElementById("host-bar");

  function fmtDur(sec) {
    if (sec < 90) return sec + "s";
    var m = Math.round(sec / 60);
    if (m < 90) return m + "m";
    return (sec / 3600).toFixed(1) + "h";
  }

  async function refreshOps() {
    if (!opsRows) return;
    var data = await api("/api/admin/ops");
    var h = data.host || {};
    var bits = [];
    bits.push((data.sessions.length) + "/" + data.limits.max_sessions + " seats");
    if (h.mem_available_gib != null) bits.push(h.mem_available_gib.toFixed(1) + " GiB RAM free");
    if (h.gpu) bits.push("VRAM " + (h.gpu.vram_used_mib / 1024).toFixed(1) + "/" +
                         (h.gpu.vram_total_mib / 1024).toFixed(1) + " GiB · GPU " + h.gpu.util_pct + "%");
    if (h.load1 != null) bits.push("load " + h.load1.toFixed(1));
    bits.push("idle timeout " + Math.round(data.limits.idle_timeout_sec / 60) + "m");
    hostBar.textContent = bits.join("  ·  ");

    opsEmpty.style.display = data.sessions.length ? "none" : "block";
    var seen = {};
    data.sessions.forEach(function (s) {
      seen[s.sid] = true;
      var row = document.getElementById("ops-" + s.sid);
      if (!row) {
        row = document.createElement("div");
        row.id = "ops-" + s.sid;
        row.style.cssText = "display:flex;gap:14px;align-items:flex-start;border:1px solid var(--border,#2a2e36);border-radius:10px;padding:10px";
        row.innerHTML =
          "<img class='shot' width='224' style='border-radius:6px;background:#000;flex:none;cursor:pointer' alt='' title='Click to watch this seat live (view-only)'>" +
          "<div style='flex:1;min-width:0'><div class='who' style='font-weight:600'></div>" +
          "<div class='meta' style='font-size:.82rem;color:var(--muted,#8b93a3);margin-top:4px'></div></div>" +
          "<button type='button' class='rm-btn stop' style='flex:none'>Stop</button>";
        row.querySelector(".stop").addEventListener("click", async function () {
          if (!confirm("Stop " + s.email + "'s session? Unsaved work in their seat is lost.")) return;
          try { await api("/api/admin/sessions/" + s.sid + "/stop", { method: "POST" }); refreshOps(); }
          catch (e) { setStatus(String(e.message || e), true); }
        });
        row.querySelector(".shot").addEventListener("click", function () {
          // View-only noVNC spectate through the same authenticated proxy the
          // owner uses — admins pass the proxy's owner check.
          var base = "/stream/" + s.sid + "/";
          window.open(base + "vnc.html?autoconnect=1&resize=scale&view_only=1&path=" +
                      encodeURIComponent(base.slice(1) + "websockify"),
                      "_blank", "noopener");
        });
        opsRows.appendChild(row);
      }
      row.querySelector(".who").textContent =
        (s.display || s.email) + (s.heavy ? "  ·  GPU seat" : "");
      row.querySelector(".meta").textContent =
        s.email + "  ·  " + s.status + "  ·  up " + fmtDur(s.uptime_sec) +
        "  ·  idle " + fmtDur(s.idle_sec) +
        (s.cpu ? "  ·  cpu " + s.cpu : "") + (s.mem ? "  ·  mem " + s.mem : "");
      var img = row.querySelector(".shot");
      if (s.status === "running" && (!img.dataset.at || Date.now() - img.dataset.at > 10000)) {
        img.dataset.at = Date.now();
        img.src = "/api/admin/screenshot/" + s.sid + "?t=" + Date.now();
      }
    });
    Array.prototype.slice.call(opsRows.children).forEach(function (row) {
      if (!seen[row.id.slice(4)]) row.remove();
    });
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

  if (opsRows) {
    refreshOps().catch(function () { hostBar.textContent = "ops unavailable"; });
    setInterval(function () {
      refreshOps().catch(function () {});
      if (document.getElementById("log-follow") &&
          document.getElementById("log-follow").checked) refreshLog().catch(function () {});
    }, 5000);
  }
})();
