"use strict";

(function () {
  var state = { account: null, loaded: {}, releases: new Map() };
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
    return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" })
      .format(new Date(Number(value) * 1000));
  }
  function dateTime(value) {
    if (!value) return "Never";
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" })
      .format(new Date(Number(value) * 1000));
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

  async function loadAI(force) {
    if (state.loaded.ai && !force) return;
    await loadUsage();
    var payload = await api("/api/providers/keys");
    renderByok(payload.keys || []);
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
    var payload = await api("/api/portal/devices");
    var table = el("device-rows"); clear(table);
    var devices = payload.devices || [];
    if (!devices.length) { var row = document.createElement("tr"); var cell = text("td", "No desktop devices are authorized.", "empty"); cell.colSpan = 5; row.appendChild(cell); table.appendChild(row); }
    devices.forEach(function (device) {
      var row = document.createElement("tr");
      row.append(text("td", device.device_name), text("td", dateTime(device.created_at)), text("td", dateTime(device.last_checkin_at)), text("td", device.revoked_at ? "Revoked" : "Active", device.revoked_at ? "status-revoked" : "status-active"));
      var action = document.createElement("td");
      if (!device.revoked_at) { var button = text("button", "Revoke", "danger small"); button.dataset.subject = device.subject; action.appendChild(button); }
      row.appendChild(action); table.appendChild(row);
    });
    state.loaded.devices = true;
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
  el("release-channel").addEventListener("change", function () { loadDownloads(true).catch(function (error) { showMessage(error.message, true); }); });
  qa(".download-action").forEach(function (button) { button.addEventListener("click", function () { var card = button.closest(".download-card"); var release = state.releases.get(card.dataset.platform + ":" + card.dataset.arch); if (release && release.url) window.location.assign(release.url); }); });
  el("device-rows").addEventListener("click", async function (event) { var button = event.target.closest("button[data-subject]"); if (!button || !window.confirm("Revoke this desktop device?")) return; button.disabled = true; try { await api("/api/portal/devices/" + encodeURIComponent(button.dataset.subject), { method: "DELETE" }); await loadDevices(true); showMessage("Device revoked. Its next refresh will be refused.", false); } catch (error) { button.disabled = false; showMessage(error.message, true); } });
  window.addEventListener("hashchange", function () { setPage(window.location.hash.slice(1), false); });

  setPage(window.location.hash.slice(1) || "account", false);
}());
