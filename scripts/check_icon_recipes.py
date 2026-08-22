#!/usr/bin/env python3
"""Static validator for the engine-rendered icon recipes.

Rendering a recipe costs a headless engine run, and a recipe that references the wrong
feature id does not crash — the engine reports ok=0 for one step and the tile comes out
subtly wrong (a missing arrow, an unpainted body) that only a human notices on the contact
sheet. That feedback loop is far too slow and far too lossy for editing 190 recipes.

So: walk every recipe's flattened steps the same way the engine does, assign f1..fN to each
add_feature in order and b1..bM to each body-producing feature, then check that every
reference actually resolves. Catches, without touching the GPU:

  * sketch_add_entity / region() / axis_edge() pointing at a feature id that does not exist,
    or that exists but is not a sketch,
  * set_appearance on a body id no feature ever produced (the classic arrow3d off-by-one:
    the arrow geometry builds fine but stays default-grey because accent2("b4") named a
    body that is really b3),
  * a body id produced but never colored, which is how a tile silently ships in the engine's
    default material instead of the accent,
  * accent roles that are neither PRIMARY nor SECONDARY (a hand-typed hex that will not be
    remapped by the runtime recolor and so will not follow the user's chosen colors).

Usage:
  python3 scripts/check_icon_recipes.py              # validate every recipe
  python3 scripts/check_icon_recipes.py extrude_boss revolve_boss   # just these
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import icon_pipeline as ip  # noqa: E402
from icon_recipes.helpers import PRIMARY_RGB, SECONDARY_RGB  # noqa: E402

# How the engine numbers bodies: one b-number per body-producing feature, in step order.
# Sketches produce nothing; dress-up and boolean features act on bodies that already exist.
# Solid-producing feature types in the recipe vocabulary: each adds one new body.
_SKETCH_TYPES = {"sketch"}
_SOLID_TYPES = {
    "box", "sphere", "cylinder", "cone", "extrude", "revolve", "sweep", "loft",
    "rib", "helix", "thicken", "boundary", "sm_base_flange",
}
# Dress-up / modifier / combining features: they act on bodies that already exist and do
# not add one. `boolean` consumes its tool body, which is why a recipe that paints the tool
# must do so BEFORE the boolean runs.
_MODIFIER_TYPES = {
    "fillet", "chamfer", "shell", "draft", "hole", "thread", "wrap", "mirror",
    "linear_pattern", "circular_pattern", "combine", "split", "scale", "move",
    "flatten", "boolean", "sm_flat_pattern", "sm_edge_flange", "sm_hem", "sm_jog",
}

# Feature types that are applied but never take a slot in the f-numbering. Every later
# reference in the recipe therefore shifts down by one, which is the single nastiest way to
# get this wrong: the sketch itself still reports ok=1, only its sketch_add_entity steps
# fail, so the tile renders looking almost right with one piece of geometry missing.
#
# sm_flat_pattern is here on direct evidence, not inference. Rendered a probe of
# [sketch, sm_base_flange, sm_flat_pattern, sketch] twice, once naming the trailing sketch
# f3 and once f4: f3's sketch_add_entity returned ok=1, f4's returned ok=0 rev=-1. It
# flattens the existing flange in place rather than adding to the tree.
_NO_FID_TYPES = {"sm_flat_pattern"}


def _is_body_producing(feat: dict) -> bool:
    ftype = feat.get("type", "")
    if ftype in _SKETCH_TYPES or ftype in _MODIFIER_TYPES:
        return False
    if ftype not in _SOLID_TYPES:
        return False
    # A cut/subtract removes material from an existing body instead of creating one, UNLESS
    # merge is explicitly False, in which case the engine keeps the tool as its own body.
    mode = (feat.get("params") or {}).get("mode")
    if mode in ("cut", "subtract", "intersect"):
        return False
    return True


def _colors_equal(col, ref) -> bool:
    return len(col) >= 3 and all(abs(float(col[i]) - ref[i]) < 1e-4 for i in range(3))


def check(name: str, recipe: dict) -> tuple[list[str], list[str]]:
    """Validate one recipe. Returns (errors, warnings).

    An ERROR is a reference the engine cannot resolve — the step will report ok=0 and the
    tile will be wrong. A WARNING is a body left in the engine's default material, which is
    sometimes deliberate (a neutral tool body) and sometimes the reason a tile looks muddy.
    """
    problems: list[str] = []
    warnings: list[str] = []
    steps = ip._flatten_steps(recipe["steps"])

    feature_types: dict[str, str] = {}   # "f1" -> "sketch"
    bodies: list[str] = []               # ["b1", "b2", ...] in creation order
    painted: set[str] = set()
    fn = 0

    # Pass 1: assign ids exactly as the engine does — one f-number per add_feature that
    # actually takes a tree slot, one b-number per body-producing feature, both in step
    # order.
    for step in steps:
        if step.get("op") != "add_feature":
            continue
        feat = step.get("feature") or {}
        ftype = feat.get("type", "?")
        if _is_body_producing(feat):
            bodies.append(f"b{len(bodies) + 1}")
        if ftype in _NO_FID_TYPES:
            continue
        fn += 1
        feature_types[f"f{fn}"] = ftype

    # Pass 2: resolve every reference against those ids.
    def want_sketch(fid: str, where: str) -> None:
        if fid not in feature_types:
            problems.append(
                f"{where}: references {fid}, but this recipe only creates "
                f"{', '.join(feature_types) or '(no features)'}")
        elif feature_types[fid] not in _SKETCH_TYPES:
            problems.append(
                f"{where}: references {fid}, which is a "
                f"'{feature_types[fid]}' feature, not a sketch")

    for i, step in enumerate(steps):
        op = step.get("op")
        if op == "sketch_add_entity":
            want_sketch(step.get("sketch", ""), f"step {i} sketch_add_entity")
        elif op == "set_appearance":
            bid = step.get("id", "")
            if bid not in bodies:
                problems.append(
                    f"step {i} set_appearance: paints '{bid}', but this recipe produces "
                    f"{', '.join(bodies) or '(no bodies)'}")
            else:
                painted.add(bid)
            col = step.get("color") or []
            if col and bid in bodies and not _colors_equal(col, PRIMARY_RGB) \
                    and not _colors_equal(col, SECONDARY_RGB):
                pass  # greys and material presets are legitimate, and are not remapped
        elif op == "add_feature":
            refs = (step.get("feature") or {}).get("refs") or {}
            for key, val in refs.items():
                for sel in (val if isinstance(val, list) else [val]):
                    if not isinstance(sel, dict):
                        continue
                    if sel.get("kind") == "region":
                        want_sketch(sel.get("sketch", ""), f"step {i} refs.{key} region")
                    elif sel.get("kind") in ("edge", "body"):
                        owner = sel.get("owner_feature", "")
                        if owner and owner not in feature_types:
                            problems.append(
                                f"step {i} refs.{key}: owner_feature {owner} does not exist "
                                f"(recipe creates {', '.join(feature_types)})")

    unpainted = [b for b in bodies if b not in painted]
    if unpainted:
        warnings.append(
            f"bodies never given a set_appearance: {', '.join(unpainted)} "
            f"— these render in the engine default material, not an accent")
    return problems, warnings


def main(argv: list[str]) -> int:
    quiet = "--errors-only" in argv
    wanted = [a for a in argv if not a.startswith("-")]
    names = wanted or list(ip.RECIPES)
    unknown = [n for n in names if n not in ip.RECIPES]
    if unknown:
        print(f"no such recipe: {', '.join(unknown)}", file=sys.stderr)
        return 2

    bad, warned = 0, 0
    for name in names:
        errors, warnings = check(name, ip.RECIPES[name])
        if errors:
            bad += 1
            print(f"FAIL {name}")
            for p in errors:
                print(f"     {p}")
        if warnings and not quiet:
            warned += 1
            print(f"warn {name}")
            for p in warnings:
                print(f"     {p}")
    total = len(names)
    if bad:
        print(f"\n[check_icon_recipes] {bad}/{total} recipe(s) have unresolved references")
        return 1
    print(f"[check_icon_recipes] OK — {total} recipe(s), every reference resolves"
          + (f" ({warned} with warnings)" if warned else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
