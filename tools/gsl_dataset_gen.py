#!/usr/bin/env python3
"""GSL synthetic dataset factory — labels-for-free training/eval corpus generator.

Per docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md §3.4: before any learned model is justified,
build a deterministic-engine-labelled corpus and measure where deterministic recognition
actually fails. This script builds that corpus: seven parametric family templates, each a
function ``(rng: random.Random) -> list[op dict]`` that emits a randomized, VALID
aicad.part/1 ops batch (the same op vocabulary documented in
``services/agent/prompts/shared/ops_cheatsheet.md`` and built with the ``dim``/``feat``
helpers from ``services/mcp/core_bridge.py``).

Every sample is built on the REAL C++ engine (``core_bridge`` -> ``libaicad``), so every
face/edge/feature label comes for free from the engine's own feature tree and owner_map —
no manual annotation, no learned model. This is exactly the "free synthetic labels" the
review doc names in §3.4.

Usage (from the repo root — the ``mcp_server`` symlink -> services/mcp must resolve):

    python3 tools/gsl_dataset_gen.py --count 50
    python3 tools/gsl_dataset_gen.py --count 10 --families plate_bolt_circle,turned_shaft
    python3 tools/gsl_dataset_gen.py --count 5 --render --out corpora/gsl_dataset --seed 1000

Consumers (downstream, not built by this script):
  - tools/gsl_eval.py       — measured-residual reverse-engineering eval harness (WS-D D2).
  - tools/gsl_gnn/          — GraphSAGE adapter scaffold trained on faces.json + adjacency.json
                              (WS-D D3), gated on tools/gsl_eval.py's verdict.

See corpora/gsl_dataset/README.md for the on-disk layout and label semantics.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# `tools/` sits directly under the repo root; the `mcp_server` symlink (-> services/mcp) also
# lives at the repo root, so it is importable only once the repo root is on sys.path (running
# `python3 tools/gsl_dataset_gen.py` puts `tools/` on sys.path[0], not the repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_server import core_bridge as cb  # noqa: E402
from mcp_server.core_bridge import CoreError, dim, feat  # noqa: E402

GENERATOR_VERSION = "1.0.0"


# ============================================================== family op-batch templates
#
# Each function takes a random.Random and returns a JSON-able list of doc ops for
# doc_apply_ops, grounded against services/agent/prompts/shared/ops_cheatsheet.md and
# services/agent/prompts/shared/cheatsheets/sweep.md. All were smoke-tested directly against
# the live engine while writing this file.


def plate_bolt_circle(rng: random.Random) -> list[dict]:
    """A rectangular plate with a bolt circle of 3-8 holes (box + bolt_circle intent feature)."""
    length = round(rng.uniform(60, 150), 1)
    width = round(rng.uniform(40, 100), 1)
    thickness = round(rng.uniform(5, 15), 1)
    count = rng.randint(3, 8)
    pcd = round(min(length, width) * rng.uniform(0.5, 0.75), 1)
    hole_d = round(rng.uniform(4, 10), 1)
    return [
        feat("box", "Plate", {"length": dim(length), "width": dim(width), "height": dim(thickness)}),
        {"op": "add_feature", "feature": {
            "type": "bolt_circle", "name": "Bolts",
            "params": {"diameter": hole_d, "count": count, "pcd": pcd},
            "refs": {"face": {"of": "face", "side": "top"}}}},
    ]


def pocketed_block(rng: random.Random) -> list[dict]:
    """A block with 1-2 rectangular pocket cuts (sketch + extrude cut, blind depth)."""
    length = round(rng.uniform(60, 140), 1)
    width = round(rng.uniform(50, 100), 1)
    height = round(rng.uniform(15, 30), 1)
    ops: list[dict] = [feat("box", "Block", {"length": dim(length), "width": dim(width), "height": dim(height)})]
    for i in range(rng.randint(1, 2)):
        pw = round(rng.uniform(length * 0.15, length * 0.30), 1)
        ph = round(rng.uniform(width * 0.15, width * 0.30), 1)
        depth = round(rng.uniform(height * 0.2, height * 0.6), 1)
        cx = round(rng.uniform(-length * 0.25, length * 0.25), 1)
        cy = round(rng.uniform(-width * 0.25, width * 0.25), 1)
        sk_idx = len(ops)
        ops.append(feat("sketch", f"PocketSk{i}", {"entities": [], "constraints": []},
                        {"plane": {"of": "face", "side": "top"}}))
        x0, x1, y0, y1 = cx - pw / 2, cx + pw / 2, cy - ph / 2, cy + ph / 2
        for p1, p2 in [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                       ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]:
            ops.append({"op": "sketch_add_entity", "sketch": f"${sk_idx}",
                        "entity": {"type": "line", "p1": list(p1), "p2": list(p2)}})
        ops.append(feat("extrude", f"Pocket{i}", {"mode": "cut", "end": "blind", "depth": dim(depth)},
                        {"profile": [{"kind": "region", "sketch": f"${sk_idx}",
                                      "centroid": [cx, cy], "hint_index": 0}]}))
    return ops


def turned_shaft(rng: random.Random) -> list[dict]:
    """A revolved shaft with 2-4 stacked diameters (sketch half-profile staircase + revolve)."""
    n_steps = rng.randint(2, 4)
    radii = sorted((round(rng.uniform(5, 25), 1) for _ in range(n_steps)), reverse=True)
    heights = [round(rng.uniform(10, 40), 1) for _ in range(n_steps)]
    ops: list[dict] = [feat("sketch", "Profile", {"entities": [], "constraints": []}, {"plane": "plane.front"})]
    pts = [(0.0, 0.0)]
    z = 0.0
    for r, h in zip(radii, heights):
        z_new = z + h
        pts.append((r, z))
        pts.append((r, z_new))
        z = z_new
    pts.append((0.0, z))
    seq = pts + [(0.0, 0.0)]  # close back along the revolve axis
    for p1, p2 in zip(seq, seq[1:]):
        ops.append({"op": "sketch_add_entity", "sketch": "$0",
                    "entity": {"type": "line", "p1": list(p1), "p2": list(p2)}})
    ops.append(feat("revolve", "Shaft", {"mode": "boss", "angle": dim(360)},
                    {"profile": [{"kind": "region", "sketch": "$0",
                                  "centroid": [radii[0] / 2, z / 2], "hint_index": 0}],
                     "axis": {"kind": "datum", "feature": "axis.z"}}))
    return ops


def ribbed_bracket(rng: random.Random) -> list[dict]:
    """A base plate + a vertical wall boss, then EITHER a rib gusset OR a second boss (50/50)."""
    length = round(rng.uniform(60, 120), 1)
    width = round(rng.uniform(50, 90), 1)
    thickness = round(rng.uniform(6, 12), 1)
    wall_h = round(rng.uniform(20, 50), 1)
    wall_t = round(rng.uniform(5, 10), 1)
    ops: list[dict] = [
        feat("box", "Base", {"length": dim(length), "width": dim(width), "height": dim(thickness)}),
        feat("box", "Wall", {"length": dim(length), "width": dim(wall_t), "height": dim(wall_h)},
             {}, at=[-length / 2, width / 2 - wall_t, thickness / 2], centered=False,
             mode="boss", merge=True),
    ]
    if rng.random() < 0.5:
        # Rib gusset bridging the wall's front face down to the base plate, on plane.right.
        rib_th = round(rng.uniform(2, 4), 1)
        sk_idx = len(ops)
        ops.append(feat("sketch", "RibSk", {"entities": [], "constraints": []}, {"plane": "plane.right"}))
        p_top = [width / 2 - wall_t, thickness / 2 + wall_h * 0.6]
        p_bot = [0.0, thickness / 2]
        ops.append({"op": "sketch_add_entity", "sketch": f"${sk_idx}",
                    "entity": {"type": "line", "p1": p_top, "p2": p_bot}})
        ops.append(feat("rib", "Gusset", {"thickness": dim(rib_th)},
                        {"profile": [{"kind": "region", "sketch": f"${sk_idx}",
                                      "centroid": [(p_top[0] + p_bot[0]) / 2,
                                                   (p_top[1] + p_bot[1]) / 2], "hint_index": 0}]}))
    else:
        # A second cylindrical boss on the plate, away from the wall.
        bx = round(rng.uniform(-length * 0.2, length * 0.2), 1)
        by = round(rng.uniform(-width * 0.35, -width * 0.1), 1)
        br = round(rng.uniform(5, 10), 1)
        bh = round(rng.uniform(5, 15), 1)
        sk_idx = len(ops)
        ops.append(feat("sketch", "BossSk", {"entities": [], "constraints": []},
                        {"plane": {"of": "face", "side": "top"}}))
        ops.append({"op": "sketch_add_entity", "sketch": f"${sk_idx}",
                    "entity": {"type": "circle", "center": [bx, by], "radius": br}})
        ops.append(feat("extrude", "Boss", {"mode": "boss", "end": "blind", "depth": dim(bh)},
                        {"profile": [{"kind": "region", "sketch": f"${sk_idx}",
                                      "centroid": [bx, by], "hint_index": 0}]}))
    return ops


def shelled_enclosure(rng: random.Random) -> list[dict]:
    """A box shelled open on top (constant-wall enclosure)."""
    length = round(rng.uniform(50, 120), 1)
    width = round(rng.uniform(40, 100), 1)
    height = round(rng.uniform(30, 80), 1)
    thickness = round(rng.uniform(2, 5), 1)
    return [
        feat("box", "Enclosure", {"length": dim(length), "width": dim(width), "height": dim(height)}),
        feat("shell", "Shell", {"thickness": dim(thickness)}, {"remove": {"of": "face", "side": "top"}}),
    ]


def bladed_disk(rng: random.Random) -> list[dict]:
    """A disk with one radial blade boss, circular-patterned 4-8 times."""
    radius = round(rng.uniform(30, 60), 1)
    thickness = round(rng.uniform(5, 12), 1)
    n_blades = rng.randint(4, 8)
    blade_h = round(rng.uniform(5, 15), 1)
    blade_w = round(rng.uniform(3, 6), 1)
    r_in = round(radius * 0.3, 1)
    r_out = round(radius * 0.9, 1)
    ops: list[dict] = [
        feat("cylinder", "Disk", {"radius": dim(radius), "height": dim(thickness)}),
        feat("sketch", "BladeSk", {"entities": [], "constraints": []}, {"plane": {"of": "face", "side": "top"}}),
    ]
    sk_idx = 1
    x0, x1, y0, y1 = r_in, r_out, -blade_w / 2, blade_w / 2
    for p1, p2 in [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                   ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]:
        ops.append({"op": "sketch_add_entity", "sketch": f"${sk_idx}",
                    "entity": {"type": "line", "p1": list(p1), "p2": list(p2)}})
    extrude_idx = len(ops)
    ops.append(feat("extrude", "Blade", {"mode": "boss", "end": "blind", "depth": dim(blade_h)},
                    {"profile": [{"kind": "region", "sketch": f"${sk_idx}",
                                  "centroid": [(x0 + x1) / 2, 0], "hint_index": 0}]}))
    ops.append(feat("circular_pattern", "Blades",
                    {"count": n_blades, "angle": dim(360), "equal_spacing": True},
                    {"axis": {"kind": "datum", "feature": "axis.z"}, "features": [f"${extrude_idx}"]}))
    return ops


def swept_pipe(rng: random.Random) -> list[dict]:
    """A round pipe swept along an L-shaped path (per cheatsheets/sweep.md's worked example)."""
    r = round(rng.uniform(3, 8), 1)
    leg1 = round(rng.uniform(30, 80), 1)
    leg2 = round(rng.uniform(30, 80), 1)
    return [
        feat("sketch", "Profile", {"entities": [], "constraints": []}, {"plane": "plane.right"}),
        {"op": "sketch_add_entity", "sketch": "$0", "entity": {"type": "circle", "center": [0, 0], "radius": r}},
        feat("sketch", "Path", {"entities": [], "constraints": []}, {"plane": "plane.top"}),
        {"op": "sketch_add_entity", "sketch": "$2", "entity": {"type": "line", "p1": [0, 0], "p2": [leg1, 0]}},
        {"op": "sketch_add_entity", "sketch": "$2", "entity": {"type": "line", "p1": [leg1, 0], "p2": [leg1, leg2]}},
        feat("sweep", "Pipe", {"mode": "boss"},
             {"profile": [{"kind": "region", "sketch": "$0", "centroid": [0, 0], "hint_index": 0}],
              "path": {"kind": "region", "sketch": "$2", "centroid": [leg1 / 2, 0], "hint_index": 0}}),
    ]


FAMILIES: dict[str, Callable[[random.Random], list[dict]]] = {
    "plate_bolt_circle": plate_bolt_circle,
    "pocketed_block": pocketed_block,
    "turned_shaft": turned_shaft,
    "ribbed_bracket": ribbed_bracket,
    "shelled_enclosure": shelled_enclosure,
    "bladed_disk": bladed_disk,
    "swept_pipe": swept_pipe,
}


# ============================================================== artifact assembly


def _label_faces(doc_id: str, tree_result: dict) -> dict:
    """doc_query({"of":"face"}) matches augmented with `label` = the owning feature's TYPE.

    doc_query does not itself populate `owner_feature` (that field is only threaded through
    ref selectors, not the plain query path) — so we resolve it ourselves from doc_tree's
    owner_map, which records each feature's owned (body_id, face_id) pairs. face_id in
    owner_map is 1-based and equal to the query match's 0-based `hint_index` + 1 (both walk
    the same describe_faces() enumeration order — see core/doc/session.cpp:doc_query comment).
    doc_query only ever returns the LAST body's faces, so we resolve that body's id from the
    current tess bodies list (default 1 for the common single-body case).
    """
    owner_map = tree_result.get("owner_map") or {}
    feats = ((tree_result.get("tree") or {}).get("features")) or []
    fmap = {f.get("id"): f for f in feats if isinstance(f, dict)}
    owner_of: dict[tuple[int, int], str] = {}
    for fid, om in owner_map.items():
        for pair in om.get("faces", []):
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                owner_of[(pair[0], pair[1])] = fid

    body_id = 1
    try:
        tess = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": []}).get("tess", {})
        bodies = tess.get("bodies") or []
        if bodies:
            body_id = bodies[-1].get("body_id", 1)
    except Exception:  # noqa: BLE001 — best-effort; default body_id=1 covers the common case.
        pass

    result = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}})
    matches = result.get("matches", [])
    labelled = []
    for m in matches:
        hint = m.get("hint_index")
        face_id = (hint + 1) if isinstance(hint, int) else None
        fid = owner_of.get((body_id, face_id)) if face_id is not None else None
        m2 = dict(m)
        m2["owner_feature_id"] = fid
        m2["label"] = fmap.get(fid, {}).get("type") if fid else "import_or_base"
        labelled.append(m2)
    return {"count": result.get("count", len(labelled)), "matches": labelled}


def _outline_text_for(doc_id: str) -> str:
    """Best-effort describe_part outline text. gsl_tools.py is being extended concurrently by a
    sibling task, so import it lazily and degrade to a placeholder on any failure instead of
    crashing the whole generation run."""
    try:
        from mcp_server.gsl_tools import _digest, _outline_text  # noqa: PLC0415
        return _outline_text(_digest(doc_id))
    except Exception as e:  # noqa: BLE001
        return f"(outline unavailable: {e!r})"


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True))


def _build_sample(family: str, sample_id: str, ops: list[dict], sample_dir: Path,
                   render: bool) -> None:
    """Apply `ops` on a fresh doc, gather every artifact, then close the doc. Raises on any
    engine failure (validation error, dropped feature) so the caller can log-and-continue."""
    doc_id = cb.call("doc_new", {"kind": "part", "name": f"{family}_{sample_id}"})["doc_id"]
    try:
        apply_result = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
        feature_status = (apply_result.get("rebuild") or {}).get("feature_status", [])
        failed = [s for s in feature_status if not s.get("ok", True)]
        if failed:
            raise CoreError(f"feature(s) failed to build: {failed}")

        tree_result = cb.call("doc_tree", {"doc_id": doc_id})
        faces = _label_faces(doc_id, tree_result)
        adjacency = cb.call("doc_adjacency", {"doc_id": doc_id})
        outline = _outline_text_for(doc_id)
        bbox = cb.call("doc_bbox", {"doc_id": doc_id})
        try:
            volume_mm3 = cb.call("eval_mass", {"doc_id": doc_id, "density_g_cm3": 1.0}).get("volume_mm3")
        except Exception:  # noqa: BLE001
            volume_mm3 = None

        sample_dir.mkdir(parents=True, exist_ok=True)
        _write_json(sample_dir / "document.json", tree_result)
        _write_json(sample_dir / "faces.json", faces)
        _write_json(sample_dir / "adjacency.json", adjacency)
        (sample_dir / "outline.txt").write_text(outline)
        _write_json(sample_dir / "meta.json", {
            "family": family,
            "seed": sample_id,
            "ops_count": len(ops),
            "feature_count": len(feature_status),
            "bbox": bbox,
            "volume_mm3": volume_mm3,
            "generator_version": GENERATOR_VERSION,
        })

        if render:
            png = str(sample_dir / "render.png")
            try:
                r = cb.call("doc_render_png", {"doc_id": doc_id, "out_path": png})
                if not r.get("ok"):
                    print(f"  [{family}/{sample_id}] render skipped: {r.get('reason', r.get('error'))}")
            except Exception as e:  # noqa: BLE001 — rendering is a bonus, never fatal.
                print(f"  [{family}/{sample_id}] render skipped: {e}")
    finally:
        # doc_close IS a real RPC (services/mcp/cad_tools.py calls it the same way) — always
        # release the doc so a long --count run doesn't accumulate open sessions.
        try:
            cb.call("doc_close", {"doc_id": doc_id})
        except Exception:  # noqa: BLE001
            pass


# ============================================================== CLI


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--count", type=int, default=50, help="samples PER family (default 50)")
    ap.add_argument("--families", type=str, default=None,
                    help=f"comma-separated subset of: {', '.join(FAMILIES)} (default: all)")
    ap.add_argument("--render", action="store_true", help="also render.png each sample (doc_render_png)")
    ap.add_argument("--out", type=str, default="corpora/gsl_dataset", help="output root directory")
    ap.add_argument("--seed", type=int, default=0, help="base seed; sample i uses seed base+i")
    args = ap.parse_args(argv)

    if not cb.available():
        print("FATAL: the AI-CAD core engine library (libaicad) could not be located/loaded — "
              "build core/build-linux (or core/build-occt-linux) first.", file=sys.stderr)
        return 1

    if args.families:
        selected = [f.strip() for f in args.families.split(",") if f.strip()]
        unknown = [f for f in selected if f not in FAMILIES]
        if unknown:
            print(f"FATAL: unknown family/families: {unknown} (known: {sorted(FAMILIES)})",
                  file=sys.stderr)
            return 1
    else:
        selected = list(FAMILIES)

    out_root = Path(args.out)
    overall_ok = 0
    overall_total = 0
    family_failures: dict[str, int] = {}

    for family in selected:
        gen = FAMILIES[family]
        family_dir = out_root / family
        ok = 0
        errors: list[str] = []
        for i in range(args.count):
            seed = args.seed + i
            sample_id = str(seed)
            rng = random.Random(seed)
            overall_total += 1
            try:
                ops = gen(rng)
                _build_sample(family, sample_id, ops, family_dir / sample_id, args.render)
                ok += 1
                overall_ok += 1
            except Exception as e:  # noqa: BLE001 — a family that won't build is REPORTED, not fatal.
                msg = f"{type(e).__name__}: {e}"
                errors.append(msg)
                print(f"[{family}] sample seed={seed} FAILED: {msg}")
        family_failures[family] = len(errors)
        status = "OK" if ok == args.count else ("PARTIAL" if ok else "ALL FAILED")
        print(f"== {family}: {ok}/{args.count} samples built [{status}] ==")

    print(f"\nTotal: {overall_ok}/{overall_total} samples across {len(selected)} families "
          f"written under {out_root}/")
    failed_families = [f for f, n in family_failures.items() if n == args.count]
    if failed_families:
        print(f"Families that built ZERO samples on this engine: {failed_families}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
