#!/usr/bin/env python3
"""Parts Library relevance + latency benchmark.

Scores the LOCAL search path only (zero network) so it is deterministic and
safe to run in CI. Two numbers matter:

  * relevance -- for each probe query, does an acceptable part appear, and how
    high? Scored with reciprocal rank so "right answer at #1" beats "right
    answer at #8", and a wrong-but-confident answer scores zero.
  * latency -- cold bootstrap, warm bootstrap, and per-query search time.

Usage:  python3 scripts/parts_bench.py [--json]

Run it against a scratch XDG_DATA_HOME to exercise the cold path honestly:
  rm -rf /tmp/pb && XDG_DATA_HOME=/tmp/pb python3 scripts/parts_bench.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (query, regex that an acceptable top result must match, regex that must NOT
# match -- the known-bad answer this query used to return).
PROBES: list[tuple[str, str, str | None]] = [
    ("m8 socket head",      r"socket head|shcs",            None),
    ("m8x1.25 shcs",        r"socket head|shcs",            None),
    ("bearing 608",         r"\b608\b",                     None),
    ("608zz",               r"\b608\b",                     None),
    ("deep groove 6203",    r"\b6203\b",                    None),
    ("stepper nema 17",     r"nema\s*17|stepper",           r"bearing"),
    ("nema17 motor",        r"nema\s*17|stepper",           r"bearing"),
    ("gt2 pulley 20 tooth", r"gt2|timing pulley",           r"bearing"),
    ("timing belt gt2",     r"gt2|timing belt",             r"bearing"),
    ("shaft coupler 5mm",   r"coupl",                       None),
    ("o-ring 2-014",        r"o-?ring",                     None),
    ("as568 014 viton",     r"o-?ring",                     None),
    ("aluminum 6061 bar",   r"6061",                        r"heatsink|heat sink"),
    ("1/4 aluminum plate",  r"6061|7075",                   r"heatsink|heat sink"),
    ("304 stainless tube",  r"304|stainless",               None),
    ("2020 extrusion",      r"2020|t-?slot",                None),
    ("t-slot 4040",         r"4040|t-?slot",                None),
    ("delrin rod",          r"delrin|acetal",               None),
    ("m5 washer",           r"washer",                      None),
    ("hex nut m6",          r"nut",                         None),
]

TOP_N = 10

def _text(rec: dict) -> str:
    parts = [
        str(rec.get(k) or "")
        for k in ("title", "name", "designation", "standard", "text", "description")
    ]
    return " ".join(parts).lower()


def run() -> dict:
    from services.parts.hub import PartsHub

    t0 = time.perf_counter()
    hub = PartsHub()
    hub.bootstrap()
    cold_ms = (time.perf_counter() - t0) * 1000.0
    status = hub.index_status()

    t0 = time.perf_counter()
    PartsHub().bootstrap()
    warm_ms = (time.perf_counter() - t0) * 1000.0

    rows, rr_total, hits, false_confident, lat = [], 0.0, 0, 0, []
    for query, want, forbid in PROBES:
        t0 = time.perf_counter()
        try:
            res = hub.search(query, limit=TOP_N, include_live=False)
        except Exception as exc:  # noqa: BLE001 -- a crashing probe scores zero
            res = []
            print(f"  !! {query!r} raised {exc!r}", file=sys.stderr)
        lat.append((time.perf_counter() - t0) * 1000.0)

        want_re = re.compile(want, re.I)
        forbid_re = re.compile(forbid, re.I) if forbid else None

        rank = next((i for i, r in enumerate(res, 1) if want_re.search(_text(r))), 0)
        rr = 1.0 / rank if rank else 0.0
        rr_total += rr
        hits += 1 if rank else 0

        # A "false confident" answer: we returned something, the right thing is
        # nowhere in the top N, and the top hit is the known-bad family.
        bad = bool(res) and not rank and forbid_re is not None \
            and bool(forbid_re.search(_text(res[0])))
        false_confident += 1 if bad else 0

        rows.append({
            "query": query, "n": len(res), "rank": rank, "rr": round(rr, 3),
            "false_confident": bad,
            "top": (_text(res[0])[:60] if res else ""),
        })

    n = len(PROBES)
    return {
        "index": {"cold_ms": round(cold_ms, 1), "warm_ms": round(warm_ms, 1),
                  "count": status.get("count"), "source": status.get("source")},
        "search_ms": {"mean": round(sum(lat) / len(lat), 2), "max": round(max(lat), 2)},
        "relevance": {
            "mrr": round(rr_total / n, 3),
            "hit_rate": round(hits / n, 3),
            "hits": hits, "total": n,
            "false_confident": false_confident,
        },
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    out = run()
    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    idx, rel = out["index"], out["relevance"]
    print(f"index   : {idx['count']} parts | cold {idx['cold_ms']} ms | "
          f"warm {idx['warm_ms']} ms | {idx['source']}")
    print(f"search  : mean {out['search_ms']['mean']} ms | max {out['search_ms']['max']} ms")
    print(f"relevance: MRR {rel['mrr']} | hit {rel['hits']}/{rel['total']} "
          f"({rel['hit_rate']:.0%}) | false-confident {rel['false_confident']}")
    print()
    for r in out["rows"]:
        mark = "OK " if r["rank"] == 1 else (f"#{r['rank']:<2}" if r["rank"] else "MISS")
        flag = " <-- confident junk" if r["false_confident"] else ""
        print(f"  {mark} {r['query']:<22} n={r['n']:<3} {r['top']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
