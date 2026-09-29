"""Generate MFCAD++ parts: python -m training.viewpoint.datasets.mfcadpp_gen --count N."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Optional

from . import default_dir
from .runner import DEFAULT_TIMEOUT_S, generate


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--count", type=int, default=100, help="number of seeds")
    ap.add_argument("--seed", type=int, default=0, help="first seed")
    ap.add_argument("--out", type=Path, default=None,
                    help="output dir (default $CHUDWARE_VIEWNET_DATA/raw/mfcadpp_gen)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help="seconds per part before its worker is killed")
    args = ap.parse_args(argv)
    t0 = time.time()
    res = generate(range(args.seed, args.seed + args.count), args.out or default_dir(),
                   workers=args.workers, timeout_s=args.timeout)
    ok = sum(1 for r in res.values() if r["ok"])
    reasons: dict = {}
    for r in res.values():
        if not r["ok"]:
            key = str(r["reason"]).split(":", 1)[0]
            reasons[key] = reasons.get(key, 0) + 1
    print(json.dumps({"seeds": len(res), "ok": ok, "failed": len(res) - ok,
                      "failure_reasons": reasons, "wall_s": round(time.time() - t0, 1)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
