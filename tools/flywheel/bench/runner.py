"""Bench runner + CLI.

`run_bench(id)` prepares a bench's frozen dataset, scores it, writes a JSON report under
`training/benches/<id>/results/latest.json`, and returns the BenchResult. `--smoke` runs a
small held sample — that is what `scripts/gate.sh` stage 6 invokes (env-gated).

Each bench registers a `BenchDriver` describing how to prepare its dataset and run its
scorer. Datasets that are generated deterministically (regen-fix corruption corpus) are
built into a temp/work dir and verified against a frozen hash; gold-set datasets
(elicitation, vlm, ...) load committed fixtures under `training/benches/<id>/`.

Reproducibility: a report always records the `dataset_hash` it scored, so a second machine
re-running the same bench must produce the same hash (else the dataset drifted).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

from .schema import BenchResult

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BENCHES_DIR = os.path.join(_REPO_ROOT, "training", "benches")


@dataclass
class BenchDriver:
    bench_id: str
    # run(smoke: bool, workdir: str) -> BenchResult
    run: Callable[[bool, str], BenchResult]


# ---- regen-fix driver -------------------------------------------------------

def _run_regen_fix(smoke: bool, workdir: str) -> BenchResult:
    from .benches import regen_fix
    from .corpus.regen_corpus import build_corpus
    count = 25 if smoke else 500
    corpus_dir = os.path.join(workdir, "regen_fix_corpus")
    spec = build_corpus(corpus_dir, count=count, seed=1337)
    return regen_fix.run(corpus_dir, spec.hash, split="smoke" if smoke else "all")


REGISTRY: dict[str, BenchDriver] = {
    "regen_fix": BenchDriver("regen_fix", _run_regen_fix),
}


def register(driver: BenchDriver) -> None:
    """Benches wire themselves in here (append-only as each of the five lands)."""
    REGISTRY[driver.bench_id] = driver


def run_bench(bench_id: str, *, smoke: bool = False,
              write: bool = True, workdir: Optional[str] = None) -> BenchResult:
    if bench_id not in REGISTRY:
        raise KeyError(f"unknown bench {bench_id!r}; known: {sorted(REGISTRY)}")
    own_tmp = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix=f"bench_{bench_id}_")
    try:
        result = REGISTRY[bench_id].run(smoke, workdir)
    finally:
        pass
    if write:
        out_dir = os.path.join(BENCHES_DIR, bench_id, "results")
        os.makedirs(out_dir, exist_ok=True)
        name = "smoke_result.json" if smoke else "latest.json"
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(result.to_json())
    return result


def _summary_line(r: BenchResult) -> str:
    agg = r.aggregate
    key = next((k for k in ("pct_fixed", "coverage", "in_tol_rate", "gouge_free_rate",
                            "claim_accuracy", "mean_score") if k in agg), None)
    metric = f"{key}={agg.get(key):.3f}" if key else ""
    resid = r.residual.get("status", "?")
    bl = ",".join(f"{b.kind}:{b.status}" for b in r.baselines)
    return f"  {r.bench_id:<12} n={r.n_examples:<4} {metric:<18} residual={resid:<9} baselines[{bl}]"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run product-task eval benches (T-H8).")
    ap.add_argument("benches", nargs="*", help="bench ids (default: all registered)")
    ap.add_argument("--smoke", action="store_true", help="run small held sample (gate hook)")
    ap.add_argument("--all", action="store_true", help="run every registered bench")
    ap.add_argument("--no-write", action="store_true", help="do not write report files")
    args = ap.parse_args(argv)

    ids = list(REGISTRY) if (args.all or not args.benches) else args.benches
    rc = 0
    print(f"=== T-H8 benches ({'smoke' if args.smoke else 'full'}) ===")
    for bid in ids:
        try:
            r = run_bench(bid, smoke=args.smoke, write=not args.no_write)
            print(_summary_line(r))
        except Exception as e:  # a broken bench should not mask the others
            print(f"  {bid:<12} ERROR {type(e).__name__}: {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
