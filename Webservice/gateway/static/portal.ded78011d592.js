"use strict";

(function () {
  var state = { account: null, loaded: {}, releases: new Map(), subProviders: new Map(), subFlows: {} };
  var pages = ["account", "billing", "usage", "ai", "downloads", "devices"];

  function el(id) { return document.getElementById(id); }
  function q(selector, root) { return (root || document).querySelector(selector); }
  function qa(selector, root) { return Array.from((root || document).querySelectorAll(selector)); }
  function titleCase(value) {
    value = String(value || "");
    return value ? value.charAt(0).toUpperCase() + value.slice(1) : "—";
  }
  function number(value) { return new Intl.NumberFormat().format(Number(value || 0)); }
  function date(value) {
    if (!value) return "—";
    var parsed = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" })
      .format(parsed);
  }
  function dateTime(value) {
    if (!value) return "Never";
    var parsed = typeof value === "number" ? new Date(value * 1000) : new Date(value);
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
    if (pages.indexOf(name) < 0) name = "account";
    qa(".nav-item").forEach(function (button) {
      button.classList.toggle("active", button.dataset.page === name);
    });
    qa(".page").forEach(function (page) {
      page.classList.toggle("active", page.dataset.view === name);
    });
    if (name !== "ai") { stopSubscriptionFlows(); clear(el("suboauth-flow")); }
    el("page-title").textContent = titleCase(name === "ai" ? "AI setup" : name);
    document.title = "Chudware · " + titleCase(name === "ai" ? "AI setup" : name);
    document.body.classList.remove("menu-open");
    el("menu-button").setAttribute("aria-expanded", "false");
    if (updateHash && window.location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    loadPage(name).catch(function (error) { showMessage(error.message, true); });
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  function applyCloudGate() {
    var allowed = Boolean(state.account && state.account.cloud);
    [el("open-cloud"), el("hero-cloud")].forEach(function (link) {
      if (!link) return;
      link.href = allowed ? "/app/" : "#billing";
      link.classList.toggle("disabled", !allowed);
      if (link.id === "hero-cloud") link.textContent = allowed ? "Open cloud workspace" : "Upgrade to Studio";
      link.setAttribute("aria-disabled", allowed ? "false" : "true");
    });
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
    el("account-summary").textContent = account.plan === "trial"
      ? "Your trial includes the browser workspace and native desktop apps."
      : account.cloud
        ? "Your " + titleCase(account.plan) + " plan includes the browser workspace and native desktop apps."
        : "Your Desktop plan keeps native CAD active. Upgrade to Studio for the browser workspace.";
    el("admin-link").hidden = account.role !== "admin";
    el("password-form").hidden = !account.has_password;
    if (!account.has_password) el("password-status").textContent = "This account signs in through a linked provider and has no password.";
    el("credits-option").hidden = !account.ai_grant;
    var identities = el("identity-list");
    clear(identities);
    if (account.has_password) {
      var password = text("div", "", "identity");
      password.append(text("i", "P"), text("span", "Password"), text("b", "Active"));
      identities.appendChild(password);
    }
    (account.linked_identities || []).forEach(function (provider) {
      var row = text("div", "", "identity");
      row.append(text("i", provider.charAt(0).toUpperCase()), text("span", provider), text("b", "Linked"));
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

  async function buyCredits(pack) {
    var result = await api("/api/billing/credits/checkout", { method: "POST", body: JSON.stringify({ pack: pack }) });
    window.location.assign(result.url);
  }

  async function loadBilling() {
    if (state.loaded.billing) return;
    var table = el("invoice-rows");
    clear(table);
    if (!state.account.billing_configured) {
      var unavailable = document.createElement("tr");
      var cell = text("td", "Billing is not configured on this deployment.", "empty");
      cell.colSpan = 5;
      unavailable.appendChild(cell);
      table.appendChild(unavailable);
      el("billing-portal").disabled = true;
      qa(".plan-action,.pack-action").forEach(function (button) { button.disabled = true; });
      state.loaded.billing = true;
      return;
    }
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

  async function loadDownloads(force) {
    if (state.loaded.downloads && !force) return;
    var channel = el("release-channel").value;
    var cards = qa(".download-card");
    await Promise.all(cards.map(async function (card) {
      var meta = q(".release-meta", card); var button = q(".download-action", card);
      meta.textContent = "Checking latest release…"; button.disabled = true;
      try {
        var query = new URLSearchParams({ channel: channel, platform: card.dataset.platform, arch: card.dataset.arch });
        var release = await api("/api/releases/latest?" + query.toString());
        state.releases.set(card.dataset.platform + ":" + card.dataset.arch, release);
        meta.textContent = "Version " + release.version + " · " + number(release.size) + " bytes";
        button.disabled = !release.url;
        button.textContent = release.url ? "Download" : "Sign in to download";
      } catch (error) {
        meta.textContent = error.message;
        button.textContent = "Unavailable";
      }
    }));
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
    if (!devices.length) { var row = document.createElement("tr"); var cell = text("td", "No desktop devices are authorized.", "empty"); cell.colSpan = 7; row.appendChild(cell); table.appendChild(row); }
    devices.forEach(function (device) {
      var status = securityState(device);
      var row = document.createElement("tr");
      row.append(text("td", device.device_name), text("td", dateTime(device.created_at)), text("td", dateTime(device.last_checkin_at)), text("td", securityStateLabel(status), "status-" + status), text("td", reasonLabels(device.reason_codes)), text("td", dateTime(device.action_at || device.revoked_at)));
      var action = document.createElement("td");
      if (!device.revoked_at) { var button = text("button", "Revoke", "danger small"); button.dataset.subject = device.subject; action.appendChild(button); }
      if (device.revoked_at && !appeals.some(function (appeal) { return appeal.subject === device.subject && appeal.state === "pending"; })) {
        var option = document.createElement("option"); option.value = device.subject; option.textContent = device.device_name; select.appendChild(option);
      }
      row.appendChild(action); table.appendChild(row);
    });
    renderAppeals(appeals);
    el("appeal-submit").disabled = select.options.length === 1;
    state.loaded.devices = true;
  }

  function renderAppeals(appeals) {
    var host = el("appeal-list"); clear(host);
    if (!appeals.length) { host.appendChild(text("p", "No appeals submitted. Revoked devices eligible for review appear in the form.", "empty")); return; }
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

  async function loadPage(name) {
    if (!state.account) await loadAccount();
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
  el("password-form").addEventListener("submit", async function (event) { event.preventDefault(); var status = el("password-status"); status.textContent = "Updating…"; try { await api("/api/portal/password", { method: "POST", body: JSON.stringify({ current_password: el("current-password").value, new_password: el("new-password").value }) }); this.reset(); status.textContent = "Password updated."; } catch (error) { status.textContent = error.message; } });
  el("signout-all").addEventListener("click", async function () { if (!window.confirm("Sign out every session, including this browser?")) return; try { await api("/api/portal/signout-everywhere", { method: "POST" }); window.location.assign("/"); } catch (error) { showMessage(error.message, true); } });
  el("billing-portal").addEventListener("click", function () { openBillingPortal().catch(function (error) { showMessage(error.message, true); }); });
  qa(".plan-action").forEach(function (button) { button.addEventListener("click", function () { choosePlan(button.dataset.plan).catch(function (error) { showMessage(error.message, true); }); }); });
  qa(".pack-action").forEach(function (button) { button.addEventListener("click", function () { buyCredits(button.dataset.pack).catch(function (error) { showMessage(error.message, true); }); }); });
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
  el("release-channel").addEventListener("change", function () { loadDownloads(true).catch(function (error) { showMessage(error.message, true); }); });
  qa(".download-action").forEach(function (button) { button.addEventListener("click", function () { var card = button.closest(".download-card"); var release = state.releases.get(card.dataset.platform + ":" + card.dataset.arch); if (release && release.url) window.location.assign(release.url); }); });
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

  setPage(window.location.hash.slice(1) || "account", false);
}());
