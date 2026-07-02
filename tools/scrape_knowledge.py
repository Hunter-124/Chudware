#!/usr/bin/env python3
"""Draft-generator for engineering-knowledge cheatsheets.

Fetches source pages for a set of topics (fits, threads, bearings, materials, ...) and writes
RAW MARKDOWN DRAFTS to an output directory for a human to read, extract facts from, and hand-
curate into the committed cheatsheets under
`services/agent/prompts/shared/cheatsheets/`.

This script NEVER writes into that directory and NEVER overwrites a committed cheatsheet — it
only produces scratch drafts (default `/tmp/knowledge_drafts/<topic>.md`) that a person turns
into the real, table-first, hand-authored reference files.

Fetch strategy per URL:
  1. If `FIRECRAWL_API_KEY` is set, POST to the Firecrawl REST v1 `/scrape` endpoint
     (https://api.firecrawl.dev/v1/scrape) with `formats: ["markdown"]` and use the returned
     markdown directly (Firecrawl already does content extraction + boilerplate stripping).
  2. Otherwise, fall back to `urllib.request` with a browser-like User-Agent and a crude
     HTML tag-strip (no network dependency beyond the standard library).

Each fetched section in the draft is preceded by a header:

    ## <url>
    Source: <url> (retrieved YYYY-MM-DD)

so a human curator can trace every fact back to where it came from.

Usage:
    python3 tools/scrape_knowledge.py --topics fits,threads --out /tmp/knowledge_drafts
    python3 tools/scrape_knowledge.py                      # all built-in topics, default out dir
    FIRECRAWL_API_KEY=fc-... python3 tools/scrape_knowledge.py --topics bearings
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Built-in topic -> source URL list. Public, generally-accessible reference pages covering the
# facts the committed cheatsheets derive tables from. Curators may pass --topics to restrict
# which of these run, or extend this dict for new cheatsheet topics.
TOPIC_URLS: Dict[str, List[str]] = {
    "fits": [
        "https://en.wikipedia.org/wiki/ISO_metric_screw_thread",
        "https://en.wikipedia.org/wiki/Interference_fit",
        "https://en.wikipedia.org/wiki/Engineering_fit",
    ],
    "threads": [
        "https://en.wikipedia.org/wiki/ISO_metric_screw_thread",
        "https://en.wikipedia.org/wiki/Unified_Thread_Standard",
        "https://en.wikipedia.org/wiki/Tap_and_die",
    ],
    "bearings": [
        "https://en.wikipedia.org/wiki/Ball_bearing",
        "https://en.wikipedia.org/wiki/Rolling-element_bearing",
    ],
    "materials": [
        "https://en.wikipedia.org/wiki/6061_aluminium_alloy",
        "https://en.wikipedia.org/wiki/SAE_steel_grades",
        "https://en.wikipedia.org/wiki/Acrylonitrile_butadiene_styrene",
        "https://en.wikipedia.org/wiki/Polylactic_acid",
    ],
}

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def _today() -> str:
    return datetime.date.today().isoformat()


def _strip_html(html: str) -> str:
    """Crude tag-strip fallback: drop script/style blocks, tags, collapse whitespace."""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = _WS_RE.sub(" ", text)
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return _BLANKLINES_RE.sub("\n\n", text)


def _fetch_urllib(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    charset = resp.headers.get_content_charset() or "utf-8"
    html = raw.decode(charset, errors="replace")
    return _strip_html(html)


def _fetch_firecrawl(url: str, api_key: str) -> str:
    payload = json.dumps({"url": url, "formats": ["markdown"]}).encode("utf-8")
    req = urllib.request.Request(
        FIRECRAWL_SCRAPE_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="replace"))
    markdown = (body.get("data") or {}).get("markdown")
    if not markdown:
        raise ValueError(f"firecrawl response had no markdown for {url}: {body!r}")
    return markdown


def fetch_one(url: str, api_key: str | None) -> str:
    if api_key:
        try:
            return _fetch_firecrawl(url, api_key)
        except (urllib.error.URLError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
            print(f"  ! firecrawl failed for {url}: {exc}; falling back to urllib", file=sys.stderr)
    return _fetch_urllib(url)


def build_draft(topic: str, urls: List[str], api_key: str | None) -> str:
    today = _today()
    sections = [f"# DRAFT — {topic}\n", "(auto-generated scrape draft; hand-curate before use)\n"]
    for url in urls:
        print(f"  fetching {url} ...", file=sys.stderr)
        try:
            content = fetch_one(url, api_key)
        except Exception as exc:  # noqa: BLE001 - best-effort scraper, keep going
            content = f"(fetch failed: {exc})"
            print(f"  ! {url}: {exc}", file=sys.stderr)
        sections.append(f"## {url}")
        sections.append(f"Source: {url} (retrieved {today})\n")
        sections.append(content.strip())
        sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--topics",
        default=",".join(TOPIC_URLS.keys()),
        help=f"comma-separated topic names to scrape (default: all of {list(TOPIC_URLS)})",
    )
    parser.add_argument(
        "--out",
        default="/tmp/knowledge_drafts",
        help="output directory for draft markdown files (default: /tmp/knowledge_drafts)",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    committed_dir = (
        Path(__file__).resolve().parent.parent
        / "services" / "agent" / "prompts" / "shared" / "cheatsheets"
    )
    if out_dir.resolve() == committed_dir.resolve():
        parser.error(
            "refusing to write drafts into the committed cheatsheets directory "
            f"({committed_dir}); pick a scratch --out directory"
        )

    import os

    api_key = os.environ.get("FIRECRAWL_API_KEY") or None
    if api_key:
        print("Using Firecrawl REST v1 scrape endpoint.", file=sys.stderr)
    else:
        print("FIRECRAWL_API_KEY not set; falling back to urllib + tag-strip.", file=sys.stderr)

    requested = [t.strip() for t in args.topics.split(",") if t.strip()]
    unknown = [t for t in requested if t not in TOPIC_URLS]
    if unknown:
        parser.error(f"unknown topic(s): {unknown}; known topics: {list(TOPIC_URLS)}")

    for topic in requested:
        urls = TOPIC_URLS[topic]
        print(f"[{topic}] {len(urls)} source(s)", file=sys.stderr)
        draft = build_draft(topic, urls, api_key)
        dest = out_dir / f"{topic}.md"
        dest.write_text(draft, encoding="utf-8")
        print(f"[{topic}] wrote {dest} ({len(draft)} bytes)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
