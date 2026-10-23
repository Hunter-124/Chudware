/**
 * Sitemap, derived rather than maintained.
 *
 * `@astrojs/sitemap` would do this, but it is a dependency and a build hook to
 * emit forty lines of XML for a static site whose routes are already knowable
 * from the filesystem and the two content collections. Deriving it means a new
 * page under `src/pages/` is listed the moment it exists — a hand-kept list is
 * a list that goes stale on the first page someone forgets.
 */
import type { APIRoute } from 'astro';
import { getCollection } from 'astro:content';

const SITE = 'https://chudware.com';

/**
 * Priority by depth, so the homepage and the top-level marketing pages outrank
 * individual doc entries. Crawlers treat this as a hint at best; it costs
 * nothing and it is at least honest about the shape of the site.
 */
function priority(route: string): string {
  if (route === '/') return '1.0';
  const depth = route.split('/').filter(Boolean).length;
  return depth <= 1 ? '0.8' : '0.6';
}

export const GET: APIRoute = async () => {
  // Eager is required: this runs at build time and needs the key set, not the
  // lazily-importable modules behind it.
  const files = Object.keys(import.meta.glob('./**/*.astro', { eager: false }));

  const routes = new Set<string>();
  for (const file of files) {
    // Dynamic segments are expanded from their collections below, and a 404 is
    // not a destination.
    if (file.includes('[') || file.endsWith('/404.astro')) continue;
    const route = file
      .replace(/^\./, '')
      .replace(/\/index\.astro$/, '/')
      .replace(/\.astro$/, '/');
    routes.add(route === '' ? '/' : route);
  }

  for (const entry of await getCollection('docs')) routes.add(`/docs/${entry.id}/`);
  for (const entry of await getCollection('changelog')) routes.add(`/changelog/${entry.id}/`);

  const body = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ...[...routes]
      .sort()
      .map((route) =>
        `  <url><loc>${SITE}${route}</loc><priority>${priority(route)}</priority></url>`,
      ),
    '</urlset>',
    '',
  ].join('\n');

  return new Response(body, {
    headers: { 'Content-Type': 'application/xml; charset=utf-8' },
  });
};
