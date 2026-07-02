"""Synthetic vision corpus v1 generator (T-H10 DoD item 2).

Produces three example kinds where the geometry behind EVERY image is KNOWN — that ground
truth is the render-grounded reward signal a later training leg consumes:

  * `render`  — an iso schematic of a part + its `known_geometry` facts (bench schema).
  * `drawing` — an orthographic drawing sheet with KNOWN dimension callouts (facts of
                kind "dimension"); exercises the drawing/callout-reading task (b).
  * `diff_pair` — two renders A/B differing by ONE known geometric change (a dropped boss,
                a missed hole); exercises render-vs-photo compare (d). The VLM answers the
                COARSE "what changed?" claim; deterministic code owns the fine verdict.

Determinism (cross-machine reproducibility DoD): geometry is generated from `(family, seed)`
only — no wall-clock, no RNG entropy beyond the seed — and the FROZEN hash is over the
canonical ground-truth manifest JSON (like the T-H8 bench `spec.json`), never over PNG
bytes. Images are regenerable DVC-tracked artifacts drawn with PIL (fully controlled, no
matplotlib version drift). Every example is stamped `provenance: "synthetic"` per the
telemetry contract vocabulary (Driver notes).

This generator imports NO ML library and needs NO engine — it runs in CI. A future
enhancement can swap the PIL backend for real `render_part()` engine renders behind a
guard (the manifest/known_geometry contract is identical); v0 uses the deterministic PIL
backend so the corpus is self-contained and hash-stable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from PIL import Image, ImageDraw
    _PIL = True
except Exception:  # pragma: no cover - PIL is in mcp-venv; guard keeps import non-fatal
    _PIL = False

PROVENANCE = "synthetic"
_IMG = 384  # square canvas; big enough for a small VLM's tiler, small enough to commit


# ---- deterministic parametric families --------------------------------------------------
# Each family maps a seed -> (known_geometry facts, a draw spec). Facts use the SAME schema
# as the T-H8 bench (`kind` + attrs; `visible: false` for internal features a face-normal
# read can't see). Numbers are a deterministic function of the seed only.

def _bracket(seed: int) -> tuple[list[dict], dict]:
    x, y, z = 80 + seed * 5, 50 + seed * 3, 6 + seed
    n_holes = 2 + (seed % 3)
    d = 6.0 + (seed % 2)
    facts = [
        {"kind": "bbox", "x": x, "y": y, "z": z},
        {"kind": "hole", "count": n_holes, "diameter": d},
        {"kind": "fillet", "edges": "vertical"},
        {"kind": "rib", "count": 1, "visible": False},  # internal — det. fallback can't see
    ]
    return facts, {"shape": "bracket", "x": x, "y": y, "holes": n_holes, "fillet": True}


def _flange(seed: int) -> tuple[list[dict], dict]:
    od = 100 + seed * 8
    n_bolts = 4 + 2 * (seed % 3)
    bd = 8.0
    facts = [
        {"kind": "bbox", "x": od, "y": od, "z": 12 + seed},
        {"kind": "bolt_circle", "count": n_bolts, "diameter": bd},
        {"kind": "bore", "count": 1, "diameter": 30.0 + seed},
        {"kind": "boss", "count": 1},
    ]
    return facts, {"shape": "flange", "od": od, "bolts": n_bolts, "bore": True}


def _shaft(seed: int) -> tuple[list[dict], dict]:
    length = 120 + seed * 10
    dia = 20 + seed * 2
    facts = [
        {"kind": "bbox", "x": length, "y": dia, "z": dia},
        {"kind": "cylinder", "count": 1, "diameter": float(dia)},
        {"kind": "chamfer", "edges": "ends"},
        {"kind": "thread", "count": 1, "visible": False},  # thread pitch: not face-normal visible
    ]
    return facts, {"shape": "shaft", "length": length, "dia": dia}


def _plate(seed: int) -> tuple[list[dict], dict]:
    x, y = 120 + seed * 6, 80 + seed * 4
    n_holes = 4 + (seed % 4)
    facts = [
        {"kind": "bbox", "x": x, "y": y, "z": 4},
        {"kind": "hole", "count": n_holes, "diameter": 5.0},
        {"kind": "fillet", "edges": "corners"},
    ]
    return facts, {"shape": "plate", "x": x, "y": y, "holes": n_holes}


def _enclosure(seed: int) -> tuple[list[dict], dict]:
    x, y, z = 90 + seed * 5, 60 + seed * 4, 40 + seed * 3
    facts = [
        {"kind": "bbox", "x": x, "y": y, "z": z},
        {"kind": "shell", "count": 1, "depth": 3.0},
        {"kind": "boss", "count": 2, "visible": False},   # internal mounting bosses
        {"kind": "rib", "count": 3, "visible": False},
    ]
    return facts, {"shape": "enclosure", "x": x, "y": y, "z": z}


FAMILIES: dict[str, Callable[[int], tuple[list[dict], dict]]] = {
    "bracket": _bracket, "flange": _flange, "shaft": _shaft,
    "plate": _plate, "enclosure": _enclosure,
}


# ---- deterministic PIL image backends ---------------------------------------------------

def _canvas() -> tuple[Any, Any]:
    im = Image.new("RGB", (_IMG, _IMG), (245, 245, 248))
    return im, ImageDraw.Draw(im)


def render_part_image(spec: dict, path: str) -> None:
    """Iso schematic of a part: a parallelepiped for the body + feature glyphs (holes as
    ellipses, bolt circles as ringed dots, fillets as rounded hints). Deterministic — same
    spec always draws the same pixels."""
    im, dr = _canvas()
    cx, cy = _IMG // 2, _IMG // 2 + 20
    w, h, dz = 150, 90, 45  # iso body extents (glyph space, not mm)
    # top face (parallelogram) + front + side for a cheap iso solid
    top = [(cx - w // 2, cy - h // 2), (cx + w // 2 - dz, cy - h // 2 - dz),
           (cx + w // 2, cy - h // 2 + dz - dz), (cx - w // 2 + dz, cy - h // 2 + dz)]
    front = [(cx - w // 2, cy - h // 2), (cx - w // 2 + dz, cy - h // 2 + dz),
             (cx - w // 2 + dz, cy + h // 2 + dz), (cx - w // 2, cy + h // 2)]
    dr.polygon(top, fill=(200, 214, 230), outline=(40, 60, 90))
    dr.polygon(front, fill=(170, 188, 210), outline=(40, 60, 90))
    dr.rectangle([cx - w // 2, cy - h // 2, cx + w // 2 - dz, cy + h // 2],
                 fill=(185, 200, 222), outline=(40, 60, 90))
    shape = spec.get("shape")
    if shape in ("plate", "bracket"):
        n = spec.get("holes", 0)
        for i in range(n):
            hx = cx - w // 2 + 25 + (i % 4) * 28
            hy = cy - h // 2 + 22 + (i // 4) * 34
            dr.ellipse([hx - 7, hy - 7, hx + 7, hy + 7], outline=(20, 20, 20), width=2)
    elif shape == "flange":
        r = 60
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(40, 60, 90), width=2)
        for i in range(spec.get("bolts", 0)):
            import math
            a = 2 * math.pi * i / max(1, spec.get("bolts", 1))
            bx, by = cx + int(r * 0.7 * math.cos(a)), cy + int(r * 0.7 * math.sin(a))
            dr.ellipse([bx - 5, by - 5, bx + 5, by + 5], outline=(20, 20, 20), width=2)
        dr.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], fill=(245, 245, 248),
                   outline=(40, 60, 90), width=2)
    elif shape == "shaft":
        dr.rectangle([cx - 120, cy - 22, cx + 120, cy + 22], fill=(180, 195, 218),
                     outline=(40, 60, 90))
        dr.line([cx - 120, cy, cx + 120, cy], fill=(90, 90, 90))
    im.save(path, "PNG")


def render_drawing_image(callouts: list[dict], path: str, title: str) -> None:
    """An orthographic drawing sheet: a front-view rectangle, dimension lines with the KNOWN
    numeric callouts as text, and a title block. This is the (b) drawing-reading substrate —
    the VLM must READ the callouts; the known values are the ground truth."""
    im, dr = _canvas()
    dr.rectangle([8, 8, _IMG - 8, _IMG - 8], outline=(0, 0, 0))          # sheet border
    dr.rectangle([90, 90, _IMG - 90, _IMG - 130], outline=(0, 0, 0), width=2)  # part view
    # horizontal dim (top) + vertical dim (left) from the first two callouts
    for i, co in enumerate(callouts[:4]):
        val = co.get("value")
        txt = f"{val}" + (f"±{co['tolerance']}" if "tolerance" in co else "")
        if i == 0:
            dr.line([90, 70, _IMG - 90, 70], fill=(0, 0, 0)); dr.text((_IMG // 2 - 10, 55), txt, fill=(0, 0, 0))
        elif i == 1:
            dr.line([60, 90, 60, _IMG - 130], fill=(0, 0, 0)); dr.text((30, _IMG // 2 - 130), txt, fill=(0, 0, 0))
        else:
            dr.text((100, 100 + i * 16), f"{co.get('kind','')}: {txt}", fill=(0, 0, 0))
    dr.rectangle([_IMG - 150, _IMG - 40, _IMG - 12, _IMG - 12], outline=(0, 0, 0))  # title block
    dr.text((_IMG - 145, _IMG - 32), title[:20], fill=(0, 0, 0))
    im.save(path, "PNG")


# ---- corpus assembly --------------------------------------------------------------------

# Features a face-normal / describe_scene B-rep read cannot depict in a schematic and — for
# a DRAWING — cannot OCR from annotation text. Only a VLM reads these.
_DEPICTED_COUNTABLE = ("hole", "bolt_circle")


def _drawing_callouts(facts: list[dict]) -> list[dict]:
    """Derive KNOWN dimension callouts from a part's bbox/diameter facts. Marked
    `visible: false` DELIBERATELY: parsing a 2D drawing's annotation TEXT is exactly the
    task-(b) skill a non-vision `describe_scene`/B-rep read CANNOT do — so the deterministic
    fallback must get NO recall credit on drawings, which is what exposes the drawing
    residual a VLM has to earn. (If these were visible:true the fallback would score
    near-perfect and structurally hide the residual DoD 1 measures.)"""
    out: list[dict] = []
    for f in facts:
        if f.get("kind") == "bbox":
            out.append({"kind": "dimension", "axis": "x", "value": f["x"],
                        "tolerance": 0.2, "visible": False})
            out.append({"kind": "dimension", "axis": "y", "value": f["y"],
                        "tolerance": 0.2, "visible": False})
        elif "diameter" in f:
            out.append({"kind": "dimension", "feature": f["kind"], "value": f["diameter"],
                        "tolerance": 0.1, "visible": False})
    return out[:4]


def _drop_one_feature(facts: list[dict]) -> tuple[list[dict], dict] | None:
    """Make a B-variant by dropping a DEPICTED countable feature (hole/bolt_circle) so the
    A/B images genuinely differ in pixels — a "known-diff render pair" whose renders don't
    differ would be a silent lie. Returns None when the family has no depictable countable
    feature to drop (shaft/enclosure) — those get no diff pair rather than an identical one."""
    for i, f in enumerate(facts):
        if f.get("kind") in _DEPICTED_COUNTABLE and "count" in f:
            b = [ff for j, ff in enumerate(facts) if j != i]
            return b, {"kind": "missing_feature", "feature": f["kind"]}
    return None


def build_manifest(seeds: int = 3) -> dict:
    """Assemble the ground-truth manifest (no images yet). Deterministic in `seeds`."""
    examples: list[dict] = []
    for fam, gen in FAMILIES.items():
        for s in range(seeds):
            facts, spec = gen(s)
            base = f"{fam}_{s}"
            examples.append({
                "example_id": base, "kind": "render", "structure": fam,
                "provenance": PROVENANCE, "image": f"renders/{base}.png",
                "known_geometry": facts, "draw_spec": spec,
            })
            if s == 0:  # one drawing per family
                callouts = _drawing_callouts(facts)
                examples.append({
                    "example_id": f"{fam}_dwg", "kind": "drawing", "structure": fam,
                    "provenance": PROVENANCE, "image": f"drawings/{fam}_dwg.png",
                    "known_geometry": callouts, "draw_spec": spec,
                })
            if s == 1:  # one diff pair per family — ONLY if a depicted feature can be dropped
                dropped = _drop_one_feature(facts)
                if dropped is not None:      # shaft/enclosure have none -> no fake identical pair
                    b_facts, diff = dropped
                    examples.append({
                        "example_id": f"{fam}_diff", "kind": "diff_pair", "structure": fam,
                        "provenance": PROVENANCE,
                        "image_a": f"pairs/{fam}_a.png", "image_b": f"pairs/{fam}_b.png",
                        "known_geometry_a": facts, "known_geometry_b": b_facts,
                        "known_diff": diff, "draw_spec_a": spec, "draw_spec_b": spec,
                    })
    return {"corpus_id": "vlm_corpus_v1", "provenance": PROVENANCE, "seeds": seeds,
            "n_examples": len(examples), "families": sorted(FAMILIES),
            "examples": examples}


def manifest_hash(manifest: dict) -> str:
    """Frozen content hash over the ground truth ONLY (ids, geometry, callouts, diffs) —
    NOT image paths or pixels — so a second machine gets the same hash from the same seeds."""
    core = [{"example_id": e["example_id"], "kind": e["kind"], "structure": e["structure"],
             "known_geometry": e.get("known_geometry"),
             "known_geometry_a": e.get("known_geometry_a"),
             "known_geometry_b": e.get("known_geometry_b"),
             "known_diff": e.get("known_diff")}
            for e in manifest["examples"]]
    blob = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def generate(out_dir: str, seeds: int = 3, *, images: bool = True) -> dict:
    """Write the corpus to `out_dir`: manifest.json + spec.json (frozen hash) + (optionally)
    the PNG images. `images=False` skips PIL for a pure-ground-truth run (still hash-stable).
    Returns the spec."""
    manifest = build_manifest(seeds)
    os.makedirs(out_dir, exist_ok=True)
    for sub in ("renders", "drawings", "pairs"):
        os.makedirs(os.path.join(out_dir, sub), exist_ok=True)

    if images:
        if not _PIL:
            raise RuntimeError("PIL not available; run with images=False for ground-truth only")
        for e in manifest["examples"]:
            if e["kind"] == "render":
                render_part_image(e["draw_spec"], os.path.join(out_dir, e["image"]))
            elif e["kind"] == "drawing":
                render_drawing_image(e["known_geometry"], os.path.join(out_dir, e["image"]),
                                     e["example_id"])
            elif e["kind"] == "diff_pair":
                render_part_image(e["draw_spec_a"], os.path.join(out_dir, e["image_a"]))
                b_spec = dict(e["draw_spec_b"])
                # reflect the dropped feature in the B image so the pixels differ
                if e["known_diff"]["feature"] in ("hole", "bolt_circle"):
                    b_spec["holes"] = max(0, b_spec.get("holes", 0) - 1)
                    b_spec["bolts"] = max(0, b_spec.get("bolts", 0) - 1)
                render_part_image(b_spec, os.path.join(out_dir, e["image_b"]))

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    spec = {"corpus_id": manifest["corpus_id"], "provenance": PROVENANCE,
            "seeds": seeds, "n_examples": manifest["n_examples"],
            "hash": manifest_hash(manifest), "families": manifest["families"]}
    with open(os.path.join(out_dir, "spec.json"), "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, sort_keys=True)
    return spec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the synthetic VLM corpus v1 (T-H10).")
    ap.add_argument("out_dir", help="output corpus dir (e.g. training/corpora/vlm/v1)")
    ap.add_argument("--seeds", type=int, default=3, help="parametric seeds per family")
    ap.add_argument("--no-images", action="store_true", help="ground truth only (no PIL)")
    args = ap.parse_args(argv)
    spec = generate(args.out_dir, seeds=args.seeds, images=not args.no_images)
    print(json.dumps(spec, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
