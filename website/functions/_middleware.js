/**
 * Logs every Functions-routed request to chudware.com so scanner and bot traffic is
 * visible: path, query, all headers, Cloudflare's client metadata (IP, ASN, TLS,
 * geography), a body excerpt for non-GET requests, and the response status.
 *
 * Two sinks:
 * - console: one JSON line per request with the complete record, for
 *   `wrangler pages deployment tail --project-name chudware-site --format json`
 *   or the dashboard's real-time logs. Pages keeps no history of these.
 * - Analytics Engine (`REQUEST_LOG` binding, dataset `chudware_requests`): kept for
 *   three months and queryable with SQL. Column layout is documented in
 *   docs/dev/website-request-logging.md; keep the two in step.
 *
 * The middleware never alters the response. Logging runs in waitUntil, so it adds no
 * latency, and a logging failure cannot fail the request. Static responses still get
 * their `_headers` rules because those are applied before middleware sees them.
 */

const BODY_BYTES = 3072;
const HEADERS_BYTES = 5120;
const CF_BYTES = 2048;
const FIELD_BYTES = 512;
const PATH_BYTES = 1024;
const INDEX_BYTES = 96;

const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: false });

// Analytics Engine caps all blobs of one data point at 16 KB, measured in bytes.
function clip(value, limit = FIELD_BYTES) {
  const text = value == null ? '' : String(value);
  const bytes = encoder.encode(text);
  return bytes.length <= limit ? text : decoder.decode(bytes.subarray(0, limit));
}

async function readBodyExcerpt(request) {
  if (request.method === 'GET' || request.method === 'HEAD' || !request.body) return '';
  const reader = request.clone().body.getReader();
  const excerpt = new Uint8Array(BODY_BYTES);
  let filled = 0;
  try {
    while (filled < BODY_BYTES) {
      const { done, value } = await reader.read();
      if (done) break;
      const take = Math.min(value.byteLength, BODY_BYTES - filled);
      excerpt.set(value.subarray(0, take), filled);
      filled += take;
    }
  } finally {
    reader.cancel().catch(() => {});
  }
  return decoder.decode(excerpt.subarray(0, filled));
}

function writeDataPoint(dataset, record) {
  const cf = record.cf;
  dataset.writeDataPoint({
    indexes: [clip(record.ip, INDEX_BYTES)],
    blobs: [
      clip(record.method, 16),
      clip(record.path, PATH_BYTES),
      clip(record.query, PATH_BYTES),
      clip(record.ip, 64),
      clip(cf.country, 8),
      clip(cf.asOrganization, 128),
      clip(record.headers['user-agent']),
      clip(record.headers.referer),
      clip(record.host, 256),
      clip(record.ray, 64),
      clip(cf.colo, 8),
      clip(cf.httpProtocol, 16),
      clip(cf.tlsVersion, 16),
      clip(record.headers.accept, 256),
      clip(record.headers['accept-language'], 128),
      clip(JSON.stringify(record.headers), HEADERS_BYTES),
      clip(record.body, BODY_BYTES),
      clip(JSON.stringify(cf), CF_BYTES),
      clip(record.responseType, 128),
      clip(record.error, 256),
    ],
    doubles: [
      record.status,
      record.durationMs,
      Number(cf.asn) || 0,
      Number(record.headers['content-length']) || 0,
      Number(cf.clientTcpRtt) || 0,
    ],
  });
}

export async function onRequest(context) {
  const { request, env } = context;
  const started = Date.now();
  const body = readBodyExcerpt(request).catch((err) => `<<body read failed: ${err}>>`);

  let response;
  let failure;
  try {
    response = await context.next();
  } catch (err) {
    failure = err;
  }
  const durationMs = Date.now() - started;

  context.waitUntil(
    body.then((bodyExcerpt) => {
      const url = new URL(request.url);
      const headers = Object.fromEntries(request.headers);
      const record = {
        kind: 'http',
        ts: new Date(started).toISOString(),
        ray: headers['cf-ray'] ?? '',
        ip: headers['cf-connecting-ip'] ?? '',
        method: request.method,
        host: url.host,
        path: url.pathname,
        query: url.search,
        status: response ? response.status : 500,
        responseType: response?.headers.get('content-type') ?? '',
        durationMs,
        error: failure ? String(failure?.stack ?? failure) : '',
        headers,
        body: bodyExcerpt,
        cf: request.cf ?? {},
      };
      console.log(JSON.stringify(record));
      if (env.REQUEST_LOG) {
        try {
          writeDataPoint(env.REQUEST_LOG, record);
        } catch (err) {
          console.error(`request log write failed: ${err}`);
        }
      }
    }),
  );

  if (failure) throw failure;
  return response;
}
