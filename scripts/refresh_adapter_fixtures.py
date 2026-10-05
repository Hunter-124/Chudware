#!/usr/bin/env python3
"""Refresh captured search-page fixtures for the browser source adapters.

For each registered site (or a single ``--site``), run the site's real
``adapter.search(canary)`` with ``browser_fetch`` wrapped to capture the
rendered HTML, save it to ``services/browser/adapters/fixtures/<site>_search.html``,
and print how many hits the adapter's parser extracted from that exact page —
a fixture whose markup drifted past the parser is caught here, at refresh
time, instead of silently in production.

Dev-only: needs the explicitly provisioned browser backend selected by
``services.browser.driver.headless_client`` and a user-owned saved site profile.
Captured HTML is intentionally gitignored.

Usage:
    python3 scripts/refresh_adapter_fixtures.py
    python3 scripts/refresh_adapter_fixtures.py --site grainger
    python3 scripts/refresh_adapter_fixtures.py --site grainger --query "1/2 in end mill"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIXTURES_DIR = _ROOT / "services" / "browser" / "adapters" / "fixtures"


def refresh_site(site: str, query: str | None = None) -> dict:
    """Capture ``site``'s canary search page + report parser hit count. Never raises."""
    from services.browser import session
    from services.browser.adapters import get_adapter
    from services.browser.health import CANARY_QUERIES

    adapter = get_adapter(site)
    if adapter is None:
        return {"site": site, "ok": False, "error": f"no adapter registered for '{site}'"}
    canary = query or CANARY_QUERIES.get(site, site)

    captured: dict = {}
    real_fetch = session.browser_fetch

    def capturing_fetch(url, fetch_site, **kw):
        result = real_fetch(url, fetch_site, **kw)
        if result.get("ok") and result.get("content"):
            captured["url"] = url
            captured["content"] = result["content"]
        return result

    session.browser_fetch = capturing_fetch
    try:
        hits = adapter.search(canary)
    except Exception as exc:  # noqa: BLE001 — a dev report, not a crash
        return {"site": site, "ok": False, "error": str(exc) or type(exc).__name__}
    finally:
        session.browser_fetch = real_fetch

    report: dict = {"site": site, "query": canary}
    if not captured:
        failure = next((h for h in hits if isinstance(h, dict) and h.get("ok") is False), None)
        report.update(ok=False, error=(failure or {}).get("reason", "no page captured"),
                      hint=(failure or {}).get("hint"))
        return report

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    dest = FIXTURES_DIR / f"{site}_search.html"
    dest.write_text(captured["content"], encoding="utf-8")

    good = [h for h in hits if isinstance(h, dict) and h.get("ok", True)
            and h.get("title") and h.get("url")]
    degraded = [h for h in hits if isinstance(h, dict) and h.get("ok") is False]
    report.update(ok=bool(good), fixture=str(dest), source_url=captured["url"],
                  bytes=len(captured["content"]), hit_count=len(good),
                  degraded=[h.get("reason") for h in degraded] or None)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="refresh a single site (default: every registered site)")
    parser.add_argument("--query", help="override the canary query (only with --site)")
    args = parser.parse_args(argv)

    from services.browser.adapters import all_adapters

    sites = [args.site] if args.site else sorted(a.key for a in all_adapters())
    if args.query and not args.site:
        parser.error("--query requires --site")

    any_failed = False
    for site in sites:
        report = refresh_site(site, query=args.query)
        if report.get("ok"):
            print(f"[ok]   {site:<18} {report['hit_count']:>3} hits  "
                  f"({report['bytes']} bytes -> {report['fixture']})")
        else:
            any_failed = True
            detail = report.get("error") or "0 hits parsed"
            if report.get("fixture"):
                detail += f" (page saved to {report['fixture']} for inspection)"
            print(f"[FAIL] {site:<18} {detail}")
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
