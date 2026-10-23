"use strict";

(function () {
  var state = {
    account: null,
    loaded: {},
    // releases: channel -> the builds[] array from /api/releases/available.
    releases: new Map(),
    download: { detected: null, target: null, substituted: false, channel: null, installCommand: null, copyTimer: 0 },
    subProviders: new Map(),
    subFlows: {},
    orgs: [],
    openTeams: {},
    pendingInviteToken: null
  };
  var pages = ["overview", "team", "downloads", "ai", "usage", "billing", "account", "devices"];
  var PAGE_TITLES = {
    overview: "Overview",
    team: "Files & teams",
    downloads: "Download",
    ai: "AI setup",
    usage: "Usage & credits",
    billing: "Plans & billing",
    account: "Account & security",
    devices: "Devices"
  };

  function el(id) { return document.getElementById(id); }
  function qa(selector, root) { return Array.from((root || document).querySelectorAll(selector)); }
  function titleCase(value) {
    value = String(value || "");
    return value ? value.charAt(0).toUpperCase() + value.slice(1) : "—";
  }
  function number(value) { return new Intl.NumberFormat().format(Number(value || 0)); }
  function asDate(value) {
    // Postgres numeric columns (EXTRACT(EPOCH FROM …)) arrive as JSON STRINGS,
    // e.g. "1787850111.706034", so an epoch has to be recognised before
    // falling back to Date's string parsing. Treating one as a date literal
    // produced an Invalid Date and Intl then threw "Invalid time value",
    // which took the whole Devices table down.
    var epoch = typeof value === "number" ? value : Number(value);
    var parsed = isFinite(epoch) ? new Date(epoch * 1000) : new Date(value);
    return isFinite(parsed.getTime()) ? parsed : null;
  }
  function date(value) {
    if (!value) return "—";
    var parsed = asDate(value);
    if (!parsed) return "—";
    return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" })
      .format(parsed);
  }
  function dateTime(value) {
    if (!value) return "Never";
    var parsed = asDate(value);
    if (!parsed) return "Unknown";
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" })
      .format(parsed);
  }
  function relativeDays(value) {
    if (!value) return null;
    return Math.ceil((Number(value) * 1000 - Date.now()) / 86400000);
  }
  function money(minor, currency) {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: String(currency || "usd").toUpperCase() })
      .format(Number(minor || 0) / 100);
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function text(tag, value, className) {
    var node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  }
  function showMessage(message, isError) {
    var box = el("global-message");
    box.textContent = message;
    box.classList.toggle("error", Boolean(isError));
    box.hidden = false;
    window.setTimeout(function () { box.hidden = true; }, 7000);
  }
  var SAFE_REASON_LABELS = {
    integrity_violation: "App integrity issue",
    identity_mismatch: "Device identity changed",
    credential_compromise: "Credential security issue",
    replay_detected: "Repeated security receipt",
    policy_violation: "Security policy issue",
    anomalous_activity: "Unusual device activity"
  };
  function reasonLabels(codes) {
    var labels = (Array.isArray(codes) ? codes : []).map(function (code) {
      return SAFE_REASON_LABELS[String(code)] || "Security policy issue";
    });
    return labels.filter(function (label, index) { return labels.indexOf(label) === index; }).join(", ") || "No action recorded";
  }
  function securityState(device) {
    return String(device.security_status || (device.revoked_at ? "revoked" : "active"));
  }
  function securityStateLabel(value) {
    return ({ active: "Active", pending: "Review pending", revoked: "Revoked", restored: "Restored", upheld: "Upheld" })[value] || "Under review";
  }

  async function api(path, options) {
    var init = Object.assign({ credentials: "same-origin", headers: { Accept: "application/json" } }, options || {});
    if (init.body) init.headers["Content-Type"] = "application/json";
    var response = await fetch(path, init);
    var contentType = response.headers.get("content-type") || "";
    var body = contentType.includes("json") ? await response.json() : null;
    if (!response.ok) {
      var detail = body && body.detail ? body.detail : "Request failed (" + response.status + ")";
      throw new Error(detail);
    }
    return body;
  }

  function setPage(name, updateHash) {
    if (pages.indexOf(name) < 0) name = "overview";
    qa(".nav-item").forEach(function (button) {
      button.classList.toggle("active", button.dataset.page === name);
    });
    qa(".page").forEach(function (page) {
      page.classList.toggle("active", page.dataset.view === name);
    });
    if (name !== "ai") { stopSubscriptionFlows(); clear(el("suboauth-flow")); }
    el("page-title").textContent = PAGE_TITLES[name];
    document.title = "Chudware · " + PAGE_TITLES[name];
    document.body.classList.remove("menu-open");
    el("menu-button").setAttribute("aria-expanded", "false");
    if (updateHash && window.location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    loadPage(name).catch(function (error) { showMessage(error.message, true); });
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  function arrowLink(link, label, glyph) {
    clear(link);
    link.appendChild(document.createTextNode(label + " "));
    var mark = text(glyph === "→" ? "i" : "span", glyph);
    mark.setAttribute("aria-hidden", "true");
    link.appendChild(mark);
  }

  function applyCloudGate() {
    var allowed = Boolean(state.account && state.account.cloud);
    var workspace = state.account && state.account.workspace;
    var appUrl = "/app/?team=" + encodeURIComponent(workspace ? workspace.id : "personal");
    var topbar = el("open-cloud");
    topbar.href = allowed ? appUrl : "#billing";
    arrowLink(topbar, allowed ? "Open Chudware" : "Get cloud access", "↗");
    var hero = el("hero-cloud");
    hero.href = allowed ? appUrl : "#billing";
    hero.textContent = allowed ? (workspace ? "Open " + workspace.name : "Open cloud workspace") : "Upgrade to Studio";
    var dest = el("dest-cloud");
    dest.href = allowed ? appUrl : "#billing";
    dest.classList.toggle("gated", !allowed);
    arrowLink(el("dest-cloud-cta"), allowed ? "Open workspace" : "Included with Studio — upgrade", "→");
  }

  var PROVIDER_LABELS = { google: "Google", github: "GitHub", microsoft: "Microsoft", apple: "Apple" };
  function providerLabel(provider) {
    var key = String(provider || "").toLowerCase();
    return PROVIDER_LABELS[key] || titleCase(provider);
  }
  function providerSentence(providers) {
    var names = (Array.isArray(providers) ? providers : []).map(providerLabel);
    if (!names.length) return "a linked identity provider";
    if (names.length === 1) return names[0];
    return names.slice(0, -1).join(", ") + " or " + names[names.length - 1];
  }

  function renderAccount(account) {
    state.account = account;
    var display = account.display || account.email.split("@")[0];
    el("welcome").textContent = "Welcome back, " + display;
    el("side-email").textContent = account.email;
    el("side-plan").textContent = account.plan + " plan";
    el("avatar").textContent = display.charAt(0).toUpperCase();
    el("account-email").textContent = account.email;
    el("account-plan").textContent = account.plan;
    el("account-plan-detail").textContent = titleCase(account.plan);
    el("account-renewal").textContent = account.paid_through ? date(account.paid_through) : "—";
    el("renewal-label").textContent = account.paid_through ? "Renews " + date(account.paid_through) : "No paid renewal scheduled";
    var days = relativeDays(account.trial_ends_at);
    el("trial-countdown").textContent = days === null ? "Not on trial" : days > 0 ? days + " day" + (days === 1 ? "" : "s") + " remaining" : "Trial ended";
    el("account-summary").textContent = account.workspace
      ? account.workspace.org_name + " / " + account.workspace.name + (account.workspace.storage_provider === "box" ? " is your selected live Box-backed workspace. " : " is your selected shared workspace. ") + "Account plan and AI credits are unchanged."
      : account.plan === "trial"
      ? "Your trial includes the browser workspace and native desktop apps."
      : account.cloud
        ? "Your " + titleCase(account.plan) + " plan includes the browser workspace and native desktop apps."
        : "Your Desktop plan keeps native CAD active. Upgrade to Studio for the browser workspace.";
    el("admin-link").hidden = account.role !== "admin";
    el("password-form").hidden = !account.has_password;
    // The form is hidden for provider-only accounts, so the explanation lives
    // OUTSIDE it: hiding the form must never leave an empty card behind.
    el("password-status").textContent = account.has_password
      ? ""
      : "This account signs in with " + providerSentence(account.linked_identities)
        + ", so there is no password to change.";
    el("credits-option").hidden = !account.ai_grant;
    var identities = el("identity-list");
    clear(identities);
    if (account.has_password) {
      var password = text("div", "", "identity");
      password.append(icon("key"), text("span", "Password"), text("b", "Active"));
      identities.appendChild(password);
    }
    (account.linked_identities || []).forEach(function (provider) {
      var row = text("div", "", "identity");
      row.append(icon("account"), text("span", providerLabel(provider)), text("b", "Linked"));
      identities.appendChild(row);
    });
    if (!identities.childElementCount) identities.appendChild(text("p", "No sign-in method is linked.", "empty"));
    applyCloudGate();
    qa("[data-plan-card]").forEach(function (card) { card.classList.toggle("current", card.dataset.planCard === account.plan); });
    qa(".plan-action").forEach(function (button) {
      if (button.dataset.plan === account.plan) {
        button.textContent = "Current plan";
        button.disabled = true;
      } else {
        button.textContent = account.subscription_status ? "Switch to " + titleCase(button.dataset.plan) : "Choose " + titleCase(button.dataset.plan);
      }
    });
  }

  async function loadAccount() {
    var account = await api("/api/portal/account");
    renderAccount(account);
    state.loaded.account = true;
  }

  async function openBillingPortal() {
    var result = await api("/api/billing/portal", { method: "POST" });
    window.location.assign(result.url);
  }

  async function choosePlan(plan) {
    if (state.account && state.account.subscription_status) {
      await openBillingPortal();
      return;
    }
    var result = await api("/api/billing/checkout", { method: "POST", body: JSON.stringify({ plan: plan }) });
    window.location.assign(result.url);
  }


  async function loadBilling() {
    if (state.loaded.billing) return;
    var table = el("invoice-rows");
    clear(table);
    var note = el("billing-portal-note");
    if (!state.account.billing_configured) {
      var unavailable = document.createElement("tr");
      var cell = text("td", "Billing is not configured on this deployment.", "empty");
      cell.colSpan = 5;
      unavailable.appendChild(cell);
      table.appendChild(unavailable);
      el("billing-portal").disabled = true;
      qa(".plan-action").forEach(function (button) { button.disabled = true; });
      note.textContent = "Payments are not set up on this deployment yet — contact support to change your plan.";
      state.loaded.billing = true;
      return;
    }
    note.textContent = "";
    var payload = await api("/api/portal/invoices");
    var invoices = payload.invoices || [];
    if (!invoices.length) {
      var none = document.createElement("tr");
      var empty = text("td", "No invoices yet.", "empty");
      empty.colSpan = 5;
      none.appendChild(empty);
      table.appendChild(none);
    }
    invoices.forEach(function (invoice) {
      var row = document.createElement("tr");
      row.append(text("td", invoice.number || invoice.id), text("td", date(invoice.created)), text("td", titleCase(invoice.status)), text("td", money(invoice.amount_paid, invoice.currency)));
      var linkCell = document.createElement("td");
      if (invoice.hosted_invoice_url) {
        var link = text("a", "View", "invoice-link");
        link.href = invoice.hosted_invoice_url;
        link.rel = "noopener";
        linkCell.appendChild(link);
      }
      row.appendChild(linkCell);
      table.appendChild(row);
    });
    state.loaded.billing = true;
  }

  function renderChart(history) {
    var host = el("usage-chart");
    clear(host);
    if (!history.length) {
      host.appendChild(text("p", "No credit spend in this period.", "empty"));
      return;
    }
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 900 280");
    svg.setAttribute("preserveAspectRatio", "none");
    var defs = document.createElementNS(ns, "defs");
    var grad = document.createElementNS(ns, "linearGradient");
    grad.id = "chart-fill"; grad.setAttribute("x1", "0"); grad.setAttribute("y1", "0"); grad.setAttribute("x2", "0"); grad.setAttribute("y2", "1");
    [["0%", ".28"], ["100%", "0"]].forEach(function (stopData) { var stop = document.createElementNS(ns, "stop"); stop.setAttribute("offset", stopData[0]); stop.setAttribute("stop-color", "#42ecce"); stop.setAttribute("stop-opacity", stopData[1]); grad.appendChild(stop); });
    defs.appendChild(grad); svg.appendChild(defs);
    var values = history.map(function (item) { return Number(item.credits || 0); });
    var max = Math.max.apply(null, values.concat([1]));
    [30, 95, 160, 225].forEach(function (y) { var line = document.createElementNS(ns, "line"); line.setAttribute("x1", "38"); line.setAttribute("x2", "885"); line.setAttribute("y1", String(y)); line.setAttribute("y2", String(y)); line.setAttribute("class", "grid-line"); svg.appendChild(line); });
    var points = history.map(function (item, index) { var x = 45 + index * (830 / Math.max(1, history.length - 1)); var y = 230 - Number(item.credits || 0) / max * 190; return [x, y]; });
    var area = document.createElementNS(ns, "path");
    area.setAttribute("d", "M" + points[0][0] + ",230 L" + points.map(function (p) { return p[0] + "," + p[1]; }).join(" L") + " L" + points[points.length - 1][0] + ",230 Z"); area.setAttribute("class", "area"); svg.appendChild(area);
    var linePath = document.createElementNS(ns, "path"); linePath.setAttribute("d", "M" + points.map(function (p) { return p[0] + "," + p[1]; }).join(" L")); linePath.setAttribute("class", "line"); svg.appendChild(linePath);
    points.forEach(function (point, index) { var dot = document.createElementNS(ns, "circle"); dot.setAttribute("cx", String(point[0])); dot.setAttribute("cy", String(point[1])); dot.setAttribute("r", "3"); dot.setAttribute("class", "dot"); var label = document.createElementNS(ns, "title"); label.textContent = history[index].day + ": " + number(history[index].credits) + " credits"; dot.appendChild(label); svg.appendChild(dot); });
    [0, Math.floor((history.length - 1) / 2), history.length - 1].filter(function (v, i, a) { return a.indexOf(v) === i; }).forEach(function (index) { var label = document.createElementNS(ns, "text"); label.setAttribute("x", String(points[index][0])); label.setAttribute("y", "260"); label.setAttribute("text-anchor", index === 0 ? "start" : index === history.length - 1 ? "end" : "middle"); label.textContent = history[index].day.slice(5); svg.appendChild(label); });
    host.appendChild(svg);
  }

  async function loadUsage() {
    if (state.loaded.usage) return;
    var payload = await api("/api/portal/usage?days=30");
    el("usage-balance").textContent = number(payload.credit_balance);
    el("usage-ceiling").textContent = payload.credit_ceiling_monthly ? number(payload.credit_ceiling_monthly) : "No cap";
    el("ai-credit-balance").textContent = number(payload.credit_balance) + " credits available";
    renderChart(payload.history || []);
    state.loaded.usage = true;
  }

  function renderByok(keys) {
    var host = el("byok-list");
    clear(host);
    if (!keys.length) {
      host.appendChild(text("p", "No personal provider keys are configured yet.", "empty"));
      return;
    }
    keys.forEach(function (key) {
      var chip = text("span", "", "provider-chip byok-chip");
      chip.append(text("i", ""), text("span", titleCase(key.provider)));
      var detail = key.hint ? "Validated · " + key.hint : titleCase(key.state);
      chip.appendChild(text("small", detail));
      var remove = text("button", "Remove", "danger small");
      remove.type = "button";
      remove.dataset.byokProvider = key.provider;
      chip.appendChild(remove);
      host.appendChild(chip);
    });
  }

  function subscriptionPath(provider, action) {
    return "/api/providers/accounts/" + encodeURIComponent(provider) + "/" + action;
  }

  function subscriptionLabel(provider) {
    var meta = state.subProviders.get(provider);
    return meta ? meta.label : titleCase(provider);
  }

  function timeLeft(seconds) {
    var minutes = Math.floor(seconds / 60);
    var rest = seconds % 60;
    return minutes + ":" + (rest < 10 ? "0" : "") + rest;
  }

  function stopSubscriptionFlow(provider) {
    var flow = state.subFlows[provider];
    if (!flow) return;
    if (flow.timer) window.clearInterval(flow.timer);
    delete state.subFlows[provider];
  }

  function stopSubscriptionFlows() {
    Object.keys(state.subFlows).forEach(function (provider) { stopSubscriptionFlow(provider); });
  }

  function subscriptionFlowHost() {
    var host = el("suboauth-flow");
    clear(host);
    return host;
  }

  function renderSubscriptionAccounts(accounts) {
    var host = el("suboauth-list");
    clear(host);
    if (!accounts.length) {
      host.appendChild(text("p", "No subscription is connected yet. Connect Claude or ChatGPT in step 02 above.", "empty"));
      return;
    }
    accounts.forEach(function (account) {
      var chip = text("span", "", "provider-chip byok-chip");
      chip.append(text("i", ""), text("span", subscriptionLabel(account.provider)));
      var detail = account.label || "Connected";
      if (account.plan) detail += " · " + titleCase(account.plan);
      chip.appendChild(text("small", detail));
      if (account.expired) chip.appendChild(text("small", "Expired", "status-revoked"));
      var remove = text("button", "Disconnect", "danger small");
      remove.type = "button";
      remove.dataset.suboauthProvider = account.provider;
      remove.dataset.suboauthAccount = account.account_key;
      chip.appendChild(remove);
      host.appendChild(chip);
    });
  }

  async function loadSubscriptionAccounts() {
    try {
      var payload = await api("/api/providers/accounts");
      state.subProviders = new Map((payload.providers || []).map(function (provider) { return [provider.provider, provider]; }));
      qa("[data-suboauth-begin]").forEach(function (button) { button.hidden = !state.subProviders.has(button.dataset.suboauthBegin); });
      renderSubscriptionAccounts((payload.accounts || []).filter(function (account) { return account.scope === "personal"; }));
    } catch (error) {
      var host = el("suboauth-list");
      clear(host);
      host.appendChild(text("p", error.message, "empty"));
    }
  }

  async function subscriptionConnected(provider, account) {
    stopSubscriptionFlows();
    var host = subscriptionFlowHost();
    var label = account && account.label ? account.label : subscriptionLabel(provider);
    host.appendChild(text("p", "Connected " + label + ". Turns on this subscription use no Chudware credits.", "fine"));
    state.loaded.ai = false;
    state.loaded.usage = false;
    await loadAI(true);
  }

  function renderPasteFlow(provider, flow) {
    window.open(flow.authorize_url, "_blank", "noopener");
    var host = subscriptionFlowHost();
    host.appendChild(text("p", flow.instructions, "fine"));
    var field = document.createElement("input");
    field.type = "text";
    field.autocomplete = "off";
    field.spellcheck = false;
    field.placeholder = "Paste the address here";
    field.setAttribute("aria-label", "Address from the finished sign-in tab");
    var link = text("a", "Open the sign-in page", "secondary small");
    link.href = flow.authorize_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    var finish = text("button", "Finish connecting", "primary small");
    finish.type = "button";
    var status = text("p", "", "fine");
    var row = text("div", "", "button-row");
    row.append(link, finish);
    host.append(field, row, status);
    state.subFlows[provider] = { flowId: flow.flow_id, timer: 0 };
    finish.addEventListener("click", async function () {
      var pasted = field.value.trim();
      if (!pasted) { status.textContent = "Paste the address from the finished sign-in tab first."; return; }
      finish.disabled = true;
      status.textContent = "Finishing the connection…";
      try {
        var result = await api(subscriptionPath(provider, "complete"), { method: "POST", body: JSON.stringify({ flow_id: flow.flow_id, code: pasted }) });
        if (result.status === "connected") { field.value = ""; await subscriptionConnected(provider, result.account); return; }
        status.textContent = result.detail || "Waiting for you to approve access.";
        finish.disabled = false;
      } catch (error) {
        status.textContent = error.message;
        finish.disabled = false;
      }
    });
  }

  function renderDeviceFlow(provider, flow) {
    var host = subscriptionFlowHost();
    host.appendChild(text("p", flow.instructions, "fine"));
    host.appendChild(text("strong", flow.user_code, "device-code"));
    var link = text("a", "Open the sign-in page", "secondary small");
    link.href = flow.verification_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    var row = text("div", "", "button-row");
    row.appendChild(link);
    var status = text("p", "Waiting for you to approve the code…", "fine");
    host.append(row, status);
    var record = { flowId: flow.flow_id, timer: 0, deadline: Date.now() + Number(flow.expires_in || 600) * 1000 };
    state.subFlows[provider] = record;
    record.timer = window.setInterval(function () {
      pollDeviceFlow(provider, record, status).catch(function (error) { status.textContent = error.message; });
    }, 5000);
  }

  async function pollDeviceFlow(provider, record, status) {
    if (state.subFlows[provider] !== record) return;
    var remaining = Math.max(0, Math.ceil((record.deadline - Date.now()) / 1000));
    if (!remaining) {
      stopSubscriptionFlow(provider);
      status.textContent = "This code expired before it was approved. Start again to get a new one.";
      return;
    }
    var result;
    try {
      result = await api(subscriptionPath(provider, "complete"), { method: "POST", body: JSON.stringify({ flow_id: record.flowId }) });
    } catch (error) {
      if (state.subFlows[provider] !== record) return;
      stopSubscriptionFlow(provider);
      status.textContent = error.message;
      return;
    }
    if (state.subFlows[provider] !== record) return;
    if (result.status === "connected") { await subscriptionConnected(provider, result.account); return; }
    status.textContent = (result.detail || "Waiting for you to approve the code.") + " · " + timeLeft(remaining) + " left";
  }

  async function beginSubscriptionFlow(provider, button) {
    stopSubscriptionFlows();
    var host = subscriptionFlowHost();
    host.appendChild(text("p", "Preparing the sign-in link…", "fine"));
    button.disabled = true;
    try {
      var flow = await api(subscriptionPath(provider, "begin"), { method: "POST", body: JSON.stringify({ scope: "personal" }) });
      if (flow.kind === "device") renderDeviceFlow(provider, flow);
      else renderPasteFlow(provider, flow);
    } catch (error) {
      clear(host);
      host.appendChild(text("p", error.message, "fine flow-error"));
    } finally {
      button.disabled = false;
    }
  }

  async function loadAI(force) {
    if (state.loaded.ai && !force) return;
    await loadUsage();
    var payload = await api("/api/providers/keys");
    renderByok(payload.keys || []);
    await loadSubscriptionAccounts();
    state.loaded.ai = true;
  }

  // ---- Downloads ----------------------------------------------------------
  // /api/releases/available is the only source of truth for what exists. The
  // portal asks once per channel and renders exactly the builds it names: no
  // card, chip, or row is ever drawn for a tuple the API did not return, and
  // neither the artifact filename nor the install command is reconstructed
  // here.

  var SVG_NS = "http://www.w3.org/2000/svg";
  // Solid marks are OS logos (filled), line icons are stroked. Path data is the
  // canonical set from the design contract — do not redraw it.
  var ICONS = {
    windows: {
      solid: true,
      shapes: [["path", { d: "M3.4 6.2 10.3 5.25v6.29H3.4zM11.35 5.1 20.6 3.84v7.7h-9.25zM3.4 12.46h6.9v6.29L3.4 17.8zM11.35 12.46h9.25v7.7l-9.25-1.26z" }]]
    },
    apple: {
      solid: true,
      shapes: [
        ["path", { d: "M17.02 12.62c.02-2.2 1.8-3.25 1.88-3.3-1.03-1.5-2.62-1.71-3.18-1.74-1.35-.14-2.64.79-3.32.79-.69 0-1.74-.77-2.87-.75-1.48.02-2.85.86-3.6 2.19-1.54 2.67-.4 6.62 1.1 8.79.74 1.06 1.61 2.24 2.77 2.2 1.1-.04 1.53-.71 2.87-.71 1.33 0 1.71.71 2.87.69 1.19-.02 1.95-1.08 2.69-2.14.85-1.23 1.2-2.42 1.22-2.48-.03-.01-2.34-.9-2.36-3.54z" }],
        ["path", { d: "M14.86 5.9c.6-.73 1.01-1.75.9-2.77-.87.04-1.93.58-2.56 1.31-.56.65-1.05 1.69-.92 2.68.97.08 1.96-.49 2.58-1.22z" }]
      ]
    },
    linux: {
      solid: true,
      shapes: [["path", {
        "fill-rule": "evenodd",
        d: "M8.55 19.75c.72.2 1.05 1.05.74 1.9-.31.85-1.15 1.3-1.87 1.05-.72-.25-.99-1.1-.62-1.93.37-.83 1.03-1.22 1.75-1.02zM15.45 19.75c-.72.2-1.05 1.05-.74 1.9.31.85 1.15 1.3 1.87 1.05.72-.25.99-1.1.62-1.93-.37-.83-1.03-1.22-1.75-1.02z M12 2.4c-2.52 0-4.18 1.76-4.18 4.32 0 .74.06 1.38.06 1.92 0 .95-.6 1.76-1.46 2.96-1.03 1.44-1.84 2.66-2.2 4.16-.37 1.53.26 2.5 1.44 2.5.23 0 .36.17.46.62.4 1.68 2.33 3.08 5.88 3.08s5.48-1.4 5.88-3.08c.1-.45.23-.62.46-.62 1.18 0 1.81-.97 1.44-2.5-.36-1.5-1.17-2.72-2.2-4.16-.86-1.2-1.46-2.01-1.46-2.96 0-.54.06-1.18.06-1.92 0-2.56-1.66-4.32-4.18-4.32z M12 4.85c1.83 0 3.32 1.4 3.32 3.13 0 1.72-1.49 3.12-3.32 3.12S8.68 9.7 8.68 7.98c0-1.73 1.49-3.13 3.32-3.13z M10.62 6.55c.5 0 .9.52.9 1.16s-.4 1.16-.9 1.16-.9-.52-.9-1.16.4-1.16.9-1.16z M13.38 6.55c.5 0 .9.52.9 1.16s-.4 1.16-.9 1.16-.9-.52-.9-1.16.4-1.16.9-1.16z M12 8.6l1.35 1.05L12 10.7l-1.35-1.05z"
      }]]
    },
    package: {
      shapes: [
        ["path", { d: "M3.6 7.9 12 3.6l8.4 4.3v8.2L12 20.4 3.6 16.1z" }],
        ["path", { d: "M3.6 7.9 12 12.2l8.4-4.3M12 12.2v8.2" }]
      ]
    },
    copy: {
      shapes: [
        ["rect", { x: "9", y: "9", width: "11", height: "11", rx: "2.2" }],
        ["path", { d: "M15 9V6.2A2.2 2.2 0 0 0 12.8 4H6.2A2.2 2.2 0 0 0 4 6.2v6.6A2.2 2.2 0 0 0 6.2 15H9" }]
      ]
    },
    check: { shapes: [["path", { d: "M5 12.6 9.6 17.2 19 7.4" }]] },
    key: {
      shapes: [
        ["circle", { cx: "8.6", cy: "15.4", r: "3.9" }],
        ["path", { d: "M11.4 12.6 19.4 4.6M16.6 7.4l2.2 2.2M18.8 5.2l2.2 2.2" }]
      ]
    },
    account: {
      shapes: [
        ["path", { d: "M12 3.2l7 2.5v5.5c0 4.3-2.8 7.4-7 8.6-4.2-1.2-7-4.3-7-8.6V5.7z" }],
        ["path", { d: "M9.1 11.9l2.2 2.2 4.1-4.4" }]
      ]
    }
  };

  function icon(name) {
    var spec = ICONS[name] || ICONS.package;
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    if (spec.solid) {
      svg.setAttribute("fill", "currentColor");
    } else {
      svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor");
      svg.setAttribute("stroke-width", "1.7");
      svg.setAttribute("stroke-linecap", "round");
      svg.setAttribute("stroke-linejoin", "round");
    }
    spec.shapes.forEach(function (shape) {
      var node = document.createElementNS(SVG_NS, shape[0]);
      Object.keys(shape[1]).forEach(function (attribute) { node.setAttribute(attribute, shape[1][attribute]); });
      svg.appendChild(node);
    });
    return svg;
  }

  var PLATFORM_MARKS = { windows: "windows", macos: "apple", linux: "linux" };
  var PLATFORM_LABELS = { windows: "Windows", macos: "macOS", linux: "Linux" };
  var ARCH_LABELS = { x86_64: "64-bit", arm64: "ARM64" };
  var MACOS_ARCH_LABELS = { x86_64: "Intel", arm64: "Apple silicon" };
  // Platform priority is Hunter's call, not alphabetical: Windows and Linux
  // are the shipping targets and the macOS CI runner is intermittently
  // offline, so macOS lists last. The API sorts platforms alphabetically —
  // every platform enumeration in this file re-ranks through PLATFORM_RANK.
  var PLATFORM_RANK = { windows: 0, linux: 1, macos: 2 };
  // Format order within a platform (design contract), for the primary card's
  // chips and the all-platforms list alike: msi then the .exe on Windows;
  // deb, rpm, AppImage, tar.zst on Linux.
  var CHIP_ORDER = {
    windows: ["msi", "default", "deb", "rpm", "tar"],
    linux: ["deb", "rpm", "default", "tar"],
    macos: ["default", "deb", "rpm", "tar"]
  };
  var GENERIC_CHIP_ORDER = ["msi", "default", "deb", "rpm", "tar"];
  var FORMAT_CHIPS = { msi: "Installer (.msi)", deb: ".deb", rpm: ".rpm", tar: ".tar.zst" };
  var DEFAULT_FORMAT_CHIPS = { windows: "Setup (.exe)", macos: ".zip", linux: "AppImage" };
  var FORMAT_NOUNS = { msi: "MSI installer", deb: "deb package", rpm: "rpm package", tar: "tar.zst archive" };
  var DEFAULT_FORMAT_NOUNS = { windows: "installer", macos: "zip archive", linux: "AppImage" };
  var FORMAT_NOTES = {
    msi: "MSI package for managed and scripted deployment — Group Policy, Intune, and msiexec.",
    deb: "Debian package for Debian, Ubuntu, Mint, and other apt systems.",
    rpm: "RPM package for Fedora, RHEL, and openSUSE.",
    tar: "Portable archive — unpack it anywhere, no package manager needed."
  };
  var DEFAULT_FORMAT_NOTES = {
    windows: "Interactive installer for Windows — run it and follow the prompts.",
    macos: "Zipped application bundle — unzip it into Applications.",
    linux: "Single-file AppImage — make it executable and run it."
  };

  function platformLabel(platform) { return PLATFORM_LABELS[platform] || titleCase(platform); }
  function archLabel(platform, arch) {
    if (platform === "macos") return MACOS_ARCH_LABELS[arch] || String(arch || "");
    return ARCH_LABELS[arch] || String(arch || "");
  }
  function targetLabel(platform, arch) {
    var suffix = archLabel(platform, arch);
    return suffix ? platformLabel(platform) + " " + suffix : platformLabel(platform);
  }
  function formatChipLabel(platform, format) {
    return format === "default" ? (DEFAULT_FORMAT_CHIPS[platform] || "Default") : (FORMAT_CHIPS[format] || format);
  }
  function formatNoun(platform, format) {
    return format === "default" ? (DEFAULT_FORMAT_NOUNS[platform] || "build") : (FORMAT_NOUNS[format] || format);
  }
  function formatNote(platform, format) {
    if (format === "default") return DEFAULT_FORMAT_NOTES[platform] || "Native application for " + platformLabel(platform) + ".";
    return FORMAT_NOTES[format] || "Native application for " + platformLabel(platform) + ".";
  }
  function chipOrder(platform) { return CHIP_ORDER[platform] || GENERIC_CHIP_ORDER; }

  function formatBytes(bytes) {
    var value = Number(bytes);
    if (!isFinite(value) || value <= 0) return "unknown size";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    var rounded = index > 0 && value < 10 ? value.toFixed(1) : String(Math.round(value));
    return rounded + " " + units[index];
  }

  function shortDigest(digest) {
    var value = String(digest || "");
    return value.length > 12 ? value.slice(0, 4) + "…" + value.slice(-4) : value;
  }

  function platformFromString(value) {
    var probe = String(value || "").toLowerCase();
    if (probe.indexOf("win") >= 0) return "windows";
    if (probe.indexOf("iphone") >= 0 || probe.indexOf("ipod") >= 0) return null;
    if (probe.indexOf("android") >= 0) return null;
    if (probe.indexOf("cros") >= 0 || probe.indexOf("chrome os") >= 0) return null;
    if (probe.indexOf("mac") >= 0 || probe.indexOf("darwin") >= 0) return "macos";
    if (probe.indexOf("linux") >= 0 || probe.indexOf("x11") >= 0 || probe.indexOf("bsd") >= 0) return "linux";
    return null;
  }

  function archFromHint(value) {
    // Client hints report "x86" for both 32- and 64-bit Intel; only 64-bit
    // desktops are published and /api/releases/available decides what exists,
    // so the hint only has to pick the right family.
    var probe = String(value || "").toLowerCase();
    if (probe === "arm" || probe === "arm64" || probe === "aarch64") return "arm64";
    if (probe === "x86" || probe === "x86_64" || probe === "x64" || probe === "amd64") return "x86_64";
    return null;
  }

  function rendererMentionsApple() {
    try {
      var canvas = document.createElement("canvas");
      var gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
      if (!gl) return false;
      var debugInfo = gl.getExtension("WEBGL_debug_renderer_info");
      if (!debugInfo) return false;
      return String(gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) || "").indexOf("Apple") >= 0;
    } catch (error) {
      return false;
    }
  }

  function archWithoutHints(platform) {
    if (platform === "macos") {
      // Apple silicon exposes a touch-capable pointer surface or an "Apple"
      // WebGL renderer string; Intel Macs do neither.
      return navigator.maxTouchPoints > 0 || rendererMentionsApple() ? "arm64" : "x86_64";
    }
    var probe = String(navigator.platform || "") + " " + String(navigator.userAgent || "");
    return /aarch64|arm64|armv8/i.test(probe) ? "arm64" : "x86_64";
  }

  async function detectPlatform() {
    var hintSource = navigator.userAgentData;
    if (hintSource && typeof hintSource.getHighEntropyValues === "function") {
      try {
        var hints = await hintSource.getHighEntropyValues(["architecture", "platform", "platformVersion"]);
        var hinted = platformFromString(hints.platform || hintSource.platform);
        if (hinted) return { platform: hinted, arch: archFromHint(hints.architecture) || archWithoutHints(hinted) };
      } catch (error) {
        // Hints can be refused; fall through to the legacy navigator fields.
      }
    }
    var platform = platformFromString(String(navigator.platform || "") + " " + String(navigator.userAgent || ""));
    if (!platform) return { platform: null, arch: null };
    return { platform: platform, arch: archWithoutHints(platform) };
  }

  function findBuild(builds, platform, arch, format) {
    for (var index = 0; index < builds.length; index += 1) {
      var build = builds[index];
      if (build.platform === platform && build.arch === arch && build.format === format) return build;
    }
    return null;
  }

  function availableFormats(builds, platform, arch) {
    return chipOrder(platform).filter(function (format) {
      return Boolean(findBuild(builds, platform, arch, format));
    });
  }

  function platformRank(platform) {
    var rank = PLATFORM_RANK[platform];
    return rank === undefined ? Object.keys(PLATFORM_RANK).length : rank;
  }

  function formatRank(platform, format) {
    var index = chipOrder(platform).indexOf(format);
    return index < 0 ? chipOrder(platform).length : index;
  }

  function groupBuilds(builds) {
    // The API sorts platform → arch → format alphabetically; the list ranks
    // platforms through PLATFORM_RANK (Windows, Linux, macOS — see the note
    // there) and formats through CHIP_ORDER. Arch keeps the API's order.
    var groups = [];
    var byTarget = new Map();
    builds.forEach(function (build) {
      var key = build.platform + ":" + build.arch;
      var group = byTarget.get(key);
      if (!group) {
        group = { platform: build.platform, arch: build.arch, builds: [] };
        byTarget.set(key, group);
        groups.push(group);
      }
      group.builds.push(build);
    });
    groups.sort(function (a, b) { return platformRank(a.platform) - platformRank(b.platform); });
    groups.forEach(function (group) {
      group.builds.sort(function (a, b) {
        return formatRank(group.platform, a.format) - formatRank(group.platform, b.format);
      });
    });
    return groups;
  }

  function renderPrimaryMeta(build) {
    var meta = el("primary-meta");
    clear(meta);
    if (!build) return;
    meta.appendChild(document.createTextNode("Version " + build.version + " · " + formatBytes(build.size) + " · sha256 "));
    meta.appendChild(text("code", shortDigest(build.sha256), "digest"));
  }

  function renderFormatChips(builds, target) {
    var host = el("primary-formats");
    clear(host);
    var formats = availableFormats(builds, target.platform, target.arch);
    // A single format is no choice — hide the row rather than render one
    // pressed chip with no alternative (macOS today).
    host.hidden = formats.length < 2;
    if (host.hidden) return;
    formats.forEach(function (format) {
      var chip = text("button", formatChipLabel(target.platform, format), "format-chip");
      chip.type = "button";
      chip.dataset.format = format;
      chip.setAttribute("aria-pressed", format === target.format ? "true" : "false");
      host.appendChild(chip);
    });
  }

  function clearFormatChips() {
    var host = el("primary-formats");
    clear(host);
    host.hidden = true;
  }

  function cloudFallbackLink() {
    var allowed = Boolean(state.account && state.account.cloud);
    var link = text("a", allowed ? "Open the browser workspace" : "See plans that include the browser workspace");
    link.href = allowed ? "/app/" : "#billing";
    return link;
  }

  function substitutionNote(target) {
    var detected = state.download.detected;
    if (target.platform === "macos" && detected.arch === "arm64" && target.arch === "x86_64") {
      // Rosetta 2 is Apple's translation layer for x86_64 binaries. This states
      // a fact about macOS, not a claim that an Apple-silicon build exists.
      return "The Apple-silicon build is not published yet. This Intel build runs on Apple silicon through Rosetta 2.";
    }
    return "The " + targetLabel(detected.platform, detected.arch) + " build is not published yet. This is the "
      + archLabel(target.platform, target.arch) + " build.";
  }

  function renderPrimary(builds) {
    var target = state.download.target;
    var detected = state.download.detected || { platform: null, arch: null };
    var mark = el("primary-mark");
    var note = el("primary-note");
    var action = el("primary-action");
    clear(mark);
    clear(note);
    if (target) {
      mark.appendChild(icon(PLATFORM_MARKS[target.platform] || "package"));
      el("primary-eyebrow").textContent = "Detected · " + targetLabel(detected.platform, detected.arch);
      el("primary-title").textContent = "Chudware for " + platformLabel(target.platform);
      note.textContent = state.download.substituted ? substitutionNote(target) : formatNote(target.platform, target.format);
      renderPrimaryMeta(findBuild(builds, target.platform, target.arch, target.format));
      action.hidden = false;
      action.disabled = false;
      action.textContent = "Download " + formatNoun(target.platform, target.format);
      renderFormatChips(builds, target);
      return;
    }
    mark.appendChild(icon(PLATFORM_MARKS[detected.platform] || "package"));
    el("primary-title").textContent = "Chudware desktop";
    el("primary-meta").textContent = "";
    clearFormatChips();
    action.hidden = true;
    action.disabled = true;
    if (detected.platform) {
      el("primary-eyebrow").textContent = "Detected · " + targetLabel(detected.platform, detected.arch);
      note.appendChild(document.createTextNode("No build is published for " + platformLabel(detected.platform) + " yet. "));
      note.appendChild(cloudFallbackLink());
      note.appendChild(document.createTextNode(" in the meantime, or pick another platform below."));
    } else {
      el("primary-eyebrow").textContent = "Choose your platform";
      note.textContent = "We could not tell which computer you are using. Pick a build from the list below.";
    }
    el("download-all").open = true;
  }

  function renderDownloadList(builds) {
    var host = el("download-list");
    clear(host);
    var groups = groupBuilds(builds);
    if (!groups.length) {
      host.appendChild(text("p", "No builds have been published on this channel yet.", "empty"));
      return;
    }
    groups.forEach(function (group) {
      var section = text("div", "", "download-group");
      var head = text("div", "", "download-group-head");
      head.appendChild(icon(PLATFORM_MARKS[group.platform] || "package"));
      head.appendChild(text("h3", platformLabel(group.platform)));
      head.appendChild(text("span", archLabel(group.platform, group.arch), "download-group-arch"));
      section.appendChild(head);
      group.builds.forEach(function (build) {
        var row = text("div", "", "download-row");
        row.appendChild(text("span", formatChipLabel(group.platform, build.format), "download-row-name"));
        row.appendChild(text("span", "Version " + build.version + " · " + formatBytes(build.size), "download-row-meta"));
        var button = text("button", "Download", "secondary small download-row-action");
        button.type = "button";
        button.dataset.platform = build.platform;
        button.dataset.arch = build.arch;
        button.dataset.format = build.format;
        row.appendChild(button);
        section.appendChild(row);
      });
      host.appendChild(section);
    });
  }

  function setCopyButton(iconName, label) {
    var button = el("install-copy");
    clear(button);
    button.appendChild(icon(iconName));
    button.appendChild(text("span", label));
  }

  function selectNodeContents(node) {
    var range = document.createRange();
    range.selectNodeContents(node);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  async function mintInstallCommand() {
    // The command — host, script path, and token format — is issued by the
    // server, so it is rendered exactly as given and never assembled here.
    var issued = await api("/api/portal/download-token", { method: "POST" });
    state.download.installCommand = issued.install_command;
    el("install-command").textContent = issued.install_command;
    var minutes = Math.round(Number(issued.expires_in || 0) / 60);
    el("install-status").textContent = minutes > 0
      ? "This command is personal to your account and expires in " + minutes + " minute" + (minutes === 1 ? "" : "s") + "."
      : "This command is personal to your account.";
    return issued.install_command;
  }

  async function copyInstallCommand() {
    var command = el("install-command");
    var value = String(command.textContent || "").trim();
    // Tokens are short-lived, so the copy is what gets minted: re-issue on
    // every click so the command the customer pastes is freshly signed.
    try {
      value = await mintInstallCommand();
    } catch (error) {
      if (!value) {
        el("install-status").textContent = "The install command could not be issued: " + error.message;
        return;
      }
      el("install-status").textContent = "Could not refresh the command (" + error.message + "). Copying the one already shown.";
    }
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopyButton("check", "Copied");
      window.clearTimeout(state.download.copyTimer);
      state.download.copyTimer = window.setTimeout(function () { setCopyButton("copy", "Copy"); }, 1600);
    } catch (error) {
      selectNodeContents(command);
      el("install-status").textContent = "Your browser blocked clipboard access. The command is selected — press Ctrl+C to copy it.";
    }
  }

  async function refreshInstallCard() {
    var card = el("install-card");
    var target = state.download.target;
    if (!target || target.platform !== "linux") {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    if (state.download.installCommand) {
      el("install-command").textContent = state.download.installCommand;
      return;
    }
    el("install-command").textContent = "Requesting your install command…";
    try {
      await mintInstallCommand();
    } catch (error) {
      el("install-command").textContent = "";
      el("install-status").textContent = "The install command could not be issued: " + error.message;
    }
  }

  async function startDownload(platform, arch, format, button) {
    var label = button.textContent;
    button.disabled = true;
    button.textContent = "Preparing…";
    try {
      var query = new URLSearchParams({
        channel: el("release-channel").value,
        platform: platform,
        arch: arch,
        format: format
      });
      var manifest = await api("/api/releases/latest?" + query.toString());
      if (!manifest.url) {
        showMessage("Sign in again to get a download link.", true);
        return;
      }
      window.location.assign(manifest.url);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  function selectFormat(format) {
    var target = state.download.target;
    if (!target || target.format === format) return;
    target.format = format;
    renderPrimary(state.releases.get(state.download.channel) || []);
    refreshInstallCard().catch(function (error) { showMessage(error.message, true); });
  }

  function setDownloadsPending() {
    clear(el("primary-mark"));
    el("primary-eyebrow").textContent = "Detecting your computer…";
    el("primary-note").textContent = "";
    el("primary-meta").textContent = "";
    clearFormatChips();
    var action = el("primary-action");
    action.hidden = false;
    action.disabled = true;
    action.textContent = "Download";
    var list = el("download-list");
    clear(list);
    list.appendChild(text("p", "Loading published builds…", "empty"));
  }

  function renderDownloadsFailure(error) {
    var mark = el("primary-mark");
    clear(mark);
    mark.appendChild(icon("package"));
    el("primary-eyebrow").textContent = "Downloads unavailable";
    el("primary-title").textContent = "Chudware desktop";
    el("primary-note").textContent = "The list of published builds could not be loaded: " + error.message;
    el("primary-meta").textContent = "";
    clearFormatChips();
    var action = el("primary-action");
    action.hidden = true;
    action.disabled = true;
    el("install-card").hidden = true;
    clear(el("download-list"));
  }

  function preferredFormat(formats, previous) {
    // A remembered choice wins; otherwise `default` — the AppImage, .exe or
    // .zip — because it is the one build that installs on any distribution.
    // Chip ORDER is a separate, purely visual contract.
    if (previous && formats.indexOf(previous) >= 0) return previous;
    return formats.indexOf("default") >= 0 ? "default" : formats[0];
  }

  function chooseTarget(builds) {
    var detected = state.download.detected;
    var previous = state.download.target ? state.download.target.format : null;
    state.download.substituted = false;
    if (!detected.platform) return null;
    var formats = availableFormats(builds, detected.platform, detected.arch);
    if (formats.length) {
      return {
        platform: detected.platform,
        arch: detected.arch,
        format: preferredFormat(formats, previous)
      };
    }
    // Nothing published for the detected architecture. Offer another
    // architecture of the same platform rather than a dead button: x86_64
    // first, because translation layers run Intel binaries on ARM and not the
    // other way round.
    var alternatives = ["x86_64", "arm64"].filter(function (arch) {
      return arch !== detected.arch && availableFormats(builds, detected.platform, arch).length > 0;
    });
    if (!alternatives.length) return null;
    var substituteArch = alternatives[0];
    var substituteFormats = availableFormats(builds, detected.platform, substituteArch);
    state.download.substituted = true;
    return {
      platform: detected.platform,
      arch: substituteArch,
      format: preferredFormat(substituteFormats, previous)
    };
  }

  async function loadDownloads(force) {
    if (state.loaded.downloads && !force) return;
    var channel = el("release-channel").value;
    if (!state.download.detected) state.download.detected = await detectPlatform();
    setDownloadsPending();
    var payload;
    try {
      payload = await api("/api/releases/available?" + new URLSearchParams({ channel: channel }).toString());
    } catch (error) {
      renderDownloadsFailure(error);
      throw error;
    }
    var builds = Array.isArray(payload.builds) ? payload.builds : [];
    state.releases.set(channel, builds);
    state.download.channel = channel;
    state.download.target = chooseTarget(builds);
    renderPrimary(builds);
    renderDownloadList(builds);
    await refreshInstallCard();
    state.loaded.downloads = true;
  }

  async function loadDevices(force) {
    if (state.loaded.devices && !force) return;
    var results = await Promise.all([api("/api/portal/devices"), api("/api/portal/security-appeals")]);
    var devices = results[0].devices || [];
    var appeals = results[1].appeals || [];
    var table = el("device-rows"); clear(table);
    var select = el("appeal-subject");
    while (select.options.length > 1) select.remove(1);
    if (!devices.length) {
      var empty = document.createElement("tr");
      var emptyCell = text("td", "No desktop devices are authorized.", "empty");
      emptyCell.colSpan = 5;
      empty.appendChild(emptyCell);
      table.appendChild(empty);
    }
    devices.forEach(function (device) {
      var status = securityState(device);
      var row = document.createElement("tr");
      row.append(
        text("td", device.device_name),
        text("td", dateTime(device.created_at)),
        text("td", dateTime(device.last_checkin_at))
      );
      // Security status carries its reason and action time inline: the table is
      // five columns wide, not seven.
      var statusCell = document.createElement("td");
      statusCell.appendChild(text("span", securityStateLabel(status), "status-" + status));
      var actionAt = device.action_at || device.revoked_at;
      var detail = [];
      if (Array.isArray(device.reason_codes) && device.reason_codes.length) detail.push(reasonLabels(device.reason_codes));
      if (actionAt) detail.push(dateTime(actionAt));
      if (detail.length) statusCell.appendChild(text("small", detail.join(" · ")));
      row.appendChild(statusCell);
      var action = document.createElement("td");
      if (!device.revoked_at) {
        var button = text("button", "Revoke", "danger small");
        button.dataset.subject = device.subject;
        action.appendChild(button);
      }
      if (device.revoked_at && !appeals.some(function (appeal) { return appeal.subject === device.subject && appeal.state === "pending"; })) {
        var option = document.createElement("option");
        option.value = device.subject;
        option.textContent = device.device_name;
        select.appendChild(option);
      }
      row.appendChild(action);
      table.appendChild(row);
    });
    renderAppeals(appeals);
    // The first option is the "Select a revoked device" placeholder, so one
    // option means nothing is eligible.
    renderAppealForm(select.options.length > 1);
    state.loaded.devices = true;
  }

  function renderAppealForm(eligible) {
    var form = el("appeal-form");
    var placeholder = el("appeal-empty");
    form.hidden = !eligible;
    el("appeal-submit").disabled = !eligible;
    if (eligible) {
      if (placeholder) placeholder.remove();
      return;
    }
    // A form whose only outcome is an error is not a form: the card says what
    // has to happen first instead, matching the neighbouring appeals card.
    if (!placeholder) {
      placeholder = text("p", "No devices are eligible for review. Appeals appear here after a device is revoked.", "empty");
      placeholder.id = "appeal-empty";
      form.parentNode.insertBefore(placeholder, form.nextSibling);
    }
  }

  function renderAppeals(appeals) {
    var host = el("appeal-list"); clear(host);
    if (!appeals.length) { host.appendChild(text("p", "No appeals submitted. A review you request for a revoked device appears here.", "empty")); return; }
    appeals.forEach(function (appeal) {
      var item = text("article", "", "appeal-item");
      var head = text("div", "", "appeal-item-head");
      head.append(text("strong", appeal.device_name || "Previous device"), text("span", securityStateLabel(appeal.state), "status-" + appeal.state));
      item.append(head, text("p", reasonLabels(appeal.reason_codes)), text("small", "Action " + dateTime(appeal.action_at) + " · Submitted " + dateTime(appeal.created_at)));
      if (appeal.resolved_at) item.appendChild(text("small", "Decision recorded " + dateTime(appeal.resolved_at)));
      if (appeal.decision_note) item.appendChild(text("p", appeal.decision_note, "appeal-note"));
      host.appendChild(item);
    });
  }

  // ---- Team & files: organizations, teams, shared buckets ----

  var ORG_ROLES = ["member", "admin"];
  var TEAM_ROLES = ["member", "manager"];

  function orgPath(orgId) { return "/api/orgs/" + encodeURIComponent(orgId); }
  function teamPath(orgId, teamId) { return orgPath(orgId) + "/teams/" + encodeURIComponent(teamId); }
  function canManageOrg(org) { return org.role === "owner" || org.role === "admin"; }
  function canManageTeam(org, team) { return canManageOrg(org) || team.role === "manager"; }
  function orgCreationAllowed() {
    return Boolean(state.account)
      && state.account.status === "active"
      && (state.account.plan === "studio" || state.account.plan === "enterprise");
  }

  function roleChip(role) {
    return text("span", titleCase(role), "role-chip role-" + String(role || "member"));
  }

  function roleSelect(current, roles, label) {
    var select = document.createElement("select");
    select.className = "role-select";
    roles.forEach(function (role) {
      var option = document.createElement("option");
      option.value = role;
      option.textContent = titleCase(role);
      select.appendChild(option);
    });
    select.value = current;
    select.setAttribute("aria-label", label);
    return select;
  }

  function sectionTitle(label) { return text("h4", label); }

  function confirmedOrgCall(message, request, done) {
    if (!window.confirm(message)) return Promise.resolve();
    return request().then(function () { return refreshTeam(done); });
  }

  async function refreshTeam(message) {
    state.loaded.team = false;
    await loadTeam(true);
    if (message) showMessage(message, false);
  }

  function renderTeamGate() {
    var allowed = orgCreationAllowed();
    el("team-upsell").hidden = allowed;
    el("org-create-card").hidden = !allowed;
  }

  function clearInviteParam() {
    state.pendingInviteToken = null;
    history.replaceState(null, "", window.location.pathname + "#team");
  }

  async function acceptInvitation(body, statusNode) {
    statusNode.textContent = "Joining…";
    var result = await api("/api/orgs/invitations/accept", { method: "POST", body: JSON.stringify(body) });
    var org = result.org || {};
    var team = result.team || {};
    await refreshTeam("Joined " + (org.name || "the organization") + ". Shared files live in its " + (team.name || "General") + " team.");
  }

  function renderInviteBanner() {
    var host = el("org-invite-banner");
    clear(host);
    if (!state.pendingInviteToken) { host.hidden = true; return; }
    host.hidden = false;
    host.appendChild(text("strong", "You have an invitation"));
    host.appendChild(text("p", "Accept it to join the organization. You will be added to its General team right away, with access to the team's shared files.", "fine"));
    var status = text("p", "", "fine");
    var accept = text("button", "Accept invitation", "primary small");
    accept.type = "button";
    var dismiss = text("button", "Not now", "secondary small");
    dismiss.type = "button";
    var row = text("div", "", "button-row");
    row.append(accept, dismiss);
    host.append(row, status);
    accept.addEventListener("click", async function () {
      accept.disabled = true;
      var token = state.pendingInviteToken;
      try {
        clearInviteParam();
        await acceptInvitation({ token: token }, status);
      } catch (error) {
        accept.disabled = false;
        state.pendingInviteToken = token;
        status.textContent = error.message;
      }
    });
    dismiss.addEventListener("click", function () {
      clearInviteParam();
      host.hidden = true;
    });
  }

  function renderMyInvitations(invitations) {
    var card = el("org-invitations-card");
    var host = el("org-invitations");
    clear(host);
    card.hidden = !invitations.length;
    invitations.forEach(function (invite) {
      var row = text("div", "", "invite-row");
      var line = text("div", "", "member-line");
      var info = document.createElement("div");
      info.appendChild(text("strong", invite.org_name || "Organization"));
      var meta = "Invited by " + (invite.invited_by || "an organization admin") + " · Joins as " + titleCase(invite.role);
      if (invite.expires_at) meta += " · Expires " + date(invite.expires_at);
      info.appendChild(text("small", meta, "member-meta"));
      var accept = text("button", "Accept", "primary small");
      accept.type = "button";
      var status = text("p", "", "fine");
      line.append(info, accept);
      row.append(line, status);
      accept.addEventListener("click", async function () {
        accept.disabled = true;
        try {
          await acceptInvitation({ invitation_id: invite.id }, status);
        } catch (error) {
          accept.disabled = false;
          status.textContent = error.message;
        }
      });
      host.appendChild(row);
    });
  }

  function inlineForm(fields, submitLabel, onSubmit) {
    var form = document.createElement("form");
    form.className = "inline-form";
    fields.forEach(function (field) {
      var label = text("label", field.label + " ");
      label.appendChild(field.control);
      form.appendChild(label);
    });
    var submit = text("button", submitLabel, "secondary small");
    submit.type = "submit";
    var status = text("p", "", "fine");
    status.setAttribute("role", "status");
    form.append(submit, status);
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      submit.disabled = true;
      status.textContent = "Working…";
      try {
        await onSubmit(status);
      } catch (error) {
        submit.disabled = false;
        status.textContent = error.message;
      }
    });
    return form;
  }

  function emailInput(placeholder) {
    var input = document.createElement("input");
    input.type = "email";
    input.required = true;
    input.autocomplete = "off";
    input.placeholder = placeholder;
    return input;
  }

  function orgHead(org) {
    var head = text("div", "", "org-head");
    var info = document.createElement("div");
    info.appendChild(text("p", "Organization", "eyebrow"));
    var title = text("h3", org.name + " ");
    title.appendChild(roleChip(org.role));
    info.appendChild(title);
    info.appendChild(text("p", org.member_count + (org.member_count === 1 ? " member" : " members") + " · Owned by " + org.owner_email, "org-meta"));
    head.appendChild(info);
    if (canManageOrg(org)) {
      var actions = text("div", "", "button-row");
      var rename = text("button", "Rename", "secondary small");
      rename.type = "button";
      rename.addEventListener("click", async function () {
        var name = window.prompt("Organization name", org.name);
        if (!name || !name.trim() || name.trim() === org.name) return;
        try {
          await api(orgPath(org.id), { method: "PATCH", body: JSON.stringify({ name: name.trim() }) });
          await refreshTeam("Organization renamed.");
        } catch (error) { showMessage(error.message, true); }
      });
      actions.appendChild(rename);
      if (org.role === "owner") {
        var remove = text("button", "Delete organization", "danger small");
        remove.type = "button";
        remove.addEventListener("click", function () {
          confirmedOrgCall(
            "Delete " + org.name + "? Every team and its shared files become inaccessible to all members.",
            function () { return api(orgPath(org.id), { method: "DELETE" }); },
            "Organization deleted."
          ).catch(function (error) { showMessage(error.message, true); });
        });
        actions.appendChild(remove);
      }
      head.appendChild(actions);
    }
    return head;
  }

  async function openTeamPanel(org, team, panel, toggle) {
    state.openTeams[org.id + "/" + team.id] = true;
    toggle.setAttribute("aria-expanded", "true");
    toggle.textContent = "Hide members";
    panel.hidden = false;
    clear(panel);
    panel.appendChild(text("p", "Loading members…", "fine"));
    var detail;
    try {
      detail = await api(teamPath(org.id, team.id));
    } catch (error) {
      clear(panel);
      panel.appendChild(text("p", error.message, "fine"));
      return;
    }
    clear(panel);
    var full = detail.team || team;
    var members = detail.members || [];
    var manage = canManageTeam(org, team);
    if (!members.length) panel.appendChild(text("p", "No members in this team yet.", "fine"));
    members.forEach(function (member) {
      var row = text("div", "", "member-row");
      var line = text("div", "", "member-line");
      var info = document.createElement("div");
      info.appendChild(text("strong", member.display || member.email));
      info.appendChild(text("small", member.email + " · Added " + date(member.added_at), "member-meta"));
      line.appendChild(info);
      var controls = text("div", "", "button-row");
      if (manage) {
        var select = roleSelect(member.role, TEAM_ROLES, "Team role for " + member.email);
        select.addEventListener("change", async function () {
          try {
            await api(teamPath(org.id, team.id) + "/members/" + encodeURIComponent(member.email), { method: "PATCH", body: JSON.stringify({ role: select.value }) });
            await refreshTeam(member.email + " is now a team " + select.value + ".");
          } catch (error) {
            select.value = member.role;
            showMessage(error.message, true);
          }
        });
        controls.appendChild(select);
        var remove = text("button", "Remove", "danger small");
        remove.type = "button";
        remove.addEventListener("click", function () {
          confirmedOrgCall(
            "Remove " + member.email + " from " + team.name + "? They immediately lose access to the team's shared files.",
            function () { return api(teamPath(org.id, team.id) + "/members/" + encodeURIComponent(member.email), { method: "DELETE" }); },
            member.email + " removed from " + team.name + "."
          ).catch(function (error) { showMessage(error.message, true); });
        });
        controls.appendChild(remove);
      } else {
        controls.appendChild(roleChip(member.role));
      }
      line.appendChild(controls);
      row.appendChild(line);
      panel.appendChild(row);
    });
    if (manage) {
      var email = emailInput("teammate@company.com");
      var role = roleSelect("member", TEAM_ROLES, "Role for the new team member");
      panel.appendChild(inlineForm(
        [{ label: "Add an organization member to this team", control: email }, { label: "Team role", control: role }],
        "Add to team",
        async function () {
          await api(teamPath(org.id, team.id) + "/members", { method: "POST", body: JSON.stringify({ email: email.value.trim(), role: role.value }) });
          await refreshTeam(email.value.trim() + " added to " + team.name + ".");
        }
      ));
    }
    if (canManageOrg(org)) {
      var adminRow = text("div", "", "button-row");
      var renameTeam = text("button", "Rename team", "secondary small");
      renameTeam.type = "button";
      renameTeam.addEventListener("click", async function () {
        var name = window.prompt("Team name", full.name);
        if (!name || !name.trim() || name.trim() === full.name) return;
        try {
          await api(teamPath(org.id, team.id), { method: "PATCH", body: JSON.stringify({ name: name.trim() }) });
          await refreshTeam("Team renamed.");
        } catch (error) { showMessage(error.message, true); }
      });
      adminRow.appendChild(renameTeam);
      if (!full.is_general) {
        var removeTeam = text("button", "Delete team", "danger small");
        removeTeam.type = "button";
        removeTeam.addEventListener("click", function () {
          confirmedOrgCall(
            "Delete the " + team.name + " team? Its shared bucket becomes inaccessible to all members.",
            function () { return api(teamPath(org.id, team.id), { method: "DELETE" }); },
            "Team deleted."
          ).catch(function (error) { showMessage(error.message, true); });
        });
        adminRow.appendChild(removeTeam);
      }
      panel.appendChild(adminRow);
    }
  }

  function closeTeamPanel(org, team, panel, toggle) {
    delete state.openTeams[org.id + "/" + team.id];
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = "Members";
    panel.hidden = true;
    clear(panel);
  }

  function teamRow(org, team) {
    var row = text("div", "", "team-row");
    var head = text("div", "", "team-row-head");
    var info = document.createElement("div");
    var name = text("strong", team.name + " ");
    if (team.role) name.appendChild(roleChip(team.role));
    info.appendChild(name);
    info.appendChild(text("small", team.member_count + (team.member_count === 1 ? " member" : " members"), "team-meta"));
    head.appendChild(info);
    var actions = text("div", "", "button-row");
    var open = text("a", "Open files", "secondary small");
    open.href = team.bucket_url;
    actions.appendChild(open);
    var panel = text("div", "", "team-panel");
    panel.hidden = true;
    if (team.role || canManageOrg(org)) {
      var toggle = text("button", "Members", "secondary small");
      toggle.type = "button";
      toggle.setAttribute("aria-expanded", "false");
      toggle.addEventListener("click", function () {
        if (panel.hidden) openTeamPanel(org, team, panel, toggle);
        else closeTeamPanel(org, team, panel, toggle);
      });
      actions.appendChild(toggle);
      if (state.openTeams[org.id + "/" + team.id]) openTeamPanel(org, team, panel, toggle);
    }
    head.appendChild(actions);
    row.append(head, panel);
    return row;
  }

  function orgTeamsSection(org, detail) {
    var section = text("section", "", "org-section");
    section.appendChild(sectionTitle("Teams"));
    var list = text("div", "", "team-list");
    var teams = detail.teams || [];
    if (!teams.length) list.appendChild(text("p", "No teams yet.", "fine"));
    teams.forEach(function (team) { list.appendChild(teamRow(org, team)); });
    section.appendChild(list);
    if (canManageOrg(org)) {
      var name = document.createElement("input");
      name.type = "text";
      name.required = true;
      name.maxLength = 120;
      name.placeholder = "e.g. Drivetrain";
      section.appendChild(inlineForm(
        [{ label: "New team", control: name }],
        "Create team",
        async function () {
          await api(orgPath(org.id) + "/teams", { method: "POST", body: JSON.stringify({ name: name.value.trim() }) });
          await refreshTeam("Team created with its own shared bucket.");
        }
      ));
    }
    return section;
  }

  function orgMembersSection(org, detail) {
    var section = text("section", "", "org-section");
    section.appendChild(sectionTitle("Members"));
    var list = text("div", "", "member-list");
    (detail.members || []).forEach(function (member) {
      var row = text("div", "", "member-row");
      var line = text("div", "", "member-line");
      var info = document.createElement("div");
      info.appendChild(text("strong", member.display || member.email));
      info.appendChild(text("small", member.email + " · Joined " + date(member.joined_at), "member-meta"));
      line.appendChild(info);
      var controls = text("div", "", "button-row");
      if (member.role === "owner" || !canManageOrg(org)) {
        controls.appendChild(roleChip(member.role));
      } else {
        var select = roleSelect(member.role, ORG_ROLES, "Organization role for " + member.email);
        select.addEventListener("change", async function () {
          try {
            await api(orgPath(org.id) + "/members/" + encodeURIComponent(member.email), { method: "PATCH", body: JSON.stringify({ role: select.value }) });
            await refreshTeam(member.email + " is now an organization " + select.value + ".");
          } catch (error) {
            select.value = member.role;
            showMessage(error.message, true);
          }
        });
        controls.appendChild(select);
        if (org.role === "owner") {
          var transfer = text("button", "Make owner", "secondary small");
          transfer.type = "button";
          transfer.addEventListener("click", function () {
            confirmedOrgCall(
              "Transfer ownership of " + org.name + " to " + member.email + "? You become an admin and the shared quota follows the new owner's plan.",
              function () { return api(orgPath(org.id) + "/transfer-ownership", { method: "POST", body: JSON.stringify({ email: member.email }) }); },
              member.email + " now owns " + org.name + "."
            ).catch(function (error) { showMessage(error.message, true); });
          });
          controls.appendChild(transfer);
        }
        var remove = text("button", "Remove", "danger small");
        remove.type = "button";
        remove.addEventListener("click", function () {
          confirmedOrgCall(
            "Remove " + member.email + " from " + org.name + "? They immediately lose access to every team bucket.",
            function () { return api(orgPath(org.id) + "/members/" + encodeURIComponent(member.email), { method: "DELETE" }); },
            member.email + " removed from " + org.name + "."
          ).catch(function (error) { showMessage(error.message, true); });
        });
        controls.appendChild(remove);
      }
      line.appendChild(controls);
      row.appendChild(line);
      list.appendChild(row);
    });
    section.appendChild(list);
    return section;
  }

  function orgInvitesSection(org, detail) {
    var section = text("section", "", "org-section");
    var invitations = detail.invitations || [];
    if (!invitations.length && !canManageOrg(org)) return section;
    section.appendChild(sectionTitle("Invitations"));
    var list = text("div", "", "invite-list");
    if (!invitations.length) list.appendChild(text("p", "No pending invitations.", "fine"));
    invitations.forEach(function (invite) {
      var row = text("div", "", "invite-row");
      var line = text("div", "", "member-line");
      var info = document.createElement("div");
      info.appendChild(text("strong", invite.email || "Pending invitation"));
      var meta = "Joins as " + titleCase(invite.role) + " · Sent " + date(invite.created_at);
      if (invite.expires_at) meta += " · Expires " + date(invite.expires_at);
      info.appendChild(text("small", meta, "member-meta"));
      line.appendChild(info);
      if (canManageOrg(org)) {
        var controls = text("div", "", "button-row");
        var revoke = text("button", "Revoke", "danger small");
        revoke.type = "button";
        revoke.addEventListener("click", function () {
          confirmedOrgCall(
            "Revoke this invitation? Its email link stops working immediately.",
            function () { return api(orgPath(org.id) + "/invitations/" + encodeURIComponent(invite.id), { method: "DELETE" }); },
            "Invitation revoked."
          ).catch(function (error) { showMessage(error.message, true); });
        });
        controls.appendChild(revoke);
        line.appendChild(controls);
      }
      row.appendChild(line);
      list.appendChild(row);
    });
    section.appendChild(list);
    if (canManageOrg(org)) {
      var email = emailInput("teammate@company.com");
      var role = roleSelect("member", ORG_ROLES, "Organization role for the invitee");
      section.appendChild(inlineForm(
        [{ label: "Invite by email", control: email }, { label: "Organization role", control: role }],
        "Send invitation",
        async function () {
          await api(orgPath(org.id) + "/invitations", { method: "POST", body: JSON.stringify({ email: email.value.trim(), role: role.value }) });
          await refreshTeam("Invitation sent to " + email.value.trim() + ". They join the General team when they accept the emailed link.");
        }
      ));
    }
    return section;
  }

  async function renderOrgs(orgs) {
    var host = el("org-list");
    clear(host);
    if (!orgs.length) {
      host.appendChild(text(
        "p",
        orgCreationAllowed()
          ? "You are not part of an organization yet. Create one above to share files with a team."
          : "You are not part of an organization yet. Accept an invitation to join one, or choose Studio to create your own.",
        "empty"
      ));
      return;
    }
    var mounts = orgs.map(function (org) {
      var card = text("article", "", "card org-card");
      card.appendChild(orgHead(org));
      var body = text("div", "", "org-body");
      body.appendChild(text("p", "Loading organization…", "empty"));
      card.appendChild(body);
      host.appendChild(card);
      return { org: org, body: body };
    });
    await Promise.all(mounts.map(async function (mount) {
      try {
        var detail = await api(orgPath(mount.org.id));
        clear(mount.body);
        mount.body.append(
          orgTeamsSection(mount.org, detail),
          orgMembersSection(mount.org, detail),
          orgInvitesSection(mount.org, detail)
        );
      } catch (error) {
        clear(mount.body);
        mount.body.appendChild(text("p", error.message, "empty"));
      }
    }));
  }

  async function loadTeam(force) {
    if (state.loaded.team && !force) return;
    renderTeamGate();
    renderInviteBanner();
    var payload = await api("/api/orgs");
    state.orgs = payload.organizations || [];
    renderMyInvitations(payload.invitations || []);
    await renderOrgs(state.orgs);
    state.loaded.team = true;
  }

  async function loadPage(name) {
    if (!state.account) await loadAccount();
    if (name === "team") await loadTeam(false);
    if (name === "billing") await loadBilling();
    if (name === "usage") await loadUsage();
    if (name === "ai") await loadAI();
    if (name === "downloads") await loadDownloads(false);
    if (name === "devices") await loadDevices(false);
  }

  qa(".nav-item").forEach(function (button) { button.addEventListener("click", function () { setPage(button.dataset.page, true); }); });
  qa("[data-go]").forEach(function (button) { button.addEventListener("click", function () { setPage(button.dataset.go, true); }); });
  el("menu-button").addEventListener("click", function () { var open = document.body.classList.toggle("menu-open"); this.setAttribute("aria-expanded", String(open)); });
  el("open-cloud").addEventListener("click", function (event) { if (state.account && !state.account.cloud) { event.preventDefault(); setPage("billing", true); showMessage("The cloud workspace is included with Studio. Choose Studio to continue.", false); } });
  el("hero-cloud").addEventListener("click", function (event) { if (state.account && !state.account.cloud) { event.preventDefault(); setPage("billing", true); showMessage("Upgrade to Studio to open Chudware in the browser.", false); } });
  el("dest-cloud").addEventListener("click", function (event) { if (state.account && !state.account.cloud) { event.preventDefault(); setPage("billing", true); showMessage("The cloud workspace is included with Studio. Choose Studio to continue.", false); } });
  el("org-create-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var status = el("org-create-status");
    var input = el("org-create-name");
    status.textContent = "Creating organization…";
    try {
      await api("/api/orgs", { method: "POST", body: JSON.stringify({ name: input.value.trim() }) });
      this.reset();
      await refreshTeam("Organization created. Invite teammates below — they join its General team automatically.");
    } catch (error) {
      status.textContent = error.message;
    }
  });
  el("password-form").addEventListener("submit", async function (event) { event.preventDefault(); var status = el("password-status"); status.textContent = "Updating…"; try { await api("/api/portal/password", { method: "POST", body: JSON.stringify({ current_password: el("current-password").value, new_password: el("new-password").value }) }); this.reset(); status.textContent = "Password updated."; } catch (error) { status.textContent = error.message; } });
  el("signout-all").addEventListener("click", async function () { if (!window.confirm("Sign out every session, including this browser?")) return; try { await api("/api/portal/signout-everywhere", { method: "POST" }); window.location.assign("/"); } catch (error) { showMessage(error.message, true); } });
  el("billing-portal").addEventListener("click", function () { openBillingPortal().catch(function (error) { showMessage(error.message, true); }); });
  qa(".plan-action").forEach(function (button) { button.addEventListener("click", function () { choosePlan(button.dataset.plan).catch(function (error) { showMessage(error.message, true); }); }); });
  el("byok-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var status = el("byok-status");
    var input = el("byok-key");
    status.textContent = "Validating with the provider…";
    try {
      await api("/api/providers/keys", {
        method: "POST",
        body: JSON.stringify({ provider: el("byok-provider").value, key: input.value })
      });
      status.textContent = "Validated and saved. The key will not be shown again.";
      state.loaded.ai = false;
      await loadAI(true);
    } catch (error) {
      status.textContent = error.message;
    } finally {
      input.value = "";
    }
  });
  el("byok-list").addEventListener("click", async function (event) {
    var button = event.target.closest("button[data-byok-provider]");
    if (!button || !window.confirm("Remove this provider key?")) return;
    button.disabled = true;
    try {
      await api("/api/providers/keys/" + encodeURIComponent(button.dataset.byokProvider), { method: "DELETE" });
      state.loaded.ai = false;
      await loadAI(true);
      showMessage("Provider key removed.", false);
    } catch (error) {
      button.disabled = false;
      showMessage(error.message, true);
    }
  });
  qa("[data-suboauth-begin]").forEach(function (button) { button.addEventListener("click", function () { beginSubscriptionFlow(button.dataset.suboauthBegin, button).catch(function (error) { showMessage(error.message, true); }); }); });
  el("suboauth-list").addEventListener("click", async function (event) {
    var button = event.target.closest("button[data-suboauth-provider]");
    if (!button || !window.confirm("Disconnect this subscription? Its turns will stop running on your own account.")) return;
    button.disabled = true;
    try {
      await api("/api/providers/accounts/" + encodeURIComponent(button.dataset.suboauthProvider) + "/" + encodeURIComponent(button.dataset.suboauthAccount), { method: "DELETE" });
      state.loaded.ai = false;
      await loadAI(true);
      showMessage("Subscription disconnected.", false);
    } catch (error) {
      button.disabled = false;
      showMessage(error.message, true);
    }
  });
  el("release-channel").addEventListener("change", function () {
    // The chosen format is kept across channels when the other channel
    // publishes it; chooseTarget falls back when it does not.
    loadDownloads(true).catch(function (error) { showMessage(error.message, true); });
  });
  el("primary-formats").addEventListener("click", function (event) {
    var chip = event.target.closest("button[data-format]");
    if (chip) selectFormat(chip.dataset.format);
  });
  el("primary-action").addEventListener("click", function () {
    var target = state.download.target;
    if (!target) return;
    startDownload(target.platform, target.arch, target.format, this)
      .catch(function (error) { showMessage(error.message, true); });
  });
  el("download-list").addEventListener("click", function (event) {
    var button = event.target.closest("button[data-format]");
    if (!button) return;
    startDownload(button.dataset.platform, button.dataset.arch, button.dataset.format, button)
      .catch(function (error) { showMessage(error.message, true); });
  });
  el("install-copy").addEventListener("click", function () {
    copyInstallCommand().catch(function (error) { showMessage(error.message, true); });
  });
  el("device-rows").addEventListener("click", async function (event) { var button = event.target.closest("button[data-subject]"); if (!button || !window.confirm("Revoke this desktop device?")) return; button.disabled = true; try { await api("/api/portal/devices/" + encodeURIComponent(button.dataset.subject), { method: "DELETE" }); await loadDevices(true); showMessage("Device revoked. Its next refresh will be refused.", false); } catch (error) { button.disabled = false; showMessage(error.message, true); } });
  el("appeal-message").addEventListener("input", function () { el("appeal-count").textContent = this.value.length + " / 4096"; });
  el("appeal-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var status = el("appeal-status"); var submit = el("appeal-submit");
    submit.disabled = true; status.textContent = "Submitting appeal…";
    try {
      await api("/api/portal/security-appeals", { method: "POST", body: JSON.stringify({ subject: el("appeal-subject").value, message: el("appeal-message").value }) });
      this.reset(); el("appeal-count").textContent = "0 / 4096"; state.loaded.devices = false; await loadDevices(true);
      status.textContent = "Appeal submitted. You can continue to open, view, and export documents while it is reviewed.";
    } catch (error) { status.textContent = error.message; submit.disabled = false; }
  });
  window.addEventListener("hashchange", function () { setPage(window.location.hash.slice(1), false); });

  var bootQuery = new URLSearchParams(window.location.search);
  if (bootQuery.get("org_invite")) state.pendingInviteToken = bootQuery.get("org_invite");
  setPage(window.location.hash.slice(1) || (state.pendingInviteToken ? "team" : "overview"), false);
}());
