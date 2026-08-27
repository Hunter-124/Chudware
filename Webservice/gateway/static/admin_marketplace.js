/* chudware-web /admin — marketplace operations */
(function () {
  "use strict";
  if (!document.getElementById('marketplace-mount')) return;

  var mount = document.getElementById("marketplace-mount");
  var ROOT = "/api/admin/marketplace";
  var cursors = {};
  var activeView = "attestations";

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

  function params(values) {
    var out = [];
    Object.keys(values).forEach(function (key) {
      if (values[key] !== "" && values[key] != null) out.push(encodeURIComponent(key) + "=" + encodeURIComponent(values[key]));
    });
    return out.length ? "?" + out.join("&") : "";
  }

  function safeUrl(value) {
    value = String(value || "");
    return /^(https?:\/\/|\/)/.test(value) ? value : "";
  }

  function setStatus(message, error) {
    var el = document.getElementById("amk-status");
    el.textContent = message || "";
    el.className = error ? "amk-status is-error" : "amk-status";
  }

  function age(seconds) {
    if (seconds == null) return "No waiting items";
    seconds = Number(seconds) || 0;
    if (seconds < 3600) return Math.max(1, Math.floor(seconds / 60)) + "m old";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h old";
    return Math.floor(seconds / 86400) + "d old";
  }

  function date(value) {
    var parsed = new Date(value);
    return value && !isNaN(parsed.getTime()) ? parsed.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "—";
  }

  function num(value) { return Number(value || 0).toLocaleString(); }
  function cash(value) { return "$" + (Number(value || 0) / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function request(path, body) { return api(ROOT + path, { method: "POST", body: JSON.stringify(body) }); }
  function empty(title, detail) { return "<div class='amk-empty'><strong>" + esc(title) + "</strong><span>" + esc(detail) + "</span></div>"; }
  function loading() { return "<div class='amk-loading'><i></i><i></i><i></i></div>"; }

  function arm(button, label, callback) {
    if (button.getAttribute("data-confirm") !== "yes") {
      button.setAttribute("data-confirm", "yes");
      button.setAttribute("data-label", button.textContent);
      button.textContent = "Confirm " + label.toLowerCase();
      window.setTimeout(function () {
        if (button.getAttribute("data-confirm") === "yes") {
          button.removeAttribute("data-confirm");
          button.textContent = button.getAttribute("data-label");
        }
      }, 5000);
      return;
    }
    button.removeAttribute("data-confirm");
    button.disabled = true;
    Promise.resolve().then(callback).catch(function (error) { setStatus(String(error.message || error), true); }).finally(function () { button.disabled = false; });
  }

  mount.innerHTML =
    "<section class='amk-shell'><header class='amk-head'><div><p class='amk-kicker'>Marketplace operations</p><h2>Moderation and curation</h2></div><div id='amk-status' class='amk-status' aria-live='polite'></div></header>" +
    "<div id='amk-queues' class='amk-queues'>" + loading() + "</div>" +
    "<div class='amk-toolbar'><div class='amk-view-tabs' role='tablist'>" +
    ["attestations", "listings", "reports", "payouts", "imports", "sellers", "revenue"].map(function (view) { return "<button type='button' data-view='" + view + "'>" + view.charAt(0).toUpperCase() + view.slice(1) + "</button>"; }).join("") +
    "</div><button type='button' class='btn ghost' id='amk-refresh'>Refresh</button></div><div id='amk-workspace' class='amk-workspace'>" + loading() + "</div></section>";

  var queueCopy = {
    listings: ["Listing review", "Submitted models awaiting publication"],
    attestations: ["Human-made claims", "Process evidence awaiting review"],
    reports: ["Open reports", "Listings with unresolved complaints"],
    payouts: ["Credit payouts", "Cash-out requests awaiting approval"],
    imports: ["Pending imports", "Third-party ingestion jobs"]
  };

  async function overview() {
    var data = await api(ROOT + "/overview");
    var box = document.getElementById("amk-queues");
    box.innerHTML = Object.keys(queueCopy).map(function (key) {
      var item = data.queues[key] || {};
      return "<button type='button' class='amk-queue" + (activeView === key ? " is-active" : "") + "' data-queue='" + key + "'><span class='amk-queue-top'><strong>" + num(item.depth) + "</strong><span>" + esc(queueCopy[key][0]) + "</span></span><small>" + esc(age(item.oldest_age_seconds)) + "</small><em>" + esc(queueCopy[key][1]) + "</em></button>";
    }).join("");
    Array.prototype.forEach.call(box.querySelectorAll("[data-queue]"), function (button) {
      button.addEventListener("click", function () { show(button.getAttribute("data-queue")); });
    });
  }

  function shell(title, controls) {
    document.getElementById("amk-workspace").innerHTML = "<div class='amk-work-head'><h3>" + esc(title) + "</h3><div class='amk-controls'>" + (controls || "") + "</div></div><div id='amk-content'>" + loading() + "</div>";
  }

  function activate(view) {
    activeView = view;
    Array.prototype.forEach.call(document.querySelectorAll("[data-view],[data-queue]"), function (button) {
      button.classList.toggle("is-active", (button.getAttribute("data-view") || button.getAttribute("data-queue")) === view);
    });
  }

  function more(view, cursor) {
    return cursor ? "<div class='amk-more'><button type='button' class='btn ghost' data-more='" + view + "'>Load next page</button></div>" : "";
  }

  function wireMore(view, loader) {
    var button = document.querySelector("[data-more='" + view + "']");
    if (button) button.addEventListener("click", function () { button.disabled = true; loader(true); });
  }

  function table(view, headings, items, renderRow, cursor, append) {
    var content = document.getElementById("amk-content");
    var rows = items.map(renderRow).join("");
    if (append) {
      var body = content.querySelector("tbody");
      if (body) body.insertAdjacentHTML("beforeend", rows);
      var old = content.querySelector(".amk-more");
      if (old) old.remove();
      content.insertAdjacentHTML("beforeend", more(view, cursor));
    } else if (!items.length) {
      content.innerHTML = empty("Nothing to review", "No records match the current filter.");
    } else {
      content.innerHTML = "<div class='amk-table-wrap'><table class='amk-table'><thead><tr>" + headings.map(function (h) { return "<th>" + esc(h) + "</th>"; }).join("") + "</tr></thead><tbody>" + rows + "</tbody></table></div>" + more(view, cursor);
    }
  }

  function media(items, evidence) {
    if (!items.length) return "<div class='amk-media-empty'>" + (evidence ? "No evidence attached" : "No listing renders") + "</div>";
    return "<div class='amk-media-strip'>" + items.map(function (asset) {
      var url = safeUrl(asset.url);
      var image = /^image\//.test(String(asset.content_type || ""));
      return "<a class='amk-media-item' href='" + esc(url || "#") + "' target='_blank' rel='noopener'>" + (image && url ? "<img src='" + esc(url) + "' alt='" + esc(asset.filename) + "' loading='lazy'>" : "<span class='amk-file'>FILE</span>") + "<small>" + esc(asset.filename) + "</small></a>";
    }).join("") + "</div>";
  }

  function claimCard(item) {
    var listing = item.listing || {}, seller = item.seller || {};
    var tools = (item.tools || []).map(function (tool) { return "<span class='amk-chip'>" + esc(tool) + "</span>"; }).join("") || "Not declared";
    return "<article class='amk-claim' data-claim='" + esc(item.id) + "'><div class='amk-claim-head'><div><span class='amk-state'>" + esc(item.status) + "</span><h4>" + esc(listing.title) + "</h4><p>" + esc(seller.display_name) + " · @" + esc(seller.handle) + "</p></div><time>Submitted " + esc(date(item.submitted_at)) + "</time></div>" +
      "<div class='amk-claim-grid'><div class='amk-claim-copy'><h5>Process claim</h5><p class='amk-process'>" + esc(item.process_note) + "</p><dl><div><dt>Tools</dt><dd>" + tools + "</dd></div><div><dt>Hours</dt><dd>" + esc(item.hours || "Not declared") + "</dd></div><div><dt>Declared AI use</dt><dd>" + esc(item.ai_tools_note || "None declared") + "</dd></div></dl></div>" +
      "<div class='amk-claim-media'><section><h5>Evidence</h5>" + media(item.evidence_assets || [], true) + "</section><section><h5>Listing renders</h5>" + media(item.listing_previews || [], false) + "</section></div></div>" +
      "<div class='amk-decision'><label>Required reviewer note<textarea rows='3' data-note placeholder='Record what was verified or why the claim failed.'></textarea></label><div class='amk-actions'><button class='btn primary' data-decision='verified'>Verify</button><button class='btn danger' data-decision='rejected'>Reject</button><button class='btn ghost' data-decision='revoked'>Revoke</button></div></div></article>";
  }

  async function attestations(append) {
    var filter = document.getElementById("amk-attestation-filter");
    if (!append) cursors.attestations = null;
    var data = await api(ROOT + "/attestations" + params({ status: filter.value, cursor: append ? cursors.attestations : "" }));
    cursors.attestations = data.next_cursor;
    var html = data.items.length ? data.items.map(claimCard).join("") : empty("No attestation claims", "The selected review queue is clear.");
    var content = document.getElementById("amk-content");
    if (append) {
      var old = content.querySelector(".amk-more"); if (old) old.remove();
      content.querySelector(".amk-attestations").insertAdjacentHTML("beforeend", html);
      content.insertAdjacentHTML("beforeend", more("attestations", data.next_cursor));
    } else content.innerHTML = "<div class='amk-attestations'>" + html + "</div>" + more("attestations", data.next_cursor);
    Array.prototype.forEach.call(content.querySelectorAll("[data-decision]"), function (button) {
      button.onclick = function () {
        var card = button.closest("[data-claim]");
        var note = card.querySelector("[data-note]").value.trim();
        if (!note) { setStatus("A reviewer note is required.", true); return; }
        arm(button, button.textContent, async function () {
          await request("/attestations/" + encodeURIComponent(card.getAttribute("data-claim")) + "/decide", { decision: button.getAttribute("data-decision"), note: note });
          setStatus("Attestation decision recorded."); await Promise.all([attestations(false), overview()]);
        });
      };
    });
    wireMore("attestations", attestations);
  }

  async function listings(append) {
    var filter = document.getElementById("amk-listing-filter"), search = document.getElementById("amk-listing-search");
    if (!append) cursors.listings = null;
    var data = await api(ROOT + "/listings" + params({ status: filter.value, q: search.value, cursor: append ? cursors.listings : "" }));
    cursors.listings = data.next_cursor;
    table("listings", ["Model", "Seller", "Provenance", "Price", "Submitted", "Actions"], data.items, function (item) {
      var price = item.pricing_kind === "free" ? "Free" : item.price_cents ? cash(item.price_cents) : num(item.price_credits) + " credits";
      return "<tr data-listing='" + esc(item.id) + "'><td><strong>" + esc(item.title) + "</strong><small>" + esc(item.status) + (item.featured ? " · featured" : "") + "</small></td><td>" + esc(item.seller_name) + "<small>@" + esc(item.seller_handle) + "</small></td><td>" + esc(item.origin) + "<small>" + esc(item.attestation_status) + "</small></td><td>" + esc(price) + "</td><td>" + esc(date(item.created_at)) + "</td><td><input class='amk-inline-note' data-note placeholder='Moderation note'><div class='amk-row-actions'><button class='btn primary' data-action='approve'>Approve</button><button class='btn danger' data-action='reject'>Reject</button><button class='btn danger' data-action='remove'>Remove</button><button class='btn ghost' data-action='restore'>Restore</button><button class='btn ghost' data-action='" + (item.featured ? "unfeature" : "feature") + "'>" + (item.featured ? "Unfeature" : "Feature") + "</button></div></td></tr>";
    }, data.next_cursor, append);
    Array.prototype.forEach.call(document.querySelectorAll("[data-listing] [data-action]"), function (button) {
      button.onclick = function () {
        var row = button.closest("[data-listing]"), action = button.getAttribute("data-action"), note = row.querySelector("[data-note]").value.trim();
        if ((action === "reject" || action === "remove") && !note) { setStatus("Reject and remove require a note.", true); return; }
        var run = async function () { await request("/listings/" + encodeURIComponent(row.getAttribute("data-listing")) + "/moderate", { action: action, note: note }); setStatus("Listing moderation recorded."); await Promise.all([listings(false), overview()]); };
        if (["reject", "remove", "restore"].indexOf(action) >= 0) arm(button, button.textContent, run); else run().catch(function (error) { setStatus(String(error.message || error), true); });
      };
    });
    wireMore("listings", listings);
  }

  async function reports(append) {
    var filter = document.getElementById("amk-report-filter");
    if (!append) cursors.reports = null;
    var data = await api(ROOT + "/reports" + params({ status: filter.value, cursor: append ? cursors.reports : "" }));
    cursors.reports = data.next_cursor;
    table("reports", ["Listing", "Complaints", "Reasons", "Oldest", "Resolution"], data.items, function (item) {
      var reasons = (item.reports || []).map(function (r) { return r.reason; }).filter(function (v, i, a) { return a.indexOf(v) === i; }).join(", ");
      var isOpen = (item.reports || []).some(function (report) { return report.status === "open"; });
      var resolution = isOpen ? "<textarea rows='2' data-note placeholder='Required resolution'></textarea><div class='amk-row-actions'><button class='btn danger' data-action='actioned'>Action taken</button><button class='btn ghost' data-action='dismissed'>Dismiss</button></div>" : "<span>" + esc((item.reports[0] && item.reports[0].resolution) || "Resolved") + "</span>";
      return "<tr data-report='" + esc(item.report_ids[0]) + "'><td><strong>" + esc(item.title) + "</strong><small>@" + esc(item.seller_handle) + "</small></td><td><strong>" + num(item.report_count) + "</strong></td><td>" + esc(reasons) + "</td><td>" + esc(date(item.oldest_at)) + "</td><td>" + resolution + "</td></tr>";
    }, data.next_cursor, append);
    Array.prototype.forEach.call(document.querySelectorAll("[data-report] [data-action]"), function (button) {
      button.onclick = function () {
        var row = button.closest("[data-report]"), note = row.querySelector("[data-note]").value.trim();
        if (!note) { setStatus("A resolution is required.", true); return; }
        arm(button, button.textContent, async function () { await request("/reports/" + encodeURIComponent(row.getAttribute("data-report")) + "/resolve", { status: button.getAttribute("data-action"), resolution: note }); setStatus("All open reports for the listing were resolved."); await Promise.all([reports(false), overview()]); });
      };
    });
    wireMore("reports", reports);
  }

  async function sellers(append) {
    var search = document.getElementById("amk-seller-search");
    if (!append) cursors.sellers = null;
    var data = await api(ROOT + "/sellers" + params({ q: search.value, cursor: append ? cursors.sellers : "" }));
    cursors.sellers = data.next_cursor;
    table("sellers", ["Seller", "Catalogue", "Earned", "Trust", "Publishing"], data.items, function (item) {
      return "<tr data-seller='" + esc(item.email) + "'><td><strong>" + esc(item.display_name) + "</strong><small>@" + esc(item.handle) + " · " + esc(item.email) + "</small></td><td>" + num(item.published_count) + " published<small>" + num(item.listing_count) + " total</small></td><td>" + cash(item.earned_cents) + "<small>" + num(item.earned_credits) + " credits</small></td><td><div class='amk-row-actions'><button class='btn ghost' data-flag='verified' data-value='" + (!item.verified) + "'>" + (item.verified ? "Remove verification" : "Verify seller") + "</button><button class='btn ghost' data-flag='featured' data-value='" + (!item.featured) + "'>" + (item.featured ? "Unfeature seller" : "Feature seller") + "</button></div></td><td><input class='amk-inline-note' data-reason value='" + esc(item.blocked_reason) + "' placeholder='Required blocking reason'><button class='btn " + (item.publishing_blocked ? "ghost" : "danger") + "' data-block='" + (!item.publishing_blocked) + "'>" + (item.publishing_blocked ? "Unblock" : "Block publishing") + "</button></td></tr>";
    }, data.next_cursor, append);
    Array.prototype.forEach.call(document.querySelectorAll("[data-seller] [data-flag]"), function (button) {
      button.onclick = function () {
        var row = button.closest("[data-seller]"), body = {};
        var next = button.getAttribute("data-value") === "true";
        body[button.getAttribute("data-flag")] = next;
        var run = async function () {
          await request("/sellers/" + encodeURIComponent(row.getAttribute("data-seller")) + "/flags", body);
          setStatus("Seller flag updated.");
          await sellers(false);
        };
        if (next) run().catch(function (error) { setStatus(String(error.message || error), true); });
        else arm(button, button.textContent, run);
      };
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-seller] [data-block]"), function (button) {
      button.onclick = function () {
        var row = button.closest("[data-seller]"), blocked = button.getAttribute("data-block") === "true", reason = row.querySelector("[data-reason]").value.trim();
        if (blocked && !reason) { setStatus("Blocking requires a reason.", true); return; }
        arm(button, button.textContent, async function () { await request("/sellers/" + encodeURIComponent(row.getAttribute("data-seller")) + "/flags", { publishing_blocked: blocked, blocked_reason: reason }); setStatus("Seller publishing state updated."); await Promise.all([sellers(false), overview()]); });
      };
    });
    wireMore("sellers", sellers);
  }

  async function payouts(append) {
    var filter = document.getElementById("amk-payout-filter");
    if (!append) cursors.payouts = null;
    var data = await api(ROOT + "/payouts" + params({ status: filter.value, cursor: append ? cursors.payouts : "" }));
    cursors.payouts = data.next_cursor;
    table("payouts", ["Seller", "Credits", "Cash value", "Connect", "Requested", "Decision"], data.items, function (item) {
      return "<tr data-payout='" + esc(item.id) + "'><td><strong>" + esc(item.display_name) + "</strong><small>@" + esc(item.handle) + "</small></td><td>" + num(item.credits) + "</td><td>" + cash(item.cents) + "</td><td>" + (item.connect_payouts_enabled ? "Ready" : "Not ready") + "</td><td>" + esc(date(item.requested_at)) + "</td><td><input class='amk-inline-note' data-note placeholder='Note required to deny'><div class='amk-row-actions'><button class='btn primary' data-action='approved'>Approve payout</button><button class='btn danger' data-action='denied'>Deny</button></div></td></tr>";
    }, data.next_cursor, append);
    Array.prototype.forEach.call(document.querySelectorAll("[data-payout] [data-action]"), function (button) {
      button.onclick = function () {
        var row = button.closest("[data-payout]"), decision = button.getAttribute("data-action"), note = row.querySelector("[data-note]").value.trim();
        if (decision === "denied" && !note) { setStatus("A denial note is required.", true); return; }
        arm(button, button.textContent, async function () { await request("/payouts/" + encodeURIComponent(row.getAttribute("data-payout")) + "/decide", { decision: decision, note: note }); setStatus(decision === "approved" ? "Payout executed." : "Payout denied and credits restored."); await Promise.all([payouts(false), overview()]); });
      };
    });
    wireMore("payouts", payouts);
  }

  async function imports(append) {
    var filter = document.getElementById("amk-import-filter");
    if (!append) cursors.imports = null;
    var data = await api(ROOT + "/imports" + params({ status: filter.value, cursor: append ? cursors.imports : "" }));
    cursors.imports = data.next_cursor;
    table("imports", ["Source", "Requester", "Detected license", "Status", "Outcome", "Created"], data.items, function (item) {
      var url = safeUrl(item.source_url);
      return "<tr><td><strong>" + esc(item.source_name) + "</strong><small>" + (url ? "<a href='" + esc(url) + "' target='_blank' rel='noopener'>Open source record</a>" : esc(item.source_ref)) + "</small></td><td>" + esc(item.requested_by) + "</td><td>" + esc(item.detected_license || "Not detected") + "</td><td>" + esc(item.status) + "</td><td>" + esc(item.outcome || item.error || "Pending") + "</td><td>" + esc(date(item.created_at)) + "</td></tr>";
    }, data.next_cursor, append);
    wireMore("imports", imports);
  }

  function chart(series) {
    var w = 720, h = 180, p = 14;
    var cashValues = series.map(function (r) { return Number(r.cash_gmv_cents || 0); });
    var creditValues = series.map(function (r) { return Number(r.credit_gmv || 0) / 10; });
    var max = Math.max.apply(Math, cashValues.concat(creditValues).concat([1]));
    function points(values) { return values.map(function (v, i) { var x = p + (values.length < 2 ? 0 : i * (w - 2 * p) / (values.length - 1)); var y = h - p - v * (h - 2 * p) / max; return x.toFixed(1) + "," + y.toFixed(1); }).join(" "); }
    return "<div class='amk-chart'><svg viewBox='0 0 " + w + " " + h + "' role='img' aria-label='Cash and credit marketplace gross sales over time'><line x1='" + p + "' y1='" + (h - p) + "' x2='" + (w - p) + "' y2='" + (h - p) + "'></line><polyline class='is-cash' points='" + points(cashValues) + "'></polyline><polyline class='is-credit' points='" + points(creditValues) + "'></polyline></svg><div class='amk-legend'><span class='is-cash'>Cash GMV</span><span class='is-credit'>Credit GMV at 1 credit = $0.001</span></div></div>";
  }

  async function revenue() {
    var data = await api(ROOT + "/revenue" + params({ days: document.getElementById("amk-revenue-days").value })), t = data.totals;
    document.getElementById("amk-content").innerHTML = "<div class='amk-metrics'><div><span>Cash GMV</span><strong>" + cash(t.cash_gmv_cents) + "</strong></div><div><span>Credit GMV</span><strong>" + num(t.credit_gmv) + "</strong></div><div><span>Fees earned</span><strong>" + cash(t.fee_cents) + " + " + num(t.fee_credits) + " credits</strong></div><div><span>Unit sales</span><strong>" + num(t.unit_sales) + "</strong></div><div><span>New listings</span><strong>" + num(t.new_listings) + "</strong></div><div><span>New sellers</span><strong>" + num(t.new_sellers) + "</strong></div></div>" + chart(data.series || []);
  }

  async function show(view) {
    activate(view); setStatus("");
    try {
      if (view === "attestations") { shell("Human-made attestation review", "<label>Status <select id='amk-attestation-filter'><option value='pending'>Pending</option><option value='verified'>Verified</option><option value='rejected'>Rejected</option><option value='revoked'>Revoked</option></select></label>"); document.getElementById("amk-attestation-filter").onchange = function () { attestations(false); }; await attestations(false); }
      else if (view === "listings") { shell("Listing moderation", "<input id='amk-listing-search' type='search' placeholder='Search listings'><select id='amk-listing-filter'><option value='in_review'>In review</option><option value='published'>Published</option><option value='rejected'>Rejected</option><option value='removed'>Removed</option><option value='unlisted'>Unlisted</option><option value='draft'>Draft</option><option value=''>All</option></select>"); document.getElementById("amk-listing-filter").onchange = document.getElementById("amk-listing-search").onchange = function () { listings(false); }; await listings(false); }
      else if (view === "reports") { shell("Grouped listing reports", "<label>Status <select id='amk-report-filter'><option value='open'>Open</option><option value='actioned'>Actioned</option><option value='dismissed'>Dismissed</option><option value=''>All</option></select></label>"); document.getElementById("amk-report-filter").onchange = function () { reports(false); }; await reports(false); }
      else if (view === "sellers") { shell("Seller trust and publishing", "<input id='amk-seller-search' type='search' placeholder='Search seller, handle or email'>"); document.getElementById("amk-seller-search").onchange = function () { sellers(false); }; await sellers(false); }
      else if (view === "payouts") { shell("Credit cash-out requests", "<label>Status <select id='amk-payout-filter'><option value='pending'>Pending</option><option value='approved'>Approved</option><option value='paid'>Paid</option><option value='denied'>Denied</option><option value='failed'>Failed</option><option value=''>All</option></select></label>"); document.getElementById("amk-payout-filter").onchange = function () { payouts(false); }; await payouts(false); }
      else if (view === "imports") { shell("Third-party import audit", "<label>Status <select id='amk-import-filter'><option value='pending'>Pending</option><option value='fetched'>Fetched</option><option value='linked'>Linked</option><option value='published'>Published</option><option value='failed'>Failed</option><option value=''>All</option></select></label>"); document.getElementById("amk-import-filter").onchange = function () { imports(false); }; await imports(false); }
      else { shell("Marketplace revenue", "<label>Window <select id='amk-revenue-days'><option value='30'>30 days</option><option value='90' selected>90 days</option><option value='180'>180 days</option><option value='365'>365 days</option></select></label>"); document.getElementById("amk-revenue-days").onchange = revenue; await revenue(); }
      await overview();
    } catch (error) {
      setStatus(String(error.message || error), true);
      var content = document.getElementById("amk-content"); if (content) content.innerHTML = empty("Marketplace data unavailable", "Check the gateway and try again.");
    }
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-view]"), function (button) { button.addEventListener("click", function () { show(button.getAttribute("data-view")); }); });
  document.getElementById("amk-refresh").addEventListener("click", function () { show(activeView); });
  overview().catch(function (error) { setStatus(String(error.message || error), true); });
  show("attestations");
})();
