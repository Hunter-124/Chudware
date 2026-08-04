"""Render the T-W11 comparison + gap ledger from persisted leg checkpoints.

The matrix writes one ``leg_*.json`` per completed cell precisely so a report
can be produced from whatever actually finished — a spend cap, a timeout or an
interrupted run must never cost the rows the quota already paid for.

    python -m tools.flywheel.bench.benches.advisor_e2e_report [out.md]
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

from . import advisor_e2e as A


def load_legs(results_dir: Path) -> list[A.LegResult]:
    legs: list[A.LegResult] = []
    for path in sorted(glob.glob(str(results_dir / "leg_*.json"))):
        raw = json.loads(Path(path).read_text())
        leg = A.LegResult(
            loop=raw["loop"], tier=raw["tier"], model=raw.get("model", ""),
            seed=raw["seed"], run_id=raw["run_id"], doc_id=raw.get("doc_id", ""),
            ok=raw.get("ok", False), stop_reason=raw.get("stop_reason", ""),
            error=raw.get("error", ""), elapsed_s=raw.get("elapsed_s", 0.0),
            iterations=raw.get("iterations", 0),
            advisories=raw.get("advisories", []),
            usage=raw.get("usage", {}), scores=raw.get("scores", {}),
        )
        leg.tool_calls = [A.ToolCall(**c) for c in raw.get("tool_calls", [])]
        legs.append(leg)
    # seed, then tier, then baseline before advisory — reading order of the matrix
    legs.sort(key=lambda l: (l.seed, l.tier, l.loop != "baseline"))
    return legs


def main(argv: list[str]) -> int:
    results = Path(A.BENCHES_DIR) / A.BENCH_ID / "results"
    legs = load_legs(results)
    if not legs:
        print(f"no leg checkpoints under {results}")
        return 1

    try:
        from .. import spend_guard as sg
        spend = "\n".join(r.summary() for r in sg.read_all().values())
    except Exception as exc:  # noqa: BLE001
        spend = f"(spend meter unavailable: {exc})"

    ledger = A.gap_ledger(legs)
    md = A.render_markdown(legs, ledger, spend=spend)

    by_loop = A._by_loop(legs)
    extra = ["\n## Per-loop totals\n",
             "| loop | legs | mean score | tool calls | tool failures | "
             "fail rate | tokens | advisories |",
             "|---|---|---|---|---|---|---|---|"]
    for loop, row in sorted(by_loop.items()):
        rate = (row["tool_failures"] / row["tool_calls"]) if row["tool_calls"] else 0.0
        extra.append(
            f"| {loop} | {row['legs']} | {row['mean_score']:.2f} | "
            f"{row['tool_calls']} | {row['tool_failures']} | {rate:.1%} | "
            f"{row['tokens']:,} | {row['advisories']} |")
    md += "\n".join(extra) + "\n"

    out = Path(argv[1]) if len(argv) > 1 else (results / "comparison.md")
    out.write_text(md)
    print(md)
    print(f"\n[written] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
