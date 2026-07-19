#!/usr/bin/env python3
"""Firecrawl key-pool CLI — search/scrape with automatic API-key rotation.

Thin wrapper over `engine/research/firecrawl_pool.py` so anything that can spawn a
process (the omp custom tool, shell one-liners, other agents) gets the same rotating
key pool as the in-app research tools. Keys come from FIRECRAWL_API_KEYS /
FIRECRAWL_API_KEY / config.json `keys.firecrawl`; cooldowns are shared via the pool
state file, so a key that 429s here is skipped by the CAD app too.

Usage:
    python3 tools/firecrawl_pool.py search "ISO 286-2 hole basis" [--limit 10] [--json]
    python3 tools/firecrawl_pool.py scrape https://example.org/spec [--json]
    python3 tools/firecrawl_pool.py keys          # pool status (masked keys)

Exit codes: 0 success, 2 pool exhausted / API error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from engine.research import firecrawl_pool  # noqa: E402


def _cmd_search(args: argparse.Namespace) -> int:
    body = firecrawl_pool.search(args.query, limit=args.limit)
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    results = ((body.get("data") or {}).get("web")
               or body.get("data")
               or [])
    if isinstance(results, dict):
        results = results.get("web") or []
    for i, item in enumerate(results, 1):
        title = item.get("title") or "(no title)"
        url = item.get("url") or ""
        desc = (item.get("description") or "").strip().replace("\n", " ")
        print(f"[{i}] {title}\n    {url}")
        if desc:
            print(f"    {desc[:240]}")
    if not results:
        print("(no results)")
    return 0


def _cmd_scrape(args: argparse.Namespace) -> int:
    markdown, meta = firecrawl_pool.scrape(args.url)
    if args.json:
        print(json.dumps({"markdown": markdown, "metadata": meta}, indent=2))
    else:
        title = meta.get("title")
        if title:
            print(f"# {title}\n")
        print(markdown)
    return 0


def _cmd_keys(args: argparse.Namespace) -> int:
    rows = firecrawl_pool.pool_status()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no Firecrawl keys configured "
              "(FIRECRAWL_API_KEYS / FIRECRAWL_API_KEY / config.json keys.firecrawl)")
        return 2
    for row in rows:
        state = "ready" if row["ready"] else f"cooling {row['cooldown_remaining_s']}s ({row['reason']})"
        print(f"[{row['index']:>2}] {row['key']:<16} {state}")
    ready = sum(1 for r in rows if r["ready"])
    print(f"{ready}/{len(rows)} keys ready")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search", help="web search via /v2/search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(fn=_cmd_search)

    p_scrape = sub.add_parser("scrape", help="scrape a URL via /v1/scrape")
    p_scrape.add_argument("url")
    p_scrape.add_argument("--json", action="store_true")
    p_scrape.set_defaults(fn=_cmd_scrape)

    p_keys = sub.add_parser("keys", help="show pool status")
    p_keys.add_argument("--json", action="store_true")
    p_keys.set_defaults(fn=_cmd_keys)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (firecrawl_pool.PoolExhaustedError, firecrawl_pool.FirecrawlAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
