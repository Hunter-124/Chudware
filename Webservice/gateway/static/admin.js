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

  var state = { defaultProject: "" };

  var defForm = document.getElementById("default-form");
  if (defForm) {
    defForm.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      var p = document.getElementById("f-default-project").value.trim();
      setStatus(p ? "Setting default project to " + p + "…" : "Clearing default project…");
      try {
        await api("/api/admin/default-project", { method: "PUT", body: JSON.stringify({ project: p }) });
        setStatus(p ? "Default project set: everyone without an explicit project now opens " + p +
                      " on their next session start." : "Default project cleared.");
        refresh();
      } catch (e) { setStatus(String(e.message || e), true); }
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
    rows.innerHTML = "";
    data.users.forEach(function (u) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + esc(u.email) + "</td>" +
        "<td>" + esc(u.display) + "</td>" +
        "<td><span class='pill " + (u.role === "admin" ? "admin" : "user") + "'>" + esc(u.role) + "</span>" +
        (u.heavy ? "<span class='pill heavy'>GPU</span>" : "") + "</td>" +
        "<td>" + (u.project ? esc(u.project)
                 : state.defaultProject ? "<span style='opacity:.6'>" + esc(state.defaultProject) + " (default)</span>"
                 : "—") + "</td>" +
        "<td></td>";
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
})();
