// Admin dashboard: polls /admin/api/metrics, renders tables with textContent only.
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  // Always same-origin: on chudware.com the Pages proxy forwards these calls
  // (with the Cloudflare Access JWT) to the front door; the JWT never exists
  // in the browser, so a cross-origin call to app.chudware.com could not auth.
  const API = '';
  const fmtBytes = (n) => {
    if (n === null || n === undefined) return '-';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return (i ? v.toFixed(1) : String(v)) + ' ' + units[i];
  };
  const fmtTime = (epoch) => epoch ? new Date(epoch * 1000).toLocaleString() : '-';
  const ago = (epoch, now) => epoch ? Math.max(0, Math.round(now - epoch)) + 's ago' : '-';

  function table(el, headers, rows) {
    el.textContent = '';
    if (!rows.length) { el.textContent = 'None.'; return; }
    const t = document.createElement('table');
    const head = t.insertRow();
    for (const h of headers) { const th = document.createElement('th'); th.textContent = h; head.appendChild(th); }
    for (const row of rows) {
      const tr = t.insertRow();
      for (const cell of row) {
        const td = tr.insertCell();
        if (cell && typeof cell === 'object') td.appendChild(cell); else td.textContent = String(cell ?? '-');
      }
    }
    el.appendChild(t);
  }
  function button(label, fn) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'secondary'; b.textContent = label;
    b.addEventListener('click', fn);
    return b;
  }
  async function post(path, body) {
    const r = await fetch(API + path, { method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    if (r.status === 401) { showDenied(); throw new Error('auth'); }
    if (!r.ok) throw new Error(path + ' ' + r.status);
    return r.json();
  }
  function showDenied() {
    $('dash').hidden = true; $('denied').hidden = false;
  }

  function render(d) {
    const now = d.now;
    const active = d.sessions.filter((s) => s.state === 'active').length;
    const queued = d.sessions.filter((s) => s.state === 'queued').length;
    const cards = [
      ['Active sessions', active + ' / ' + d.limits.max_sessions],
      ['Queued', queued],
      ['Sleeping workspaces', d.workspaces.length],
      ['Stored files', fmtBytes(d.stored_bytes) + ' of ' + fmtBytes(d.limits.workspaces_total_bytes)],
      ['Disk free', fmtBytes(d.host.disk_free_bytes) + ' of ' + fmtBytes(d.host.disk_total_bytes)],
      ['Memory available', fmtBytes(d.host.mem_available_bytes)],
      ['Load (1m)', d.host.load[0] + ' / ' + d.host.cpu_count + ' cpu'],
      ['Pageviews today', (d.traffic.today.pageviews || 0) + ' (' + (d.traffic.today.unique_clients || 0) + ' clients)'],
      ['Requests today', d.traffic.today.requests || 0],
      ['Front door uptime', Math.round(d.traffic.uptime_seconds / 60) + ' min'],
      ['Claude today', d.ai.claude_today_usd === null ? '-' : '$' + d.ai.claude_today_usd + ' (month $' + d.ai.claude_month_usd + ')'],
      ['AI lanes', 'free: ' + (d.ai.free_available ? 'up' : 'down') + ' · claude: ' + (d.ai.claude_available ? 'up' : 'down')],
    ];
    $('cards').textContent = '';
    for (const [k, v] of cards) {
      const div = document.createElement('div'); div.className = 'card';
      const b = document.createElement('strong'); b.textContent = String(v);
      const s = document.createElement('span'); s.textContent = k;
      div.append(b, s); $('cards').appendChild(div);
    }
    const alerts = [];
    if (d.capacity_blocked) alerts.push('Capacity guard is holding the queue (host memory/disk low).');
    if (d.storage_blocked) alerts.push('Stored-workspace total cap reached: new visitors held, returning ones still reopen.');
    if (d.host.disk_free_bytes < 3 * 1024 ** 3) alerts.push('Under 3 GiB disk free — the host is nearly full.');
    $('alerts').textContent = alerts.join(' ');
    $('alerts').className = alerts.length ? 'error' : '';

    if (d.site && d.site.configured && !d.site.error) {
      table($('site'), ['Counter', 'Today'], [
        ['Document pageviews', d.site.today.pageviews],
        ['Total requests', d.site.today.requests],
        ['Unique IPs', d.site.today.unique_ips],
        ['404s (scans/probes)', d.site.today.not_found],
      ]);
      table($('site-paths'), ['Path', 'Pageviews (7d)'],
        d.site.top_paths_7d.map((x) => [x.path, x.hits]));
      table($('site-daily'), ['Day (UTC)', 'Pageviews', 'Unique IPs', 'Requests', '404s'],
        d.site.daily.slice().reverse().map((x) => [x.day, x.pageviews, x.unique_ips, x.requests, x.not_found]));
    } else {
      $('site').textContent = d.site && d.site.configured
        ? 'Site metrics query failed (' + (d.site.error || 'unknown') + ').'
        : 'Site metrics not configured on the front door.';
      $('site-paths').textContent = ''; $('site-daily').textContent = '';
    }
    $('who').textContent = 'Signed in as ' + (d.admin_email || 'operator');
    table($('traffic'), ['Counter', 'Today', 'All time (since tracking)'], [
      ['Document pageviews', d.traffic.today.pageviews || 0, d.traffic.pageviews_total],
      ['Total HTTP requests', d.traffic.today.requests || 0, d.traffic.requests_total],
      ['App asset requests', d.traffic.today.asset_requests || 0, d.traffic.routes.app_asset || 0],
      ['Status polls (not pageviews)', '-', d.traffic.routes.status_poll || 0],
      ['404s (scans/probes)', d.traffic.today.not_found || 0, d.traffic.statuses['404'] || 0],
      ['Joins', d.traffic.today.joins || 0, d.traffic.events.join || 0],
      ['Sessions started/resumed', d.traffic.today.sessions_started || 0,
        (d.traffic.events.session_started || 0) + (d.traffic.events.session_resumed || 0)],
    ]);
    table($('daily'), ['Day (UTC)', 'Pageviews', 'Unique clients', 'Requests', 'Assets', '404s', 'Joins', 'Sessions'],
      d.traffic.daily.slice().reverse().map((x) => [x.day, x.pageviews, x.unique_clients, x.requests,
        x.asset_requests, x.not_found, x.joins, x.sessions_started]));

    table($('sessions'), ['Session', 'State', 'Client tag', 'Active for', 'Idle in', 'Workspace', 'AI req/tok', 'Claude $', ''],
      d.sessions.map((s) => [s.sid, s.state, s.ip_tag,
        s.activated_at ? ago(s.activated_at, now) : '-', s.idle_in_seconds === null ? '-' : s.idle_in_seconds + 's',
        fmtBytes(s.workspace_bytes), s.ai_requests + ' / ' + s.ai_tokens, s.claude_usd,
        s.state === 'active' || s.state === 'queued' || s.state === 'starting'
          ? button('Sleep now', async () => { await post('/admin/api/session/end', { sid: s.sid }); refresh(); }) : '']));
    table($('workspaces'), ['Workspace', 'Last seen', 'Slept because', 'Size', 'AI req/tok', 'Claude $', ''],
      d.workspaces.map((w) => [w.sid, fmtTime(w.last_seen), w.reason || '-', fmtBytes(w.workspace_bytes),
        w.ai_requests + ' / ' + w.ai_tokens, w.claude_usd,
        button('Delete files', async () => {
          if (!confirm('Delete this workspace and all its files? This cannot be undone.')) return;
          await post('/admin/api/workspace/delete', { sid: w.full_sid }); refresh();
        })]));

    const sec = Object.entries(d.traffic.events)
      .filter(([k]) => k.includes('refused') || k.startsWith('slept:') || k.includes('admin_') || k.includes('websocket'));
    table($('security'), ['Signal', 'Count'],
      sec.length ? sec : [['No refusals/sleeps recorded since start', 0]]);
    const detail = [['Route class', d.traffic.routes], ['HTTP status', d.traffic.statuses],
                    ['Events', d.traffic.events],
                    ['Top client tags (hashed)', Object.fromEntries(d.traffic.top_client_tags.map((x) => [x.tag, x.requests]))]];
    $('detail').textContent = '';
    for (const [title, obj] of detail) {
      const wrap = document.createElement('div');
      const h = document.createElement('h3'); h.textContent = title; wrap.appendChild(h);
      const pre = document.createElement('pre'); pre.textContent = JSON.stringify(obj, null, 1);
      wrap.appendChild(pre); $('detail').appendChild(wrap);
    }
    $('updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
  }

  async function refresh() {
    try {
      const r = await fetch(API + '/admin/api/metrics', { credentials: 'include', cache: 'no-store' });
      if (r.status === 401) { showDenied(); return; }
      if (!r.ok) throw new Error('metrics ' + r.status);
      $('denied').hidden = true; $('dash').hidden = false;
      render(await r.json());
    } catch (e) { /* next tick retries */ }
  }
  refresh();
  setInterval(refresh, 5000);
})();
