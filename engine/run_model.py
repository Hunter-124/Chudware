"""CLI: build a model from a .py file (or a bundled sample) and render it.

    python -m engine.run_model engine/samples/mounting_plate.py
    python -m engine.run_model mounting_plate          # bundled sample by name

Writes STEP/STL + multi-view PNGs + a 2x2 contact sheet into
``%APPDATA%\\AI-CAD\\cache\\<name>`` and prints a JSON summary.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from engine import paths, render
from engine.kernel import run_code


def _resolve(arg: str) -> tuple[str, str]:
    p = Path(arg)
    if p.exists():
        return p.read_text(encoding="utf-8"), p.stem
    sample = Path(__file__).parent / "samples" / f"{arg}.py"
    if sample.exists():
        return sample.read_text(encoding="utf-8"), sample.stem
    raise SystemExit(f"Not found: {arg} (no file, no sample named '{arg}')")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    code, name = _resolve(argv[0])
    out_dir = paths.model_dir(name)

    res = run_code(code, out_dir, name)
    summary = json.loads(res.to_json())

    if res.ok and res.stl_path:
        summary["screenshots"] = render.render_views(res.stl_path, out_dir, prefix=name)
        summary["contact_sheet"] = render.render_grid(
            res.stl_path, out_dir / f"{name}_grid.png"
        )

    print(json.dumps(summary, indent=2))
    print(f"\noutput dir: {out_dir}", file=sys.stderr)
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
