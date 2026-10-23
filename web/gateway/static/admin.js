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

  /* ---- shared line icons (paths verbatim from the brand contract) ----
     Icons are decorative: every svg is aria-hidden and the adjacent text or
     the host control's aria-label carries the meaning. */
  var ICON_PATHS = {
    "arrow-right": "<path d='M4 12h14.6M13 6.4 18.6 12 13 17.6'/>",
    "archive": "<path d='M4.6 4.6h14.8v3.6H4.6zM6 8.2v11.2h12V8.2'/><path d='M10.4 11.8h3.2'/>",
    "package": "<path d='M3.6 7.9 12 3.6l8.4 4.3v8.2L12 20.4 3.6 16.1z'/><path d='M3.6 7.9 12 12.2l8.4-4.3M12 12.2v8.2'/>"
  };
  function iconHtml(name, extraClass) {
    return "<svg class='icon" + (extraClass ? " " + extraClass : "") +
           "' viewBox='0 0 24 24' aria-hidden='true' fill='none' stroke='currentColor'" +
           " stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'>" +
           ICON_PATHS[name] + "</svg>";
  }

  /* ---- tab shell: hash-routed panels ----
     Dashboard/Releases/Accounts/Billing/Marketplace/Terminal/System.
     Billing and Marketplace render into a bare mount div from
     admin_billing.js / admin_marketplace.js; they need no loader hook here
     because each of those files self-initialises on its own mount id. This
     array only has to contain them so showTab() recognises the hash and
     toggles the panel -- a tab missing from it renders as a dead nav link. */
  var TABS = ["dashboard", "releases", "accounts", "billing", "marketplace", "terminal", "system"];
  var shellEl = document.querySelector(".admin-shell");
  var tabLinks = Array.prototype.slice.call(document.querySelectorAll(".admin-tab"));
  var currentTab = null;
  if (shellEl) shellEl.classList.add("js-tabs");

  function tabFromHash() {
    var name = String(location.hash || "").replace(/^#/, "");
    return TABS.indexOf(name) >= 0 ? name : "dashboard";
  }

  // admin_terminal.js exports no fit hook on window.__wm, but it debounces a
  // scheduleFit() off window "resize" (and a ResizeObserver on #term-host /
  // #wm-root). The pane keeps its box while hidden, so a synthetic resize is
  // the supported way to re-fit xterm and re-clamp WinBox after activation.
  function fitTerminal() {
    requestAnimationFrame(function () {
      window.dispatchEvent(new Event("resize"));
    });
  }

  function showTab(name) {
    if (TABS.indexOf(name) < 0) name = "dashboard";
    TABS.forEach(function (tab) {
      var panel = document.getElementById("panel-" + tab);
      if (panel) panel.classList.toggle("active", tab === name);
    });
    tabLinks.forEach(function (link) {
      var on = link.getAttribute("data-tab") === name;
      link.classList.toggle("active", on);
      link.setAttribute("aria-selected", on ? "true" : "false");
      // Narrow viewports scroll the tab strip; keep the active tab reachable.
      if (on && link.scrollIntoView) link.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
    var changed = currentTab !== name;
    currentTab = name;
    if (name === "terminal") fitTerminal();
    else if (changed && name === "dashboard") refreshDashboard();
    else if (changed && name === "releases") refreshReleaseTables();
  }

  window.addEventListener("hashchange", function () { showTab(tabFromHash()); });
  Array.prototype.forEach.call(document.querySelectorAll("[data-goto]"), function (btn) {
    btn.addEventListener("click", function () {
      location.hash = "#" + btn.getAttribute("data-goto");
      showTab(tabFromHash());
    });
  });
  // Initial routing happens at the bottom of this file: showTab() kicks off the
  // panel's data load, and those loaders are declared further down.

  /* ---- shared formatting ---- */
  function fmtBytes(n) {
    var v = Number(n);
    if (!isFinite(v) || v < 0) return "\u2014";
    if (v < 1024) return v + " B";
    var units = ["KB", "MB", "GB", "TB"];
    var i = 0;
    v /= 1024;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v < 10 ? 2 : 1) + " " + units[i];
  }

  function fmtStamp(value) {
    if (value == null || value === "") return "\u2014";
    var d = new Date(value);
    return isNaN(d.getTime()) ? String(value) : d.toLocaleString();
  }

  function shortHash(value, n) {
    var s = String(value || "");
    return s ? s.slice(0, n || 12) : "\u2014";
  }

  function platformLabel(row) {
    return String(row.platform || "?") + "/" + String(row.arch || "?");
  }

  // Release rows carry a `format` since migration 0018: default|deb|rpm|tar.
  // "default" is the platform's native artifact (AppImage / .exe / .zip), so
  // the badge names the artifact a customer actually downloads.
  var FORMAT_LABEL = { deb: "deb", rpm: "rpm", tar: "tar.zst" };
  var DEFAULT_FORMAT_LABEL = { linux: "AppImage", windows: "exe", macos: "zip" };
  function rowFormat(row) {
    return String(row.format || "default");
  }
  function formatLabel(row) {
    var f = rowFormat(row);
    if (f !== "default") return FORMAT_LABEL[f] || f;
    return DEFAULT_FORMAT_LABEL[String(row.platform || "")] || "default";
  }
  function formatBadge(row) {
    return "<span class='fmt-badge' title='format: " + esc(rowFormat(row)) + "'>" +
           esc(formatLabel(row)) + "</span>";
  }

  function evidenceComplete(row) {
    return row.evidence_complete === true;
  }
  function hasStablePair(row) {
    return row.has_stable_pair === true;
  }

  /* ---- shared release-manifest fetch: dashboard, table and manual card ---- */
  var manifestCache = { at: 0, promise: null };
  var manifestReloaders = [];

  function fetchManifests() {
    var now = Date.now();
    if (manifestCache.promise && now - manifestCache.at < 2500) return manifestCache.promise;
    manifestCache.at = now;
    manifestCache.promise = api("/api/admin/release-manifests").then(function (rows) {
      return Array.isArray(rows) ? rows : (rows && rows.releases) || [];
    });
    return manifestCache.promise;
  }

  function reloadManifestViews() {
    manifestCache.promise = null;
    manifestReloaders.forEach(function (fn) { fn(); });
  }

  function fetchCiBuilds(limit) {
    return api("/api/admin/ci/builds?limit=" + (limit || 20)).then(function (rows) {
      return Array.isArray(rows) ? rows : (rows && rows.builds) || [];
    });
  }

  var CI_STATUS_CLASS = {
    published: "st-active",
    failed: "st-denied",
    queued: "st-pending",
    building: "st-running",
    testing: "st-running",
    packaging: "st-running",
    publishing: "st-running"
  };
  function ciPill(status) {
    var key = String(status || "").toLowerCase();
    var span = document.createElement("span");
    span.className = "pill " + (CI_STATUS_CLASS[key] || "st-unknown");
    span.textContent = key || "unknown";
    return span;
  }

  var SAFE_REASON_LABELS = {
    integrity_violation: "App integrity issue",
    identity_mismatch: "Device identity changed",
    credential_compromise: "Credential security issue",
    replay_detected: "Repeated security receipt",
    policy_violation: "Security policy issue",
    anomalous_activity: "Unusual device activity"
  };
  function appealReasons(codes) {
    var values = Array.isArray(codes) ? codes : [];
    var labels = values.map(function (code) { return SAFE_REASON_LABELS[String(code)] || "Security policy issue"; });
    return labels.filter(function (label, index) { return labels.indexOf(label) === index; }).join(", ") || "Security policy issue";
  }
  function appealState(value) { return ({ pending: "Pending", restored: "Restored", upheld: "Upheld" })[value] || "Under review"; }
  function appealStateClass(value) { return value === "pending" ? "st-pending" : value === "restored" ? "st-active" : "st-denied"; }
  function fmtAppealDate(value) { var d = value ? new Date(value) : null; return d && !isNaN(d.getTime()) ? d.toLocaleString() : "—"; }

  async function loadAppeals() {
    var host = document.getElementById("admin-appeal-list");
    var status = document.getElementById("admin-appeal-status");
    var payload = await api("/api/admin/security/appeals");
    var appeals = payload.appeals || [];
    var pending = appeals.filter(function (appeal) { return appeal.state === "pending"; }).length;
    var badge = document.getElementById("appeal-pending"); badge.textContent = pending ? pending + " pending" : ""; badge.hidden = !pending;
    setDashNumber("dash-appeals-pending", pending);
    host.innerHTML = "";
    if (!appeals.length) { host.innerHTML = "<p class='ac-dim'>No security appeals.</p>"; return; }
    appeals.forEach(function (appeal) {
      var item = document.createElement("article"); item.className = "appeal-admin-item";
      item.innerHTML = "<div class='appeal-admin-head'><strong>" + esc(appeal.email) + "</strong><span class='pill " + appealStateClass(appeal.state) + "'>" + esc(appealState(appeal.state)) + "</span></div>" +
        "<p class='appeal-admin-meta'>" + esc(appeal.device_name || appeal.subject) + " · Action " + esc(fmtAppealDate(appeal.action_at)) + " · Submitted " + esc(fmtAppealDate(appeal.created_at)) + "</p>" +
        "<p class='appeal-admin-reason'><strong>Reason:</strong> " + esc(appealReasons(appeal.reason_codes)) + "</p>" +
        "<p class='appeal-admin-reason'><strong>Customer message:</strong> " + esc(appeal.message || "No message provided") + "</p>";
      if (appeal.state === "pending") {
        var form = document.createElement("form"); form.className = "appeal-resolve";
        form.innerHTML = "<label>Decision note<textarea maxlength='4096' required aria-label='Decision note for " + esc(appeal.email) + "'></textarea></label><div class='appeal-actions'><button type='submit' class='btn primary' data-decision='restore_device'>Restore device</button><button type='submit' class='rm-btn' data-decision='uphold'>Uphold action</button></div>";
        form.addEventListener("submit", async function (event) {
          event.preventDefault(); var decision = event.submitter && event.submitter.dataset.decision; if (!decision) return;
          Array.prototype.forEach.call(form.querySelectorAll("button"), function (button) { button.disabled = true; });
          status.textContent = "Recording decision…";
          try {
            await api("/api/admin/security/appeals/" + encodeURIComponent(appeal.id) + "/resolve", { method: "POST", body: JSON.stringify({ decision: decision, note: form.querySelector("textarea").value }) });
            status.textContent = decision === "restore_device" ? "Device restored. The customer must sign in again with a newly rotated identity." : "Security action upheld.";
            await loadAppeals();
          } catch (e) { status.textContent = e.message || String(e); status.className = "err"; Array.prototype.forEach.call(form.querySelectorAll("button"), function (button) { button.disabled = false; }); }
        });
        item.appendChild(form);
      } else {
        item.insertAdjacentHTML("beforeend", "<p class='appeal-admin-meta'>Decided " + esc(fmtAppealDate(appeal.resolved_at)) + " by " + esc(appeal.decided_by || "administrator") + (appeal.decision_note ? " · " + esc(appeal.decision_note) : "") + "</p>");
      }
      host.appendChild(item);
    });
  }
  /* ---- accounts: the approval queue and everyone already through it ---- */

  var PAGE_SIZE = 50;
  var planCatalog = [];
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

  // Nearly every account is a plain user, so a whole Role column was noise:
  // the rare admin gets a chip beside their email instead.
  function adminChip(role) {
    return role === "admin" ? " <span class='pill admin'>admin</span>" : "";
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
    setDashNumber("dash-accounts-pending", v);
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
    planCatalog.forEach(function (plan) {
      var o = document.createElement("option");
      o.value = plan.id;
      o.textContent = plan.display_name;
      if (plan.id === a.plan) o.selected = true;
      sel.appendChild(o);
    });
    if (!planCatalog.some(function (plan) { return plan.id === a.plan; })) {
      var unknown = document.createElement("option");
      unknown.value = a.plan || "";
      unknown.textContent = "Unknown: " + (a.plan || "(blank)");
      unknown.selected = true;
      unknown.disabled = true;
      sel.appendChild(unknown);
    }
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
    else host.appendChild(document.createTextNode(a.plan || "trial"));
  }
  function creditsUrl(email, action) {
    return "/api/admin/credits/" + encodeURIComponent(email) + "/" + action;
  }

  function creditControls(a) {
    var host = document.createElement("div");
    host.className = "credit-controls";
    var balance = document.createElement("span");
    balance.className = "credit-balance";
    balance.textContent = String(Number(a.credit_balance) || 0) + " credits";
    host.appendChild(balance);

    var grantLabel = document.createElement("label");
    var grantCheck = document.createElement("input");
    grantCheck.type = "checkbox";
    grantCheck.checked = Boolean(a.ai_grant);
    grantLabel.appendChild(grantCheck);
    grantLabel.appendChild(document.createTextNode(" pooled AI"));
    host.appendChild(grantLabel);

    var unlimitedLabel = document.createElement("label");
    var unlimited = document.createElement("input");
    unlimited.type = "checkbox";
    unlimited.checked = Number(a.credit_ceiling_monthly) === 0;
    unlimitedLabel.appendChild(unlimited);
    unlimitedLabel.appendChild(document.createTextNode(" Unlimited"));
    host.appendChild(unlimitedLabel);

    var ceiling = document.createElement("input");
    ceiling.type = "number";
    ceiling.min = "1";
    ceiling.step = "1";
    ceiling.placeholder = "monthly cap";
    ceiling.title = "Monthly pooled-AI credit ceiling";
    ceiling.disabled = unlimited.checked;
    if (!unlimited.checked) ceiling.value = String(a.credit_ceiling_monthly);
    unlimited.addEventListener("change", function () {
      ceiling.disabled = unlimited.checked;
      if (!unlimited.checked && !ceiling.value) ceiling.value = "1";
    });
    host.appendChild(ceiling);

    host.appendChild(actionBtn("Save", "btn ghost", async function () {
      setStatus("Saving credit controls for " + a.email + "\u2026");
      try {
        await api(creditsUrl(a.email, "controls"), {
          method: "POST",
          body: JSON.stringify({
            ai_grant: grantCheck.checked,
            credit_ceiling_monthly: unlimited.checked ? 0 : Number(ceiling.value)
          })
        });
        setStatus("Credit controls saved for " + a.email + ".");
        await load();
      } catch (e) { setStatus(String(e.message || e), true); }
    }));
    host.appendChild(actionBtn("Grant\u2026", "btn ghost", async function () {
      var raw = prompt("Credits to grant to " + a.email + ":", "1000");
      if (raw == null) return;
      var amount = Number(raw);
      if (!Number.isInteger(amount) || amount <= 0) {
        setStatus("Grant must be a positive whole number of credits.", true);
        return;
      }
      setStatus("Granting " + amount + " credits to " + a.email + "\u2026");
      try {
        await api(creditsUrl(a.email, "grant"), {
          method: "POST", body: JSON.stringify({ credits: amount })
        });
        setStatus("Granted " + amount + " credits to " + a.email + ".");
        await load();
      } catch (e) { setStatus(String(e.message || e), true); }
    }));
    return host;
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
    rows.innerHTML = "<tr><td colspan='6' class='table-empty'>" + esc(msg) + "</td></tr>";
    cardsEl.innerHTML = "<div class='ac-dim'>" + esc(msg) + "</div>";
  }

  function render(list) {
    rows.innerHTML = "";
    cardsEl.innerHTML = "";
    list.forEach(function (a) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td class='ac-email'>" + esc(a.email) + adminChip(a.role) +
          (a.display ? "<div class='ac-dim'>" + esc(a.display) + "</div>" : "") + "</td>" +
        "<td>" + statusPill(a.status) + "</td>" +
        "<td class='ac-plan'></td>" +
        "<td class='ac-credit'></td>" +
        "<td class='ac-when' title='" + esc(fmtWhen(a.created_at)) + "'>" +
          esc(fmtDate(a.created_at)) + "</td>" +
        "<td class='ac-act'><div class='ac-actions'></div></td>";
      fillPlan(tr.querySelector(".ac-plan"), a, "");
      tr.querySelector(".ac-credit").appendChild(creditControls(a));
      fillActions(tr.querySelector(".ac-actions"), a);
      rows.appendChild(tr);

      var card = document.createElement("div");
      card.className = "account-card";
      card.innerHTML =
        "<div class='ac-email'>" + esc(a.email) + adminChip(a.role) + "</div>" +
        (a.display ? "<div class='ac-meta'>" + esc(a.display) + "</div>" : "") +
        "<div class='ac-row'>" + statusPill(a.status) + "</div>" +
        "<div class='ac-meta'>Created " + esc(fmtDate(a.created_at)) + "</div>" +
        "<div class='ac-plan'></div>" +
        "<div class='ac-credit'></div>" +
        "<div class='ac-actions'></div>";
      fillPlan(card.querySelector(".ac-plan"), a, "Plan ");
      card.querySelector(".ac-credit").appendChild(creditControls(a));
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

  async function loadPlanCatalog() {
    var data = await api("/api/plans");
    var catalog = data && data.plans;
    if (!Array.isArray(catalog) || !catalog.length) {
      throw new Error("server returned an empty plan catalog");
    }
    planCatalog = catalog.map(function (plan) {
      return {
        id: String(plan.id),
        display_name: String(plan.display_name || plan.id),
        purchasable: Boolean(plan.purchasable)
      };
    });
  }

  placeholder("Loading accounts\u2026");
  loadPlanCatalog().then(reload).catch(function (e) {
    placeholder("Couldn't load plan catalog.");
    setStatus("Failed to load plans: " + (e.message || e), true);
  });
  loadAppeals().catch(function (e) {
    document.getElementById("admin-appeal-list").innerHTML = "<p class='ac-dim'>Couldn't load security appeals.</p>";
    document.getElementById("admin-appeal-status").textContent = e.message || String(e);
  });

  var trialDomains = document.getElementById("trial-domains");
  var trialDomainsSave = document.getElementById("trial-domains-save");
  async function loadTrialDomains() {
    if (!trialDomains) return;
    var data = await api("/api/admin/credits/trial-domains");
    trialDomains.value = (data.trial_domains || []).join("\n");
  }
  if (trialDomainsSave) {
    trialDomainsSave.addEventListener("click", async function () {
      var domains = trialDomains.value.split(/[\n,]+/).map(function (v) {
        return v.trim();
      }).filter(Boolean);
      try {
        var data = await api("/api/admin/credits/trial-domains", {
          method: "PUT", body: JSON.stringify({ trial_domains: domains })
        });
        trialDomains.value = (data.trial_domains || []).join("\n");
        setStatus("Trial domains saved.");
      } catch (e) { setStatus(String(e.message || e), true); }
    });
  }
  loadTrialDomains().catch(function (e) {
    setStatus("Failed to load trial domains: " + (e.message || e), true);
  });

  /* ---- host folder browser (admin-only, read-only) ---- */
  var modal = document.getElementById("browse-modal");

  async function browseTo(path) {
    var data = await api("/api/admin/browse?path=" + encodeURIComponent(path || ""));
    document.getElementById("browse-path").textContent = data.path;
    var list = document.getElementById("browse-list");
    list.innerHTML = "";
    // Rows use the contract line icons (archive = folder, package = part file,
    // flipped arrow = up one level) — no emoji or arrow glyphs as icons.
    function row(label, cls, iconName, iconClass, onClick) {
      var d = document.createElement("div");
      d.className = "browse-row" + (cls ? " " + cls : "");
      d.innerHTML = iconHtml(iconName, iconClass) + "<span></span>";
      d.querySelector("span").textContent = label;
      if (onClick) d.addEventListener("click", onClick);
      list.appendChild(d);
    }
    if (data.parent) row("..", "dir", "arrow-right", "icon-up", function () { browseTo(data.parent).catch(showBrowseErr); });
    data.dirs.forEach(function (n) {
      row(n, "dir", "archive", "", function () {
        browseTo(data.path.replace(/\/$/, "") + "/" + n).catch(showBrowseErr);
      });
    });
    data.parts.forEach(function (n) {
      row(n, "part", "package", "", null);
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

  /* ---- deployment: read-only operator status / logs ---- */
  (function initDeploy() {
    var statusPill = document.getElementById("deploy-status");
    var logPre = document.getElementById("deploy-log");
    var refreshButton = document.getElementById("deploy-refresh");
    if (!statusPill || !logPre || !refreshButton) return;

    function setPill(cls, text) {
      statusPill.className = "dep-pill " + cls;
      statusPill.textContent = text;
    }

    async function refreshStatus() {
      try {
        var st = await api("/api/admin/deploy/status");
        if (st.running) {
          setPill("dep-run", "recorded running: " + (st.kind || "deployment"));
        } else if (st.last) {
          var last = st.last;
          var ok = last.rc === 0;
          var label = "last: " + (ok ? "ok" : "fail") + " rc=" + last.rc +
                      " · " + (last.kind || "deployment");
          if (last.release) label += " · " + last.release;
          setPill(ok ? "dep-ok" : "dep-fail", label);
        } else {
          setPill("dep-idle", "no recorded deployment");
        }
      } catch (e) {
        setPill("dep-fail", "deployment status unavailable");
      }
    }

    async function refreshDeployLog() {
      try {
        var response = await fetch("/api/admin/deploy/log?lines=400");
        if (!response.ok) throw new Error("HTTP " + response.status);
        logPre.textContent = (await response.text()) || "(no operator deployment log yet)";
        logPre.scrollTop = logPre.scrollHeight;
      } catch (e) {
        logPre.textContent = "Deployment log unavailable: " + String(e.message || e);
      }
    }

    function refreshDeployment() {
      refreshStatus();
      refreshDeployLog();
    }

    refreshButton.addEventListener("click", refreshDeployment);
    refreshDeployment();
    // Observe qualified operator runs started outside this browser, including new runs.
    setInterval(refreshDeployment, 5000);
  })();

  /* ---- signed customer release manifests ---- */
  (function initReleaseManifests() {
    var form = document.getElementById("manifest-form");
    var list = document.getElementById("manifest-list");
    var status = document.getElementById("manifest-status");
    if (!form || !list) return;

    function value(id) { return document.getElementById(id).value.trim(); }
    function setValue(id, value) { document.getElementById(id).value = value == null ? "" : String(value); }
    function payload() {
      return {
        channel: value("manifest-channel"),
        version: value("manifest-version"),
        min_supported: value("manifest-min"),
        platform: value("manifest-platform"),
        arch: value("manifest-arch"),
        format: value("manifest-format"),
        sha256: value("manifest-sha"),
        size: Number(value("manifest-size")),
        signature: value("manifest-signature"),
        notes_url: value("manifest-notes"),
        artifact_key: value("manifest-artifact")
      };
    }
    function fill(row) {
      setValue("manifest-channel", row.channel);
      setValue("manifest-version", row.version);
      setValue("manifest-min", row.min_supported);
      setValue("manifest-platform", row.platform);
      setValue("manifest-arch", row.arch);
      setValue("manifest-format", rowFormat(row));
      setValue("manifest-sha", row.sha256);
      setValue("manifest-size", row.size);
      setValue("manifest-signature", row.signature);
      setValue("manifest-notes", row.notes_url);
      setValue("manifest-artifact", row.artifact_key);
      form.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    async function remove(row) {
      if (!window.confirm("Delete " + row.channel + " " + row.version + " " +
                          row.platform + "/" + row.arch + " (" + formatLabel(row) + ")?")) return;
      // format is explicit: omitting it would address the platform-default row.
      var query = new URLSearchParams({
        channel: row.channel, version: row.version,
        platform: row.platform, arch: row.arch, format: rowFormat(row)
      });
      await api("/api/admin/release-manifests?" + query.toString(), { method: "DELETE" });
      status.textContent = "Manifest deleted.";
      reloadManifestViews();
    }
    async function loadManifests() {
      var rows = await fetchManifests();
      list.textContent = "";
      if (!rows.length) {
        list.appendChild(document.createTextNode("No customer release manifests published."));
        return;
      }
      rows.forEach(function (row) {
        var host = document.createElement("div");
        host.className = "manifest-row";
        var info = document.createElement("div");
        var name = document.createElement("strong");
        name.textContent = row.channel + " · " + row.version + " · " +
                           row.platform + "/" + row.arch + " · " + formatLabel(row);
        var meta = document.createElement("span");
        meta.textContent = row.artifact_key + " · " + fmtBytes(row.size);
        info.appendChild(name);
        info.appendChild(meta);
        var actions = document.createElement("div");
        actions.className = "ac-actions";
        actions.appendChild(actionBtn("Edit", "btn ghost", function () { fill(row); }));
        actions.appendChild(actionBtn("Delete", "btn danger", function () {
          remove(row).catch(function (e) { status.textContent = e.message || String(e); });
        }));
        host.appendChild(info);
        host.appendChild(actions);
        list.appendChild(host);
      });
    }
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      status.textContent = "Saving signed manifest…";
      try {
        await api("/api/admin/release-manifests", {
          method: "POST", body: JSON.stringify(payload())
        });
        status.textContent = "Manifest saved.";
        reloadManifestViews();
      } catch (e) {
        status.textContent = e.message || String(e);
      }
    });
    function reloadManualList() {
      loadManifests().catch(function (e) {
        list.textContent = "Failed to load manifests: " + (e.message || e);
      });
    }
    manifestReloaders.push(reloadManualList);
    reloadManualList();
  })();

  /* ---- installer files: raw uploads on the gateway host ---- */
  (function initReleases() {
    var mount = document.getElementById("releases-files-mount");
    if (!mount) return;

    var card = document.createElement("div");
    card.className = "admin-card releases-card";
    card.innerHTML =
      "<h2>Installer files</h2>" +
      "<div class='releases-list' id='releases-list'>Loading…</div>" +
      "<div class='release-upload'>" +
        "<input type='file' id='release-file' />" +
        "<button type='button' class='btn ghost' id='release-upload-btn'>Upload</button>" +
        "<span class='release-upload-status' id='release-upload-status'></span>" +
      "</div>";
    mount.appendChild(card);

    var listEl = document.getElementById("releases-list");
    var fileEl = document.getElementById("release-file");
    var uploadBtn = document.getElementById("release-upload-btn");
    var uploadStatus = document.getElementById("release-upload-status");

    // Sizes and stamps use the shared fmtBytes/fmtStamp helpers above — the
    // card used to carry its own near-identical copies.

    async function refreshReleases() {
      var data = await api("/api/admin/releases");
      if (!data || !data.length) {
        listEl.innerHTML =
          "<p class='deploy-note'>No installer files on this host yet. Windows installers " +
          "can't be built on this Linux host — build one on a Windows box " +
          "(scripts\\build_windows.ps1, then installer\\build_release.py --zip, then " +
          "packaging\\windows\\chudware.iss) and upload it below, or drop it into " +
          "~/chudware-releases on the server.</p>";
        return;
      }
      listEl.innerHTML = data.map(function (r) {
        return "<div class='release-row'>" +
          "<div class='release-info'>" +
            "<span class='release-name'>" + esc(r.name) + "</span>" +
            "<span class='release-meta'>" + fmtBytes(r.size) + " \xb7 " + fmtStamp(r.mtime) + "</span>" +
          "</div>" +
          "<div class='release-actions'>" +
            "<a class='btn ghost' href='/api/admin/releases/download/" + encodeURIComponent(r.name) +
              "' download>Download</a>" +
            "<button type='button' class='btn danger release-delete' data-name='" +
              esc(r.name) + "'>Delete</button>" +
          "</div>" +
        "</div>";
      }).join("");

      Array.prototype.forEach.call(listEl.querySelectorAll(".release-delete"), function (btn) {
        btn.addEventListener("click", async function () {
          var name = btn.getAttribute("data-name");
          if (!window.confirm("Delete " + name + " from the host? Customers can no longer download it.")) return;
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
        uploadStatus.textContent = "Choose a file first.";
        return;
      }
      uploadBtn.disabled = true;
      uploadStatus.textContent = "Uploading " + f.name + "\u2026";
      try {
        var fd = new FormData();
        fd.append("file", f, f.name);
        var res = await fetch("/api/admin/releases/upload", { method: "POST", body: fd });
        var data = null;
        try { data = await res.json(); } catch (e) { /* non-JSON */ }
        if (!res.ok) {
          throw new Error((data && (data.detail || data.error)) || res.status + " " + res.statusText);
        }
        uploadStatus.textContent = "Uploaded " + f.name + ".";
        fileEl.value = "";
        await refreshReleases();
      } catch (e) {
        uploadStatus.textContent = "Upload failed: " + (e.message || e);
      } finally {
        uploadBtn.disabled = false;
      }
    });

    refreshReleases().catch(function (e) {
      listEl.innerHTML = "<p class='deploy-note'>Failed to load releases: " + esc(e.message || e) + "</p>";
    });
  })();

  /* ---- AI subscription accounts: admin-managed Claude and ChatGPT links ---- */
  (function initSubscriptionAccounts() {
    var flowHost = document.getElementById("sub-flow");
    var listEl = document.getElementById("sub-list");
    var status = document.getElementById("sub-status");
    if (!flowHost || !listEl || !status) return;

    var connectBtns = Array.prototype.slice.call(
      document.querySelectorAll("#sub-connect button[data-provider]"));

    var POLL_MS = 5000;
    var providerLabels = {};
    var pollTimer = null;
    // Bumped whenever a flow is cancelled or replaced, so a request already in
    // flight can tell that its answer is no longer the one being waited on.
    var flowSeq = 0;

    function setSubStatus(msg, isErr) {
      status.textContent = msg || "";
      status.className = isErr ? "ac-dim err" : "ac-dim";
    }

    function clearFlow() {
      flowSeq++;
      if (pollTimer != null) { clearTimeout(pollTimer); pollTimer = null; }
      flowHost.textContent = "";
      flowHost.hidden = true;
    }

    function providerLabel(provider) {
      return providerLabels[provider] || provider;
    }

    function fmtSubWhen(iso) {
      var d = iso ? new Date(iso) : null;
      return d && !isNaN(d.getTime()) ? d.toLocaleString() : "\u2014";
    }

    function accountUrlFor(provider, key) {
      return "/api/providers/accounts/" + encodeURIComponent(provider) +
             (key == null ? "" : "/" + encodeURIComponent(key));
    }

    async function disconnect(account) {
      if (!window.confirm("Disconnect " + providerLabel(account.provider) +
                          " from this admin account?")) return;
      setSubStatus("Disconnecting \u2026");
      await api(accountUrlFor(account.provider, account.account_key), { method: "DELETE" });
      setSubStatus(providerLabel(account.provider) + " disconnected.");
      await refresh();
    }

    function renderAccounts(accounts) {
      listEl.textContent = "";
      if (!accounts.length) {
        listEl.innerHTML = "<p class='ac-dim'>No shared subscription links connected yet.</p>";
        return;
      }
      accounts.forEach(function (account) {
        var row = document.createElement("div");
        row.className = "sub-row";

        var info = document.createElement("div");
        var who = document.createElement("div");
        who.className = "sub-who";
        who.appendChild(document.createTextNode(providerLabel(account.provider)));
        if (account.expired) {
          var flag = document.createElement("span");
          flag.className = "pill st-denied";
          flag.textContent = "needs reconnecting";
          who.appendChild(flag);
        }
        info.appendChild(who);

        var bits = [];
        if (account.label) bits.push(account.label);
        if (account.plan) bits.push(account.plan + " plan");
        bits.push("connected " + fmtSubWhen(account.created_at));
        bits.push(account.last_used_at
          ? "last used " + fmtSubWhen(account.last_used_at)
          : "not used yet");
        var meta = document.createElement("p");
        meta.className = "sub-meta";
        meta.textContent = bits.join(" \u00b7 ");
        info.appendChild(meta);
        row.appendChild(info);

        var actions = document.createElement("div");
        actions.className = "ac-actions";
        actions.appendChild(actionBtn("Disconnect", "btn danger", function () {
          disconnect(account).catch(function (e) { setSubStatus(String(e.message || e), true); });
        }));
        row.appendChild(actions);

        listEl.appendChild(row);
      });
    }

    async function refresh() {
      var data = await api("/api/providers/accounts");
      (data.providers || []).forEach(function (p) {
        providerLabels[p.provider] = p.label || p.provider;
      });
      renderAccounts((data.accounts || []).filter(function (a) { return a.scope === "shared"; }));
    }

    function reloadAccounts() {
      refresh().catch(function (e) {
        listEl.innerHTML = "<p class='ac-dim'>Couldn't load subscription accounts.</p>";
        setSubStatus(String(e.message || e), true);
      });
    }

    function flowShell(provider, instructions) {
      flowHost.textContent = "";
      flowHost.hidden = false;
      var head = document.createElement("p");
      head.className = "sub-step";
      var name = document.createElement("strong");
      name.textContent = "Connecting " + providerLabel(provider);
      head.appendChild(name);
      flowHost.appendChild(head);
      if (instructions) {
        var step = document.createElement("p");
        step.className = "sub-step ac-dim";
        step.textContent = instructions;
        flowHost.appendChild(step);
      }
    }

    function flowLink(url, label) {
      var a = document.createElement("a");
      a.className = "btn ghost";
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = label;
      return a;
    }

    function cancelBtn() {
      return actionBtn("Cancel", "btn ghost", function () {
        clearFlow();
        setSubStatus("");
      });
    }

    // Resolves true when the flow is finished with (connected, cancelled or
    // superseded) and false while the provider is still waiting on the person.
    async function completeFlow(provider, body, seq) {
      var res = await api(accountUrlFor(provider) + "/complete", {
        method: "POST", body: JSON.stringify(body)
      });
      if (seq !== flowSeq) return true;
      if (res && res.status === "connected") {
        clearFlow();
        await refresh();
        setSubStatus(providerLabel(provider) + " connected.");
        return true;
      }
      setSubStatus((res && res.detail) || "Waiting for you to approve the sign-in \u2026");
      return false;
    }

    function showPasteFlow(provider, flow) {
      flowShell(provider, flow.instructions);
      var seq = flowSeq;
      if (flow.authorize_url) window.open(flow.authorize_url, "_blank", "noopener");

      var input = document.createElement("input");
      input.type = "text";
      input.className = "sub-paste";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.placeholder = "Paste the whole address here";
      input.setAttribute("aria-label", "Address from the approval page");
      flowHost.appendChild(input);

      var actions = document.createElement("div");
      actions.className = "ac-actions";
      var finish = actionBtn("Finish", "btn primary", function () {
        var code = input.value.trim();
        if (!code) {
          setSubStatus("Paste the address from the approval page first.", true);
          return;
        }
        finish.disabled = true;
        setSubStatus("Finishing sign-in \u2026");
        completeFlow(provider, { flow_id: flow.flow_id, code: code }, seq)
          .then(function (done) { if (!done) finish.disabled = false; })
          .catch(function (e) {
            if (seq !== flowSeq) return;
            finish.disabled = false;
            setSubStatus(String(e.message || e), true);
          });
      });
      actions.appendChild(finish);
      if (flow.authorize_url) actions.appendChild(flowLink(flow.authorize_url, "Open the page again"));
      actions.appendChild(cancelBtn());
      flowHost.appendChild(actions);
      setSubStatus("");
      input.focus();
    }

    function showDeviceFlow(provider, flow) {
      flowShell(provider, flow.instructions);
      var seq = flowSeq;

      if (flow.user_code) {
        var code = document.createElement("code");
        code.className = "sub-code";
        code.textContent = flow.user_code;
        flowHost.appendChild(code);
      }
      var actions = document.createElement("div");
      actions.className = "ac-actions";
      if (flow.verification_url) {
        actions.appendChild(flowLink(flow.verification_url, "Open the sign-in page"));
      }
      actions.appendChild(cancelBtn());
      flowHost.appendChild(actions);

      var deadline = Date.now() + (Number(flow.expires_in) || 600) * 1000;
      setSubStatus("Waiting for you to approve the sign-in \u2026");

      function tick() {
        pollTimer = null;
        if (seq !== flowSeq) return;
        if (Date.now() >= deadline) {
          clearFlow();
          setSubStatus("The sign-in code ran out. Start again when you're ready.", true);
          return;
        }
        completeFlow(provider, { flow_id: flow.flow_id }, seq)
          .then(function (done) { if (!done && seq === flowSeq) pollTimer = setTimeout(tick, POLL_MS); })
          .catch(function (e) {
            if (seq !== flowSeq) return;
            clearFlow();
            setSubStatus(String(e.message || e), true);
          });
      }
      pollTimer = setTimeout(tick, POLL_MS);
    }

    async function beginFlow(provider) {
      clearFlow();
      connectBtns.forEach(function (b) { b.disabled = true; });
      setSubStatus("Starting sign-in \u2026");
      try {
        var flow = await api(accountUrlFor(provider) + "/begin", {
          method: "POST", body: JSON.stringify({ scope: "shared" })
        });
        if (flow.kind === "device") showDeviceFlow(provider, flow);
        else showPasteFlow(provider, flow);
      } catch (e) {
        setSubStatus(String(e.message || e), true);
      } finally {
        connectBtns.forEach(function (b) { b.disabled = false; });
      }
    }

    connectBtns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        beginFlow(btn.getAttribute("data-provider"));
      });
    });

    reloadAccounts();
  })();

  /* ---- dashboard aggregates ---- */
  function setDashNumber(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = value == null ? "\u2014" : String(value);
  }

  function dashList(id) {
    var host = document.getElementById(id);
    if (host) { host.textContent = ""; host.className = "dash-list"; }
    return host;
  }

  function dashMessage(id, text, isErr) {
    var host = document.getElementById(id);
    if (!host) return;
    host.textContent = text;
    host.className = "dash-list ac-dim" + (isErr ? " err" : "");
  }

  function dashItem(host, who, what, pill) {
    var row = document.createElement("div");
    row.className = "dash-item";
    var left = document.createElement("div");
    var name = document.createElement("div");
    name.className = "dash-who";
    name.textContent = who;
    left.appendChild(name);
    if (what) {
      var meta = document.createElement("div");
      meta.className = "dash-what";
      meta.textContent = what;
      left.appendChild(meta);
    }
    row.appendChild(left);
    if (pill) row.appendChild(pill);
    host.appendChild(row);
  }

  // Newest row wins per bucket; the endpoints already order newest-first.
  function latestPerKey(rows, keyOf) {
    var seen = Object.create(null);
    var out = [];
    rows.forEach(function (row) {
      var key = keyOf(row);
      if (seen[key]) return;
      seen[key] = true;
      out.push(row);
    });
    return out;
  }

  async function dashLoadReleases() {
    var rows = await fetchManifests();
    var host = dashList("dash-releases");
    if (!host) return;
    if (!rows.length) {
      dashMessage("dash-releases", "Nothing published yet.");
      return;
    }
    // Key includes format: a (platform, arch) can publish up to four builds
    // (default/deb/rpm/tar) and each newest one deserves a line.
    latestPerKey(rows, function (row) {
      return row.channel + "|" + row.platform + "|" + row.arch + "|" + rowFormat(row);
    }).slice(0, 8).forEach(function (row) {
      var pill = document.createElement("span");
      pill.className = "pill " + (row.channel === "stable" ? "st-active" : "st-pending");
      pill.textContent = row.channel;
      dashItem(host,
        row.version + " \u00b7 " + platformLabel(row),
        formatLabel(row) + " \u00b7 " + fmtBytes(row.size) + " \u00b7 " + fmtStamp(row.published_at),
        pill);
    });
  }

  async function dashLoadCi() {
    var builds = await fetchCiBuilds(50);
    var host = dashList("dash-ci");
    if (!host) return;
    if (!builds.length) {
      dashMessage("dash-ci", "No CI builds reported yet.");
      return;
    }
    latestPerKey(builds, function (build) {
      return build.platform + "|" + build.arch;
    }).slice(0, 8).forEach(function (build) {
      dashItem(host,
        platformLabel(build) + " \u00b7 " + (build.version || "?"),
        "run " + shortHash(build.run_id, 10) + " \u00b7 " + shortHash(build.commit_sha, 8) +
          " \u00b7 " + fmtStamp(build.updated_at),
        ciPill(build.status));
    });
  }

  async function dashLoadAccounts() {
    // limit=1 keeps this cheap: only the unfiltered totals are wanted here.
    var data = await api("/api/admin/accounts?limit=1");
    setDashNumber("dash-accounts-total", Number(data.total) || 0);
    setDashNumber("dash-accounts-pending", Number(data.pending) || 0);
  }

  async function dashLoadAppeals() {
    var payload = await api("/api/admin/security/appeals");
    var appeals = (payload && payload.appeals) || [];
    setDashNumber("dash-appeals-pending", appeals.filter(function (appeal) {
      return appeal.state === "pending";
    }).length);
  }

  function refreshDashboard() {
    if (!document.getElementById("panel-dashboard")) return;
    dashLoadReleases().catch(function (e) {
      dashMessage("dash-releases", "Couldn't load releases: " + (e.message || e), true);
    });
    dashLoadCi().catch(function (e) {
      dashMessage("dash-ci", "Couldn't load CI builds: " + (e.message || e), true);
    });
    dashLoadAccounts().catch(function () {});
    dashLoadAppeals().catch(function () {});
  }

  /* ---- releases tab: published manifests (with promote) + CI builds ---- */
  function relStatus(msg, isErr) {
    var el = document.getElementById("rel-status");
    if (!el) return;
    el.textContent = msg || "";
    el.className = isErr ? "ac-dim err" : "ac-dim";
  }

  function tableMessage(tbody, cols, text, isErr) {
    tbody.innerHTML = "";
    var tr = document.createElement("tr");
    if (isErr) tr.className = "row-err";
    var td = document.createElement("td");
    td.colSpan = cols;
    td.className = isErr ? "" : "table-empty";
    td.textContent = text;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  async function promoteRelease(row) {
    var target = row.stable_version || "the paired stable version";
    if (!window.confirm(
          "Promote " + row.version + " (" + platformLabel(row) + ", " + formatLabel(row) +
          ") to the stable channel as " + target +
          "?\n\nEvery customer on stable will be offered this build.")) return false;
    relStatus("Promoting " + row.version + " to stable " + target + " \u2026");
    var created = await api("/api/admin/releases/promote", {
      method: "POST",
      body: JSON.stringify({
        // format is explicit: omitting it would promote the platform-default
        // (AppImage/exe/zip) row even when this row is a deb/rpm/tar build.
        channel: "beta", version: row.version,
        platform: row.platform, arch: row.arch, format: rowFormat(row)
      })
    });
    relStatus("Published stable " + ((created && created.version) || target) + " for " +
              platformLabel(row) + ".");
    return true;
  }

  async function deleteRelease(row) {
    if (!window.confirm("Delete the " + row.channel + " " + row.version + " " +
                        platformLabel(row) + " (" + formatLabel(row) + ") manifest?")) return false;
    // format is explicit: omitting it would address the platform-default row.
    var query = new URLSearchParams({
      channel: row.channel, version: row.version,
      platform: row.platform, arch: row.arch, format: rowFormat(row)
    });
    relStatus("Deleting " + row.channel + " " + row.version + " \u2026");
    await api("/api/admin/release-manifests?" + query.toString(), { method: "DELETE" });
    relStatus("Manifest deleted.");
    return true;
  }

  function releaseActions(row) {
    var host = document.createElement("div");
    host.className = "ac-actions";
    var evidence = evidenceComplete(row);
    var pair = hasStablePair(row);
    if (row.channel === "beta") {
      if (row.promotable === true) {
        host.appendChild(actionBtn("Promote to stable", "btn primary", function () {
          promoteRelease(row).then(function (done) {
            if (done) reloadManifestViews();
          }).catch(function (e) {
            relStatus("Promote failed: " + (e.message || e), true);
          });
        }));
      } else {
        var why = document.createElement("span");
        why.className = "ac-dim";
        why.style.fontSize = "var(--step--2)";
        why.textContent = !pair ? "no stable signature pair" :
                          !evidence ? "evidence incomplete" : "publisher verification required";
        why.title = "Only verified beta builds approved for stable publication can be promoted.";
        host.appendChild(why);
      }
    }
    host.appendChild(actionBtn("Delete", "rm-btn", function () {
      deleteRelease(row).then(function (done) {
        if (done) reloadManifestViews();
      }).catch(function (e) {
        relStatus("Delete failed: " + (e.message || e), true);
      });
    }));
    return host;
  }

  function renderReleaseRows(tbody, rows) {
    tbody.innerHTML = "";
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      var evidence = evidenceComplete(row);
      tr.innerHTML =
        "<td><span class='pill " + (row.channel === "stable" ? "st-active" : "st-pending") + "'>" +
          esc(row.channel) + "</span></td>" +
        "<td>" + esc(row.version) +
          (hasStablePair(row) && row.channel === "beta"
            ? "<span class='pair-badge' title='Signed stable pair ready'>" +
              iconHtml("arrow-right") + esc(row.stable_version || "stable") + "</span>"
            : "") + "</td>" +
        "<td>" + esc(platformLabel(row)) + "</td>" +
        "<td>" + formatBadge(row) + "</td>" +
        "<td class='col-num'>" + esc(fmtBytes(row.size)) + "</td>" +
        "<td class='col-mono' title='" + esc(row.sha256) + "'>" + esc(shortHash(row.sha256, 12)) + "</td>" +
        "<td><span class='evi-badge " + (evidence ? "ok" : "missing") + "'>" +
          (evidence ? "complete" : "missing") + "</span></td>" +
        "<td class='col-when'>" + esc(fmtStamp(row.published_at)) + "</td>" +
        "<td class='col-act'></td>";
      tr.querySelector(".col-act").appendChild(releaseActions(row));
      tbody.appendChild(tr);
    });
  }

  async function loadReleaseTable() {
    var tbody = document.getElementById("rel-rows");
    if (!tbody) return;
    var rows = await fetchManifests();
    if (!rows.length) {
      tableMessage(tbody, 9, "No customer release manifests published.");
      return;
    }
    renderReleaseRows(tbody, rows);
  }

  async function loadCiTable() {
    var tbody = document.getElementById("ci-rows");
    if (!tbody) return;
    var builds = await fetchCiBuilds(30);
    if (!builds.length) {
      tableMessage(tbody, 7, "No CI builds reported yet.");
      return;
    }
    tbody.innerHTML = "";
    builds.forEach(function (build) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td class='col-mono' title='" + esc(build.run_id) + "'>" + esc(shortHash(build.run_id, 10)) + "</td>" +
        "<td>" + esc(platformLabel(build)) + "</td>" +
        "<td>" + esc(build.version || "\u2014") + "</td>" +
        "<td class='col-pill'></td>" +
        "<td class='col-mono' title='" + esc(build.commit_sha) + "'>" + esc(shortHash(build.commit_sha, 8)) + "</td>" +
        "<td class='col-when'>" + esc(fmtStamp(build.updated_at)) + "</td>" +
        "<td class='col-detail'>" + esc(build.detail || "") + "</td>";
      tr.querySelector(".col-pill").appendChild(ciPill(build.status));
      tbody.appendChild(tr);
    });
  }

  function refreshReleaseTables() {
    var relBody = document.getElementById("rel-rows");
    if (relBody) {
      loadReleaseTable().catch(function (e) {
        tableMessage(relBody, 9, "Couldn't load release manifests: " + (e.message || e), true);
      });
    }
    var ciBody = document.getElementById("ci-rows");
    if (ciBody) {
      loadCiTable().catch(function (e) {
        tableMessage(ciBody, 7, "Couldn't load CI builds: " + (e.message || e), true);
      });
    }
  }

  (function initReleaseTables() {
    if (!document.getElementById("rel-rows")) return;
    manifestReloaders.push(function () {
      var relBody = document.getElementById("rel-rows");
      loadReleaseTable().catch(function (e) {
      if (relBody) tableMessage(relBody, 9, "Couldn't load release manifests: " + (e.message || e), true);
      });
      refreshDashboard();
    });
    var relRefresh = document.getElementById("rel-refresh");
    if (relRefresh) relRefresh.addEventListener("click", function () {
      manifestCache.promise = null;
      relStatus("");
      refreshReleaseTables();
    });
    var ciRefresh = document.getElementById("ci-refresh");
    if (ciRefresh) ciRefresh.addEventListener("click", function () {
      var ciStatus = document.getElementById("ci-status");
      if (ciStatus) ciStatus.textContent = "";
      loadCiTable().catch(function (e) {
        if (ciStatus) { ciStatus.textContent = e.message || String(e); ciStatus.className = "ac-dim err"; }
      });
    });
    // Prune superseded beta builds: always a dry run first, then an explicit
    // confirmation that names what will be removed. Stable rows are never
    // touched; the gateway keeps the newest 3 beta versions per build. The
    // same sweep also garbage-collects incoming/ orphans (failed CI publishes,
    // abandoned chunk uploads) — nothing published points at those, and their
    // numbers are reported separately from the release rows. A daily systemd
    // timer (chudware-release-retention) runs this automatically; the button
    // is the manual override.
    var relPrune = document.getElementById("rel-prune");
    if (relPrune) relPrune.addEventListener("click", async function () {
      relPrune.disabled = true;
      relStatus("Checking what pruning would remove \u2026");
      try {
        var dry = await api("/api/admin/releases/prune?dry_run=true", { method: "POST", body: "{}" });
        var doomed = (dry && dry.pruned) || [];
        var orphans = (dry && dry.incoming_orphans) || [];
        if (!doomed.length && !orphans.length) {
          relStatus("Nothing to prune \u2014 every beta build is still within the newest " +
                    ((dry && dry.keep_beta_versions) || 3) + " versions and there are no orphaned uploads.");
          return;
        }
        var lines = doomed.slice(0, 12).map(function (r) {
          return r.channel + " " + r.version + " " + platformLabel(r) + " (" + formatLabel(r) + ")";
        });
        if (doomed.length > 12) lines.push("\u2026 and " + (doomed.length - 12) + " more");
        if (orphans.length) {
          lines.push("plus " + orphans.length + " orphaned upload(s) in incoming/, " +
                     fmtBytes(dry.incoming_freed_bytes) + " \u2014 nothing published points at them");
        }
        var totalFreed = (Number(dry && dry.freed_bytes) || 0) +
                         (Number(dry && dry.incoming_freed_bytes) || 0);
        if (!window.confirm(
              "Prune " + doomed.length + " superseded beta build(s), freeing " +
              fmtBytes(totalFreed) + " in total?\n\n" + lines.join("\n") +
              "\n\nStable builds and the newest 3 beta versions of each build are kept.")) {
          relStatus("");
          return;
        }
        var res = await api("/api/admin/releases/prune?dry_run=false", { method: "POST", body: "{}" });
        var errors = (res && res.errors) || [];
        var freed = (Number(res && res.freed_bytes) || 0) +
                    (Number(res && res.incoming_freed_bytes) || 0);
        var cleaned = ((res && res.incoming_orphans) || []).length;
        relStatus("Pruned " + ((res && res.pruned) || []).length + " build(s)" +
                  (cleaned ? " and " + cleaned + " orphaned upload(s)" : "") +
                  ", freed " + fmtBytes(freed) +
                  (errors.length ? " \u2014 " + errors.length + " error(s), see the gateway log." : "."),
                  errors.length > 0);
        reloadManifestViews();
      } catch (e) {
        relStatus("Prune failed: " + (e.message || e), true);
      } finally {
        relPrune.disabled = false;
      }
    });
    // The tables load on first activation of the Releases tab (see showTab).
  })();

  // Route now that every panel loader above exists.
  showTab(tabFromHash());
})();
