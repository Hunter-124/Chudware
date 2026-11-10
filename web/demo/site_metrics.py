"""chudware.com (the Pages site) metrics from Cloudflare Analytics Engine.

The website middleware already logs every Functions-routed request to the
``chudware_requests`` dataset (docs/dev/website-request-logging.md). This reads
it back with the Analytics Engine SQL API using a read-only account token, so
/admin can show both halves of the webservice side by side with the same
honest pageview definition: a GET that returned 200 for a document path --
not an asset, feed, or scanner probe.

Results are cached for five minutes; any failure degrades to
``{"configured": true, "error": ...}`` and never breaks the demo metrics.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

CACHE_SECONDS = 300.0
_ASSET = ("blob2 NOT LIKE '/_astro/%' AND blob2 NOT LIKE '/pagefind/%' "
          "AND blob2 NOT LIKE '/fonts/%' AND blob2 NOT LIKE '%.ico' "
          "AND blob2 NOT LIKE '%.txt' AND blob2 NOT LIKE '%.xml' "
          "AND blob2 NOT LIKE '%.png' AND blob2 NOT LIKE '%.svg' "
          "AND blob2 NOT LIKE '%.js' AND blob2 NOT LIKE '%.css'")
_PAGEVIEW = f"blob1 = 'GET' AND double1 = 200 AND {_ASSET}"


class SiteMetrics:
    def __init__(self, account_id: str, token: str, *,
                 clock: Callable[[], float] = time.time,
                 http: httpx.AsyncClient | None = None):
        self.account_id, self.token = account_id, token
        self.clock = clock
        self._http = http
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.token)

    async def _query(self, sql: str) -> list[dict[str, Any]]:
        client = self._http or httpx.AsyncClient(timeout=20.0, trust_env=False)
        try:
            resp = await client.post(
                f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/analytics_engine/sql",
                headers={"Authorization": f"Bearer {self.token}"},
                content=sql.encode("utf-8"))
            resp.raise_for_status()
            payload = resp.json()
        finally:
            if self._http is None:
                await client.aclose()
        return payload.get("data", []) if isinstance(payload, dict) else []

    async def snapshot(self) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False}
        now = self.clock()
        if self._cache is not None and now - self._cache_at < CACHE_SECONDS:
            return self._cache
        try:
            daily = await self._query(
                "SELECT toDate(timestamp) AS day, count() AS requests, "
                f"sumIf(1, {_PAGEVIEW}) AS pageviews, "
                "countIf(double1 = 404) AS not_found, "
                f"uniqExact(blob4) AS unique_ips FROM chudware_requests "
                "WHERE timestamp > now() - INTERVAL '30' DAY GROUP BY day ORDER BY day")
            paths = await self._query(
                "SELECT blob2 AS path, count() AS hits FROM chudware_requests "
                f"WHERE timestamp > now() - INTERVAL '7' DAY AND {_PAGEVIEW} "
                "GROUP BY path ORDER BY hits DESC LIMIT 15")
            statuses = await self._query(
                "SELECT toString(double1) AS status, count() AS hits FROM chudware_requests "
                "WHERE timestamp > now() - INTERVAL '7' DAY GROUP BY status ORDER BY hits DESC")
        except (httpx.HTTPError, ValueError) as exc:
            return {"configured": True, "error": type(exc).__name__}
        rows = [{"day": str(r.get("day", "")), "requests": int(r.get("requests", 0)),
                 "pageviews": int(r.get("pageviews", 0)), "not_found": int(r.get("not_found", 0)),
                 "unique_ips": int(r.get("unique_ips", 0))} for r in daily]
        today = rows[-1] if rows else {"requests": 0, "pageviews": 0, "not_found": 0, "unique_ips": 0}
        self._cache = {"configured": True, "today": today, "daily": rows,
                       "top_paths_7d": [{"path": r.get("path", ""), "hits": int(r.get("hits", 0))}
                                        for r in paths],
                       "statuses_7d": {str(r.get("status", "")): int(r.get("hits", 0)) for r in statuses}}
        self._cache_at = now
        return self._cache
