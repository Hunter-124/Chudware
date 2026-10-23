const form = document.querySelector('[data-doc-search]');
const input = form?.querySelector('input');
const results = document.querySelector('[data-search-results]');

if (form && input && results) {
  let pagefind;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const query = input.value.trim();
    results.replaceChildren();
    if (!query) return;

    if (!pagefind) {
      const module = await import('/pagefind/pagefind.js');
      pagefind = module.createInstance({ basePath: '/pagefind/', noWorker: true });
      await pagefind.init();
    }
    const search = await pagefind.search(query);
    const entries = await Promise.all(search.results.slice(0, 8).map((item) => item.data()));

    if (!entries.length) {
      const empty = document.createElement('p');
      empty.textContent = 'No documentation matched that search.';
      results.append(empty);
      return;
    }

    const list = document.createElement('ul');
    for (const entry of entries) {
      const item = document.createElement('li');
      const link = document.createElement('a');
      const summary = document.createElement('p');
      link.href = entry.url;
      link.textContent = entry.meta.title || entry.url;
      summary.textContent = entry.excerpt.replace(/<[^>]*>/g, '');
      item.append(link, summary);
      list.append(item);
    }
    results.append(list);
  });
}
