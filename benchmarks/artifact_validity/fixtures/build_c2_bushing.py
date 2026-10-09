"""Regenerate the C2 input fixture ``c2_bushing.step+`` with the real CAD tools (no model).

    CHUDWARE_ENGINE_DAEMON=0 .venv-mcp/bin/python -X utf8 \
        -m benchmarks.artifact_validity.fixtures.build_c2_bushing

A flanged bushing with a sketch-driven flange and hub (each sketch carries a driving
diameter relation), a Ø20 through bore as a ``hole`` feature, and a 4 × Ø6.6 bolt circle on
PCD 52. Feature ids are f1..f6 in the order of :func:`ops`. After regenerating, update
``C2_FIXTURE_SHA256`` in ``tasks/core.py`` (the grader refuses to grade against a fixture
whose bytes it did not declare).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Callable

os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"

FIXTURE = Path(__file__).resolve().parent / "c2_bushing.step+"


def _dim(v: float) -> dict:
    return {"value": v, "expr": None}


def _circle_sketch(name: str, radius: float, offset: float | None) -> dict:
    params: dict = {
        "entities": [{"id": "e1", "type": "circle", "center": [0, 0], "radius": radius}],
        "constraints": [{"id": "c1", "type": "diameter", "refs": ["e1"],
                         "value": _dim(2 * radius), "driven": False}],
    }
    if offset is not None:
        params["offset"] = _dim(offset)
    return {"op": "add_feature", "feature": {"type": "sketch", "name": name, "params": params,
                                             "refs": {"plane": "plane.top"}}}


def _extrude(name: str, sketch: str, depth: float) -> dict:
    return {"op": "add_feature", "feature": {
        "type": "extrude", "name": name,
        "params": {"mode": "boss", "end": "blind", "depth": _dim(depth)},
        "refs": {"profile": [{"kind": "region", "sketch": sketch, "centroid": [0, 0],
                              "hint_index": 0}]}}}


def ops() -> list[dict]:
    """f1 FlangeSketch, f2 Flange (Ø70 × 8, z 0..8), f3 HubSketch, f4 Hub (Ø36, z 8..30),
    f5 Bore (hole Ø20 through), f6 BoltHoles (bolt_circle 4 × Ø6.6 on PCD 52, start 0°)."""
    return [
        _circle_sketch("FlangeSketch", 35.0, None),
        _extrude("Flange", "$0", 8.0),
        _circle_sketch("HubSketch", 18.0, 8.0),
        _extrude("Hub", "$2", 22.0),
        {"op": "add_feature", "feature": {
            "type": "hole", "name": "Bore",
            "params": {"style": "simple", "end": "through_all", "diameter": _dim(20.0)},
            "refs": {"face": {"of": "face", "side": "top", "pick": "smallest"}}}},
        {"op": "add_feature", "feature": {
            "type": "bolt_circle", "name": "BoltHoles",
            "params": {"style": "simple", "end": "through_all", "diameter": _dim(6.6),
                       "count": 4, "pcd": _dim(52.0), "start_angle": 0},
            "refs": {"face": {"of": "face", "side": "top", "pick": "largest"}}}},
    ]


def build(tools: dict[str, Callable], path: Path) -> Path:
    """Build the bushing with ``new_part``/``add_features``/``save_part`` and save to ``path``."""
    doc = json.loads(tools["new_part"](name="c2_bushing", fresh=True))["doc_id"]
    built = json.loads(tools["add_features"](doc_id=doc, ops=ops()))
    if not built.get("ok") or built.get("body_count") != 1:
        raise RuntimeError(f"bushing build failed: {json.dumps(built)[:2000]}")
    saved = json.loads(tools["save_part"](path=str(path), doc_id=doc))
    if not saved.get("ok"):
        raise RuntimeError(f"bushing save failed: {saved}")
    tools["close_part"](doc_id=doc, discard=True)
    return path


def _tools() -> dict[str, Callable]:
    from services.mcp.cad_tools import register_cad_tools  # noqa: PLC0415

    class _Capture:
        def __init__(self) -> None:
            self.tools: dict[str, Callable] = {}

        def tool(self, *_a, **_k):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    m = _Capture()
    register_cad_tools(m)
    return m.tools


if __name__ == "__main__":
    out = build(_tools(), FIXTURE)
    print(f"{out}  sha256={hashlib.sha256(out.read_bytes()).hexdigest()}")
    sys.exit(0)
