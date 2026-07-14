"""The built-in agent loop: prompt -> code -> render -> visual self-check -> iterate.

The model writes a build123d script, the kernel builds the precise solid, the renderer
produces a multi-view contact sheet, and that image is fed back so the model can compare
the result against the request and either fix the code or declare it done.

Two layers of iteration:
  * within a turn  — the loop refines the code over several passes until the render matches;
  * across turns   — a ``Session`` keeps the conversation (and the last render) alive, so a
                     follow-up request like "make it 30mm taller" continues from the existing
                     design instead of starting from scratch.

Provider-agnostic: any `Provider` (Anthropic by default, OpenAI, mock) plugs in. A live
``on_event`` callback streams per-iteration detail (code, diff, reasoning, render) to a UI.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from engine import paths, render
from engine.kernel import AssemblyResult, ModelResult, run_code_multi
from mcp_server.fit_narration import _format_fit  # single home for fit/clearance text (T-F8)

# Design ledger (T-W1, Pillar 20): lazy import to keep loop.py startup fast.
# Gated by AI_CAD_LEDGER env var (default on); failures are always best-effort.
_LEDGER_ENABLED = os.environ.get("AI_CAD_LEDGER", "1") not in ("0", "false", "False")

# Advisory engine / foreman (T-W5, Pillar 20): after each build, the foreman runs
# the stage battery over the result and prepends an [ADVISORY] block to the next
# model turn.  Gated by AI_CAD_ADVISOR (default on); best-effort like the ledger.
_ADVISOR_ENABLED = os.environ.get("AI_CAD_ADVISOR", "1") not in ("0", "false", "False")

from . import config as agent_config
from .providers.base import Provider, assistant, image_part, text_part, user

SYSTEM_PROMPT = """\
You are a senior mechanical CAD engineer who designs precise, manufacturable parts as \
parametric models using build123d (a Python B-rep CAD library on the OpenCASCADE kernel). \
Units are millimeters.

Your job: turn the user's request into a build123d script, then verify the rendered \
result visually and refine until it faithfully matches the request. When the user follows \
up, MODIFY the existing design — keep what already matches and change only what they asked.

If the request comes with a BUILD PLAN (parts / dimensions / features), IMPLEMENT THE PLAN: \
use its dimensions as your named parameter values and its feature list as your checklist — \
every planned feature should exist in the script. Deviate from a planned value only when the \
geometry genuinely forces it, and say what you changed (and why) in the json "notes".

Rules for the script:
- The full `build123d` namespace is already in scope (assume `from build123d import *`).
- Assign the final solid to a variable named `result` (a Part/Solid/Compound, or a \
BuildPart context — it will be unwrapped).
- Prefer real CAD features — extrude/revolve/loft/sweep, fillet, chamfer, holes, \
counterbores, patterns, shells, threads — over mesh tricks. Keep it parametric (named \
variables for key dimensions).
- Keep it self-contained and deterministic. No file I/O, no network, no plotting.

3D SOLID PRIMITIVES (use these directly as solids):
  Box(length, width, height)
  Cylinder(radius, height)
  Sphere(radius)
  Cone(bottom_radius, top_radius, height)
  Torus(major_radius, minor_radius)

2D SKETCH -> EXTRUDE PATTERN (use for polygon / custom profiles):
  face = RegularPolygon(radius=11, side_count=6)   # hex or any n-gon (circumscribed radius)
  result = extrude(face, amount=22)

  face = Circle(radius=8)
  result = extrude(face, amount=5)

  face = Rectangle(width=30, height=20)            # rectangle is 2D - MUST be extruded
  result = extrude(face, amount=8)

BOOLEAN OPS:  result += solid   (fuse)   result -= solid  (cut)   result &= solid  (intersect)
POSITIONING:  Pos(x,y,z) * Solid(...)   or   Rot(rx,ry,rz) * Solid(...)
  Pos and Rot are STANDALONE names used exactly like that. There is NO `Mode.Pos`,
  `Align.Pos` or `.Pos` attribute on anything (writing one raises AttributeError).
  Solids are CENTERED on the origin by default: Box(L,W,H) spans -L/2..+L/2 in X, NOT 0..L.
  For corner-at-origin use align=(Align.MIN, Align.MIN, Align.MIN); otherwise place by center.
  AXES: Box(a, b, c) maps a->X, b->Y, c->Z. FIX one convention at the top of the script
  (e.g. X=width, Y=depth, Z=height) and derive EVERY feature position from those same
  variables. A cutter centered beyond +/- dim/2 on an axis lies OUTSIDE the body and
  removes NOTHING - recheck each feature's center against the half-dimensions.
PATTERNS (repeated features - hole patterns, standoffs, vents): loop and cut/fuse per spot:
  for x, y in [(x1,y1), (x2,y2), (x3,y3)]:              # explicit coordinate list
      result -= Pos(x, y, 0) * Cylinder(radius=r, height=h)
  for loc in GridLocations(x_pitch, y_pitch, nx, ny):   # regular grid of locations
      result -= loc * Cylinder(radius=r, height=h)
  (`with Locations(...): Hole(...)` works ONLY inside `with BuildPart()` - at top level it
  silently does NOTHING. Outside BuildPart always use the explicit loops above.)
FEATURES:     fillet(edges, radius=r)   chamfer(edges, length=c)
              Hole(radius, depth)  CounterBoreHole(radius, counter_bore_radius, counter_bore_depth, depth)

Example 1 - mounting plate with a bolt-hole pattern and a raised boss:
```python
plate_l, plate_w, plate_t = 80, 60, 6
hole_r = 1.7                      # M3 clearance
result = Box(plate_l, plate_w, plate_t)
result = fillet(result.edges().filter_by(Axis.Z), radius=4)
for x, y in [(-32, -22), (32, -22), (-32, 22), (32, 22)]:   # 4-corner pattern
    result -= Pos(x, y, 0) * Cylinder(radius=hole_r, height=plate_t + 2)
boss_h = 5                        # raised standoff boss on the top face, with screw bore
result += Pos(0, 0, plate_t / 2 + boss_h / 2) * Cylinder(radius=4, height=boss_h)
result -= Pos(0, 0, plate_t / 2 + boss_h / 2) * Cylinder(radius=1.25, height=boss_h + 2)
```

Example 2 - hex standoff with bore:
```python
af = 10              # across-flats (mm)
total_h = 20         # total height
bore_r = 1.6         # M3 clearance
hex_r = af / (3**0.5)  # circumscribed radius from across-flats
face = RegularPolygon(radius=hex_r, side_count=6)
result = extrude(face, amount=total_h)
result -= Cylinder(radius=bore_r, height=total_h)
```

Example 3 - build-context style (alternative):
```python
with BuildPart() as bp:
    with BuildSketch():
        RegularPolygon(radius=6, side_count=6)
    extrude(amount=20)
    Cylinder(radius=1.5, height=20, mode=Mode.SUBTRACT)
result = bp.part
```

MULTI-PART & ASSEMBLY (when the request needs SEVERAL bodies that fit together):
Instead of a single `result`, define named, individually-positioned parts and let the kernel \
assemble + fit-check them:
- `parts = {"base": base_solid, "lid": lid_solid, ...}`  — each value is a complete solid, \
positioned in the SHARED assembly coordinate space with `Pos(x,y,z) *` / `Rot(rx,ry,rz) *`.
- Optionally `assembly = Compound(children=[base_solid, lid_solid])` for explicit grouping \
(usually unnecessary — the kernel builds the assembly from `parts`).
Model each body around its own origin, then translate/rotate it into place. Define `parts` \
ONCE as a single dict (don't repeat the assignment). Mating bodies must be positioned so they \
actually meet: a peg meant to seat in a hole has to reach into it; two plates that bolt \
together should touch or leave the intended gap. But do NOT place a body where it overlaps \
solid material of another - a panel positioned AT a wall's coordinate buries itself in that \
wall (a large interference). To seat a panel/lid against a wall, offset it so it sits just \
outside (gap), or cut a matching opening/recess in the other part for it to drop into.

Example 4 - a two-part snap box (base + lid) that fit together:
```python
wall = 2.0
base = Box(40, 30, 20)
base -= Pos(0, 0, wall) * Box(40 - 2*wall, 30 - 2*wall, 20)   # hollow it out
lid = Box(40, 30, 6)
lid = Pos(0, 0, 13) * lid                                     # sits on top of the base
parts = {"base": base, "lid": lid}
```

Example 5 - a peg seated in a socket (clearance fit):
```python
socket = Box(30, 30, 20) - Cylinder(5.2, 16)                 # 10.4mm bore, 16 deep
peg = Pos(0, 0, 6) * Cylinder(5.0, 24)                       # 10mm peg, 0.2mm radial clearance
parts = {"socket": socket, "peg": peg}
```

After an assembly build you ALSO receive a FIT REPORT - the pairwise interference (overlap \
volume in mm^3 for any bodies that collide) and clearances (the gap in mm between bodies that \
don't) - plus a COLOR-CODED render with each part a distinct color and a legend. Use it: an \
unintended INTERFERENCE means two bodies overlap (shrink/move one); too large a clearance \
means a loose fit (a press-fit wants a tiny intended overlap, a slip-fit a few tenths of mm). \
Set "done": true for an assembly only once the fit report and render confirm the parts fit as \
intended.

COMMON MISTAKES TO AVOID:
- Pos/Rot are standalone: `Pos(x,y,z) * shape`. NEVER write `Mode.Pos`, `Align.Pos` or
  `shape.Pos` - none of those exist. Repeat a feature with a coordinate-list loop or
  GridLocations (see PATTERNS); `with Locations(...)` belongs inside BuildPart only.
- Rectangle / Circle / RegularPolygon are 2D - they CANNOT be used as 3D solids. Always extrude them.
- Do NOT write `Rectangle(w, h, d)` - Rectangle only takes (width, height).
- For hex profiles use RegularPolygon(radius=..., side_count=6), not Rectangle or Box.
- Use radius= and height= keywords on Cylinder/Sphere/Cone (r=/d=/h= are tolerated but discouraged).
- Solids are origin-centered (see POSITIONING). Coordinates that assume a corner at the origin
  will place features OUTSIDE the part. A cut tool that doesn't overlap the solid removes nothing.
- A hole/cutter must fully pass through its wall: make it LONGER than the wall thickness and
  orient it through the wall (use Rot for side/back panels, not a default vertical cylinder).
- fillet/chamfer take the EDGES as the FIRST POSITIONAL arg (no `edges=` keyword), and select
  the edges FROM the part you're modifying: result = fillet(result.edges().filter_by(Axis.Z),
  radius=2). chamfer uses length= (not radius=): result = chamfer(result.edges(), length=1).
  Don't pass the whole solid plus a separate edges= (that errors). Apply them to `result`
  AFTER its holes/bores so the feature isn't lost. Keep the radius/length SMALLER than the
  thinnest wall it touches - a 2mm wall cannot take a 5mm fillet (the build fails).
- Don't gold-plate texture: fine "knurling" is hard - a light chamfer or a few ridges is
  enough; never let a decorative detail break an already-correct part.
- An enclosure / case / housing must be HOLLOW: subtract the interior volume (outer Box
  minus inner Box) FIRST, then add internal features (trays, standoffs) INSIDE the cavity.
  Internal parts must not poke through or sit outside the outer walls - work out each
  feature's position from the origin-centered coordinates before placing it.
- Mounting/standoff holes are FEW and SPARSE: typically 4-10 holes at the corners and edge
  midpoints of the board/part they support, with SMALL radii (M3 clearance radius ~1.7mm,
  M4 ~2.2mm). Never carpet a face with a dense grid of large holes - list the handful of
  (x, y) mounting coordinates explicitly and loop over that list.
- Do not import from build123d - it is already in scope.

After each build you will receive: the captured stdout, mass properties (volume, bounding \
box), and a rendered contact sheet image (iso / front / back / right / top / bottom). \
STUDY THE IMAGE. Check proportions, feature presence, counts, orientation, and symmetry \
against the request - the back and bottom views exist so rear/underside features can be \
checked too; a cutout or hole pattern that appears in NO view was not actually made.

Respond every turn with EXACTLY these two fenced blocks, in this order:
1. A ```python block containing the COMPLETE script (assign `result`). If you are only \
confirming a correct prior result, you may omit this block.
2. A ```json block: {"done": <bool>, "notes": "<what you see / what you changed>"}.

Knowing when to STOP is part of the job. Set "done": true as soon as you have inspected a \
render of the CURRENT design and it satisfies the request - do NOT keep polishing a part that \
is already correct. Return another ```python block ONLY while there is a CONCRETE, visible \
problem: a wrong dimension, a missing or extra feature, a bad proportion, or - for an assembly \
- an unintended interference or an out-of-spec clearance. If the render matches the request, \
"good enough" IS done: stop and set "done": true. Don't invent new requirements the user did \
not ask for.\
"""

FIRST_USER = """\
Design request:
{request}

Produce the first version now (a ```python block) and set "done": false in the json block \
- you must inspect at least one render before finishing.\
"""

# Sent when the user follows up on an existing part. The current contact sheet is attached
# right after, so the model can see the design it is being asked to change.
REFINE_USER = """\
Follow-up request - refine the CURRENT design (shown in the image below). Keep everything \
that already matches and change only what is needed:
{request}

Return the COMPLETE updated ```python block (assign `result`) and a ```json block with \
"done": false - you must inspect a fresh render of the change before finishing.\
"""

VERIFY = """\
Compare the image above against the original request. Check these CONCRETE points first:
1. CONTAINMENT - internal features (trays, bosses, standoffs, mounts) must be INSIDE the \
body. Anything jutting out past the outer faces that shouldn't is a placement error.
2. HOLLOWNESS - if the request is an enclosure/case/box/housing, it must be hollowed out \
(interior cavity subtracted). A very high "solid material fills X% of the bounding box" \
number means it is still a solid block.
3. FEATURES - the requested features exist, in roughly the right count, size and spacing. \
Check the view that actually faces each feature (back view for rear cutouts, bottom for \
underside). A feature visible in NO view does not exist - its coordinates put it outside \
the body; fix the placement, don't declare done.
If all of these pass - even if you can imagine cosmetic tweaks the user didn't ask for - \
reply with JUST a ```json block {{"done": true, "notes": "..."}} and STOP. Only if one \
concretely fails, return a corrected ```python block with {{"done": false, "notes": "what \
is wrong + your fix"}}.\
"""

# A compact system prompt used after a context-overflow, to free tokens for small-context
# (local) models while preserving the essential build123d contract.
SYSTEM_PROMPT_COMPACT = """\
You are a CAD engineer using build123d (Python on OpenCASCADE; millimeters). The full \
build123d namespace is already in scope. Assign the final solid to `result`.
Primitives: Box(length,width,height), Cylinder(radius,height), Sphere(radius), \
Cone(bottom_radius,top_radius,height), Torus(major_radius,minor_radius). 2D (Rectangle/Circle/\
RegularPolygon) MUST be extruded: extrude(face, amount=h). Booleans: result += / -= / &= . \
Place with Pos(x,y,z)* and Rot(rx,ry,rz)* ; solids are centered on the origin by default. \
Pos/Rot are standalone - `Mode.Pos`/`Align.Pos` do not exist. Repeat features with a loop: \
`for x, y in [(0,0), (10,0)]: result -= Pos(x,y,0) * Cylinder(radius=r, height=h)` or \
`for loc in GridLocations(dx,dy,nx,ny): result -= loc * Cylinder(radius=r, height=h)`. \
fillet(edges, radius=r), chamfer(edges, length=c). An enclosure/case must be HOLLOW \
(subtract the inner volume) and internal features must sit INSIDE the walls.
For SEVERAL bodies that fit together, set `parts = {"base": solid_a, "lid": solid_b}` \
(each positioned with Pos/Rot) INSTEAD of `result`; you then get a fit report \
(interference overlap + clearances) and a color-coded render to verify the fit.
Reply with EXACTLY two fenced blocks: a ```python block with the COMPLETE script (assign \
`result`, or `parts` for an assembly), then a ```json block {"done": <bool>, "notes": "..."}. \
Set "done": true as soon as the render matches the request - don't over-polish. Keep code concise.\
"""

# ---------------------------------------------------------------------------
# PLAN MODE: before any CAD code is written, the model drafts a build plan —
# the parts, concrete dimensions that make sense, and the features of each part
# with a rough implementation route. The plan is shown to the user and injected
# into the design conversation, so the code generation aims at a thought-out
# target instead of improvising dimensions mid-script.
# ---------------------------------------------------------------------------
PLAN_PROMPT = """\
You are a senior mechanical design engineer planning a CAD build. You will receive a part \
or assembly request. Do NOT write CAD code. Produce a concise BUILD PLAN that a CAD \
programmer will implement exactly. Units are millimeters.

Work out three things:
1. PARTS - one entry per physical body. A simple object is ONE part. Use several parts only \
when pieces must separate, move, or were explicitly requested as separate (e.g. box + lid).
2. DIMENSIONS - commit to CONCRETE values for every key dimension. Honor every number the \
user gave; derive the rest from function and proportion, preferring round, manufacturable \
values. Sanity anchors: M2/M3/M4 clearance holes r=1.1/1.7/2.2; M3 heat-set boss outer \
r~3; wall thickness 2-3 for small enclosures (1.5 minimum); fillet/chamfer smaller than the \
thinnest wall it touches; PCB standoffs 4-6 tall; FDM-default diametral clearances (mm) \
to ADD to the receptacle: slip 0.40, location 0.20, press 0.10, free 0.50 (see process \
block when bound to another process).
3. FEATURES - per part, the features (holes, pockets, bosses, fillets, vents, lips, slots) \
with count, size and position, each with a short implementation route.

Implementation routes to draw from (build123d, solids are origin-centered, X=width \
Y=depth Z=height):
- plate/panel: Box(L, W, T); round corners = fillet on vertical edges; holes = loop of \
Pos(x, y, 0) * Cylinder cuts at explicit coordinates.
- enclosure/case: outer Box minus inner Box (leaves walls + floor), hollow FIRST, then add \
bosses/standoffs inside the cavity; openings = Box/Cylinder cutters through a wall.
- standoff/spacer/bushing: Cylinder (or extruded hex RegularPolygon) minus a bore Cylinder.
- bracket: two plates at 90 degrees (two Boxes), optional triangular gusset; holes per leg.
- lid: thin Box (or plate with a lip rim) sized to the opening minus clearance.
- peg/pin in a hole: size the bore with the process slip/press clearance (FDM slip +0.40 mm
  diametral on the bore for a shaft passthrough).
- repeated features: explicit (x, y) coordinate list or GridLocations.

Reply with exactly ONE ```json block, no code, shaped like:
{
  "name": "short part name",
  "summary": "one-sentence overall approach",
  "parts": [
    {
      "name": "base",
      "purpose": "what this body does",
      "dimensions": {"length": 80, "width": 60, "thickness": 6},
      "features": [
        "4x M3 clearance holes (r=1.7) at corners inset 8 - loop Pos(x,y,0)*Cylinder",
        "fillet the 4 vertical edges r=4"
      ]
    }
  ],
  "fit": "how parts mate + intended clearances ('' for a single part)"
}
Keep it tight: at most 8 parts and ~6 features per part. No prose outside the json block.\
"""

PLAN_USER = """\
Request:
{request}

Produce the build plan json now.\
"""

_PLAN_HEADER = "BUILD PLAN (follow it; adjust a value only if the geometry forces it):"


def _extract_plan(reply: str) -> dict | None:
    """Parse the planning reply into a normalized plan dict, or None if unusable."""
    raw = None
    m = _JSON_RE.search(reply)
    if m:
        raw = m.group(1)
    else:  # tolerate a bare top-level {...} without the fence
        a, b = reply.find("{"), reply.rfind("}")
        if 0 <= a < b:
            raw = reply[a:b + 1]
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or not isinstance(data.get("parts"), list):
        return None
    parts = []
    for p in data["parts"][:8]:
        if not isinstance(p, dict):
            continue
        dims = p.get("dimensions")
        if isinstance(dims, dict):
            dim_s = ", ".join(f"{k} {v}" for k, v in list(dims.items())[:10])
        else:
            dim_s = str(dims or "").strip()
        feats = []
        for f in (p.get("features") or [])[:6]:
            if isinstance(f, dict):  # tolerate {"feature": ..., "how": ...} objects
                feats.append(" - ".join(str(v) for v in f.values() if v))
            elif f:
                feats.append(str(f))
        parts.append({"name": str(p.get("name") or f"part{len(parts) + 1}"),
                      "purpose": str(p.get("purpose") or ""),
                      "dimensions": dim_s, "features": feats})
    if not parts:
        return None
    return {"name": str(data.get("name") or ""),
            "summary": str(data.get("summary") or ""),
            "parts": parts, "fit": str(data.get("fit") or "")}


def _plan_text(plan: dict) -> str:
    """Render the plan as compact text for the design conversation."""
    lines = [_PLAN_HEADER]
    head = " - ".join(x for x in (plan.get("name"), plan.get("summary")) if x)
    if head:
        lines.append(head)
    for p in plan["parts"]:
        lines.append(f'Part "{p["name"]}"'
                     + (f' - {p["purpose"]}' if p["purpose"] else "")
                     + (f'. Dimensions (mm): {p["dimensions"]}' if p["dimensions"] else ""))
        lines.extend(f"  - {f}" for f in p["features"])
    if plan.get("fit"):
        lines.append(f"Fit: {plan['fit']}")
    # T-W15: append ordered build checklist when buildgraph_first was used
    checklist = plan.get("buildgraph_checklist")
    if checklist:
        lines.append("\nBUILD ORDER (prerequisite-safe; follow this sequence):")
        lines.extend(f"  {i + 1}. {step}" for i, step in enumerate(checklist))
    warnings = plan.get("buildgraph_warnings")
    if warnings:
        lines.append("\nBUILD PLAN WARNINGS (resolve before building):")
        lines.extend(f"  ! {w}" for w in warnings)
    text = "\n".join(lines)
    return text if len(text) <= 1800 else text[:1800] + "\n...[plan truncated]"


def _make_plan(session: Session, request: str, provider: Provider,
               on_event: EventFn | None) -> dict | None:
    """Run the planning call. Best-effort: any failure just means we design without a
    plan — planning must never block the build itself."""
    turn = session.turns + 1
    _emit(on_event, kind="status", index=0, turn=turn, phase="planning",
          text="Drafting a build plan (parts, dimensions, features)...")
    try:
        reply, _ = provider.complete_ex(
            PLAN_PROMPT, [user([text_part(PLAN_USER.format(request=request))])],
            max_tokens=2000)
    except Exception as e:  # noqa: BLE001
        _emit(on_event, kind="status", index=0, turn=turn, phase="plan_failed",
              text=f"Planning skipped ({type(e).__name__}) - designing directly.")
        return None
    plan = _extract_plan(reply or "")
    if plan is None:
        _emit(on_event, kind="status", index=0, turn=turn, phase="plan_failed",
              text="Plan wasn't parseable - designing directly.")
        return None
    session.plan = plan
    _emit(on_event, kind="plan", turn=turn, plan=plan, raw=_cap(reply, 4000))
    return plan


def _make_plan_with_buildgraph(
    session: "Session",
    request: str,
    provider: "Provider",
    on_event: "EventFn | None",
) -> "dict | None":
    """Run planning then attach an ordered build checklist from aicad.buildgraph/1.

    T-W15 seam (behind AICAD_BUILDGRAPH_PLAN=1 env flag or buildgraph_first=True).
    Best-effort: buildgraph failure only emits an advisory; the plan itself is kept.
    """
    plan = _make_plan(session, request, provider, on_event)
    if plan is None:
        return None
    try:
        from services.agent.buildgraph.planner import build_graph
        graph, advisories = build_graph(plan)
        nodes = graph.get("nodes", [])
        # Build an ordered checklist: topo order with kinds and labels
        ordered = [
            f"[{n['kind']}] {n['label']}"
            for n in nodes
            if n.get("kind") != "checkpoint"
        ]
        if ordered:
            plan["buildgraph_checklist"] = ordered
        if advisories:
            blocker_findings = [
                item["finding"]
                for adv in advisories
                for item in adv.get("items", [])
                if item.get("severity") == "blocker"
            ]
            if blocker_findings:
                plan["buildgraph_warnings"] = blocker_findings
        session.plan = plan
        turn = session.turns + 1
        _emit(on_event, kind="buildgraph", turn=turn,
              node_count=len(nodes), advisory_count=len(advisories))
    except Exception:  # noqa: BLE001
        pass  # buildgraph failure never blocks the build
    return plan


# Substrings (lowercased) that mark a provider "ran out of context" error, across backends
# (LM Studio / llama.cpp, OpenAI, Anthropic). Matching one triggers auto-compaction + retry.
_CTX_OVERFLOW_SIGNS = (
    "context size", "context length", "context_length_exceeded", "maximum context",
    "context window", "available context", "prompt is too long", "too many tokens",
    "reduce the length", "exceeds the maximum", "input is too long",
)
_MAX_COMPACT_PASSES = 4

# How many refine passes one turn may take. Generous, so the agent can keep "cooking" on a
# hard part — but it normally stops far sooner, the moment a render matches the request (see
# the convergence logic in run_turn). Overridable per call or via config.json "max_iters".
DEFAULT_MAX_ITERS = 24
# Stop a turn after this many consecutive iterations that rebuild the SAME geometry without the
# model declaring done — it has stopped making progress (settled, or stuck going in circles).
_STAGNATION_LIMIT = 3
# Give up a turn when the model is stuck on failures it can't fix — quickly when the SAME
# error keeps coming back (it's looping), more patiently when each failure is DIFFERENT
# (a model working through distinct problems — a typo, then a too-big fillet — is making
# progress and deserves the extra passes). Empty replies stay on the tighter limit.
_SAME_ERROR_LIMIT = 3
_MAX_FAIL_STREAK = 8
_NO_CODE_LIMIT = 4

_PY_RE = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.DOTALL)
_JSON_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r'\{[^{}]*"done"[^{}]*\}', re.DOTALL)  # bare {...} mentioning "done"
_DONE_TRUE_RE = re.compile(r'"done"\s*:\s*true', re.IGNORECASE)
_ANY_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.DOTALL)
_SCRIPTISH_RE = re.compile(r"^\s*(?:result|parts|assembly)\s*=", re.M)

# Event callback: receives a dict describing live progress (see _emit_* below).
EventFn = Callable[[dict], None]


def _extract_code(reply: str) -> str | None:
    m = _PY_RE.search(reply)
    if m:
        return m.group(1).strip()
    # Fallback for (small) models that drop the language tag: accept the first fenced
    # block that looks like a build123d script (assigns result/parts/assembly) and isn't
    # the json verdict — otherwise a perfectly good script triggers a "no code" retry.
    for lang, body in _ANY_FENCE_RE.findall(reply):
        if lang.lower() == "json":
            continue
        body = body.strip()
        if _SCRIPTISH_RE.search(body):
            return body
    return None


def _extract_decision(reply: str) -> dict:
    """Parse the model's {"done", "notes"} verdict, tolerant of format drift.

    Tries a fenced ```json block first, then any bare ``{...}`` object that mentions "done",
    then a last-resort structured ``"done": true`` match — so a missing/garbled fence never
    silently reads as "not done" and traps the loop into running every iteration.
    """
    blobs: list[str] = []
    m = _JSON_RE.search(reply)
    if m:
        blobs.append(m.group(1).strip())
    blobs.extend(_JSON_OBJ_RE.findall(reply))
    for blob in blobs:
        try:
            d = json.loads(blob)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(d, dict) and "done" in d:
            return {"done": bool(d.get("done", False)), "notes": str(d.get("notes", ""))}
    if _DONE_TRUE_RE.search(reply):  # broken fence but an unambiguous done:true is present
        return {"done": True, "notes": ""}
    return {"done": False, "notes": ""}


def _loop_done_guardrail(decision: dict, res: Any, ledger: Any,
                         *, is_assembly: bool = False) -> str | None:
    """WI-11/13/14: reject loop done when HARD floors fail. ImportError-safe.

    Quick-loop always mutates geometry (run_code_multi), so battery is required
    unless waived via decision notes/waivers. Interference required for assemblies.
    """
    try:
        from services.agent import guardrails as _gr  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        handoff: dict = {}
        if isinstance(decision, dict):
            # Accept waivers nested under decision or notes JSON.
            if isinstance(decision.get("waivers"), (list, dict)):
                handoff["waivers"] = decision["waivers"]
            if isinstance(decision.get("handoff"), dict):
                handoff.update(decision["handoff"])
            notes = decision.get("notes") or ""
            if notes and not handoff.get("waivers"):
                # Best-effort: notes may carry a JSON waivers blob.
                try:
                    import json as _json  # noqa: PLC0415
                    maybe = _json.loads(notes) if notes.strip().startswith("{") else None
                    if isinstance(maybe, dict) and maybe.get("waivers"):
                        handoff["waivers"] = maybe["waivers"]
                except Exception:  # noqa: BLE001
                    pass
        if is_assembly:
            handoff["doc_kind"] = "assembly"
        # Fabricate a synthetic mutating call so battery is required after a build.
        calls = [{"tool": "add_features", "ok": True, "args": {}, "result": {"ok": True}}]
        # If ledger already has battery evidence from prior tools, check_done sees it.
        verdict = _gr.check_done(
            handoff, ledger=ledger, calls=calls, actor="loop",
            require_battery_if_no_mutation=True,
            force_assembly=True if is_assembly else False,
            # Orphan sketch: quick-loop has no doc tree; skip unless handoff supplies.
            skip={"orphan_sketch"} if not handoff.get("doc_id") else set(),
        )
        if verdict.ok:
            return None
        return verdict.message or verdict.reason or _gr.MSG_VERIFY_BATTERY
    except Exception:  # noqa: BLE001
        return None



def _dims(res: ModelResult) -> str:
    bb = res.bbox or {}
    size = bb.get("size")
    if not size:
        return ""
    txt = f"{size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm"
    if res.volume:
        txt += f"  |  {res.volume:.0f} mm^3"
    return txt


def _fill_ratio(res: ModelResult) -> float | None:
    """Solid material volume as a fraction of the bounding-box volume. A strong, cheap
    signal the model can reason from: an enclosure/case near 1.0 was never hollowed out;
    a plate stays high; a frame/bracket runs low."""
    bb = res.bbox or {}
    size = bb.get("size")
    if not size or not res.volume:
        return None
    box = size[0] * size[1] * size[2]
    if box <= 0:
        return None
    return res.volume / box


# _format_fit moved to services/mcp/fit_narration.py (T-F8) — imported at module top.
# Same input shape {summary, interferences:[{a,b,volume}], clearances:[{a,b,distance}]},
# same output; gsl_tools (describe_assembly) shares the identical implementation.


def _feedback_ok(res: "ModelResult | AssemblyResult") -> str:
    out = res.stdout.strip()
    is_asm = getattr(res, "is_assembly", False)
    head = f"Build OK. Bounding box: {_dims(res) or 'n/a'}. Triangles: {res.triangles}."
    fill = _fill_ratio(res)
    if fill is not None:
        head += f"\nSolid material fills {fill * 100:.0f}% of the bounding box."
        if not is_asm and fill > 0.85:
            head += (" If this is meant to be a hollow enclosure/case, it is NOT hollow - "
                     "subtract the interior volume.")
    faces = getattr(res, "faces", None)
    if faces:
        head += (f"\nFaces: {faces['total']} total, {faces['planar']} planar, "
                 f"{faces['cylindrical']} cylindrical.")
        if faces["cylindrical"] == 0:
            head += (" Zero cylindrical faces = this part has NO round holes or bosses; "
                     "if the request needs them (screw holes, standoffs, bores), they do "
                     "not exist yet - their cutters/solids missed the body.")
    if is_asm:
        names = ", ".join(p["name"] for p in (res.parts or []))
        head += f"\nParts ({len(res.parts or [])}): {names}"
        fit_txt = _format_fit(getattr(res, "fit", None))
        if fit_txt:
            head += "\n" + fit_txt
        tail = ("\nRendered ASSEMBLY contact sheet (each part a distinct color, see legend; "
                "iso / front / back / right / top / bottom):")
    else:
        tail = "\nRendered contact sheet (iso / front / back / right / top / bottom):"
    return head + (f"\nstdout:\n{out}" if out else "") + tail


def _no_vision_note(res: "ModelResult | AssemblyResult") -> str:
    """Guidance appended ONLY for non-vision providers in place of the render: no image
    is coming this turn, so point the model at the numeric substitutes already present
    in `_feedback_ok`'s digest instead of letting it guess from a picture it can't see."""
    msg = ("\n(No image available - this model does not receive renders, so judge the "
           "build from the numbers above, not a picture. The face histogram (planar vs. "
           "cylindrical count) is your only evidence for round features like holes or "
           "bosses; the bounding box and fill % are your only evidence for proportions "
           "and hollowness.")
    if getattr(res, "is_assembly", False):
        msg += (" For an assembly, the FIT REPORT above is your only evidence of "
                "interference or clearance between parts - treat any listed overlap as "
                "real unless it is an intended press-fit.")
    msg += (" Confirm every feature the request named has a matching entry in these "
            "numbers before declaring done.)")
    return msg


def _feedback_err(res: ModelResult, can_revert: bool = False) -> str:
    msg = (
        "Build FAILED. Fix the script and return a corrected ```python block.\n"
        f"Error:\n{res.error}"
        + (f"\nstdout:\n{res.stdout}" if res.stdout.strip() else "")
    )
    if can_revert:
        msg += ("\nNOTE: an earlier version already BUILT successfully. If you can't fix this "
                "error in one step, REVERT to that last working version and make a smaller, "
                "safer change (or drop the change that broke it) - don't pile on more changes "
                "that compound the failure.")
    return msg


# Prepended to VERIFY when the code changed but produced byte-identical geometry — the
# model "added" features that touched nothing (outside the body / inside a void), which it
# otherwise tends not to notice (and a weak judge then hallucinates them in the render).
NOOP_CHANGE_NOTE = """\
NOTE: your script changed but the resulting geometry is IDENTICAL to the previous build \
(same volume, same triangle count). Whatever you added or edited had NO effect - new \
cutters probably sit outside the body or inside an already-empty region, or new solids \
were never fused into `result`. Re-derive those features' coordinates from the body's \
half-dimensions, check which AXIS each one should act along (a hole through a wall runs \
along the wall's normal), and make sure every cutter overlaps solid material.
"""


def _repeat_nudge(res: "ModelResult | AssemblyResult") -> str:
    """Sent when the model resubmits IDENTICAL code without finishing — a pointed kick to
    either declare done or make a concrete change, so it stops spinning on the same script."""
    msg = ("You returned the SAME script as the previous step, so nothing changed. Do ONE of "
           "these:\n"
           '- If the render already satisfies the request, reply with just {"done": true}.\n'
           "- Otherwise make a CONCRETE, specific change this time (different dimensions or "
           "positions) - do NOT resend identical code.")
    fit = getattr(res, "fit", None)
    if fit and fit.get("interferences"):
        it = fit["interferences"][0]
        msg += (f"\nThe interference between {it['a']} and {it['b']} ({it['volume']:.0f} mm^3) is "
                "still unresolved: the two bodies occupy the same space. MOVE one of them (change "
                "its Pos along the overlapping axis) or CUT an opening for it - resending the same "
                "positions cannot fix it.")
    return msg


def _advisory_block(res: "ModelResult | AssemblyResult", seq: int,
                    ledger=None, output_text: str = "",
                    tool_names: "list[str] | None" = None,
                    cooldown_state: "dict | None" = None) -> str:
    """Run the foreman over a build result and return an [ADVISORY] block (T-W5).

    The loop path has no doc_id — only ``res.fit`` (interference by part name +
    volume) and ``res.error`` (rebuild) are available, so only those findings
    fire (see services/agent/advisor/adapters.py).  Best-effort: any failure
    returns "" so the design loop never breaks.  Advisories are also logged to
    the ledger event stream (§3) when one is present.

    T-W21: ``output_text`` / ``tool_names`` / ``cooldown_state`` are threaded
    into the Observation so trigger-based context injection fires on this turn.
    """
    if not _ADVISOR_ENABLED:
        return ""
    try:
        from services.agent.advisor import adapters as _adv_adapters
        from services.agent.advisor import engine as _adv_engine
        from services.agent.advisor import render as _adv_render
        obs = _adv_adapters.observe_build(res, seq=seq)
        # T-W21: wire trigger fields so context packs fire on matching turns.
        obs.output_text = output_text or ""
        obs.tool_names = list(tool_names or [])
        obs.cooldown_state = cooldown_state

        def _ledger_sink(events: list) -> None:
            if ledger is not None and events:
                try:
                    for ev in events:
                        ledger.append("foreman", "trigger_fired", ev)
                except Exception:  # noqa: BLE001
                    pass

        obs.trigger_ledger_sink = _ledger_sink

        if not obs.findings and not obs.output_text and not obs.tool_names:
            return ""
        advisory = _adv_engine.build_advisory(obs)
        if not advisory.get("items"):
            return ""
        if ledger is not None:
            try:
                ledger.append("foreman", "advisory", advisory)
            except Exception:  # noqa: BLE001
                pass
        return _adv_render.render_advisory(advisory)
    except Exception:  # noqa: BLE001
        return ""


def _diff(old: str | None, new: str) -> str | None:
    """Unified diff old->new, or None when there is nothing meaningful to show."""
    if not old or old == new:
        return None
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="previous", tofile="current", lineterm="\n",
    )
    return "".join(lines)


def _save_iteration(out_dir: Path, name: str, i: int, code: str, res: ModelResult) -> None:
    """Persist each iteration's script (plus a 'latest' copy) so a failed design is
    debuggable and the user can inspect / reuse the generated build123d code."""
    try:
        status = "OK" if res.ok else "FAILED"
        tail = ""
        if not res.ok and res.error:
            lines = res.error.strip().splitlines()
            tail = f"  ({lines[-1].strip()})" if lines else ""
        header = f"# AI-CAD generated build123d script - iteration {i} - {status}{tail}\n\n"
        (out_dir / f"{name}_iter{i:02d}.py").write_text(header + code, encoding="utf-8")
        (out_dir / f"{name}.py").write_text(header + code, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass  # persisting code is best-effort; never let it break the design loop


@dataclass
class IterationLog:
    index: int
    ok: bool
    done: bool
    notes: str
    error: str | None = None
    code: str | None = None
    diff: str | None = None
    thinking: str | None = None
    contact_sheet: str | None = None
    stl_path: str | None = None   # this pass's built mesh, for live viewport preview
    dims: str | None = None
    volume: float | None = None
    triangles: int | None = None
    stdout: str | None = None
    is_assembly: bool = False
    parts: list | None = None     # per-part [{name, stl_path, bbox, volume, ...}] (assemblies)
    fit: dict | None = None       # interference/clearance report (assemblies)


@dataclass
class AgentRun:
    request: str
    success: bool
    iterations: int
    final_code: str | None
    result: dict | None           # ModelResult / AssemblyResult as dict
    contact_sheet: str | None
    out_dir: str
    session_id: str | None = None
    turn: int = 1
    log: list = field(default_factory=list)
    is_assembly: bool = False
    fit: dict | None = None       # final assembly fit report (None for single parts)
    converged: bool = False       # True = the model declared/settled done; False = hit the cap
    stop_reason: str = ""         # confirmed | stabilized | failed | no_code | max_iters
    plan: dict | None = None      # the session's build plan (parts/dimensions/features)


@dataclass
class Session:
    """Conversation state for one part, kept alive across follow-up turns so the agent
    can iterate on its own prior work instead of restarting each message."""
    name: str
    messages: list = field(default_factory=list)
    last_code: str | None = None
    last_ok_res: dict | None = None      # ModelResult as dict (for context / fallback)
    contact_sheet: str | None = None     # latest render, re-shown on the next turn
    turns: int = 0
    compact: bool = False                # set once a provider overflows: stay in compact mode
    plan: dict | None = None             # build plan drafted before the first code pass
    role_prompt: str | None = None       # T-W7: role card system prompt (overrides SYSTEM_PROMPT)


def _emit(on_event: EventFn | None, **data) -> None:
    if on_event is not None:
        try:
            on_event(data)
        except Exception:  # noqa: BLE001
            pass  # a flaky UI sink must never break the design loop


# T-DP18: ordered pipeline phase names (post-INTAKE) used to build the
# remaining-subtask list for ETA estimation.  Mirrors _PHASE_NODE_PREFIXES
# order.  CLARIFY and CORRECT are not in the happy path and are omitted;
# the estimator handles them if reached by their phase-name prefix fallback.
_ETA_PHASE_ORDER = ("specify", "plan", "build", "verify", "deliver")


def _emit_eta_for_remaining(
    on_event: EventFn | None,
    current_phase: str,
    *,
    run_id: str | None = None,
    tps_aggregate: float | None = None,
) -> None:
    """Emit a ``mission.eta`` event for phases not yet completed.

    Called after each phase entry so the GUI clock chip refreshes as the
    mission advances.  The *current_phase* is included in remaining (it is
    in-progress).  Uses the ETAEstimator with token costs from the T-W29/W31
    bench (benchmarks/tokenmaxx/results/matrix.json).

    The event is written to TWO sinks so the GUI receives it regardless of
    how the caller wires ``on_event``:

    1. ``on_event({...})``  — the live callback (updates in-process listeners
       such as the MCP WS bridge; may be None).
    2. ``EventJournal(run_id).emit(...)`` — the per-run events.jsonl file that
       the mission-control panel polls via dag_status → seedFromJsonlText.
       Written only when *run_id* is provided.

    NOTE on honesty: the phase→bench-shape mapping (_PHASE_TO_SHAPE in
    eta_estimator.py) is a heuristic, not directly measured.  The token
    counts themselves are real bench measurements.  When no data exists for
    a phase (unknown=True) the label degrades to "ETA unknown" rather than
    fabricating a number.  The TPS denominator defaults to the integrator
    smoke value (482 tok/s, T-W31) when no live rate is injected.
    """
    try:
        from services.agent.eta_estimator import ETAEstimator, SubtaskSpec

        # Build remaining-subtask list: current phase + all that follow it.
        idx = _ETA_PHASE_ORDER.index(current_phase) if current_phase in _ETA_PHASE_ORDER else -1
        if idx < 0:
            remaining_phases = list(_ETA_PHASE_ORDER)
        else:
            remaining_phases = list(_ETA_PHASE_ORDER[idx:])

        remaining = [SubtaskSpec(node_id=ph, role_shape=ph) for ph in remaining_phases]
        if not remaining:
            return

        est = ETAEstimator(tps_aggregate=tps_aggregate)
        meta = est.mission_eta(remaining)
        label = ETAEstimator.format_eta(meta)

        # Per-node labels for the DAG widget (T-DP17 accessor).
        nodes_labels: dict[str, str] = {}
        for ph, ne in meta.node_etas.items():
            nodes_labels[ph] = ETAEstimator.format_node_eta(ne)

        payload: dict = {
            "label": label,
            "remaining_phases": remaining_phases,
        }
        if meta.eta_seconds is not None:
            payload["eta_s"] = meta.eta_seconds
        if meta.eta_low_s is not None:
            payload["low_s"] = meta.eta_low_s
        if meta.eta_high_s is not None:
            payload["high_s"] = meta.eta_high_s
        if nodes_labels:
            payload["nodes"] = nodes_labels

        # Sink 1: live callback (for in-process / WS listeners).
        if on_event is not None:
            try:
                on_event({"kind": "mission.eta", **payload})
            except Exception:  # noqa: BLE001
                pass

        # Sink 2: EventJournal → events.jsonl (for dag_status panel replay).
        if run_id is not None:
            try:
                from services.dag.event_journal import EventJournal
                EventJournal(run_id).emit("mission.eta", payload)
            except Exception:  # noqa: BLE001
                pass

    except Exception:  # noqa: BLE001
        pass  # ETA is best-effort — never block the mission


def _emit_ai_artifact(
    on_event: EventFn | None,
    run_id: str,
    iteration_index: int,
    insertion_point: str,
    model_info: dict,
    prompt_hash: str,
    latency_ms: float,
    consent: str,
    verifier_scores: dict | None = None,
    input_ref: str = "",
    output_ref: str = "",
    base_revision: int = 0,
) -> None:
    """Emit an ``ai_artifact`` telemetry event alongside the existing iteration event.

    The artifact_id is the first 8 hex characters of SHA-256(run_id + str(iteration_index)),
    giving a short, deterministic, collision-resistant id that ties back to the run.

    Fields:
      run_id           — opaque session/run identifier (e.g. session.name)
      iteration_index  — 0-based pass counter within this turn
      insertion_point  — "IP-1" for the design loop
      model_info       — {"provider": str, "model": str, "adapter": None}
      prompt_hash      — first 16 hex chars of SHA-256 of the prompt string
      latency_ms       — wall-clock ms for this iteration's generate+build cycle
      consent          — tier from config.consent_tier() ("off" | "meta" | "full")
      verifier_scores  — dict populated by caller if a verifier ran, else {}
      input_ref        — sidecar path to serialised prompt if stored, else ""
      output_ref       — sidecar path to serialised output if stored, else ""
      base_revision    — model revision number before this output (0 = first pass)

    The event is written to the existing event sink (on_event / events.jsonl).
    The exporter (T-H5) is responsible for filtering by consent tier; the emitter
    always writes locally regardless of tier.
    """
    artifact_id = hashlib.sha256(
        (run_id + str(iteration_index)).encode()
    ).hexdigest()[:8]

    _emit(
        on_event,
        kind="ai_artifact",
        artifact_id=artifact_id,
        run_id=run_id,
        insertion_point=insertion_point,
        model=model_info,
        prompt_hash=prompt_hash,
        input_ref=input_ref,
        output_ref=output_ref,
        base_revision=base_revision,
        verifier_scores=verifier_scores if verifier_scores is not None else {},
        latency_ms=latency_ms,
        consent=consent,
    )


def _cap(s: str | None, n: int) -> str | None:
    if not s:
        return s
    return s if len(s) <= n else s[:n] + "\n... (truncated)"


def _is_context_overflow(err: Exception) -> bool:
    msg = str(err).lower()
    return any(s in msg for s in _CTX_OVERFLOW_SIGNS)


_ERR_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception)\b")


def _err_sig(err: str | None) -> str:
    """A stable signature for a build error: the exception line of the traceback (skipping
    any appended kernel HINT), so 'the same error again' can be told apart from a new one."""
    if not err:
        return ""
    lines = [ln.strip() for ln in err.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _ERR_LINE_RE.match(ln):
            return ln
    return lines[-1] if lines else ""


def _strip_images(messages: list, keep_last: int) -> int:
    """Replace image parts with a short placeholder, keeping only the last ``keep_last``
    images (images dominate token cost for small-context vision models)."""
    locs = [(mi, pi) for mi, m in enumerate(messages)
            for pi, p in enumerate(m["content"]) if p.get("type") == "image"]
    drop = set(locs if keep_last <= 0 else locs[:-keep_last])
    n = 0
    for mi, m in enumerate(messages):
        out = []
        for pi, p in enumerate(m["content"]):
            if (mi, pi) in drop:
                out.append(text_part("[earlier render omitted to fit context]"))
                n += 1
            else:
                out.append(p)
        m["content"] = out
    return n


def _keep_recent(messages: list, keep_pairs: int) -> int:
    """Keep messages[0] (the original request) + the most recent ``keep_pairs`` exchanges,
    preserving user/assistant alternation. The history is [user, assistant, ..., user]."""
    n = 2 * keep_pairs
    if len(messages) <= n + 1:
        return 0
    dropped = len(messages) - 1 - n
    messages[:] = messages[:1] + messages[-n:]
    return dropped


def _truncate_text(messages: list, limit: int) -> None:
    for m in messages:
        for p in m["content"]:
            if p.get("type") == "text" and len(p.get("text", "")) > limit:
                p["text"] = p["text"][:limit] + "\n...[truncated]"


def _apply_compaction(messages: list, level: int) -> None:
    """Shrink the message history in place; higher levels are more aggressive (cumulative)."""
    if level >= 1:
        _strip_images(messages, keep_last=1)     # keep only the latest render
    if level >= 2:
        _keep_recent(messages, keep_pairs=3)     # drop older exchanges
    if level >= 3:
        _strip_images(messages, keep_last=0)     # drop all images -> stats-only
        _keep_recent(messages, keep_pairs=2)
        _truncate_text(messages, 1200)
    if level >= 4:
        _keep_recent(messages, keep_pairs=1)     # just the request + the last exchange
        _truncate_text(messages, 600)


def _generate(session: Session, provider: Provider, on_event: EventFn | None,
              idx: int, turn: int) -> tuple[str, str]:
    """Get the model's next reply. On a context-overflow error, switch to the compact system
    prompt, compact the (in-place) history, and retry — so small-context models self-heal
    instead of dying. Big-context models (Claude) never trigger this."""
    # config.json "system_prompt" (set via the GUI Settings > Agent tab) fully replaces the
    # built-in prompt. The compact prompt still takes over on a context overflow — a custom
    # prompt that overflowed once would just overflow again.
    override = ""
    try:
        override = (agent_config.load_config().get("system_prompt") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    # T-W7: a role card's system prompt scopes this run (single-role loop or a
    # role-typed DAG node). It takes precedence over the built-in prompt; a GUI
    # config override still wins (the human's explicit global choice). The compact
    # prompt still takes over on a context overflow, as before.
    role_prompt = (getattr(session, "role_prompt", None) or "").strip()
    base_prompt = override or role_prompt or SYSTEM_PROMPT
    for level in range(_MAX_COMPACT_PASSES + 1):
        compact = session.compact or level > 0
        system = SYSTEM_PROMPT_COMPACT if compact else base_prompt
        max_tokens = 2048 if compact else 8000
        try:
            return provider.complete_ex(system, session.messages, max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            if not _is_context_overflow(e) or level >= _MAX_COMPACT_PASSES:
                raise
            session.compact = True
            _apply_compaction(session.messages, level + 1)
            _emit(on_event, kind="status", index=idx, turn=turn, phase="compacting",
                  text=f"Context full - auto-compacting history (pass {level + 1})...")


def run_turn(session: Session, request: str, provider: Provider,
             on_event: EventFn | None = None, max_iters: int | None = None,
             plan_first: bool = False,
             buildgraph_first: bool = False,
             role: str | None = None) -> AgentRun:
    """Run one user turn against a persistent ``session``, refining until the render matches
    the request (the model declares "done"), it stops making progress, or ``max_iters`` is hit
    (default ``DEFAULT_MAX_ITERS``). With ``plan_first``, the first turn opens with a planning
    call (parts / dimensions / features) that is streamed as a "plan" event and injected into
    the design conversation. With ``buildgraph_first`` (T-W15), the plan is also run through
    the LLM-free build-order planner to produce an ordered checklist injected into the prompt.
    Streams progress via ``on_event`` and returns an AgentRun."""
    if max_iters is None:
        max_iters = DEFAULT_MAX_ITERS
    # OFFLINE MODE defense-in-depth (C35): the legacy loop is the second call site (the
    # orchestrator run_design is the first). Refuse a non-local provider when offline mode is set.
    agent_config.assert_offline_safe(provider)
    out_dir = paths.model_dir(session.name)
    first_turn = not session.messages

    # Role card seam (T-W7): a single-role run applies the named role card's system
    # prompt + tool allowlist for the whole turn. Best-effort — an unknown role or a
    # missing roles module leaves the default behaviour intact (never breaks a run).
    # NOTE: the allowlist policy is process-global (services/mcp/tool_registry) and is
    # left pinned to this role after the run — intended for a one-shot role-scoped run.
    # The DAG node runner (services/dag/node_runner) re-applies per node, so multi-role
    # missions never inherit a stale policy from here.
    if role:
        try:
            from services.agent import roles as _roles
            _card = _roles.load_role(role)
            session.role_prompt = _card.system_prompt
            try:
                from mcp_server import tool_registry as _reg
                _card.apply_tool_policy(_reg)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    # Design ledger seam (T-W1, Pillar 20): create/load per-run ledger.
    # Best-effort — a ledger failure must never break the run (mirrors interaction._emit).
    _ledger = None
    if _LEDGER_ENABLED:
        try:
            from services.agent.ledger import Ledger
            _ledger = Ledger.load(session.name)
        except Exception:  # noqa: BLE001
            pass

    # AICAD_BUILDGRAPH_PLAN env var enables buildgraph_first site-wide without code changes.
    import os as _os
    if _os.environ.get("AICAD_BUILDGRAPH_PLAN") == "1":
        buildgraph_first = True

    if first_turn:
        if buildgraph_first:
            _make_plan_with_buildgraph(session, request, provider, on_event)
        elif plan_first:
            _make_plan(session, request, provider, on_event)
        opening = FIRST_USER.format(request=request)
        if session.plan:
            opening += "\n\n" + _plan_text(session.plan)
        session.messages.append(user([text_part(opening)]))
    else:
        parts = [text_part(REFINE_USER.format(request=request))]
        prior = session.contact_sheet
        if prior and getattr(provider, "supports_vision", True) and Path(prior).exists():
            parts.append(image_part(prior))
            parts.append(text_part("(Above: the current design you are modifying.)"))
        session.messages.append(user(parts))

    # M20: record what context was injected into this turn's prompt (plan on first
    # turn, prior render on refine turns). Emitted just below once the span exists.
    _ctx_injections: list[tuple[str, str]] = []
    if first_turn and session.plan:
        _ctx_injections.append(("plan", "build plan (parts/dimensions/features)"))
    if (not first_turn) and session.contact_sheet:
        _ctx_injections.append(("previous_output", "prior render of the current design"))

    session.turns += 1
    turn = session.turns

    # M20: per-turn root span for the agent-execution event stream. Every
    # `agent.thought` / `agent.context_injected` emitted below links to this span
    # as parent, so the GUI can build the execution DAG tree for the chat turn.
    # The legacy single-part loop is session-scoped (no mission run_id); events
    # stream LIVE via on_event (→ GUI drain). A run_id, when the caller threads one
    # onto the session, also persists them to events.jsonl for replay.
    _ae = None
    _turn_span = None
    _run_journal = None
    if on_event is not None:
        try:
            from services.agent import agent_events as _ae  # noqa: PLC0415
            _turn_span = _ae.new_span_id()
            _rid = getattr(session, "run_id", None)
            if _rid:
                from services.dag.event_journal import EventJournal  # noqa: PLC0415
                _run_journal = EventJournal(_rid)
        except Exception:  # noqa: BLE001
            _ae = None

    # M20: emit the context-injection events now that the turn span exists.
    if _ae is not None and _turn_span is not None:
        for _src, _label in _ctx_injections:
            try:
                _k, _p = _ae.context_injected_event(
                    session.name, _src, _label,
                    parent_id=_turn_span, actor=session.name)
                _ae.emit_event(_k, _p, live=on_event, journal=_run_journal)
            except Exception:  # noqa: BLE001
                pass

    log: list[IterationLog] = []
    last_code: str | None = session.last_code
    prev_built_code: str | None = session.last_code   # diff baseline across the whole session
    last_built: str | None = None     # geometry built in the prior pass THIS turn (change baseline)
    last_res: AssemblyResult | None = None
    last_ok_res: AssemblyResult | None = None
    contact_sheet: str | None = session.contact_sheet
    stagnant = 0                      # consecutive passes that rebuilt identical geometry
    fail_streak = 0                   # consecutive failed builds / empty replies
    same_err_streak = 0               # consecutive failures with the SAME error signature
    last_err_sig = ""
    prev_metrics = None               # (volume, triangles) of the previous successful build
    converged = False
    stop_reason = "max_iters"

    for i in range(max_iters):
        _iter_t0 = time.monotonic()
        _emit(on_event, kind="status", index=i, turn=turn, phase="thinking",
              text=f"Iteration {i + 1}: generating build123d...")
        reply, thinking = _generate(session, provider, on_event, i, turn)
        session.messages.append(assistant([text_part(reply)]))

        # M20: emit the model's COMPLETED thinking block (summary-on-arrival). The
        # provider seam (Provider.complete/complete_ex) returns whole strings, not a
        # token stream, so we emit one `agent.thought` per block. `agent.thought.delta`
        # (contract /2) is reserved for a future streaming seam — no producer yet.
        if _ae is not None and _turn_span is not None and thinking:
            try:
                _k, _p = _ae.thought_event(
                    session.name, thinking,
                    model=getattr(provider, "name", None),
                    parent_id=_turn_span, actor=session.name)
                _ae.emit_event(_k, _p, live=on_event, journal=_run_journal)
            except Exception:  # noqa: BLE001 — event emission never breaks the loop
                pass

        code = _extract_code(reply)
        decision = _extract_decision(reply)
        new_code = code if code is not None else last_code

        if new_code is None:
            fail_streak += 1
            if fail_streak >= _NO_CODE_LIMIT:
                stop_reason = "no_code"
                break
            session.messages.append(user([text_part(
                "No ```python block found. Provide the full script assigning `result` "
                "(or `parts` for an assembly).")]))
            _emit(on_event, kind="status", index=i, turn=turn, phase="retry",
                  text="No code block returned - asking again.")
            continue

        diff = _diff(prev_built_code, new_code) if code is not None else None
        _emit(on_event, kind="status", index=i, turn=turn, phase="building",
              text=f"Iteration {i + 1}: building solid...")

        res = run_code_multi(new_code, out_dir, session.name)
        last_code, last_res = new_code, res
        _save_iteration(out_dir, session.name, i, new_code, res)

        sheet_i: str | None = None
        if res.ok and res.stl_path:
            last_ok_res = res
            _emit(on_event, kind="status", index=i, turn=turn, phase="rendering",
                  text=f"Iteration {i + 1}: rendering views...")
            # Assemblies get a color-coded per-part contact sheet (falling back to the
            # combined mesh); single parts use the plain grid as before.
            if res.is_assembly and res.parts:
                sheet_i = render.render_assembly_grid(
                    res.parts, out_dir / f"{session.name}_grid_{i:02d}.png")
            if not sheet_i:
                sheet_i = render.render_grid(
                    res.stl_path, out_dir / f"{session.name}_grid_{i:02d}.png")
            if sheet_i:
                contact_sheet = sheet_i
                # keep a stable "latest" alias for the next turn / external callers
                try:
                    Path(out_dir / f"{session.name}_grid.png").write_bytes(
                        Path(sheet_i).read_bytes())
                except Exception:  # noqa: BLE001
                    pass

        entry = IterationLog(
            index=i, ok=res.ok, done=bool(decision["done"]) and res.ok,
            notes=decision["notes"], error=res.error,
            code=new_code, diff=diff, thinking=_cap(thinking, 6000),
            contact_sheet=sheet_i, stl_path=res.stl_path if res.ok else None,
            dims=_dims(res) if res.ok else None,
            volume=res.volume, triangles=res.triangles or None,
            stdout=_cap(res.stdout.strip() or None, 2000),
            is_assembly=res.is_assembly,
            parts=res.parts if res.is_assembly else None,
            fit=res.fit if res.is_assembly else None,
        )
        log.append(entry)
        prev_built_code = new_code
        _emit(on_event, kind="iteration", turn=turn, **asdict(entry))

        # Emit an ai_artifact telemetry event for every successful build.
        # The exporter (T-H5) filters by consent tier; we always write locally.
        if res.ok and new_code is not None:
            _iter_latency_ms = (time.monotonic() - _iter_t0) * 1000.0
            try:
                _cfg = agent_config.load_config()
                _consent = agent_config.consent_tier(_cfg)
                _provider_name = getattr(provider, "name", "unknown")
                _model_name = getattr(provider, "model", "unknown")
            except Exception:  # noqa: BLE001
                _consent = "off"
                _provider_name = "unknown"
                _model_name = "unknown"
            # Prompt hash: sha256 of the last user message's text content (first 16 hex chars).
            try:
                _last_user = session.messages[-2] if len(session.messages) >= 2 else None
                _prompt_text = ""
                if _last_user:
                    for _part in (_last_user.get("content") or []):
                        if _part.get("type") == "text":
                            _prompt_text += _part.get("text", "")
                _prompt_hash = hashlib.sha256(_prompt_text.encode()).hexdigest()[:16]
            except Exception:  # noqa: BLE001
                _prompt_hash = ""
            # T-BL22: when the caller routed this call through a RoutingTable (T-H9), the
            # RouteDecision rides on the provider — its telemetry_tag() is the decided
            # write-point for model.adapter, so every canary/adapter response is tagged.
            _decision = getattr(provider, "_routing_decision", None)
            if _decision is not None:
                try:
                    _model_info = _decision.telemetry_tag()
                except Exception:  # noqa: BLE001
                    _model_info = {"provider": _provider_name, "model": _model_name,
                                   "adapter": None}
            else:
                _model_info = {"provider": _provider_name, "model": _model_name,
                               "adapter": None}
            _emit_ai_artifact(
                on_event=on_event,
                run_id=session.name,
                iteration_index=i,
                insertion_point="IP-1",
                model_info=_model_info,
                prompt_hash=_prompt_hash,
                latency_ms=_iter_latency_ms,
                consent=_consent,
            )

            # Ledger: record artifact event for each successful build (best-effort).
            if _ledger is not None:
                try:
                    import hashlib as _hl
                    _art_id = "art_" + _hl.sha256(
                        (session.name + str(i)).encode()
                    ).hexdigest()[:12]
                    _stl = str(res.stl_path) if res.stl_path else ""
                    _sheet = str(sheet_i) if sheet_i else ""
                    _art_path = _sheet or _stl
                    _ledger.add_artifact(
                        actor="loop",
                        kind="render" if _sheet else "doc",
                        artifact_id=_art_id,
                        path=_art_path,
                        stage="feature",
                        note=f"iter={i} stl={_stl}",
                    )
                except Exception:  # noqa: BLE001
                    pass

        if res.ok and res.stl_path:
            fail_streak = 0
            same_err_streak = 0
            last_err_sig = ""
            changed = new_code != last_built
            metrics = (round(res.volume, 4) if res.volume else None, res.triangles)
            noop_change = changed and prev_metrics is not None and metrics == prev_metrics
            prev_metrics = metrics
            # 1) The model confirms it's done — accept once it has seen a render THIS turn and
            #    is confirming the CURRENT design (not declaring done on a build it just changed
            #    and hasn't seen rendered yet; that one gets a render + a final confirm first).
            #    HARD floors (WI-11/13/14): battery / interference / orphan-sketch before done.
            if decision["done"] and i >= 1 and not changed:
                _done_block = _loop_done_guardrail(
                    decision, res, _ledger, is_assembly=bool(res.is_assembly))
                if _done_block:
                    # Reject done: feed the guardrail message and keep iterating.
                    session.messages.append(user([text_part(_done_block)]))
                    prompt_text = _done_block
                    last_built = new_code
                    continue
                converged = True
                stop_reason = "confirmed"
                # Ledger: record a decision event when the model declares done (best-effort).
                if _ledger is not None:
                    try:
                        import uuid as _uuid
                        _dec_id = "D_done_" + _uuid.uuid4().hex[:8]
                        _ledger.add_decision(
                            actor="loop",
                            decision_id=_dec_id,
                            stage="feature",
                            text="Model confirmed design complete",
                            rationale=decision.get("notes", "") or "model declared done",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                break
            # 2) Stagnation — geometry stopped changing but the model won't declare done.
            #    First repeat(s) get a pointed nudge to actually change something; if it keeps
            #    resending identical code, treat it as settled and stop (don't burn the budget).
            if not changed:
                stagnant += 1
                if stagnant >= _STAGNATION_LIMIT and i >= 1:
                    converged = True
                    stop_reason = "stabilized"
                    break
                prompt_text = _repeat_nudge(res)   # repeated identical code -> kick it to change
            else:
                stagnant = 0
                last_built = new_code
                prompt_text = VERIFY               # genuine progress -> normal verdict request
                if noop_change:                    # edited code, byte-identical geometry
                    prompt_text = NOOP_CHANGE_NOTE + "\n" + VERIFY
            # Foreman advisory (T-W5 + T-W21 §20): computed before the verdict prompt
            # so it leads the next user turn — block prepended to the feedback.
            # T-W21: pass reply text so keyword triggers can fire on model output.
            _adv = _advisory_block(res, seq=i, ledger=_ledger,
                                   output_text=reply or "")
            # Show the render (+ fit report) and ask for the next step.
            if sheet_i and getattr(provider, "supports_vision", True):
                feedback = [text_part(_feedback_ok(res)), image_part(sheet_i),
                            text_part(prompt_text)]
            else:
                feedback = [text_part(_feedback_ok(res) + _no_vision_note(res) +
                                      "\n" + prompt_text)]
            if _adv:
                feedback.insert(0, text_part(_adv))
            session.messages.append(user(feedback))
        else:
            fail_streak += 1
            sig = _err_sig(res.error)
            same_err_streak = same_err_streak + 1 if (sig and sig == last_err_sig) else 1
            last_err_sig = sig
            if same_err_streak >= _SAME_ERROR_LIMIT or fail_streak >= _MAX_FAIL_STREAK:
                stop_reason = "failed"
                break
            # Foreman advisory on the rebuild failure (T-W5 + T-W21 §20): wraps the
            # error as a rebuild-class finding; also fires keyword triggers.
            _adv = _advisory_block(res, seq=i, ledger=_ledger,
                                   output_text=reply or "")
            # If a prior version built, tell the model it can revert instead of devolving.
            can_revert = last_ok_res is not None or session.last_ok_res is not None
            err_parts = [text_part(_feedback_err(res, can_revert=can_revert))]
            if _adv:
                err_parts.insert(0, text_part(_adv))
            session.messages.append(user(err_parts))

    # Prefer the last successful build over the last attempted build, so a failing
    # refinement iteration doesn't mask a good model that was already produced.
    best_res = last_ok_res or last_res
    success = bool(best_res and best_res.ok)

    # Persist session state for the next turn.
    session.last_code = last_code
    if best_res is not None:
        session.last_ok_res = asdict(best_res)
    session.contact_sheet = contact_sheet

    # A closing status so the UI can say WHY the turn ended (and whether a nudge would help).
    if stop_reason == "max_iters":
        done_text = (f"Stopped after {len(log)} iterations (cap). Ask me to keep going to "
                     f"continue refining.")
    elif stop_reason in ("failed", "no_code"):
        done_text = f"Stopped after {len(log)} iterations - couldn't get a clean build."
    else:
        done_text = f"Finished in {len(log)} iteration(s)."
    _emit(on_event, kind="status", index=len(log), turn=turn, phase="done",
          text=done_text, converged=converged, stop_reason=stop_reason)

    return AgentRun(
        request=request,
        success=success,
        iterations=len(log),
        final_code=last_code,
        result=asdict(best_res) if best_res else None,
        contact_sheet=contact_sheet,
        out_dir=str(out_dir),
        session_id=session.name,
        turn=turn,
        log=[asdict(x) for x in log],
        is_assembly=bool(best_res and best_res.is_assembly),
        fit=best_res.fit if best_res and best_res.is_assembly else None,
        converged=converged,
        stop_reason=stop_reason,
        plan=session.plan,
    )


def run(request: str, provider: Provider, name: str = "agent_model",
        max_iters: int | None = None, on_event: EventFn | None = None,
        role: str | None = None) -> AgentRun:
    """One-shot design (no persistent session) - back-compatible entry point used by the
    CLI and tests. For multi-turn refinement, create a ``Session`` and call ``run_turn``.
    ``role`` (T-W7): apply a predefined role card's system prompt + tool allowlist."""
    return run_turn(Session(name=name), request, provider,
                    on_event=on_event, max_iters=max_iters, role=role)


# ---------------------------------------------------------------------------
# MISSION MODE (T-W19): the copilot's front door for full missions.
#
# ``run_turn`` above is the single-part quick mode (one Session, one part, refine
# until the render matches).  ``run_mission`` is the OTHER front door: a request
# that decomposes into several deliverables (parts + drawings + CAM + reports)
# runs through the 8-phase machine (docs/COPILOT_MISSION_PIPELINE.md) — INTAKE
# builds a deliverable manifest, a deterministic validator gates it, the manifest
# compiles into an aicad.dag/1 mission graph with role assignments, and the DAG
# executor runs the phases.  Phase state lives in the ledger stage_log (what
# mission control + the GUI render); run_turn is untouched.
# ---------------------------------------------------------------------------

@dataclass
class MissionRun:
    """The result of a ``run_mission`` call — the durable handle the GUI / mission
    control read from (everything else lives in the ledger for ``mission_status``)."""
    request: str
    run_id: str
    phase: str                       # the phase the mission reached / stopped at
    status: str                      # "done" | "blocked" | "failed" | "running"
    manifest: dict | None = None
    graph: dict | None = None
    ledger_path: str | None = None
    phases_completed: list = field(default_factory=list)
    stop_reason: str = ""
    dag_run: dict | None = None      # the DAG executor result (nodes/status), if run
    findings: list = field(default_factory=list)  # manifest validator findings (str)


def run_mission(
    request: str,
    provider: Provider,
    *,
    name: str | None = None,
    intake_fn: "Callable[[str], dict] | None" = None,
    on_event: EventFn | None = None,
    autonomy_default: str = "approve",
    execute_dag: bool = True,
    agent_fn: "Callable | None" = None,
) -> MissionRun:
    """Run a full mission through the 8-phase pipeline (T-W19).

    Deterministic scaffolding lives here; the model does the reasoning inside each
    phase's role node (dispatched by the DAG executor / node runner).  The steps:

      0. INTAKE — obtain the deliverable manifest.  ``intake_fn(request) -> manifest``
         is injectable (tests pass a mock; production wires the intake_analyst role
         through the provider).  When absent, a provider-backed intake is attempted;
         any failure blocks the mission at INTAKE (never guesses a manifest).
      1. Validate the manifest (contract §16).  Errors bounce once then escalate
         (intake_analyst escalation_policy); blocking ambiguities route to CLARIFY.
      2. Compile the manifest into the aicad.dag/1 mission graph (role-assigned).
      3. Drive the phase machine, logging every phase to the ledger stage_log.
      4. When ``execute_dag`` and langgraph are available, hand the graph to the DAG
         executor (Phases 4–7 run as role nodes); otherwise return the planned
         mission (INTAKE→PLAN complete) for a caller that will run the DAG itself.

    Feature flags (``PipelineFlags.from_env``) gate the 1.3 org layer + councils +
    overseer; they are OFF unless the sibling tasks are merged and the env flags set.

    Returns a :class:`MissionRun`.  This function never merges, never marks done —
    it drives the machine and records state; the human/integrator owns completion.
    """
    from services.agent.pipeline import (
        validate_manifest, manifest_valid, blocking_ambiguities,
        BounceTracker, Phase, PhaseMachine, PipelineFlags,
    )
    from services.dag.mission_template import build_mission_graph

    run_id = name or f"mission_{hashlib.sha256(request.encode()).hexdigest()[:10]}"
    flags = PipelineFlags.from_env()

    # Ledger — the mission's durable state (best-effort creation).
    ledger = None
    if _LEDGER_ENABLED:
        try:
            from services.agent.ledger import Ledger
            ledger = Ledger.load(run_id)
            ledger.set_brief("pipeline", title=f"Mission {run_id}",
                             summary=request[:400])
        except Exception:  # noqa: BLE001
            ledger = None

    # T-W28: snapshot the effective effort profile (presets + user overrides,
    # all tiers) as a replayable ledger event at mission start. Best-effort —
    # a missing/broken effort config must never block a mission.
    if ledger is not None:
        try:
            from services.agent.effort import record_effort_snapshot
            from services.agent.tiers import detect_tier_from_env
            record_effort_snapshot(ledger, active_tier=detect_tier_from_env())
        except Exception:  # noqa: BLE001
            pass

    machine = PhaseMachine(ledger=ledger, flags=flags)
    completed: list[str] = []

    def _emit_phase(ph: "Phase", note: str = "") -> None:
        _emit(on_event, kind="mission_phase", run_id=run_id, phase=ph.value,
              text=note or f"Phase {ph.value}")

    # --- Phase 0: INTAKE ---------------------------------------------------
    machine.enter(Phase.INTAKE)
    _emit_phase(Phase.INTAKE, "INTAKE: building deliverable manifest")
    # T-DP18: emit initial ETA estimate covering all phases so the status-bar
    # clock chip lights up as soon as the mission starts.
    _emit_eta_for_remaining(on_event, "specify", run_id=run_id)
    tracker = BounceTracker()
    manifest: dict | None = None
    findings: list = []
    while True:
        try:
            manifest = _run_intake(request, provider, intake_fn)
        except Exception as exc:  # noqa: BLE001
            return _mission_blocked(run_id, Phase.INTAKE, request, ledger,
                                    completed, stop_reason=f"intake_failed: {exc}")
        findings = validate_manifest(manifest)
        if manifest_valid(findings):
            break
        action = tracker.record_bounce()
        if action == "escalate":
            return _mission_blocked(
                run_id, Phase.INTAKE, request, ledger, completed,
                stop_reason="manifest_invalid_escalated",
                manifest=manifest, findings=[str(f) for f in findings])
        # else: bounce once — re-run intake with the validator findings as feedback.

    if ledger is not None:
        try:
            ledger.add_artifact("intake_analyst", "manifest", "manifest",
                                path="", stage=Phase.INTAKE.value,
                                note=f"{len(manifest.get('deliverables', []))} deliverables")
        except Exception:  # noqa: BLE001
            pass

    has_blocking = bool(blocking_ambiguities(findings))
    intake_gate = machine.run_gate(Phase.INTAKE, {"manifest_findings": findings})
    machine.exit(Phase.INTAKE, intake_gate)
    completed.append(Phase.INTAKE.value)

    # A blocking ambiguity halts the mission at CLARIFY for the human/clarifier —
    # run_mission does not fabricate answers.
    if has_blocking:
        machine.enter(Phase.CLARIFY)
        _emit_phase(Phase.CLARIFY, "CLARIFY: blocking ambiguity — awaiting answers")
        return _mission_blocked(
            run_id, Phase.CLARIFY, request, ledger, completed,
            stop_reason="blocking_ambiguity", manifest=manifest,
            findings=[str(f) for f in findings])

    # --- Compile the mission DAG (role-assigned) ---------------------------
    graph = build_mission_graph(
        manifest, graph_id=run_id, org_layer=flags.org_layer,
        default_autonomy=autonomy_default)

    # --- Run the DAG (Phases 2–7 as role nodes) ----------------------------
    dag_result: dict | None = None
    if execute_dag:
        try:
            from services.dag import executor as _dag_exec
            # T-W30: when the caller injects no agent_fn, bind the mission ledger
            # into the default tool-executing agent_fn so every node turn's tool
            # calls / handoffs land as agent_turn events (inspectability half of
            # the execution-reality contract).
            #
            # Provider-leak fix: *provider* is ALSO threaded through here so every
            # internal DAG node reuses the SAME provider this mission was started
            # on. Before this fix, node_runner independently re-resolved a provider
            # via the bare config.get_provider() for every node, silently ignoring
            # whatever provider run_mission() was called with — a mission started
            # on a local tier (e.g. the gym's bundled qwen3-4b) would still leak
            # every internal node (overseer-audit, build, deliver, ...) to the
            # ambient config.json / AI_CAD_PROVIDER cloud default. Building this
            # (rather than falling straight through to the executor's own default)
            # no longer requires a ledger — it is unconditional so the fix applies
            # even when the ledger layer is disabled/unavailable.
            # Phase 4 (M11): construct the mission's live DelegationTree when the org
            # layer is on (default since T-W22), threading the mission's *on_event*
            # sink so `agent.spawn` streams live to the GUI (un-gates the M20 spawn
            # row) and every spawned sub-agent lands in the session registry
            # (subagent_list / subagent_steer).  Best-effort — a tree failure never
            # blocks the mission (delegate degrades to its structured refusal).
            delegation_tree = None
            if flags.org_layer:
                try:
                    from services.agent.delegation import DelegationTree
                    delegation_tree = DelegationTree(
                        run_id=run_id, ledger=ledger, on_event=on_event)
                except Exception:  # noqa: BLE001
                    delegation_tree = None
            effective_agent_fn = agent_fn
            if effective_agent_fn is None:
                try:
                    from services.dag import node_runner as _node_runner
                    effective_agent_fn = _node_runner.make_default_agent_fn(
                        ledger=ledger, provider=provider,
                        delegation_tree=delegation_tree, on_event=on_event)
                except Exception:  # noqa: BLE001 — fall back to executor default
                    effective_agent_fn = None
            dag_result = _dag_exec.run(
                graph, run_id=run_id, emit=on_event, agent_fn=effective_agent_fn,
                autonomy_default=autonomy_default)
        except Exception as exc:  # noqa: BLE001
            # DAG execution failure does not corrupt the mission record; report it.
            return MissionRun(
                request=request, run_id=run_id, phase=Phase.PLAN.value,
                status="failed", manifest=manifest, graph=graph,
                ledger_path=str(ledger.path) if ledger else None,
                phases_completed=completed, stop_reason=f"dag_error: {exc}",
                findings=[str(f) for f in findings])

    # --- Drive the phase machine in LOCKSTEP with the DAG result -----------
    # The mission DAG nodes map onto phases (mission_template groups them by phase).
    # We advance the PhaseMachine phase-by-phase, running each phase's exit gate
    # against context derived from the DAG node outcomes, so every phase (2–7) is
    # recorded on the LEDGER stage_log with its exit gate — the surface
    # mission_status / the GUI render.  The machine stops at the first phase whose
    # nodes did not all complete (a paused/failed DAG), which becomes the reached
    # phase.  Without a DAG result (execute_dag=False) the mission is planned only:
    # it stops at PLAN, having recorded INTAKE.
    if dag_result is None:
        return MissionRun(
            request=request, run_id=run_id, phase=Phase.PLAN.value,
            status="running", manifest=manifest, graph=graph,
            ledger_path=str(ledger.path) if ledger else None,
            phases_completed=completed, dag_run=None,
            findings=[str(f) for f in findings])

    reached, status, stop_reason = _drive_phases_from_dag(
        machine, manifest, graph, dag_result, completed, _emit_phase, Phase,
        ledger=ledger, on_event=on_event, run_id=run_id)

    return MissionRun(
        request=request, run_id=run_id, phase=reached, status=status,
        manifest=manifest, graph=graph,
        ledger_path=str(ledger.path) if ledger else None,
        phases_completed=completed, dag_run=dag_result, stop_reason=stop_reason,
        findings=[str(f) for f in findings])


# The mission-template phase order the machine walks after INTAKE (CLARIFY is
# handled inline before the DAG; ORGANIZE is feature-flagged and skipped here when
# off).  Each entry maps a phase to the node-id prefix(es) that realise it in the
# graph produced by services/dag/mission_template.build_mission_graph.
_PHASE_NODE_PREFIXES: list[tuple[str, tuple[str, ...]]] = [
    ("specify", ("n_spec_",)),
    ("plan", ("n_layout", "n_plan")),
    ("build", ("n_build_",)),
    ("verify", ("n_verify_",)),
    ("deliver", ("n_deliver_",)),
]


def _drive_phases_from_dag(machine, manifest, graph, dag_result, completed,
                           emit_phase, Phase, *,
                           ledger=None, on_event: "EventFn | None" = None,
                           run_id: str | None = None) -> "tuple[str, str, str]":
    """Advance the PhaseMachine phase-by-phase using the DAG node outcomes,
    logging each phase (enter/exit + gate) to the ledger stage_log.

    Returns ``(reached_phase, status, stop_reason)``.  Stops at the first phase
    whose realising nodes did not all reach ``done`` (a paused/failed DAG); that
    phase is entered (so mission_status shows the mission is *at* it) but not
    exited.  On full completion the machine reaches DELIVER.

    VERIFY is NOT derived from node completion (T-W30): before its gate runs the
    reviewer (``reviewer_exec.run_acceptance_checks``) executes each acceptance
    criterion's named check tool and writes per-criterion results to the ledger;
    the gate reads those results.  A criterion the reviewer did not PASS fails the
    gate and routes the mission to CORRECT.
    """
    node_states = dag_result.get("nodes", {}) or {}

    def _node_status(nid: str) -> str:
        ns = node_states.get(nid, {})
        return ns.get("status", "pending")

    def _phase_nodes(prefixes: tuple[str, ...]) -> list[str]:
        return [n["id"] for n in graph.get("nodes", [])
                if any(n["id"].startswith(p) for p in prefixes)]

    # Every acceptance-criterion id declared in the manifest.  The VERIFY gate
    # context enumerates ALL of these so a criterion with NO reviewer result
    # cannot slip through as a vacuous pass (silence => NOT-CHECKED => non-pass).
    ac_ids = [ac.get("id") for d in manifest.get("deliverables", [])
              for ac in (d.get("acceptance_criteria") or [])]

    reached = Phase.PLAN.value
    status = "running"
    stop_reason = ""

    for phase_name, prefixes in _PHASE_NODE_PREFIXES:
        phase = Phase(phase_name)
        nodes = _phase_nodes(prefixes)
        all_done = bool(nodes) and all(_node_status(n) == "done" for n in nodes)
        # The gate node 'n_spec_done' (a DAG gate) belongs to the SPECIFY exit.
        if phase is Phase.SPECIFY:
            join = "n_spec_done"
            if join in node_states:
                all_done = all_done and _node_status(join) == "done"

        machine.enter(phase)
        emit_phase(phase, f"Phase {phase.value}")
        reached = phase.value
        # T-DP18: refresh the GUI clock chip after each phase entry so the
        # status-bar ETA counts down as the mission advances.
        _emit_eta_for_remaining(on_event, phase.value, run_id=run_id)

        if not all_done:
            # This phase's nodes did not all complete → mission is at this phase.
            status = {"failed": "failed"}.get(
                dag_result.get("status"), "blocked")
            stop_reason = f"{phase.value}_incomplete"
            break

        # T-W30: run the reviewer's acceptance checks at VERIFY entry, so the gate
        # reads REAL per-criterion results (not a fabricated PASS floor).
        if phase is Phase.VERIFY:
            _run_reviewer_checks(manifest, ledger, dag_result)

        gate = machine.run_gate(
            phase, _phase_gate_context(phase, ac_ids, Phase, ledger=ledger))
        try:
            machine.exit(phase, gate)
            completed.append(phase.value)
        except Exception as exc:  # noqa: BLE001 — a failed gate stops the mission here
            # A failed VERIFY gate is a real criterion failure: route to CORRECT
            # (the phase machine legally allows VERIFY→CORRECT) rather than a bare
            # block, and surface the failing criteria as open failure cards.
            if phase is Phase.VERIFY:
                failing = _failing_criteria(manifest, ledger, ac_ids)
                try:
                    machine.enter(Phase.CORRECT)
                    emit_phase(Phase.CORRECT, "CORRECT: acceptance criteria failed")
                    reached = Phase.CORRECT.value
                except Exception:  # noqa: BLE001
                    pass
                status = "needs_correction"
                stop_reason = f"verify_failed: {failing}"
            else:
                status = "blocked"
                stop_reason = f"{phase.value}_gate_failed: {exc}"
            break
    else:
        # Every phase exited cleanly.
        status = {"done": "done"}.get(dag_result.get("status"), status)
        if status == "done":
            reached = Phase.DELIVER.value

    return reached, status, stop_reason


def _phase_gate_context(phase, ac_ids, Phase, *, ledger=None) -> dict:
    """Build the exit-gate context for *phase* from a completed DAG.

    For the structural phases (SPECIFY/PLAN/BUILD/DELIVER) a completed phase's
    nodes are ``done``, so the deterministic gate inputs are satisfied.

    VERIFY is DIFFERENT (T-W30): its context is NOT fabricated.  The criterion
    results come from ``criterion_result`` events the reviewer wrote to the ledger
    (``reviewer_exec.run_acceptance_checks``).  Every declared AC id is enumerated
    and defaults to ``NOT-CHECKED`` — a criterion with no reviewer verdict is
    non-pass, so silence can never fabricate a PASS.  Waived criteria come from the
    ledger too (a user answer that waives an AC)."""
    if phase is Phase.SPECIFY:
        return {"spec_audited": True, "uncovered_criteria": []}
    if phase is Phase.PLAN:
        return {"build_graph_blockers": [], "plan_ratified": True,
                "council_synthesis_recorded": True}
    if phase is Phase.GEOMETRY:
        # Stage 2 exit (contract design_pipeline.md §5): the GEOMETRY gate reads
        # durable ledger state only — every feature realized by ≥1 geometry_spec and
        # every geometry_spec carrying provenance. Populate the gate context from the
        # ledger accessors (T-DP1). Localized to this branch; DECOMPOSE (features)
        # is DP2's, LINKAGE/BUILDPATH are DP4's.
        feats: list = []
        specs: list = []
        if ledger is not None:
            try:
                feats = ledger.features()
                specs = ledger.geometry_specs()
            except Exception:  # noqa: BLE001 — a broken ledger fails the gate closed
                pass
        return {"features": feats, "geometry_specs": specs}
    if phase is Phase.BUILD:
        return {"unresolved_nodes": []}
    if phase is Phase.VERIFY:
        recorded, waived = _ledger_criterion_results(ledger)
        # Enumerate EVERY declared criterion; default any with no reviewer verdict
        # to NOT-CHECKED (the gate treats non-PASS as failing → routes to CORRECT).
        results = {ac: recorded.get(ac, "NOT-CHECKED") for ac in ac_ids}
        return {"criterion_results": results, "waived_criteria": sorted(waived)}
    if phase is Phase.DELIVER:
        return {"mission_report_emitted": True}
    return {}


def _ledger_criterion_results(ledger) -> "tuple[dict[str, str], set]":
    """Read the reviewer's ``criterion_result`` events + waived criteria from the
    ledger.  Returns ``({ac_id: verdict}, {waived_ac_id, ...})``.  Empty on a
    missing ledger — which, combined with the ALL-ac enumeration above, yields an
    all-NOT-CHECKED (non-pass) context rather than a vacuous pass."""
    recorded: dict[str, str] = {}
    waived: set = set()
    if ledger is None:
        return recorded, waived
    try:
        for ev in getattr(ledger, "events", []) or []:
            if ev.get("kind") == "criterion_result":
                p = ev.get("payload", {})
                ac = p.get("id")
                if ac:
                    recorded[ac] = p.get("result", "NOT-CHECKED")
            elif ev.get("kind") == "question":
                # a criterion may be waived by a confirmed ask_user answer.
                p = ev.get("payload", {})
                if p.get("waives_criterion"):
                    waived.add(p["waives_criterion"])
    except Exception:  # noqa: BLE001
        pass
    return recorded, waived


def _failing_criteria(manifest, ledger, ac_ids) -> list[str]:
    """The ac ids that did NOT pass (for open_failure_cards / CORRECT routing)."""
    recorded, waived = _ledger_criterion_results(ledger)
    return [ac for ac in ac_ids
            if recorded.get(ac, "NOT-CHECKED") != "PASS" and ac not in waived]


def _run_reviewer_checks(manifest, ledger, dag_result) -> None:
    """Invoke the deterministic reviewer to run each AC's named check tool and
    record per-criterion results on the ledger.  Best-effort: any failure leaves
    the criteria unrecorded → NOT-CHECKED → the gate fails closed (never a
    fabricated pass)."""
    if ledger is None:
        return
    try:
        from services.agent import reviewer_exec
        # Resolve the doc the mission built (if the DAG surfaced one) so the check
        # tools run against real geometry; None lets each tool fall back to _CUR.
        doc_id = None
        nodes = (dag_result or {}).get("nodes", {}) or {}
        for ns in nodes.values():
            d = (ns.get("output") or {}).get("doc_id") or ns.get("doc_id")
            if d:
                doc_id = d
                break
        reviewer_exec.run_acceptance_checks(manifest, ledger, doc_id=doc_id)
    except Exception:  # noqa: BLE001 — a broken reviewer must not fabricate a pass
        pass


def _run_intake(request: str, provider: Provider,
                intake_fn: "Callable[[str], dict] | None") -> dict:
    """Obtain the deliverable manifest for INTAKE.

    ``intake_fn`` (injectable) is the intake_analyst-role call: request → manifest.
    When absent, a minimal provider-backed intake would run; for now, in the
    absence of a wired role runner, an injected ``intake_fn`` is required (tests
    and the mission_start MCP tool supply it).  Raises if neither is available."""
    if intake_fn is not None:
        return intake_fn(request)
    raise RuntimeError(
        "no intake_fn supplied and provider-backed intake is not yet wired "
        "(pass intake_fn=... to run_mission, or run via the DAG intake_analyst node)")


def _mission_blocked(run_id: str, phase: "Phase", request: str, ledger,
                     completed: list, *, stop_reason: str,
                     manifest: dict | None = None,
                     findings: list | None = None) -> "MissionRun":
    """Build a blocked MissionRun and record the block on the ledger stage_log."""
    if ledger is not None:
        try:
            ledger.log_stage("pipeline", phase.value, exit_gate=f"blocked:{stop_reason}")
        except Exception:  # noqa: BLE001
            pass
    return MissionRun(
        request=request, run_id=run_id, phase=phase.value, status="blocked",
        manifest=manifest, ledger_path=str(ledger.path) if ledger else None,
        phases_completed=completed, stop_reason=stop_reason,
        findings=findings or [])
