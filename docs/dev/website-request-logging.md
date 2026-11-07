# chudware.com request logging

`website/functions/_middleware.js` records every request to chudware.com that runs
through Pages Functions, so we can see what scanners and bots probe. It never changes
the response; `_headers` rules still apply to static responses.

## Coverage

`website/public/_routes.json` runs the middleware on every path except the
content-hashed bundles `/_astro/*`, `/pagefind/*` and `/fonts/*`. Those paths are
excluded to save quota: Functions requests count against the Workers free plan's
100,000 requests per day (shared with any Workers on the account), while static
requests are free. Probes under the excluded prefixes are not logged.

## Live tail

Each request prints one JSON line containing method, host, path, query, status,
duration, all request headers, the first 3 KB of any non-GET body, and Cloudflare's
full `request.cf` object (ASN and organisation, geography, colo, TLS version, cipher
and ClientHello fingerprints, bot score fields).

```sh
cd website
npx wrangler login
npx wrangler pages deployment tail --project-name chudware-site --format json
```

The dashboard's real-time logs show the same lines (Workers & Pages → chudware-site →
the production deployment → Functions). Pages does not keep these lines.

## Retained log (Analytics Engine)

Every record is also written to the Workers Analytics Engine dataset
`chudware_requests` (binding `REQUEST_LOG`, declared in `website/wrangler.toml`).
Rows are kept for three months. `index1` is the client IP. Blobs are clipped by byte
length to fit the 16 KB per-row limit.

| Column | Value |
|---|---|
| `blob1` | method |
| `blob2` | path |
| `blob3` | query string, including `?` |
| `blob4` | client IP (`cf-connecting-ip`) |
| `blob5` | country |
| `blob6` | AS organisation |
| `blob7` | User-Agent |
| `blob8` | Referer |
| `blob9` | host |
| `blob10` | `cf-ray` |
| `blob11` | Cloudflare colo |
| `blob12` | HTTP protocol |
| `blob13` | TLS version |
| `blob14` | Accept |
| `blob15` | Accept-Language |
| `blob16` | all request headers as JSON |
| `blob17` | body excerpt (non-GET) |
| `blob18` | `request.cf` as JSON |
| `blob19` | response Content-Type |
| `blob20` | error thrown while serving, if any |
| `double1` | response status |
| `double2` | duration in ms |
| `double3` | ASN |
| `double4` | request Content-Length |
| `double5` | client TCP RTT in ms |

Query with the SQL API. The token needs the *Account Analytics: Read* permission.
Analytics Engine samples under heavy load, so weight counts by `_sample_interval`:

```sh
curl -s "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/analytics_engine/sql" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  --data "SELECT blob2 AS path, double1 AS status, SUM(_sample_interval) AS hits
          FROM chudware_requests
          WHERE timestamp > NOW() - INTERVAL '1' DAY AND double1 >= 400
          GROUP BY path, status ORDER BY hits DESC LIMIT 100"
```

Other useful views: `GROUP BY blob4, blob6` lists the noisiest clients and networks.
`WHERE blob1 != 'GET'` with `blob17` shows exploit payloads.
