// Waiting page for the Chudware public demo. No external assets, no inline script.
// While queued the page keeps one long-poll request in flight (/demo/status?wait=N), which
// holds its place in the queue even when the browser throttles a background tab's timers.
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const NOTES = {
    idle: 'Your session ended after 15 minutes without activity.',
    hard_limit: 'Your 45-minute session has ended.',
    ended: 'Your session was ended.',
    queue_timeout: 'You were removed from the line because this page stopped checking in.',
    start_failed: 'Your workspace could not be started. Please try again.',
    container_exited: 'Your workspace stopped unexpectedly. Please try again.',
    shutdown: 'The demo is restarting. Please rejoin in a minute.',
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

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function show(name) {
    for (const id of ['intro', 'queue', 'starting']) $(id).hidden = id !== name;
  }

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
    $('max-sessions').textContent = String(s.max_sessions || 3);
    $('load').textContent = s.state === 'queued' || s.state === 'starting'
      ? s.active_sessions + ' of ' + s.max_sessions + ' sessions running, ' + s.queue_length + ' waiting'
      : '';
    if (s.ai_remaining_today === null || s.ai_remaining_today === undefined) {
      $('ai').textContent = 'AI is not enabled on this server right now.';
    } else {
      $('ai').textContent = s.ai_remaining_today + ' free AI requests are left today.';
    }
    if (s.state === 'queued') {
      show('queue');
      $('position').textContent = String(s.position);
      $('capacity').hidden = s.note !== 'capacity';
      eta = s.eta_seconds;
      etaSince = performance.now();
      renderEta();
    } else if (s.state === 'starting') {
      show('starting');
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

  $('join').addEventListener('click', join);
  $('leave').addEventListener('click', leave);
  setInterval(renderEta, 1000);
  follow();
})();
