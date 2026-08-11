const summary = document.querySelector('[data-status-summary]');
const updated = document.querySelector('[data-status-updated]');
const services = document.querySelector('[data-status-services]');

fetch('/status.json', { cache: 'no-store', headers: { Accept: 'application/json' } })
  .then((response) => {
    if (!response.ok) throw new Error('status unavailable');
    return response.json();
  })
  .then((status) => {
    summary.textContent = status.summary || 'Operational';
    summary.dataset.state = status.state || 'operational';
    updated.textContent = status.updated_at ? `Updated ${new Date(status.updated_at).toLocaleString()}` : '';
    services.replaceChildren();
    for (const service of status.services || []) {
      const row = document.createElement('li');
      const name = document.createElement('span');
      const state = document.createElement('strong');
      name.textContent = service.name;
      state.textContent = service.state;
      row.append(name, state);
      services.append(row);
    }
  })
  .catch(() => {
    summary.textContent = 'Status feed temporarily unavailable';
    summary.dataset.state = 'unknown';
    updated.textContent = 'This page is still online; the independent status snapshot could not be read.';
  });
