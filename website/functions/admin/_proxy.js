/**
 * Shared handler for chudware.com/admin and /admin/*: proxy to the demo front
 * door on app.chudware.com. This path is protected by a Cloudflare Access
 * application (one-time email code, allowlisted to the operator). Access
 * authenticates at the edge and injects a signed JWT
 * (`Cf-Access-Jwt-Assertion`); this proxy forwards the request — JWT included —
 * to the front door, which verifies the signature and the email claim itself
 * before serving anything (see web/demo/access.py). The JWT is only ever a
 * server-to-server header here: it is not exposed to page JavaScript.
 *
 * Everything else (method, body) is passed through untouched, and the front
 * door's response is returned as-is. If the front door is down this returns
 * 502 rather than falling through to the static site.
 */

const ORIGIN = 'https://app.chudware.com';

export async function onRequest(context) {
  const { request } = context;
  const url = new URL(request.url);
  const target = ORIGIN + url.pathname + url.search;

  const headers = new Headers(request.headers);
  headers.delete('host');

  const init = { method: request.method, headers, redirect: 'manual' };
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    init.body = request.body;
    init.duplex = 'half'; // required for streaming bodies in Workers
  }

  let response;
  try {
    response = await fetch(target, init);
  } catch (err) {
    return new Response('admin origin unavailable', {
      status: 502,
      headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' },
    });
  }
  const out = new Headers(response.headers);
  out.delete('content-length');
  out.delete('transfer-encoding');
  return new Response(response.body, { status: response.status, headers: out });
}
