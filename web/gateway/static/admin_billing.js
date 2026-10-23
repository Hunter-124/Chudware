/* chudware-web /admin — billing command centre */
(function () {
  "use strict";
  if (!document.getElementById('billing-mount')) return;
  var mount = document.getElementById("billing-mount");
  var state = { days: 90, overview: {}, timeline: [], credits: [], webhook: [], webhookPage: 1,
    active: { subscriptions: true, packs: true, cash: true, credits: true, fees: true },
    page: { subscriptions: pager(), invoices: pager(), connect: pager() } };

  function pager() { return { cursor: "", next: null, back: [], number: 1, status: "" }; }
  async function api(path, opts) {
    var res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
    var data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) throw new Error((data && (data.detail || data.error)) || res.status + " " + res.statusText);
    return data;
  }
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
  function money(n, currency) { if (n == null) return "Unavailable"; try { return new Intl.NumberFormat(undefined, { style: "currency", currency: String(currency || "usd").toUpperCase() }).format(Number(n) / 100); } catch (e) { return String(currency || "USD").toUpperCase() + " " + (Number(n) / 100).toFixed(2); } }
  function compact(n) { return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(Number(n || 0)); }
  function when(v) { if (!v) return "—"; var d = typeof v === "string" ? new Date(v) : new Date(Number(v) * 1000); return isNaN(d.getTime()) ? "—" : d.toLocaleString(); }
  function empty(n, text) { return '<tr><td class="billing-empty" colspan="' + n + '">' + esc(text) + "</td></tr>"; }
  function tone(ok, warn) { return ok ? "billing-health-ok" : (warn ? "billing-health-warn" : "billing-health-bad"); }
  function table(id, title, copy, heads, filter) { return '<section class="billing-card"><div class="billing-section-head"><div class="billing-card-title"><h3>' + esc(title) + '</h3><p>' + esc(copy) + '</p></div><div class="billing-table-actions">' + (filter || "") + '</div></div><div class="billing-table-scroll"><table class="billing-table"><thead><tr>' + heads.map(function (h) { return "<th>" + esc(h) + "</th>"; }).join("") + '</tr></thead><tbody id="billing-' + id + '-rows">' + empty(heads.length, "Loading…") + '</tbody></table></div><div class="billing-pager"><button class="btn ghost" id="billing-' + id + '-prev">Previous</button><span class="billing-page-label" id="billing-' + id + '-page">Page 1</span><button class="btn ghost" id="billing-' + id + '-next">Next</button></div></section>'; }

  mount.innerHTML = '<div class="billing-console">' +
    '<header class="billing-head"><div class="billing-head-copy"><h2>Billing command centre</h2><p>Revenue, customer billing, marketplace settlement and Stripe operations.</p></div><div class="billing-controls"><label>Window<select id="billing-days"><option value="30">30 days</option><option value="90" selected>90 days</option><option value="180">180 days</option><option value="365">365 days</option></select></label><button class="btn danger" id="billing-refund-manual">Refund PaymentIntent</button><button class="btn ghost" id="billing-refresh">Refresh live data</button></div></header>' +
    '<div id="billing-notice" class="billing-notice" role="status"></div><section id="billing-kpis" class="billing-kpis"></section><section class="billing-card"><div class="billing-card-title"><h3>Subscription mix</h3><p>Current lifecycle counts by commercial plan.</p></div><div id="billing-plan-mix" class="billing-health-grid"></div></section>' +
    '<div class="billing-grid-two"><section class="billing-card"><div class="billing-card-title"><h3>Revenue timeline</h3><p>Cash values plus credit GMV converted at 1 credit = $0.001.</p></div><div id="billing-revenue-legend" class="billing-legend"></div><div id="billing-revenue-chart" class="billing-chart-wrap"></div></section><section class="billing-card"><div class="billing-card-title"><h3>Credit economy</h3><p>AI spend remains separate from marketplace movement.</p></div><div id="billing-credit-chart" class="billing-chart-wrap"></div></section></div>' +
    '<div class="billing-grid-two"><section class="billing-card"><div class="billing-card-title"><h3>Stripe health and catalog</h3><p>Platform account readiness and declarative catalog drift.</p></div><div id="billing-health"></div></section><section class="billing-card"><div class="billing-card-title"><h3>Webhook endpoints</h3><p>Expected event coverage from the provisioner contract.</p></div><div id="billing-endpoints" class="billing-endpoints"></div></section></div>' +
    table("subscriptions", "Subscriptions", "Local entitlement state joined from accounts.", ["Account", "Plan", "Status", "Price", "Subscribed", "Paid through", "Credits", "Education"], '<select id="billing-subscriptions-status"><option value="">All statuses</option><option>active</option><option>trialing</option><option>past_due</option><option>canceled</option><option value="none">none</option></select>') +
    table("invoices", "Invoices", "Live Stripe invoices with hosted invoice links.", ["Invoice", "Customer", "Status", "Created", "Paid", "Due", "Attempts", "Actions"], '<select id="billing-invoices-status"><option value="">All statuses</option><option>draft</option><option>open</option><option>paid</option><option>uncollectible</option><option>void</option></select>') +
    table("connect", "Connect sellers", "Live payout capability for every marketplace seller.", ["Seller", "Connect account", "Charges", "Payouts", "Details", "Payable", "Requirements"], "") +
    table("webhooks", "Recent webhook processing", "Processed event ledger and endpoint coverage.", ["Event", "Type", "Processed", "Covering endpoints"], "") +
    '</div><dialog id="billing-refund-dialog" class="billing-dialog"><form class="billing-dialog-body" method="dialog"><h3>Issue Stripe refund</h3><p>This moves real money. Enter the PaymentIntent id, then type it again exactly to authorize the refund.</p><label class="billing-field">PaymentIntent id<input id="billing-refund-target" autocomplete="off" spellcheck="false"></label><label class="billing-field">Amount in cents, blank for full refund<input id="billing-refund-amount" inputmode="numeric"></label><label class="billing-field">Reason<select id="billing-refund-reason"><option value="requested_by_customer">Requested by customer</option><option value="duplicate">Duplicate</option><option value="fraudulent">Fraudulent</option></select></label><label class="billing-field">Type PaymentIntent id again<input id="billing-refund-confirm" autocomplete="off" spellcheck="false"></label><div id="billing-refund-status" class="billing-refund-status"></div><div class="billing-dialog-actions"><button class="btn ghost" value="cancel">Cancel</button><button class="btn danger" id="billing-refund-submit" type="button" disabled>Refund</button></div></form></dialog>';

  function notice(text, error) { var el = mount.querySelector("#billing-notice"); el.textContent = text || ""; el.className = "billing-notice" + (text ? " show" : "") + (error ? " error" : ""); }
  function spark(values, color) { values = values.length ? values : [0]; var lo = Math.min.apply(null, values), hi = Math.max.apply(null, values), span = hi - lo || 1, points = values.map(function (v, i) { return (i * 160 / Math.max(1, values.length - 1)) + "," + (33 - (v - lo) * 31 / span); }).join(" "), box = document.createElement("div"); box.innerHTML = '<svg viewBox="0 0 160 34" preserveAspectRatio="none" aria-hidden="true"><polyline points="' + points + '" fill="none" stroke="' + color + '" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>'; return box.firstChild; }
  function renderKpis() {
    var o = state.overview, usd = (o.marketplace && o.marketplace.usd) || {}, cr = (o.marketplace && o.marketplace.credits) || {}, fees = Number(usd.platform_fee_cents || 0) + Number(cr.platform_fee_credits || 0) / 10;
    var cards = [
      ["MRR", money(o.mrr_cents), o.reasons && o.reasons.mrr || "Active and trialing subscriptions", "#42ecce", function (r) { return r.subscription_revenue_cents; }],
      ["ARR", money(o.arr_cents), "MRR annualized", "#5da9ff", function (r) { return r.subscription_revenue_cents; }],
      ["Credit packs MTD", money(o.credit_pack_revenue_cents && o.credit_pack_revenue_cents.this_month), o.reasons && o.reasons.credit_pack_revenue || "Completed Stripe checkouts", "#c18cff", function (r) { return r.credit_pack_revenue_cents; }],
      ["Marketplace fees", money(Math.round(fees)), "Lifetime cash-equivalent earned", "#f0b429", function (r) { return Number(r.platform_fee_cents || 0) + Number(r.platform_fee_credits || 0) / 10; }],
      ["Paying accounts", compact(o.unique_paying_accounts), "ARPU " + money(o.arpu_mrr_cents), "#3dd68c", function (r) { return r.new_paying_accounts; }],
      ["Failed payments", compact(o.failed_payments_30d), "Last 30 days", "#ff6b7a", function () { return 0; }]
    ];
    var root = mount.querySelector("#billing-kpis"); root.innerHTML = cards.map(function (c, i) { return '<article class="billing-kpi"><div class="billing-kpi-label">' + esc(c[0]) + '</div><div class="billing-kpi-value">' + esc(c[1]) + '</div><div class="billing-kpi-meta">' + esc(c[2]) + '</div><div class="billing-spark" data-i="' + i + '"></div></article>'; }).join("");
    cards.forEach(function (c, i) { root.querySelector('[data-i="' + i + '"]').appendChild(spark(state.timeline.map(c[4]), c[3])); });
  }
  function renderPlanMix() {
    var counts = state.overview.subscription_counts || {};
    var plans = Object.keys(counts).sort();
    mount.querySelector("#billing-plan-mix").innerHTML = plans.length ? plans.map(function (plan) {
      var c = counts[plan] || {};
      return '<div class="billing-health-stat"><span>' + esc(plan) + '</span><strong>' + esc(Number(c.active || 0) + Number(c.trialing || 0)) + ' live</strong><div class="small muted">' + esc(c.active || 0) + ' active · ' + esc(c.trialing || 0) + ' trialing · ' + esc(c.past_due || 0) + ' past due · ' + esc(c.canceled || 0) + ' canceled</div></div>';
    }).join("") : '<div class="billing-empty">No subscription accounts are recorded.</div>';
  }

  var series = [
    ["subscriptions", "Subscriptions", "#42ecce", function (r) { return +r.subscription_revenue_cents || 0; }],
    ["packs", "Credit packs", "#c18cff", function (r) { return +r.credit_pack_revenue_cents || 0; }],
    ["cash", "Marketplace cash GMV", "#5da9ff", function (r) { return +r.marketplace_cash_gmv_cents || 0; }],
    ["credits", "Marketplace credit GMV", "#f0b429", function (r) { return (+r.marketplace_credit_gmv_credits || 0) / 10; }],
    ["fees", "Platform fee", "#ff8fa3", function (r) { return (+r.platform_fee_cents || 0) + (+r.platform_fee_credits || 0) / 10; }, true]
  ];
  function renderLegend() { var root = mount.querySelector("#billing-revenue-legend"); root.innerHTML = ""; series.forEach(function (s) { var b = document.createElement("button"); b.type = "button"; b.className = state.active[s[0]] ? "" : "off"; b.style.setProperty("--series-color", s[2]); b.innerHTML = '<i></i><span>' + esc(s[1]) + "</span>"; b.addEventListener("click", function () { state.active[s[0]] = !state.active[s[0]]; renderLegend(); renderRevenue(); }); root.appendChild(b); }); }
  function renderRevenue() {
    var root = mount.querySelector("#billing-revenue-chart"), data = state.timeline;
    if (!data.length) { root.innerHTML = '<div class="billing-empty">No revenue activity in this window.</div>'; return; }
    var W = 1000, H = 350, L = 64, T = 18, PW = 918, PH = 290, active = series.filter(function (s) { return state.active[s[0]]; }), areas = active.filter(function (s) { return !s[4]; }), lines = active.filter(function (s) { return s[4]; }), max = 1;
    data.forEach(function (r) { var total = 0; areas.forEach(function (s) { total += s[3](r); }); max = Math.max(max, total); lines.forEach(function (s) { max = Math.max(max, s[3](r)); }); });
    function x(i) { return L + i * PW / Math.max(1, data.length - 1); } function y(v) { return T + PH - v * PH / max; }
    var svg = [], lower = data.map(function () { return 0; });
    for (var g = 0; g <= 5; g++) { var gy = T + g * PH / 5; svg.push('<line class="billing-gridline" x1="' + L + '" y1="' + gy + '" x2="982" y2="' + gy + '"/><text class="billing-axis-label" x="56" y="' + (gy + 4) + '" text-anchor="end">' + esc(money(max * (5 - g) / 5)) + '</text>'); }
    areas.forEach(function (s) { var upper = data.map(function (r, i) { return lower[i] + s[3](r); }), a = upper.map(function (v, i) { return x(i) + " " + y(v); }), b = lower.map(function (v, i) { return x(i) + " " + y(v); }).reverse(); svg.push('<path d="M ' + a.join(" L ") + " L " + b.join(" L ") + ' Z" fill="' + s[2] + '" fill-opacity=".2" stroke="' + s[2] + '"/>'); lower = upper; });
    lines.forEach(function (s) { svg.push('<polyline points="' + data.map(function (r, i) { return x(i) + "," + y(s[3](r)); }).join(" ") + '" fill="none" stroke="' + s[2] + '" stroke-width="2.5"/>'); });
    for (var q = 0; q < Math.min(6, data.length); q++) { var n = Math.round(q * (data.length - 1) / Math.max(1, Math.min(6, data.length) - 1)); svg.push('<text class="billing-axis-label" x="' + x(n) + '" y="338" text-anchor="middle">' + esc(data[n].date.slice(5)) + '</text>'); }
    svg.push('<line id="billing-hover-line" y1="18" y2="308" stroke="var(--text-dim)" stroke-dasharray="3 3" visibility="hidden"/><rect id="billing-hover-capture" x="64" y="18" width="918" height="290" fill="transparent"/>');
    root.innerHTML = '<svg viewBox="0 0 1000 350" role="img" aria-label="Revenue time series">' + svg.join("") + '</svg><div class="billing-chart-tooltip" id="billing-revenue-tip"></div>';
    var cap = root.querySelector("#billing-hover-capture"), line = root.querySelector("#billing-hover-line"), tip = root.querySelector("#billing-revenue-tip");
    cap.addEventListener("pointermove", function (e) { var rect = cap.ownerSVGElement.getBoundingClientRect(), px = (e.clientX - rect.left) * W / rect.width, i = Math.max(0, Math.min(data.length - 1, Math.round((px - L) / PW * (data.length - 1)))), row = data[i]; line.setAttribute("x1", x(i)); line.setAttribute("x2", x(i)); line.setAttribute("visibility", "visible"); tip.innerHTML = '<strong>' + esc(row.date) + '</strong>' + active.map(function (s) { return '<span><b style="color:' + s[2] + '">' + esc(s[1]) + '</b><em>' + esc(money(s[3](row))) + '</em></span>'; }).join(""); tip.className = "billing-chart-tooltip show"; tip.style.left = Math.min(root.clientWidth - 205, Math.max(0, e.clientX - rect.left + 12)) + "px"; tip.style.top = Math.max(0, e.clientY - rect.top - 30) + "px"; });
    cap.addEventListener("pointerleave", function () { line.setAttribute("visibility", "hidden"); tip.className = "billing-chart-tooltip"; });
  }

  function renderCreditChart() {
    var root = mount.querySelector("#billing-credit-chart"), data = state.credits;
    if (!data.length) { root.innerHTML = '<div class="billing-empty">No credit ledger activity in this window.</div>'; return; }
    var ss = [["Issuance", "#42ecce", function (r) { return r.issuance; }], ["AI consumption", "#ff6b7a", function (r) { return r.ai_consumption; }], ["Market buy", "#f0b429", function (r) { return Math.abs(r.market_buy); }], ["Market earn", "#5da9ff", function (r) { return Math.abs(r.market_earn); }], ["Market payout", "#c18cff", function (r) { return Math.abs(r.market_payout); }]], max = 1;
    data.forEach(function (r) { ss.forEach(function (s) { max = Math.max(max, s[2](r)); }); }); function x(i) { return 52 + i * 536 / Math.max(1, data.length - 1); } function y(v) { return 298 - v * 278 / max; } var svg = [];
    for (var g = 0; g <= 4; g++) { var gy = 20 + g * 278 / 4; svg.push('<line class="billing-gridline" x1="52" y1="' + gy + '" x2="588" y2="' + gy + '"/><text class="billing-axis-label" x="45" y="' + (gy + 4) + '" text-anchor="end">' + esc(compact(max * (4 - g) / 4)) + '</text>'); }
    ss.forEach(function (s) { svg.push('<polyline points="' + data.map(function (r, i) { return x(i) + "," + y(s[2](r)); }).join(" ") + '" fill="none" stroke="' + s[1] + '" stroke-width="2"/>'); });
    root.innerHTML = '<div class="billing-legend">' + ss.map(function (s) { return '<span class="billing-chip" style="border-color:' + s[1] + ';color:' + s[1] + '">' + esc(s[0]) + '</span>'; }).join("") + '</div><svg viewBox="0 0 600 330" role="img" aria-label="Credit activity by kind">' + svg.join("") + "</svg>";
  }

  function stat(label, value, ok, warn) { return '<div class="billing-health-stat"><span>' + esc(label) + '</span><strong class="' + tone(ok, warn) + '">' + esc(value) + "</strong></div>"; }
  function renderHealth(d) { var root = mount.querySelector("#billing-health"); if (!d.stripe_configured) { root.innerHTML = '<p class="billing-health-warn">' + esc(d.reason) + '</p><p class="muted small">Live values are intentionally unavailable rather than shown as zero.</p>'; return; } var a = d.account || {}, c = d.catalog, count = c ? c.missing_products.length + c.product_mismatches.length + c.missing_prices.length + c.price_mismatches.length : 0; root.innerHTML = '<div class="billing-health-grid">' + stat("Mode", d.livemode ? "Live" : "Test", true) + stat("Charges", a.charges_enabled ? "Enabled" : "Disabled", a.charges_enabled) + stat("Payouts", a.payouts_enabled ? "Enabled" : "Disabled", a.payouts_enabled) + stat("Details", a.details_submitted ? "Submitted" : "Incomplete", a.details_submitted) + stat("Catalog", !c ? "Unavailable" : c.ok ? "Matches" : "Drift", c && c.ok, !c) + stat("Drift items", count, c && c.ok, !c) + '</div>' + (d.reason ? '<p class="billing-health-warn small">' + esc(d.reason) + '</p>' : '') + (d.contract_reason ? '<p class="billing-health-warn small">' + esc(d.contract_reason) + '</p>' : '') + (c && !c.ok ? '<p class="small"><strong>Missing products:</strong> ' + esc(c.missing_products.join(", ") || "none") + '<br><strong>Missing prices:</strong> ' + esc(c.missing_prices.join(", ") || "none") + '</p>' : ''); }
  function renderEndpoints(d) { var rows = d.endpoints || []; mount.querySelector("#billing-endpoints").innerHTML = rows.length ? rows.map(function (r) { var missing = r.missing_events || []; return '<article class="billing-endpoint"><div class="billing-endpoint-head"><strong>' + esc(r.url) + '</strong><span class="' + tone(r.status === "enabled" && !missing.length) + '">' + esc(r.status || "unknown") + '</span></div><div class="small muted">' + esc(r.livemode ? "live mode" : "test mode") + " · " + esc(r.enabled_events.length) + ' events</div><div>' + (missing.length ? missing.map(function (e) { return '<span class="billing-chip bad">missing ' + esc(e) + '</span>'; }).join("") : '<span class="billing-chip">complete event coverage</span>') + "</div></article>"; }).join("") : '<div class="billing-empty">' + esc(d.reason || "No Stripe webhook endpoints found.") + "</div>"; state.webhook = d.events || []; state.webhookPage = 1; renderWebhook(); }

  function updatePage(kind, next) { var p = state.page[kind]; p.next = next || null; mount.querySelector("#billing-" + kind + "-page").textContent = "Page " + p.number; mount.querySelector("#billing-" + kind + "-prev").disabled = !p.back.length; mount.querySelector("#billing-" + kind + "-next").disabled = !p.next; }
  function renderSubscriptions(d) { var rows = d.items || []; mount.querySelector("#billing-subscriptions-rows").innerHTML = rows.length ? rows.map(function (r) { return '<tr><td><strong>' + esc(r.email) + '</strong></td><td>' + esc(r.plan) + '</td><td>' + esc(r.subscription_status || "none") + '</td><td><code>' + esc(r.price_id || "—") + '</code></td><td>' + esc(when(r.subscribed_since)) + '</td><td>' + esc(when(r.paid_through)) + '</td><td>' + esc(compact(r.credit_balance)) + '</td><td>' + esc(r.is_edu ? "Yes" : "No") + "</td></tr>"; }).join("") : empty(8, "No accounts match this subscription filter."); updatePage("subscriptions", d.next_cursor); }
  function renderInvoices(d) { var rows = d.items || [], body = mount.querySelector("#billing-invoices-rows"); body.innerHTML = rows.length ? rows.map(function (r) { var link = r.hosted_invoice_url ? '<a href="' + esc(r.hosted_invoice_url) + '" target="_blank" rel="noopener">' + esc(r.number || r.id) + '</a>' : '<code>' + esc(r.number || r.id) + '</code>', refund = r.payment_intent && r.amount_paid > 0 ? '<button class="btn danger billing-refund-open" data-pi="' + esc(r.payment_intent) + '">Refund</button>' : '<span class="muted small">Not refundable here</span>'; return '<tr><td>' + link + '</td><td>' + esc(r.customer_email || r.customer || "—") + '</td><td>' + esc(r.status) + '</td><td>' + esc(when(r.created)) + '</td><td>' + esc(money(r.amount_paid, r.currency)) + '</td><td>' + esc(money(r.amount_due, r.currency)) + '</td><td>' + esc(r.attempt_count) + '</td><td>' + refund + "</td></tr>"; }).join("") : empty(8, d.reason || "No Stripe invoices match this filter."); body.querySelectorAll(".billing-refund-open").forEach(function (b) { b.addEventListener("click", function () { openRefund(b.getAttribute("data-pi")); }); }); updatePage("invoices", d.next_cursor); }
  function renderConnect(d) { var rows = d.items || []; mount.querySelector("#billing-connect-rows").innerHTML = rows.length ? rows.map(function (r) { var live = r.live || {}, req = (live.currently_due || []).concat(live.eventually_due || []); return '<tr><td><strong>' + esc(r.display_name || r.handle) + '</strong><br><span class="muted small">' + esc(r.email) + '</span></td><td><code>' + esc(r.connect_account_id || "not created") + '</code></td><td class="' + tone(live.charges_enabled) + '">' + esc(live.charges_enabled ? "Enabled" : "Disabled") + '</td><td class="' + tone(live.payouts_enabled) + '">' + esc(live.payouts_enabled ? "Enabled" : "Disabled") + '</td><td>' + esc(live.details_submitted ? "Submitted" : "Incomplete") + '</td><td class="' + tone(r.payable) + '">' + esc(r.payable ? "Ready" : r.reason || "Blocked") + '</td><td>' + (req.length ? req.map(function (x) { return '<span class="billing-chip bad">' + esc(x) + '</span>'; }).join("") : '<span class="muted">None</span>') + "</td></tr>"; }).join("") : empty(7, d.reason || "No marketplace sellers have enrolled."); updatePage("connect", d.next_cursor); }
  function renderWebhook() { var start = (state.webhookPage - 1) * 10, rows = state.webhook.slice(start, start + 10), pages = Math.max(1, Math.ceil(state.webhook.length / 10)); mount.querySelector("#billing-webhooks-rows").innerHTML = rows.length ? rows.map(function (r) { return '<tr><td><code>' + esc(r.event_id) + '</code></td><td>' + esc(r.event_type) + '</td><td>' + esc(when(r.processed_at)) + '</td><td class="' + tone(r.configured_endpoint_count > 0) + '">' + esc(r.configured_endpoint_count) + "</td></tr>"; }).join("") : empty(4, "No processed webhook events are recorded."); mount.querySelector("#billing-webhooks-page").textContent = "Page " + state.webhookPage + " of " + pages; mount.querySelector("#billing-webhooks-prev").disabled = state.webhookPage <= 1; mount.querySelector("#billing-webhooks-next").disabled = state.webhookPage >= pages; }

  async function loadTable(kind, reset) { var p = state.page[kind]; if (reset) { p.cursor = ""; p.back = []; p.number = 1; } var path = "/api/admin/billing/" + kind + "?cursor=" + encodeURIComponent(p.cursor) + (kind === "connect" ? "" : "&status=" + encodeURIComponent(p.status)); try { var d = await api(path); if (kind === "subscriptions") renderSubscriptions(d); else if (kind === "invoices") renderInvoices(d); else renderConnect(d); } catch (e) { mount.querySelector("#billing-" + kind + "-rows").innerHTML = empty(kind === "connect" ? 7 : 8, e.message); updatePage(kind, null); } }
  async function loadPrimary() {
    notice("Loading billing data…");
    var r = await Promise.allSettled([
      api("/api/admin/billing/overview"),
      api("/api/admin/billing/timeseries?days=" + state.days),
      api("/api/admin/billing/credits?days=" + state.days),
      api("/api/admin/billing/health"),
      api("/api/admin/billing/webhooks?limit=100")
    ]);
    var errors = [], warnings = [];
    if (r[0].status === "fulfilled") state.overview = r[0].value; else errors.push(r[0].reason.message);
    if (r[1].status === "fulfilled") {
      state.timeline = r[1].value.items || [];
      Object.keys(r[1].value.reasons || {}).forEach(function (key) { warnings.push(r[1].value.reasons[key]); });
    } else errors.push(r[1].reason.message);
    if (r[2].status === "fulfilled") state.credits = r[2].value.items || []; else errors.push(r[2].reason.message);
    if (r[3].status === "fulfilled") renderHealth(r[3].value); else errors.push(r[3].reason.message);
    if (r[4].status === "fulfilled") renderEndpoints(r[4].value); else errors.push(r[4].reason.message);
    renderKpis(); renderPlanMix(); renderLegend(); renderRevenue(); renderCreditChart();
    if (!state.overview.stripe_configured) warnings.unshift("Stripe is not configured. Local state remains available; live revenue is marked unavailable.");
    notice(errors.length ? errors.join(" · ") : warnings.join(" · "), errors.length > 0);
  }
  function refresh() { return Promise.all([loadPrimary(), loadTable("subscriptions", true), loadTable("invoices", true), loadTable("connect", true)]); }
  function wirePager(kind) { mount.querySelector("#billing-" + kind + "-next").addEventListener("click", function () { var p = state.page[kind]; if (!p.next) return; p.back.push(p.cursor); p.cursor = p.next; p.number++; loadTable(kind); }); mount.querySelector("#billing-" + kind + "-prev").addEventListener("click", function () { var p = state.page[kind]; if (!p.back.length) return; p.cursor = p.back.pop(); p.number--; loadTable(kind); }); }

  function refundReady() {
    var target = mount.querySelector("#billing-refund-target").value;
    var confirm = mount.querySelector("#billing-refund-confirm").value;
    mount.querySelector("#billing-refund-submit").disabled = !target || target !== confirm;
  }
  function openRefund(pi) {
    var d = mount.querySelector("#billing-refund-dialog");
    mount.querySelector("#billing-refund-target").value = pi || "";
    mount.querySelector("#billing-refund-confirm").value = "";
    mount.querySelector("#billing-refund-amount").value = "";
    mount.querySelector("#billing-refund-status").textContent = "";
    mount.querySelector("#billing-refund-submit").disabled = true;
    d.showModal();
  }
  mount.querySelector("#billing-refund-target").addEventListener("input", refundReady);
  mount.querySelector("#billing-refund-confirm").addEventListener("input", refundReady);
  mount.querySelector("#billing-refund-submit").addEventListener("click", async function () {
    var d = mount.querySelector("#billing-refund-dialog");
    var pi = mount.querySelector("#billing-refund-target").value;
    var confirm = mount.querySelector("#billing-refund-confirm").value;
    var raw = mount.querySelector("#billing-refund-amount").value.trim();
    var status = mount.querySelector("#billing-refund-status");
    var button = this;
    if (!pi || confirm !== pi) return;
    if (raw && (!/^\d+$/.test(raw) || +raw <= 0)) {
      status.textContent = "Amount must be a positive whole number of cents.";
      return;
    }
    button.disabled = true;
    status.textContent = "Issuing refund…";
    try {
      var result = await api("/api/admin/billing/refund", {
        method: "POST",
        body: JSON.stringify({
          payment_intent: pi,
          amount_cents: raw ? +raw : null,
          reason: mount.querySelector("#billing-refund-reason").value,
          confirm: confirm
        })
      });
      status.className = "billing-refund-status billing-health-ok";
      status.textContent = "Refund accepted: " + ((result.refund && result.refund.id) || result.kind);
      setTimeout(function () { d.close(); loadTable("invoices", true); loadPrimary(); }, 900);
    } catch (e) {
      status.className = "billing-refund-status";
      status.textContent = e.message;
      button.disabled = false;
    }
  });

  mount.querySelector("#billing-days").addEventListener("change", function (e) { state.days = +e.target.value || 90; loadPrimary(); }); mount.querySelector("#billing-refresh").addEventListener("click", refresh);
  mount.querySelector("#billing-refund-manual").addEventListener("click", function () { openRefund(""); });
  mount.querySelector("#billing-subscriptions-status").addEventListener("change", function (e) { state.page.subscriptions.status = e.target.value; loadTable("subscriptions", true); }); mount.querySelector("#billing-invoices-status").addEventListener("change", function (e) { state.page.invoices.status = e.target.value; loadTable("invoices", true); });
  ["subscriptions", "invoices", "connect"].forEach(wirePager); mount.querySelector("#billing-webhooks-prev").addEventListener("click", function () { if (state.webhookPage > 1) { state.webhookPage--; renderWebhook(); } }); mount.querySelector("#billing-webhooks-next").addEventListener("click", function () { if (state.webhookPage * 10 < state.webhook.length) { state.webhookPage++; renderWebhook(); } });
  refresh();
})();
