const cards = [...document.querySelectorAll('[data-platform]')];
const detected = document.querySelector('[data-detected-platform]');
const releases = document.querySelector('[data-release-list]');
const manifestUrl = 'https://app.chudware.com/api/releases/manifest';

const platform = (() => {
  const value = `${navigator.userAgentData?.platform || ''} ${navigator.platform || ''} ${navigator.userAgent}`.toLowerCase();
  if (value.includes('win')) return 'windows';
  if (value.includes('mac')) return 'macos';
  if (value.includes('linux')) return 'linux';
  return '';
})();

if (platform) {
  detected.textContent = `We detected ${platform === 'macos' ? 'macOS' : platform[0].toUpperCase() + platform.slice(1)}.`;
  for (const card of cards) card.toggleAttribute('data-recommended', card.dataset.platform === platform);
}

fetch(manifestUrl, { headers: { Accept: 'application/json' } })
  .then((response) => {
    if (!response.ok) throw new Error('release manifest unavailable');
    return response.json();
  })
  .then((manifest) => {
    const builds = Array.isArray(manifest.builds) ? manifest.builds : [];
    releases.replaceChildren();
    for (const build of builds) {
      const row = document.createElement('article');
      const title = document.createElement('h3');
      const checksum = document.createElement('code');
      const link = document.createElement('a');
      title.textContent = `${build.platform} ${build.architecture || ''}`.trim();
      checksum.textContent = `SHA-256 ${build.sha256}`;
      link.href = build.url;
      link.textContent = 'Download build';
      row.append(title, checksum, link);
      releases.append(row);
    }
  })
  .catch(() => {
    const link = document.createElement('a');
    link.href = manifestUrl;
    link.textContent = 'Open the public release manifest';
    releases.replaceChildren(link);
  });
