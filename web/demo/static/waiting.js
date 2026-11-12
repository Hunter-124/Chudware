// Waiting page for the Chudware public demo. No external assets, no inline script.
// While queued the page keeps one long-poll request in flight (/demo/status?wait=N), which
// holds its place in the queue even when the browser throttles a background tab's timers.
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const NOTES = {
    idle: 'Your workspace went to sleep after 15 minutes without activity. Your files are saved.',
    ended: 'Your workspace was put to sleep. Your files are saved.',
    admin_ended: 'The operator put your workspace to sleep. Your files are saved.',
    quota_exceeded: 'Your workspace is over its 1 GiB storage cap, so it was put to sleep. '
      + 'Resume it and delete or download files, or delete the saved workspace below.',
    queue_timeout: 'You were removed from the line because this page stopped checking in.',
    start_failed: 'Your workspace could not be started. Please try again.',
    container_exited: 'Your workspace stopped unexpectedly. Your saved files are still there; resume below.',
    shutdown: 'The demo restarted. Your saved files are still there; resume below.',
    workspace_deleted: 'Your saved workspace was deleted.',
  };
  const REFUSALS = {
    queue_full: 'The line is full right now. Please try again in a few minutes.',
    ip_limit: 'Another demo session from your network is already active or waiting.',
    rate_limited: 'You have joined too often. Please wait a few minutes.',
    session_ending: 'Your previous session is still closing. Try again in a few seconds.',
  };

  let state = 'none';
  let eta = null;
  let etaSince = 0;
  let running = false;
  // Most joins are admitted within a second or two. The queue panel only appears
  // if the visitor is *still* queued after this grace period, so someone who gets
  // straight in never sees a queue flash past.
  const QUEUE_SHOW_AFTER_MS = 3000;
  let queuedAt = 0;
  let queueTimer = 0;
  let lastStatus = null;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function show(name) {
    for (const id of ['intro', 'queue', 'starting', 'sleeping']) $(id).hidden = id !== name;
  }
  const fmtBytes = (n) => {
    if (!n && n !== 0) return '';
    const units = ['B', 'KiB', 'MiB', 'GiB'];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return (i ? v.toFixed(1) : String(v)) + ' ' + units[i];
  };

  function showError(message) {
    const el = $('error');
    el.textContent = message || '';
    el.hidden = !message;
  }

  function renderEta() {
    const el = $('eta');
    if (eta === null) {
      el.textContent = '';
      return;
    }
    const left = Math.max(0, eta - Math.round((performance.now() - etaSince) / 1000));
    el.textContent = left < 60 ? 'Estimated wait: under a minute (rough estimate)'
      : 'Estimated wait: about ' + Math.ceil(left / 60) + ' min (rough estimate)';
  }

  function render(s) {
    state = s.state;
    lastStatus = s;
    if (s.state !== 'queued') {
      queuedAt = 0;
      if (queueTimer) { clearTimeout(queueTimer); queueTimer = 0; }
    }
    $('max-sessions').textContent = String(s.max_sessions || 3);
    $('load').textContent = s.state === 'queued' || s.state === 'starting'
      ? s.active_sessions + ' of ' + s.max_sessions + ' sessions running, ' + s.queue_length + ' waiting'
      : '';
    // Live availability only; the page text already describes the models and limits.
    const ai = [s.claude_available && 'Claude models', s.free_ai_available && 'free models'].filter(Boolean);
    $('ai').textContent = ai.length ? 'Right now: ' + ai.join(' and ') + ' available.'
      : 'Right now: AI is not enabled on this server.';
    if (s.state === 'queued') {
      if (!queuedAt) {
        queuedAt = performance.now();
        queueTimer = setTimeout(() => { if (state === 'queued' && lastStatus) render(lastStatus); },
                                QUEUE_SHOW_AFTER_MS);
      }
      if (performance.now() - queuedAt < QUEUE_SHOW_AFTER_MS) {
        show('starting');            // admitted almost immediately: no queue flash
        return;
      }
      show('queue');
      $('position').textContent = String(s.position);
      $('capacity').hidden = s.note !== 'capacity';
      eta = s.eta_seconds;
      etaSince = performance.now();
      renderEta();
    } else if (s.state === 'starting') {
      show('starting');
    } else if (s.state === 'sleeping') {
      show('sleeping');
      const size = s.workspace_bytes ? ' (' + fmtBytes(s.workspace_bytes) + ' saved)' : '';
      $('sleeping-detail').textContent = (NOTES[s.note] || 'Your workspace is saved.') + size;
    } else {
      show('intro');
      const note = $('notice');
      note.textContent = s.state === 'expired' ? (NOTES[s.note] || 'Your session has ended.') : '';
      note.hidden = s.state !== 'expired';
      $('join').disabled = false;
    }
  }

  async function fetchStatus(wait) {
    const response = await fetch('/demo/status?wait=' + wait, {
      credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
    });
    if (!response.ok) throw new Error('status ' + response.status);
    return response.json();
  }

  async function follow() {
    if (running) return;
    running = true;
    let failures = 0;
    try {
      for (;;) {
        try {
          const s = await fetchStatus(state === 'queued' || state === 'starting' ? 20 : 0);
          failures = 0;
          render(s);
          if (s.state === 'active') {
            location.replace('/app/');
            return;
          }
          if (s.state !== 'queued' && s.state !== 'starting') return;
        } catch (err) {
          failures += 1;
          await sleep(Math.min(15000, 1000 * failures));
        }
      }
    } finally {
      running = false;
    }
  }

  async function join() {
    $('join').disabled = true;
    showError('');
    try {
      const response = await fetch('/demo/join', { method: 'POST', credentials: 'same-origin' });
      const body = await response.json();
      if (!response.ok) {
        const code = body && body.error && body.error.code;
        showError(REFUSALS[code] || 'Could not join the line. Please try again.');
        $('join').disabled = false;
        return;
      }
      render(body);
      if (body.state === 'none') {
        showError('Your browser must accept cookies for this demo to work.');
        return;
      }
      follow();
    } catch (err) {
      showError('Could not reach the server. Please try again.');
      $('join').disabled = false;
    }
  }

  async function leave() {
    $('leave').disabled = true;
    try {
      await fetch('/demo/end', { method: 'POST', credentials: 'same-origin' });
    } finally {
      $('leave').disabled = false;
    }
  }

  async function deleteWorkspace() {
    if (!confirm('Delete your saved workspace and all its files? This cannot be undone.')) return;
    $('delete-workspace').disabled = true;
    try {
      const r = await fetch('/demo/workspace/delete', { method: 'POST', credentials: 'same-origin' });
      if (!r.ok) throw new Error('delete ' + r.status);
      const s = await r.json();
      render(s);
    } catch (err) {
      const el = $('sleeping-error');
      el.textContent = 'Could not delete the workspace. Please try again.';
      el.hidden = false;
    } finally {
      $('delete-workspace').disabled = false;
    }
  }

  $('join').addEventListener('click', join);
  $('resume').addEventListener('click', join);
  $('delete-workspace').addEventListener('click', deleteWorkspace);
  $('leave').addEventListener('click', leave);
  setInterval(renderEta, 1000);
  follow();
})();
