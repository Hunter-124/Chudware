#!/usr/bin/env python3
"""GSL synthetic dataset factory — engine-labelled corpus generator.

Builds a deterministic-engine-labelled corpus for measuring where deterministic feature
recognition fails and for training the candidate ranker: seven parametric family
templates, each a function ``(rng: random.Random) -> list[op dict]`` that emits a
randomized, VALID aicad.part/1 ops batch (the same op vocabulary documented in
``services/agent/prompts/shared/ops_cheatsheet.md`` and built with the ``dim``/``feat``
helpers from ``services/mcp/core_bridge.py``).

Every sample is built on the REAL C++ engine (``core_bridge`` -> ``libchudware``), so every
face/edge/feature label comes for free from the engine's own feature tree and owner_map —
no manual annotation, no learned model.

Usage from the repo root (module execution keeps canonical packages importable):

    python3 -m tools.gsl_dataset_gen --count 50
    python3 -m tools.gsl_dataset_gen --count 10 --families plate_bolt_circle,turned_shaft
    python3 -m tools.gsl_dataset_gen --count 5 --render --out corpora/gsl_dataset --seed 1000

The generated corpus feeds ``tools/gsl_gnn/train_ranker.py``. See
corpora/gsl_dataset/README.md for the on-disk layout and label semantics.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from services.mcp import core_bridge as cb
from services.mcp.core_bridge import CoreError, dim, feat

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


# ==================================================== translator-pair factory
#
# Dataset extension: the deterministic describers are the FREE label source, so a
# translator pair is (gsl-ser/v1 serialization, grounded caption). This factory emits
# the DOMAIN_MIX below, paraphrase-augments each caption, then RE-VERIFIES every pair
# through the groundedness seam AND the round-trip re-extraction diff before it may
# enter the set — never trust the paraphraser. It is engine-FREE: it
# builds gsl-ser/v1 STATE dicts directly (the `_digest`/domain shape), so the mix runs in
# CI with no libchudware. When the engine IS available, real B-rep digests can be substituted
# for `synth_brep_state` without changing the pipeline.

# Data-mix table (fractions sum to 1.0). B-rep dominates; the "what changed"
# contrastive + correction pairs are the 10% that teaches task-conditioned narration.
DOMAIN_MIX: dict[str, float] = {
    "brep": 0.40,
    "sketch": 0.15,
    "ecad": 0.15,
    "cam": 0.15,
    "fea": 0.05,
    "contrastive": 0.05,
    "correction": 0.05,
}


def _round(x: float, n: int = 1) -> float:
    return round(x, n)


def synth_brep_state(rng: random.Random) -> dict:
    """An engine-free gsl-ser/v1 B-rep state (bolt-circle plate variant)."""
    L, W, H = _round(rng.uniform(60, 150)), _round(rng.uniform(40, 100)), _round(rng.uniform(5, 15))
    n = rng.randint(3, 8)
    return {
        "domain": "brep", "part": f"plate_{rng.randint(0, 9999)}",
        "bbox_mm": [L, W, H], "faces": 6 + n, "edges": 12 + 2 * n,
        "symmetry": {"order": n, "type": "rotational", "axis": "z"},
        "feature_groups": [
            {"id": "F1", "name": "Plate", "type": "box", "faces": 6},
            {"id": "F2", "name": "Bolts", "type": "bolt_circle", "faces": n,
             "params": {"count": n, "diameter": _round(rng.uniform(4, 10)),
                        "pcd": _round(min(L, W) * rng.uniform(0.5, 0.75))}},
        ],
    }


def synth_sketch_state(rng: random.Random) -> dict:
    dims = [{"id": f"d{i}", "type": rng.choice(["distance", "radius"]),
             "value": _round(rng.uniform(5, 90)), "driven": False} for i in range(rng.randint(2, 5))]
    dims.append({"id": "da", "type": "angle", "value": _round(rng.choice([30, 45, 60, 90])),
                 "driven": False})
    return {"domain": "sketch", "part": f"sketch_{rng.randint(0, 9999)}",
            "sketch": {"dof": 0, "dims": dims}}


def synth_cam_state(rng: random.Random) -> dict:
    op_types = ["adaptive_clearing", "contour", "pocket", "face"]
    ops = []
    for i, name in enumerate(["Rough", "Finish"][: rng.randint(1, 2)]):
        ops.append({"id": f"op{i}", "name": name, "type": rng.choice(op_types),
                    "tool_diameter": _round(rng.uniform(3, 12)),
                    "machined_pct": _round(rng.uniform(60, 99.9)),
                    "cycle_time_s": _round(rng.uniform(30, 600)),
                    "feed_mm_min": float(rng.randint(400, 2000))})
    return {"domain": "cam", "part": f"job_{rng.randint(0, 9999)}",
            "cam": {"setups": 1, "operations": len(ops), "ops": ops}}


def synth_fea_state(rng: random.Random) -> dict:
    yield_mpa = _round(rng.choice([276.0, 250.0, 505.0, 215.0]))
    vm = _round(yield_mpa * rng.uniform(0.4, 0.9), 1)
    return {"domain": "fea", "part": f"study_{rng.randint(0, 9999)}",
            "fea": {"material": rng.choice(["6061_T6", "1018_steel", "Ti_6Al_4V"]),
                    "yield_mpa": yield_mpa, "max_von_mises_mpa": vm,
                    "max_disp_mm": round(rng.uniform(0.005, 0.5), 3),
                    "min_fos": round(yield_mpa / vm, 2),
                    "freqs_hz": sorted(_round(rng.uniform(100, 1500), 1)
                                       for _ in range(rng.randint(1, 3)))}}


def synth_ecad_state(rng: random.Random) -> dict:
    nets = ["SW", "VIN", "VOUT", "GND", "FB"]
    traces = [{"net": nets[i], "width_mm": round(rng.uniform(0.2, 1.5), 2),
               "impedance_ohm": _round(rng.choice([45.0, 50.0, 90.0, 100.0]), 1)}
              for i in range(rng.randint(1, 3))]
    return {"domain": "ecad", "part": f"pcb_{rng.randint(0, 9999)}",
            "ecad": {"nets": rng.randint(8, 30), "components": rng.randint(10, 60),
                     "traces": traces}}


_STATE_GENS: dict[str, Callable[[random.Random], dict]] = {
    "brep": synth_brep_state, "sketch": synth_sketch_state, "cam": synth_cam_state,
    "fea": synth_fea_state, "ecad": synth_ecad_state,
}


def paraphrase_default(caption: str, rng: random.Random) -> str:
    """Deterministic OFFLINE stand-in for the large-LLM paraphraser (the real step runs
    a big model out of band). Reorders sentences + swaps a few surface words WITHOUT
    touching numerals — the groundedness re-check must still pass. The point of the
    pipeline is that even a *misbehaving* paraphraser cannot inject an ungrounded number:
    the re-check drops it. Pass your own callable to substitute the real paraphraser."""
    swaps = [("is", "="), ("bbox", "bounding box"), ("feat", "feature"),
             ("op", "operation"), ("pct", "%")]
    sents = [s.strip() for s in caption.split(". ") if s.strip()]
    if len(sents) > 2:
        head, tail = sents[0], sents[1:]
        rng.shuffle(tail)
        sents = [head] + tail
    text = ". ".join(sents)
    for a, b in swaps:
        if rng.random() < 0.5:
            text = re.sub(rf"\b{re.escape(a)}\b", b, text)
    return text


def verify_pair(state: dict, caption: str) -> tuple[bool, list[str]]:
    """A pair is admissible iff (1) the state round-trips exactly through gsl-ser/v1 and
    (2) the caption has NO ungrounded numeral (groundedness seam). Returns (ok, reasons)."""
    from tools.translator import gsl_ser as _g  # noqa: PLC0415
    from tools.translator import groundedness as _gnd  # noqa: PLC0415
    reasons: list[str] = []
    rt = _g.roundtrip_diff(state)
    if rt:
        reasons.append(f"roundtrip:{rt[:2]}")
    ungrounded = [(v.value, v.unit) for v in _gnd.check(caption, state) if not v.grounded]
    if ungrounded:
        reasons.append(f"ungrounded:{ungrounded[:3]}")
    return (not reasons, reasons)


def _sample_domain(rng: random.Random) -> str:
    r = rng.random()
    acc = 0.0
    for dom, w in DOMAIN_MIX.items():
        acc += w
        if r <= acc:
            return dom
    return "brep"


def make_pair(domain: str, rng: random.Random, paraphraser) -> Optional[dict]:
    """One (serialization, caption) training pair for `domain`, paraphrased + re-verified.
    Returns None (dropped) if the re-check fails — never emit an unverified pair."""
    from tools.translator import gsl_ser as _g  # noqa: PLC0415

    if domain in _STATE_GENS:
        state = _STATE_GENS[domain](rng)
        caption = _g.render_caption(state)
        ok, reasons = verify_pair(state, caption)
        if not ok:
            return {"_dropped": reasons}
        para = paraphraser(caption, rng)
        ok, reasons = verify_pair(state, para)
        if not ok:                      # paraphraser injected/dropped a number -> discard
            return {"_dropped": reasons}
        return {"domain": domain, "serialization": _g.serialize(state),
                "caption": para, "template_caption": caption,
                "task": "narrate", "state": state}

    if domain == "contrastive":         # "what changed" — before/after, delta-only caption
        base = _STATE_GENS[rng.choice(["brep", "cam", "fea"])](rng)
        after = json.loads(json.dumps(base))
        slots = [s for s in _g.enumerate_slots(base) if s.kind in ("dim", "count")]
        if not slots:
            return None
        tgt = slots[rng.randrange(len(slots))]
        new_val = _g.canon(float(tgt.value) * 1.5 + 1.0, tgt.unit)
        _set_by_path(after, tgt.path, new_val)
        caption = (f"[gsl-ser/v1 what_changed] {tgt.path} changed from "
                   f"{tgt.token()} to {_g.fmt_value(new_val, tgt.unit)}.")
        # A delta caption legitimately names BOTH the before- and after-value, so it must
        # ground against before ∪ after (not `after` alone); the state must round-trip.
        from tools.translator import groundedness as _gnd  # noqa: PLC0415
        allslots = [s for s in _g.enumerate_slots(base) + _g.enumerate_slots(after)
                    if s.numeric]
        ungrounded = [(v, u) for v, u in _g.extract_numerals(caption)
                      if not any(s.matches(v, u) for s in allslots)]
        if _g.roundtrip_diff(after) or ungrounded:
            return {"_dropped": [f"contrastive:{ungrounded[:2] or _g.roundtrip_diff(after)[:1]}"]}
        return {"domain": domain, "serialization": _g.serialize(after),
                "before": _g.serialize(base), "caption": caption, "task": "what_changed",
                "state": after}

    if domain == "correction":          # wrong caption -> corrected (deterministic) caption
        state = _STATE_GENS[rng.choice(list(_STATE_GENS))](rng)
        good = _g.render_caption(state)
        slots = [s for s in _g.enumerate_slots(state) if s.numeric]
        if not slots:
            return None
        tgt = slots[len(slots) // 2]
        bad = good.replace(f"is {_g.prose_token(tgt.value, tgt.unit)}.",
                           f"is {_g.prose_token(float(tgt.value) * 2 + 3, tgt.unit)}.", 1)
        return {"domain": domain, "serialization": _g.serialize(state),
                "wrong_caption": bad, "caption": good, "task": "correct", "state": state}
    return None


def _set_by_path(state: dict, path: str, value) -> None:
    """Mutate the state slot at `path` (mirrors enumerate_slots' path scheme) in place."""
    segs = path.split(".")
    if segs[:2] == ["part", "bbox"]:
        state["bbox_mm"][{"x": 0, "y": 1, "z": 2}[segs[2]]] = value
    elif path in ("part.faces", "part.edges"):
        state[segs[1]] = value
    elif segs[0] == "feat":
        for fg in state.get("feature_groups", []):
            if fg.get("name") == segs[1] or fg.get("id") == segs[1]:
                if segs[2] == "faces":
                    fg["faces"] = value
                else:
                    fg.setdefault("params", {})[segs[2]] = value
    elif segs[0] == "cam" and len(segs) >= 4:
        for op in state.get("cam", {}).get("ops", []):
            if op.get("name") == segs[2] or op.get("id") == segs[2]:
                op[{"tool_diameter": "tool_diameter", "machined": "machined_pct",
                    "cycle_time": "cycle_time_s", "feed": "feed_mm_min"}.get(segs[3], segs[3])] = value
    elif segs[0] == "fea":
        state.setdefault("fea", {})[{"max_von_mises": "max_von_mises_mpa",
                                     "max_disp": "max_disp_mm", "yield": "yield_mpa",
                                     "min_fos": "min_fos"}.get(segs[1], segs[1])] = value


def build_translator_corpus(count: int, seed: int, out_path: Path,
                            paraphraser=None) -> dict:
    """Emit `count` verified translator pairs following DOMAIN_MIX to a JSONL file.
    Returns a stats dict (per-domain counts + drops). Engine-free."""
    paraphraser = paraphraser or paraphrase_default
    rng = random.Random(seed)
    kept: dict[str, int] = {}
    dropped = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        made = 0
        attempts = 0
        while made < count and attempts < count * 20:
            attempts += 1
            dom = _sample_domain(rng)
            pair = make_pair(dom, rng, paraphraser)
            if not pair or pair.get("_dropped"):
                dropped += 1
                continue
            fh.write(json.dumps(pair, sort_keys=True) + "\n")
            kept[dom] = kept.get(dom, 0) + 1
            made += 1
    total = sum(kept.values()) or 1
    return {"kept": kept, "kept_total": sum(kept.values()), "dropped": dropped,
            "proportions": {d: round(kept.get(d, 0) / total, 3) for d in DOMAIN_MIX},
            "target_mix": DOMAIN_MIX, "out": str(out_path)}


# ============================================================== artifact assembly


def _label_faces(doc_id: str, tree_result: dict) -> dict:
    """doc_query({"of":"face"}) matches augmented with `label` = the owning feature's TYPE.

    doc_query does not itself populate `owner_feature` (that field is only threaded through
    ref selectors, not the plain query path) — so we resolve it ourselves from doc_tree's
    owner_map, which records each feature's owned [body ordinal, per-body 1-based TopExp face
    id] pairs. doc_query enumerates EVERY body with one running `hint_index`, so hint_index+1
    equals the per-body id only on the first body; the engine stamps the exact join keys
    (`body`, `topo`) on each part-doc match (core/doc/session.cpp doc_query), and those are
    what we join on. The old `(last body, hint_index+1)` join aliased body 1's faces onto the
    last body's owners on any multi-body sample.
    """
    owner_map = tree_result.get("owner_map") or {}
    feats = ((tree_result.get("tree") or {}).get("features")) or []
    fmap = {f.get("id"): f for f in feats if isinstance(f, dict)}
    owner_of: dict[tuple[int, int], str] = {}
    for fid, om in owner_map.items():
        for pair in om.get("faces", []):
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                owner_of[(pair[0], pair[1])] = fid

    result = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}})
    matches = result.get("matches", [])
    labelled = []
    for m in matches:
        body, topo = m.get("body"), m.get("topo")
        if not isinstance(body, int) or not isinstance(topo, int):
            raise CoreError(f"doc_query face match lacks the body/topo join keys: {m!r}")
        fid = owner_of.get((body, topo))
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
        from services.mcp.gsl_tools import _digest, _outline_text  # noqa: PLC0415
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
    ap.add_argument("--translator-pairs", type=int, default=None, metavar="N",
                    help="ENGINE-FREE: emit N verified gsl-ser/v1 translator pairs following "
                         "the DOMAIN_MIX table (paraphrased + groundedness re-checked) "
                         "to --pairs-out, then exit (no libchudware needed).")
    ap.add_argument("--pairs-out", type=str, default="corpora/translator_pairs.jsonl",
                    help="output JSONL for --translator-pairs")
    args = ap.parse_args(argv)

    if args.translator_pairs is not None:
        stats = build_translator_corpus(args.translator_pairs, args.seed,
                                        Path(args.pairs_out))
        print(f"Wrote {stats['kept_total']} verified pairs to {stats['out']} "
              f"({stats['dropped']} dropped by re-check)")
        print(f"  mix (kept/target): " + ", ".join(
            f"{d} {stats['proportions'][d]:.2f}/{stats['target_mix'][d]:.2f}"
            for d in DOMAIN_MIX))
        return 0

    if not cb.available():
        print("FATAL: the Chudware core engine library (libchudware) could not be located/loaded — "
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
