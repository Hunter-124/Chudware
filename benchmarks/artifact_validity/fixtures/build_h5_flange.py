"""Regenerate the H5 input fixture ``h5_flange.step+`` with the real CAD tools (no model).

    CHUDWARE_ENGINE_DAEMON=0 .venv-mcp/bin/python -X utf8 \
        -m benchmarks.artifact_validity.fixtures.build_h5_flange

The M2 reference flange: a Ø100 × 12 ``cylinder`` (z -6..6), a Ø40 through ``hole`` and a
6 × Ø9 ``bolt_circle`` on PCD 75 starting at 0° (+X). Feature ids are f1..f3 in the order of
:func:`ops`. After regenerating, update ``H5_FIXTURE_SHA256`` in ``tasks/hard.py`` (the
grader refuses to grade against a fixture whose bytes it did not declare).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Callable

os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"

from benchmarks.artifact_validity.fixtures.build_c2_bushing import _tools  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "h5_flange.step+"


def ops() -> list[dict]:
    """f1 Disk (cylinder r50 h12), f2 Bore (hole Ø40 through), f3 Bolts (bolt_circle
    6 × Ø9 on PCD 75, start 0°) — both holes entered from the top face."""
    top = {"face": {"of": "face", "side": "top"}}
    return [
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Disk",
                                          "params": {"radius": 50, "height": 12}}},
        {"op": "add_feature", "feature": {
            "type": "hole", "name": "Bore",
            "params": {"style": "simple", "diameter": 40, "end": "through_all"}, "refs": top}},
        {"op": "add_feature", "feature": {
            "type": "bolt_circle", "name": "Bolts",
            "params": {"style": "simple", "diameter": 9, "count": 6, "pcd": 75,
                       "start_angle": 0, "end": "through_all"}, "refs": top}},
    ]


def build(tools: dict[str, Callable], path: Path) -> Path:
    """Build the flange with ``new_part``/``add_features``/``save_part`` and save to ``path``."""
    doc = json.loads(tools["new_part"](name="h5_flange", fresh=True))["doc_id"]
    built = json.loads(tools["add_features"](doc_id=doc, ops=ops()))
    if not built.get("ok") or built.get("body_count") != 1:
        raise RuntimeError(f"flange build failed: {json.dumps(built)[:2000]}")
    saved = json.loads(tools["save_part"](path=str(path), doc_id=doc))
    if not saved.get("ok"):
        raise RuntimeError(f"flange save failed: {saved}")
    tools["close_part"](doc_id=doc, discard=True)
    return path


if __name__ == "__main__":
    out = build(_tools(), FIXTURE)
    print(f"{out}  sha256={hashlib.sha256(out.read_bytes()).hexdigest()}")
    sys.exit(0)
