"""The FIXED prompt set and its objective pass criteria.

Every criterion below is a number or a boolean that the ENGINE computes — a volume in
mm^3, a watertight verdict, a bounding-box size, an ERC/DRC error count, a projected
view count, an engine-MEASURED dimension value, an FEA stress. Nothing here scores the
model's prose: a turn that narrates a perfect plate and never builds one fails.

Criteria have exactly two evidence sources, and the ``kind`` prefix names which:

``part_*`` / ``file_*``  ARTIFACT PROBE — a FRESH process reopens the .step+ / .step
                        files the turn left on disk (project workspace *and* the
                        per-prompt autosave dir, so a doc the model forgot to save is
                        still probed) and asks the engine about them. Fully independent
                        of the turn: the turn cannot lie to it.
``ledger_*``            ENGINE PAYLOAD in the turn's recorded tool results. Drawings and
                        ECAD documents live in engine-private id spaces that ``doc_save``
                        refuses (verified: ``doc_save`` on a ``dwg-cpp*`` / ``ecadcpp*``
                        id returns "no such document", and no ``drawing_doc_id`` RPC
                        exists to recover a drawing from a reopened part), so for those
                        the only engine-computed evidence is what the engine returned at
                        the time. Still not model prose — it is the tool's own JSON — but
                        it is not independently re-derived. See ``scoring.LIMITS``.

Expected volumes are computed in the comment beside each one so a reviewer can check the
arithmetic without running anything.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Prompt:
    id: str
    prompt: str
    criteria: tuple[dict[str, Any], ...]
    #: True when a REFUSAL is the correct engine answer for this prompt, so a failure
    #: here is classified ``correct_refusal`` instead of blamed on the harness.
    refusal_ok: bool = False
    notes: str = ""


# --- expected volumes (mm^3) --------------------------------------------------------- #
_V_PLATE_HOLE = 90 * 50 * 12 - math.pi * 5.0**2 * 12          # 54000 - 942.48 = 53057.52
_V_CUBE_BOSS = 40**3 + math.pi * 6.0**2 * 15                  # 64000 + 1696.46 = 65696.46
_V_SHELL = 50**3 - (50 - 6) * (50 - 6) * (50 - 3)             # 125000 - 90992 = 34008
_V_HOLE_GRID = 100 * 100 * 10 - 16 * math.pi * 3.0**2 * 10    # 100000 - 4523.89 = 95476.11
_V_CUBE60 = 60**3                                             # 216000
_V_BRACKET = 60 * 30 * 8                                      # 14400

# Cantilever 120x20x8 mm steel, 500 N at the free end, root stress and tip deflection:
#   sigma = M*c/I,  M = 500 N * 0.120 m = 60 N.m,  I = b h^3/12 = 0.020*0.008^3/12
#         = 8.533e-10 m^4,  c = 0.004 m  ->  sigma = 281 MPa
#   delta = F L^3 / (3 E I) = 500*0.120^3/(3*200e9*8.533e-10) = 1.69 mm
# The accepted band is deliberately wide (a coarse tet mesh under-predicts bending and a
# fixed-face constraint over-stiffens the root): the criterion catches a WRONG ANSWER,
# not a mesh-convergence error.
_VM_MIN, _VM_MAX = 150.0, 480.0        # MPa
_TIP_MIN, _TIP_MAX = 0.9, 2.8          # mm
# 60 mm aluminium cube: 216000 mm^3 * 2.70 g/cm^3 = 583.2 g
_MASS_MIN, _MASS_MAX = 540.0, 630.0    # g

# Every spelling a model has been observed to reach for when it wants projected views;
# the criterion accepts the engine payload from any of them.
_DWG_TOOLS = ("drawing_make", "create_drawing", "make_drawing", "drawing_get",
              "drawing_add_view", "add_drawing_view", "drawing_set_view")


PROMPTS: tuple[Prompt, ...] = (
    # ---------------------------------------------------------------- modeling (solids)
    Prompt(
        id="mod-plate-hole",
        prompt=("Model a 90 x 50 x 12 mm steel plate with a single 10 mm diameter through "
                "hole at its centre, and save it as a native Chudware part."),
        criteria=(
            {"kind": "part_health", "name": "watertight solid"},
            {"kind": "part_bbox", "size_mm": [90.0, 50.0, 12.0], "tol_mm": 0.5},
            {"kind": "part_volume", "expect_mm3": _V_PLATE_HOLE, "tol_pct": 0.5},
        ),
    ),
    Prompt(
        id="mod-cube-boss",
        prompt=("Create a 40 mm cube with a 12 mm diameter, 15 mm tall cylindrical boss "
                "standing on the centre of its top face."),
        criteria=(
            {"kind": "part_health", "name": "watertight solid"},
            {"kind": "part_volume", "expect_mm3": _V_CUBE_BOSS, "tol_pct": 0.5},
        ),
    ),
    Prompt(
        id="mod-shell-box",
        prompt=("Make a 50 x 50 x 50 mm box and hollow it out to a 3 mm wall thickness, "
                "leaving the top face open."),
        criteria=(
            {"kind": "part_health", "name": "watertight solid"},
            {"kind": "part_volume", "expect_mm3": _V_SHELL, "tol_pct": 1.0},
        ),
        notes="A shell that removes the wrong face or the wrong thickness lands outside 1%.",
    ),
    Prompt(
        id="mod-hole-grid",
        prompt=("Make a 100 x 100 x 10 mm plate with a 4 by 4 grid of 6 mm diameter "
                "through holes on 20 mm centres, centred on the plate."),
        criteria=(
            {"kind": "part_health", "name": "watertight solid"},
            {"kind": "part_bbox", "size_mm": [100.0, 100.0, 10.0], "tol_mm": 0.5},
            {"kind": "part_volume", "expect_mm3": _V_HOLE_GRID, "tol_pct": 0.5},
        ),
        notes="Volume alone proves the hole COUNT (16) and diameter; 15 holes misses by 1%+.",
    ),
    # ---------------------------------------------------------------------- drawings
    Prompt(
        id="dwg-three-views",
        prompt=("Model a 90 x 50 x 12 mm plate with a 10 mm through hole in the middle, then "
                "create a 2D drawing of it with front, top and isometric views and put a "
                "dimension on the hole."),
        criteria=(
            {"kind": "part_health", "name": "watertight solid"},
            {"kind": "ledger_view_count", "tools": _DWG_TOOLS,
             "min": 3},
            {"kind": "ledger_near", "tools": ("drawing_add_dim", "add_dimension",
                                              "drawing_dimension", "add_drawing_dim"),
             "keys": ("value", "measured", "measured_value", "dim_value"),
             "expect": 10.0, "tol": 0.05,
             "name": "engine-measured hole dimension = 10.00 mm"},
        ),
    ),
    Prompt(
        id="dwg-section",
        prompt=("Model a 90 x 50 x 12 mm plate with a 10 mm through hole in the middle, make "
                "a drawing of it, then add a horizontal section view cut through the hole and "
                "tell me the sheet scale."),
        criteria=(
            {"kind": "part_health", "name": "watertight solid"},
            {"kind": "ledger_view_type",
             "tools": _DWG_TOOLS + ("drawing_add_section", "add_section_view"),
             "from_tools": ("drawing_add_section", "add_section_view", "add_section",
                            "drawing_section"),
             "require": "section"},
        ),
    ),
    Prompt(
        id="dwg-titleblock-pdf",
        prompt=("Make a drawing for a 60 x 30 x 8 mm plate, fill in the title block with part "
                "number BRK-001, and export the drawing to PDF."),
        criteria=(
            {"kind": "part_volume", "expect_mm3": _V_BRACKET, "tol_pct": 0.5},
            {"kind": "ledger_text", "tools": ("drawing_set_titleblock", "set_title_block",
                                              "drawing_titleblock", "set_titleblock"),
             "needle": "BRK-001",
             "name": "engine echoed BRK-001 back from the stored title block"},
            {"kind": "file_present", "glob": "*.pdf", "min_pages": 1},
        ),
    ),
    # -------------------------------------------------------------------------- ECAD
    Prompt(
        id="ecad-erc-clean",
        prompt=("Create a schematic with a 5 V linear regulator, an input decoupling "
                "capacitor and an output decoupling capacitor, wire them up, and run ERC."),
        criteria=(
            # The engine assigns every placed symbol a ref (U1, C1, ...) and reports it,
            # but NO schematic tool reports a component TOTAL — the baseline run proved a
            # `components` key never appears. Distinct engine-assigned refs is the honest
            # count.
            {"kind": "ledger_distinct", "tools": ("ecad_place_symbol", "ecad_status",
                                                  "ecad_design"),
             "keys": ("ref",), "min": 3,
             "name": "engine assigned >= 3 distinct component refs"},
            {"kind": "ledger_distinct", "tools": ("ecad_wire", "ecad_add_power",
                                                  "ecad_status", "ecad_design"),
             "keys": ("net",), "min": 3,
             "name": "engine reports >= 3 distinct nets"},
            {"kind": "ledger_zero", "tools": ("ecad_run_erc", "ecad_design"),
             "keys": ("errors", "erc_errors"), "name": "ERC errors = 0"},
        ),
    ),
    Prompt(
        id="ecad-drc-clean",
        prompt=("Create a schematic with a 5 V regulator and two decoupling capacitors, turn "
                "it into a PCB, autoroute it, and run DRC."),
        criteria=(
            # ecad_autoroute answers with routed_nets as a LIST of nets, so this is a
            # length, not a number. ecad_optimize_traces (which the baseline model reached
            # for instead) reports nets_tuned and is accepted too.
            {"kind": "ledger_count",
             "tools": ("ecad_autoroute", "ecad_route_pad_to_pad", "ecad_design"),
             "keys": ("routed_nets", "segments", "routed", "tracks"), "min": 1,
             "name": "engine routed >= 1 net"},
            {"kind": "ledger_zero", "tools": ("ecad_run_drc", "ecad_design"),
             "keys": ("errors", "drc_errors", "violations_count"),
             "name": "DRC errors = 0"},
        ),
    ),
    # --------------------------------------------------------------------- simulation
    Prompt(
        id="sim-cantilever",
        prompt=("Make a 120 x 20 x 8 mm steel cantilever bar, fix one end, apply 500 N "
                "downward at the free end, and report the peak von Mises stress and the tip "
                "deflection."),
        criteria=(
            {"kind": "part_health", "name": "watertight solid"},
            {"kind": "ledger_number",
             "tools": ("run_fea_static", "run_fea", "fea_static", "run_static_study",
                       "simulate", "run_study", "fea_run", "study_run", "run_simulation"),
             "keys": ("max_von_mises_mpa", "peak_von_mises_mpa", "von_mises_max_mpa",
                      "max_stress_mpa", "peak_stress_mpa", "max_von_mises", "peak_von_mises",
                      "von_mises_max", "max_stress"),
             "min": _VM_MIN, "max": _VM_MAX, "quantity": "stress",
             "name": f"peak von Mises in [{_VM_MIN:.0f}, {_VM_MAX:.0f}] MPa (beam theory: 281)"},
            {"kind": "ledger_number",
             "tools": ("run_fea_static", "run_fea", "fea_static", "run_static_study",
                       "simulate", "run_study", "fea_run", "study_run", "run_simulation"),
             "keys": ("max_displacement_mm", "max_deflection_mm", "tip_deflection_mm",
                      "max_disp_mm", "max_displacement", "max_deflection"),
             "min": _TIP_MIN, "max": _TIP_MAX, "quantity": "length",
             "name": f"tip deflection in [{_TIP_MIN}, {_TIP_MAX}] mm (beam theory: 1.69)"},
        ),
    ),
    Prompt(
        id="sim-mass-cube",
        prompt=("Build a 60 mm cube in aluminium and tell me its mass in grams."),
        criteria=(
            {"kind": "part_health", "name": "watertight solid"},
            {"kind": "part_volume", "expect_mm3": _V_CUBE60, "tol_pct": 0.5},
            {"kind": "ledger_number",
             "tools": ("measure_part", "mass_properties", "get_mass_properties",
                       "measure_mass", "part_mass_properties", "describe_part",
                       "evaluate_mass"),
             "keys": ("mass_g", "mass_grams", "mass"),
             "min": _MASS_MIN, "max": _MASS_MAX, "quantity": "mass",
             "name": f"engine mass in [{_MASS_MIN:.0f}, {_MASS_MAX:.0f}] g (2.70 g/cm3: 583.2)"},
        ),
    ),
    # ------------------------------------------------------- anti-fabrication control
    Prompt(
        id="ctl-self-intersection",
        prompt=("Build a 40 mm cube and tell me whether the solid self-intersects anywhere."),
        criteria=(
            {"kind": "part_health", "name": "watertight solid"},
            {"kind": "ledger_false",
             "tools": ("check_geometry_health", "is_watertight", "assess_mesh_quality",
                       "analyze_stl_geometry", "health_check", "design_audit"),
             "path": "self_intersection.self_intersecting",
             "name": "engine computed self_intersection.self_intersecting = false"},
        ),
        refusal_ok=True,
        notes=("The engine runs a real BOPAlgo_CheckerSI self-intersection test, so a cube "
               "has a genuine answer: false. The criterion asserts the ENGINE produced it — "
               "a check that did not run reports self_intersecting:null and fails here, "
               "which is the thing a fabricating agent would paper over."),
    ),
)

PROMPTS_BY_ID = {p.id: p for p in PROMPTS}


def criterion_label(c: dict[str, Any]) -> str:
    """Human label for a criterion — its ``name`` when curated, else a derived one."""
    if c.get("name"):
        return str(c["name"])
    k = c.get("kind")
    if k == "part_volume":
        return f"volume = {float(c['expect_mm3']):.1f} mm3 +/-{c['tol_pct']}%"
    if k == "part_bbox":
        return "bbox = " + " x ".join(f"{v:g}" for v in c["size_mm"]) + f" mm +/-{c['tol_mm']}"
    if k == "file_present":
        return f"file {c['glob']} present"
    if k == "ledger_view_count":
        return f"engine reports >= {c['min']} drawing views"
    if k == "ledger_view_type":
        return f"engine reports a '{c['require']}' view"
    if k == "ledger_zero":
        return f"engine reports {'/'.join(c['keys'])} = 0"
    if k == "ledger_min":
        return f"engine reports {'/'.join(c['keys'])} >= {c['min']}"
    if k == "ledger_text":
        return f"engine payload contains {c['needle']!r}"
    return str(k)


__all__ = ["Prompt", "PROMPTS", "PROMPTS_BY_ID", "criterion_label"]
