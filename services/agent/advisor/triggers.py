"""Trigger-based context injection — aicad.trigger/1.

Contract: ``contracts/advisor.md`` §20 (FROZEN).

Design
------
* **Cheap matching**: all keyword patterns are compiled once at module load into a
  single list of ``re.Pattern`` per trigger.  One pass over the input text per
  trigger; no LLM in the hot path.
* **Cooldown**: the caller passes a mutable ``cooldown_state`` dict
  ``{trigger_id: last_fired_seq}`` so suppression is testable without host state.
* **Prerun calcs**: per-trigger input extraction via named-group regexes; only
  runs if the match succeeded.  ``ok=False`` from a calc → pack injected without
  numbers (fail-soft per §20).
* **Budget enforcement**: per-trigger ``budget_tokens`` caps individual pack text
  (truncation by whole lines).  The global advisory budget (render.py) does the
  final drop-first pass.
* **Ledger events**: ``match_triggers`` returns structured fire records
  ``{trigger_id, matched, tokens}``; the host writes them via
  ``ledger_note(run_id, "trigger_fired", …)`` (§2).
* **Zero-cost no-match path**: no file read, no calc, no allocation when no
  trigger fires.

Public API
----------
- :func:`match_triggers` — evaluate all triggers against an observation turn;
  returns a list of injected ``Finding``-shaped dicts and a list of ledger events.
- :func:`load_table` — parse shipped + user override table (cached; call
  :func:`clear_cache` after saves).
- :func:`list_triggers` / :func:`get_trigger` / :func:`save_trigger` /
  :func:`delete_trigger` — GUI/RPC CRUD over the merged table (user overrides
  never rewrite shipped ``triggers.yaml``).
- :func:`get_pack` / :func:`save_pack` / :func:`list_packs` — context-pack
  bodies (user packs shadow shipped packs of the same name).
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import sys
from typing import Any, Optional

_YAML_PATH = pathlib.Path(__file__).parent / "triggers.yaml"   # spec file (human-readable)
_PACKS_DIR = pathlib.Path(__file__).parent / "context_packs"

# ---------------------------------------------------------------------------
# Builtin trigger table (mirrors triggers.yaml exactly).
# This is the runtime source; triggers.yaml is the canonical spec for review.
# No PyYAML dep required — the venv does not include it.
# ---------------------------------------------------------------------------

_BUILTIN_TRIGGERS: list[dict] = [
    {
        "id": "thread_mention",
        "match": {
            "keywords": [
                r"\btap\b", r"\bthread\b", r"\bthreaded\b",
                r"\bM\d+(?:\.\d+)?(?:[xX×]\d+(?:\.\d+)?)?\b",
                r"\bUNC\b", r"\bUNF\b", r"\bpilot hole\b", r"\btap drill\b",
            ],
            "tools": [],
            "events": [],
        },
        "inject": {
            "context_pack": "threads",
            "prerun_calcs": [
                {"calc": "tap_drill", "inputs_from": "match",
                 "input_map": {"thread": "thread_designation"}},
            ],
            "exemplar": "M6x1.0 -> pilot 5.0 mm (Machinery's Handbook; ISO 2306 75% engagement)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "bearing_seat",
        "match": {
            "keywords": [
                r"\bbearing\b", r"\bbearing seat\b", r"\bbore.*press",
                r"\binner race\b", r"\bouter race\b", r"\bshaft seat\b",
            ],
            "tools": [],
            "events": [],
        },
        "inject": {
            "context_pack": "bearing_fits",
            "prerun_calcs": [
                {"calc": "fit_calc", "inputs_from": "literal",
                 "input_map": {"nominal": None, "fit": "H7/g6"}},
            ],
            "exemplar": "Shaft seat k6/m6 (press); housing bore H7/J7 (easy removal); source: iso286.json",
        },
        "budget_tokens": 300,
        "cooldown_turns": 8,
    },
    {
        "id": "press_fit_mention",
        "match": {
            "keywords": [
                r"\bpress[ -]?fit\b", r"\binterference fit\b",
                r"\bknurled\b", r"\bpressed in\b", r"\bforced fit\b",
            ],
            "tools": [],
            "events": [],
        },
        "inject": {
            "context_pack": "press_fit",
            "prerun_calcs": [
                {"calc": "fit_calc", "inputs_from": "literal",
                 "input_map": {"nominal": None, "fit": "H7/p6"}},
            ],
            "exemplar": "H7/p6 on dia 20: min interference 0.017 mm, max 0.042 mm (ISO 286, fit_calc)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "snap_fit_mention",
        "match": {
            "keywords": [
                r"\bsnap[ -]?fit\b", r"\bcantilever (?:hook|latch)\b",
                r"\bsnap hook\b", r"\bsnap arm\b",
            ],
            "tools": [],
            "events": [],
        },
        "inject": {
            "context_pack": "snap_fit",
            "prerun_calcs": [],
            "exemplar": "ABS cantilever: eps_allow ~= 0.6x(yield/E); viable if eps_root <= eps_allow (snap_fit calc)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "thin_wall",
        "match": {
            "keywords": [
                r"\bthin wall\b", r"\bthin-wall\b", r"\bwall thickness\b",
                r"\bminimum wall\b",
            ],
            "tools": ["wall_thickness"],
            "events": ["thin_wall"],
        },
        "inject": {
            "context_pack": "thin_wall",
            "prerun_calcs": [],
            "exemplar": "FDM: min wall 1.2 mm; SLA: 0.6 mm; CNC: depends on tool; source: wall_thickness.py",
        },
        "budget_tokens": 200,
        "cooldown_turns": 6,
    },
    {
        "id": "fastener_pattern",
        "match": {
            "keywords": [
                r"\bbolt circle\b", r"\bbolt pattern\b", r"\bscrew pattern\b",
                r"\bfastener pattern\b", r"\bPCD\b", r"\bflange bolt\b",
                r"\bclearance hole\b", r"\bcountersink\b", r"\bcounterbore\b",
            ],
            "tools": [],
            "events": [],
        },
        "inject": {
            "context_pack": "fastener_pattern",
            "prerun_calcs": [
                {"calc": "tap_drill", "inputs_from": "match",
                 "input_map": {"thread": "thread_designation"}},
            ],
            "exemplar": "M8 clearance hole: 8.5 mm pass-through (ISO 273 medium); pilot 6.8 mm (feeds_speeds.json)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "material_mention",
        "match": {
            "keywords": [
                r"\baluminum\b", r"\baluminium\b", r"\bsteel\b", r"\bstainless\b",
                r"\bABS\b", r"\bPLA\b", r"\bPETG\b", r"\btitanium\b",
                r"\bbr(?:ass|onze)\b", r"\bcast iron\b", r"\bpolycarbonate\b",
                r"\bnylon\b",
            ],
            "tools": ["set_material", "research_material_lookup"],
            "events": [],
        },
        "inject": {
            "context_pack": "material_props",
            "prerun_calcs": [],
            "exemplar": "6061-T6 Al: E=68900 MPa, yield=276 MPa, density=2.70 g/cm3 (materials.json)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 10,
    },
    {
        "id": "tolerance_mention",
        "match": {
            "keywords": [
                r"\btolerance\b", r"\bH7\b", r"\bg6\b",
                r"\bf7\b", r"\bk6\b", r"\bp6\b", r"\bISO 286\b",
                r"\bsliding fit\b", r"\brunning fit\b",
            ],
            "tools": ["fit_calc", "measure_between"],
            "events": [],
        },
        "inject": {
            "context_pack": "tolerance_fits",
            "prerun_calcs": [
                {"calc": "fit_calc", "inputs_from": "match",
                 "input_map": {"nominal": "nominal_mm", "fit": "fit_designation"}},
            ],
            "exemplar": "H7/g6 sliding fit on dia 20: clearance 0.007-0.041 mm (ISO 286, iso286.json)",
        },
        "budget_tokens": 300,
        "cooldown_turns": 8,
    },
    {
        "id": "thread_engagement",
        "match": {
            "keywords": [
                r"\bthread engagement\b", r"\bthread length\b",
                r"\bthrough[ -]hole\b", r"\bblind[ -]hole\b",
            ],
            "tools": ["research_thread_spec", "tap_drill"],
            "events": [],
        },
        "inject": {
            "context_pack": "thread_engagement",
            "prerun_calcs": [],
            "exemplar": "Min engagement: 1.0xD steel-in-steel; 1.5xD aluminum; 2.0xD plastic (Machinery's Handbook)",
        },
        "budget_tokens": 200,
        "cooldown_turns": 10,
    },
    # Pre-op advisory injection triggers (append-only).
    # Each fires BEFORE the model operates so relevant facts are in context at op time.
    {
        "id": "interference_pre_op",
        "match": {
            "keywords": [
                r"\bassemble\b", r"\bcheck interference\b", r"\boverlap\b",
                r"\b(?:place|mount|mate)\b(?:\s+\w+){0,3}\s+\b(?:part|component|body|board|assembly)\b",
            ],
            "tools": ["part_place", "solve_mates", "check_interference"],
            "events": ["assembly_body_added"],
        },
        "inject": {
            "context_pack": "interference_pre_op",
            "prerun_calcs": [],
            "exemplar": (
                "After placing bodies run check_interference; interference > 0 mm3 is a "
                "blocker (advisor names axis + depth)"
            ),
        },
        "budget_tokens": 200,
        "cooldown_turns": 6,
    },
    {
        "id": "dfm_pre_op",
        "match": {
            "keywords": [
                r"\bmanufactur(?:e|ed|ing|ability|able)\b", r"\bDFM\b",
                r"\bdesign for manufacturability\b", r"\b3D print(?:er|ing|ed)?\b",
                r"\bCNC machin(?:e|ed|ing)\b", r"\binjection mold(?:ed|ing)?\b",
            ],
            "tools": ["set_process", "dfm_check", "wall_thickness", "resolve_process"],
            "events": [],
        },
        "inject": {
            "context_pack": "dfm_pre_op",
            "prerun_calcs": [],
            "exemplar": (
                "Run dfm_check before export; wall < process minimum and missing draft "
                "are blockers (advisor names delta + fix)"
            ),
        },
        "budget_tokens": 200,
        "cooldown_turns": 8,
    },
    {
        "id": "interface_check_pre_op",
        "match": {
            "keywords": [
                r"\bhole pattern\b", r"\bbolt holes\b", r"\bcoaxial\b",
                r"\bcontact (?:face|plane)\b", r"\bpass[ -]through\b",
                r"\b(?:align|mate)\b(?:\s+\w+){0,3}\s+\b(?:part|component|face|axis|pattern)\b",
            ],
            "tools": ["interface_check", "part_place", "solve_mates"],
            "events": [],
        },
        "inject": {
            "context_pack": "interface_check_pre_op",
            "prerun_calcs": [],
            "exemplar": (
                "interface_check catches hole-pattern offset, coaxiality error, "
                "contact-plane gap - all pass interference but are still wrong"
            ),
        },
        "budget_tokens": 200,
        "cooldown_turns": 8,
    },
    {
        # Sheet-metal bend — contract design_pipeline.md §8 "helper to add".
        "id": "sheet_bend",
        "match": {
            "keywords": [
                r"\bbend allowance\b", r"\bbend deduction\b", r"\bsheet metal\b",
                r"\bsheet-metal\b", r"\bflat pattern\b",
                r"\bK[ -]?factor\b", r"\bbend radius\b", r"\bpress brake\b",
            ],
            "tools": ["bend_allowance"],
            "events": [],
        },
        "inject": {
            "context_pack": "sheet_bend",
            "prerun_calcs": [
                {"calc": "bend_allowance", "inputs_from": "literal",
                 "input_map": {"thickness_mm": None, "inside_radius_mm": None}},
            ],
            "exemplar": "t=2, r=2, 90 deg bend: BA=4.24 mm, BD=3.76 mm, K=0.35 (bend_allowance calc; Machinery's Handbook)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        # Molded draft — contract design_pipeline.md §8 "helper to add".
        "id": "molded_draft",
        "match": {
            "keywords": [
                r"\bdraft angle\b", r"\bmolded\b", r"\bmoulded\b",
                r"\binjection mold\b", r"\binjection mould\b", r"\brelease angle\b",
                r"\bcore pull\b",
            ],
            "tools": ["draft_analysis"],
            "events": [],
        },
        "inject": {
            "context_pack": "molded_draft",
            "prerun_calcs": [
                {"calc": "draft_check", "inputs_from": "literal",
                 "input_map": {"draw_depth_mm": None}},
            ],
            "exemplar": "Smooth molded wall: 1.0 deg min draft (mandatory for release); 30 mm draw -> 0.52 mm taper/side (draft_check; process_profiles injection)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "drawing_standards",
        "match": {
            "keywords": [
                r"\bdrawing\b", r"\bdimension", r"GD&T|geometric toleranc",
                r"\btitle block\b", r"section view", r"detail view",
                r"\bsheet format\b", r"\bannotation",
            ],
            "tools": [
                "make_drawing", "add_drawing_view", "add_section_view",
                "add_detail_view", "add_dimension", "add_gdt",
                "add_surface_finish", "export_drawing",
                "set_titleblock",
            ],
            "events": [],
        },
        "inject": {
            "context_pack": "drawing_standards",
            "prerun_calcs": [],
            "exemplar": "Base -> projected -> section/detail; dimension from measure_part, never invent (drawing_standards pack)",
        },
        "budget_tokens": 300,
        "cooldown_turns": 8,
    },
    {
        "id": "sim_setup",
        "match": {
            "keywords": [
                r"\bFEA\b", r"\bCFD\b", r"\bstress (?:analysis|simulation|study)\b",
                r"\bmodal\b", r"\bthermal\b", r"\bbuckling\b", r"\bfatigue\b",
                r"flow simulation", r"motion study", r"\b(?:simulation )?mesh quality\b",
            ],
            "tools": [
                "run_fea_static", "run_fea_adaptive", "run_fea_modal", "run_fea_fatigue",
                "run_fea_thermal", "run_fea_buckling", "run_fea_contact",
                "run_cfd_flow", "run_cfd_accurate", "simulate_motion",
                "describe_results",
            ],
            "events": [],
        },
        "inject": {
            "context_pack": "sim_setup",
            "prerun_calcs": [],
            "exemplar": "Anchor with research_eng_calc before run_fea_*; one load case per study (sim_setup pack)",
        },
        "budget_tokens": 300,
        "cooldown_turns": 8,
    },
    {
        "id": "ecad_design",
        "match": {
            "keywords": [
                r"\bschematic\b", r"\bPCB\b", r"\bnetlist\b", r"\bfootprint\b",
                r"\bimpedance\b", r"\bERC\b", r"\bDRC\b", r"\bautorout(?:e|er|ing)\b",
                r"\bnet class\b", r"\bsilkscreen\b",
            ],
            "tools": [
                "ecad_new_schematic", "ecad_new_pcb", "ecad_place_symbol", "ecad_wire",
                "ecad_autoroute", "ecad_fill_zones", "ecad_update_from_sch",
                "ecad_set_net_class", "ecad_run_erc", "ecad_run_drc",
                "ecad_add_power", "board_from_schematic", "ecad_simulate",
            ],
        },
        "inject": {
            "context_pack": "ecad_design",
            "prerun_calcs": [],
            "exemplar": "Schematic -> ERC -> board_from_schematic -> DRC; nets before SPICE (ecad_design pack)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "cam_setup",
        "match": {
            "keywords": [
                r"\bmachin(?:e|ed|ing)\b", r"\btoolpath\b", r"feeds and speeds",
                r"\bCAM (?:stock|setup|job)\b", r"\bpost[ -]?process(?:or|ing)?\b",
                r"\bg[ -]?code\b", r"\bstepover\b", r"\bstepdown\b",
            ],
            "tools": [
                "cam_new_job", "cam_add_setup", "cam_add_tool",
                "cam_add_operation", "cam_generate", "cam_simulate",
                "cam_post", "cam_set_stock",
                "cam_setup_sheet", "cam_materials", "cam_op_types",
            ],
            "events": [],
        },
        "inject": {
            "context_pack": "cam_setup",
            "prerun_calcs": [],
            "exemplar": "Stock -> setup -> tool -> op -> generate -> simulate clean -> post (cam_setup pack)",
        },
        "budget_tokens": 300,
        "cooldown_turns": 8,
    },
    {
        "id": "part_sourcing",
        "match": {
            "keywords": [
                r"\bsupplier\b", r"\bpurchase\b", r"\blead time\b",
                r"\brequest (?:a )?quote\b",
                r"\bBOM\b.*\bcost|cost.*\bBOM\b",
                r"\bsource\b.*\bpart|part.*\bsource\b",
            ],
            "tools": [
                "part_search", "part_get", "part_place", "part_source",
                "bom_price", "bom_rollup", "request_quote", "match_part_number",
                "parts_taxonomy", "vendors_health",
            ],
            "events": [],
        },
        "inject": {
            "context_pack": "sourcing",
            "prerun_calcs": [],
            "exemplar": "part_search -> part_get -> part_place; never hand-model catalog hardware (sourcing pack)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 10,
    },
    {
        # Mirror of triggers.yaml `- id: reveng_flow` — keep both in sync (tools must also
        # match the reveng_flow context pack's `# Tools:` header).
        "id": "reveng_flow",
        "match": {
            "keywords": [
                r"reverse[ -]?engineer",
                r"\bscan\b.*\b(part|bracket|housing|component|to cad)\b",
                r"\bpoint cloud\b", r"\bmesh\b.*\b(part|model)", r"\bphotogrammetry\b",
                r"\bwebcam\b", r"\bcaliper\b", r"\bcalipers\b", r"\breplacement part\b",
                r"\bcopy\b.*\b(part|bracket)\b",
                # Bare "3d print" is NOT reverse engineering. Live 2026-07-30
                # (run-4530697933837): "fabricated with a 3D printer plus Shapeoko
                # CNC" injected 250 tokens of scan/point-cloud/caliper guidance into
                # a from-scratch enclosure design. Printing is an OUTPUT of this
                # flow, so it only signals intent alongside a replicate verb --
                # matching the co-occurrence idiom the neighbouring keys already use.
                r"\b(scan|copy|replicate|duplicate|replacement|reproduce)\b.*3d print",
                r"3d print.*\b(scan|copy|replica|replacement|existing)\b",
                r"\bSTEP\b.*\bimport|import.*\bSTEP\b",
                # A generic electronics datasheet is not reverse-engineering
                # context. Require geometric extraction language in either order.
                r"\bdatasheet\b.*\b(dimensions?|drawing|geometry|profile|outline)\b",
                r"\b(dimensions?|drawing|geometry|profile|outline)\b.*\bdatasheet\b",
            ],
            "tools": [
                "reverse_engineer", "compare_parts", "reveng_status",
                "reveng_start_session", "reveng_add_images", "reveng_ingest_datasheet",
                "reveng_reconstruct", "reveng_set_scale", "reveng_fit_primitives",
                "reveng_build_parametric", "get_reveng_deviation", "reveng_attach_result",
            ],
            "events": [],
        },
        "inject": {
            "context_pack": "reveng_flow",
            "prerun_calcs": [],
            "exemplar": (
                "Lane A (file/doc_id): reverse_engineer -> compare_parts loop. "
                "Lane B (physical part): reveng_status -> reveng_start_session -> capture -> "
                "reveng_reconstruct -> reveng_set_scale [scale gate] -> reveng_fit_primitives -> "
                "[dim gate] -> reveng_build_parametric (reveng_flow pack)"
            ),
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "plm_flow",
        "match": {
            "keywords": [
                r"\bcheckpoint\b", r"\brevision\b",
                r"\bwhere[ -]used\b", r"\brollback\b", r"\bchange order\b",
                r"\bitem master\b",
            ],
            "tools": [
                "plm_init", "plm_open", "plm_save", "plm_checkpoint",
                "plm_release", "plm_history", "plm_restore", "plm_compare",
                "plm_where_used", "plm_rollback",
                "plm_status", "plm_set_ai_mode",
            ],
            "events": [],
        },
        "inject": {
            "context_pack": "plm_flow",
            "prerun_calcs": [],
            "exemplar": "Checkpoint before risk; where_used before release; history->restore for recovery (plm_flow pack)",
        },
        "budget_tokens": 200,
        "cooldown_turns": 10,
    },
]

# Human-facing metadata is kept separate from matcher mechanics so every shipped Rule has a
# concise settings label and purpose without repeating prose inside the no-PyYAML fallback table.
_RULE_METADATA: dict[str, tuple[str, str]] = {
    "thread_mention": ("Thread sizing", "Inject tap-drill and thread-selection guidance for threaded features."),
    "bearing_seat": ("Bearing fits", "Surface shaft and housing fit guidance for bearing seats."),
    "press_fit_mention": ("Press fits", "Surface interference-fit tolerances when a press fit is intended."),
    "snap_fit_mention": ("Snap fits", "Inject strain and geometry checks for snap-fit features."),
    "thin_wall": ("Thin walls", "Surface process-specific wall-thickness guidance when thin walls matter."),
    "fastener_pattern": ("Fastener patterns", "Inject fastener-hole and pattern sizing guidance."),
    "material_mention": ("Material properties", "Provide sourced material properties when a material affects the design."),
    "tolerance_mention": ("Tolerances and fits", "Inject ISO fit and tolerance guidance for mating dimensions."),
    "thread_engagement": ("Thread engagement", "Surface minimum engagement guidance for threaded joints."),
    "interference_pre_op": ("Assembly interference pre-op", "Verify physical interference before completing assembly work."),
    "dfm_pre_op": ("Design for manufacturability pre-op", "Apply process-specific manufacturability checks before export or release."),
    "interface_check_pre_op": ("Interface alignment pre-op", "Verify mating patterns, axes, and contact planes before completion."),
    "sheet_bend": ("Sheet-metal bends", "Inject bend allowance and flat-pattern guidance for sheet metal."),
    "molded_draft": ("Mold draft", "Surface draft-angle requirements for molded geometry."),
    "drawing_standards": ("Drawing standards", "Guide measured views, dimensions, annotations, and drawing export."),
    "sim_setup": ("Simulation setup", "Require sourced materials, explicit loads, constraints, and result checks."),
    "ecad_design": ("ECAD design", "Guide schematic, board, ERC, DRC, and electrical verification flow."),
    "cam_setup": ("CAM setup", "Guide stock, toolpaths, simulation, and postprocessing."),
    "part_sourcing": ("Part sourcing", "Prefer verified catalog parts and supplier data over hand-modeled hardware."),
    "reveng_flow": ("Reverse engineering", "Guide scan or reference geometry into verified parametric reconstruction."),
    "plm_flow": ("PLM workflow", "Guide checkpoints, revisions, releases, where-used checks, and recovery."),
}

_CHARS_PER_TOKEN = 4  # repo-wide heuristic (matches render.py / ledger.py)

# ---------------------------------------------------------------------------
# Named-group extraction regexes for prerun calc inputs
# ---------------------------------------------------------------------------

# Matches full metric thread designation: M6, M6x1.0, M6×1.0, M6 x 1.0
_THREAD_DESIGNATION_RE = re.compile(
    r"\b(M\d+(?:\.\d+)?(?:\s*[xX×]\s*\d+(?:\.\d+)?)?)\b"
)

# Matches ISO 286 fit designations, preferring a complete pair (H7/g6) over unrelated tokens.
_FIT_DESIGNATION_RE = re.compile(
    r"\b([A-Z]\d+[/\\][a-z]\d+|[HhGgFfKkPpRrSs]\d+)\b", re.IGNORECASE
)

# Matches common nominal dimensions: "20 mm", "Ø20", "dia 20", "diameter 20", "d=25.4".
_NOMINAL_MM_RE = re.compile(
    r"(?:(?:Ø|∅|ø|\bd(?:ia(?:meter)?)?\s*=\s*|\bdia(?:meter)?\s+)"
    r"(\d+(?:\.\d+)?)(?:\s*mm)?|(\d+(?:\.\d+)?)\s*mm\b)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Trigger table (parsed + compiled once)
# ---------------------------------------------------------------------------

_TABLE: Optional[list[dict]] = None
_RAW_MERGED: Optional[list[dict]] = None
_CACHE_TRIGGERS_MTIME: float = -1.0
_CACHE_PACKS_MTIME: float = -1.0
_PACKS_DIR_EMPTY: Optional[bool] = None  # cached "no user packs" fact (see _packs_dir_mtime)
_PACK_CACHE: dict[pathlib.Path, tuple[float, str]] = {}  # path -> (mtime, text)

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")
_PACK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")


def user_context_dir() -> pathlib.Path:
    """``~/.local/share/Chudware/context_injection`` (Windows: %APPDATA%/Chudware/...).

    Override with ``CHUDWARE_CONTEXT_INJECTION_DIR`` (tests).
    """
    override = os.environ.get("CHUDWARE_CONTEXT_INJECTION_DIR")
    if override:
        return pathlib.Path(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return pathlib.Path(base) / "Chudware" / "context_injection"


def user_triggers_path() -> pathlib.Path:
    return user_context_dir() / "triggers.json"


def user_packs_dir() -> pathlib.Path:
    return user_context_dir() / "packs"


def clear_cache() -> None:
    """Drop compiled + merged caches so the next load re-reads disk."""
    global _TABLE, _RAW_MERGED, _CACHE_TRIGGERS_MTIME, _CACHE_PACKS_MTIME, _PACKS_DIR_EMPTY
    _TABLE = None
    _RAW_MERGED = None
    _CACHE_TRIGGERS_MTIME = -1.0
    _CACHE_PACKS_MTIME = -1.0
    _PACKS_DIR_EMPTY = None
    _PACK_CACHE.clear()


def _path_mtime(path: pathlib.Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _packs_dir_mtime() -> float:
    """Max mtime over the user packs dir (0 if absent).

    The directory is restatted on every cache-freshness check. User packs are
    written by a different process, so caching an observed absent/empty directory
    for this process's lifetime makes newly created packs invisible until restart.
    One directory stat preserves cross-process freshness without IPC.
    """
    global _PACKS_DIR_EMPTY
    d = user_packs_dir()
    if not d.is_dir():
        _PACKS_DIR_EMPTY = True
        return 0.0
    latest = _path_mtime(d)
    saw_entry = False
    try:
        for path in d.iterdir():
            saw_entry = True
            try:
                latest = max(latest, path.stat().st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    _PACKS_DIR_EMPTY = not saw_entry
    return latest


def _ensure_cache_fresh() -> None:
    """Invalidate in-process caches when the GUI (or another process) wrote user data.

    ``match_triggers`` runs in the agent loop process; saves land via the worker
    RPC process. A single ``stat`` of triggers.json (+ packs dir) per call keeps
    the two in sync without IPC.
    """
    global _TABLE, _RAW_MERGED, _CACHE_TRIGGERS_MTIME, _CACHE_PACKS_MTIME
    t_mt = _path_mtime(user_triggers_path())
    p_mt = _packs_dir_mtime()
    if _RAW_MERGED is None:
        _CACHE_TRIGGERS_MTIME = t_mt
        _CACHE_PACKS_MTIME = p_mt
        return
    if t_mt != _CACHE_TRIGGERS_MTIME or p_mt != _CACHE_PACKS_MTIME:
        _TABLE = None
        _RAW_MERGED = None
        _CACHE_TRIGGERS_MTIME = t_mt
        _CACHE_PACKS_MTIME = p_mt


def _deep_copy_trigger(t: dict) -> dict:
    return copy.deepcopy(t)


def _label_for(raw: dict) -> str:
    """Human title for a rule, in precedence order: an explicit ``label`` when the
    author set one, then the curated title in ``_RULE_METADATA``, and finally the
    id read as prose (``some_rule`` -> ``Some rule``).

    Used by the Rules settings tab and by the transcript chip the GUI draws when
    a rule fires, so a firing is never labelled with a bare identifier.
    """
    label = str(raw.get("label") or "").strip()
    if label:
        return label[:80]
    tid = str(raw.get("id") or "").strip()
    if tid in _RULE_METADATA:
        return _RULE_METADATA[tid][0]
    words = tid.replace("_", " ").replace("-", " ").strip()
    return (words[:1].upper() + words[1:]) if words else ""


def _normalize_trigger(raw: dict) -> Optional[dict]:
    """Return a sanitized trigger dict, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    tid = str(raw.get("id") or "").strip()
    if not tid or not _ID_RE.match(tid):
        return None
    match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
    inject = raw.get("inject") if isinstance(raw.get("inject"), dict) else {}
    keywords = [str(k) for k in (match.get("keywords") or []) if str(k).strip()]
    tools = [str(t) for t in (match.get("tools") or []) if str(t).strip()]
    events = [str(e) for e in (match.get("events") or []) if str(e).strip()]
    prerun = inject.get("prerun_calcs") if isinstance(inject.get("prerun_calcs"), list) else []
    pack = str(inject.get("context_pack") or "").strip()
    exemplar = str(inject.get("exemplar") or "").strip()
    purpose = str(raw.get("purpose") or "").strip()
    if not purpose:
        purpose = _RULE_METADATA.get(tid, ("", exemplar))[1]
    try:
        budget = int(raw.get("budget_tokens", 250))
    except (TypeError, ValueError):
        budget = 250
    try:
        cooldown = int(raw.get("cooldown_turns", 8))
    except (TypeError, ValueError):
        cooldown = 8
    budget = max(50, min(budget, 4000))
    cooldown = max(0, min(cooldown, 64))
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = str(enabled).strip().lower() not in ("0", "false", "no", "off")
    return {
        "id": tid,
        "enabled": enabled,
        "label": _label_for(raw),
        "purpose": purpose,
        "match": {
            "keywords": keywords,
            "tools": tools,
            "events": events,
        },
        "inject": {
            "context_pack": pack,
            "prerun_calcs": copy.deepcopy(prerun),
            "exemplar": exemplar,
        },
        "budget_tokens": budget,
        "cooldown_turns": cooldown,
    }


def _shipped_triggers() -> list[dict]:
    """Shipped table: triggers.yaml when parseable, else the builtin mirror."""
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8"))
        entries = data.get("triggers", []) if isinstance(data, dict) else []
        out: list[dict] = []
        for e in entries or []:
            n = _normalize_trigger(e)
            if n:
                out.append(n)
        if out:
            return out
    except Exception:  # noqa: BLE001 — fail soft to the builtin mirror
        pass
    out = []
    for e in _BUILTIN_TRIGGERS:
        n = _normalize_trigger(e)
        if n:
            out.append(n)
    return out


def _load_user_triggers() -> list[dict]:
    path = user_triggers_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    entries = data.get("triggers", data) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for e in entries:
        n = _normalize_trigger(e if isinstance(e, dict) else {})
        if n:
            out.append(n)
    return out


def _write_user_triggers(entries: list[dict]) -> None:
    path = user_triggers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "aicad.trigger/1",
        "triggers": [_normalize_trigger(e) for e in entries if _normalize_trigger(e)],
    }
    # Drop Nones from normalize failures
    payload["triggers"] = [t for t in payload["triggers"] if t]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _merge_raw_triggers() -> list[dict]:
    """Shipped first, then user overrides by id (and user-only new ids)."""
    shipped = {_t["id"]: _deep_copy_trigger(_t) for _t in _shipped_triggers()}
    shipped_order = [t["id"] for t in _shipped_triggers()]
    user = _load_user_triggers()
    user_only: list[dict] = []
    for u in user:
        tid = u["id"]
        if tid in shipped:
            # Full replace of editable fields; keep id.
            base = shipped[tid]
            base.update({
                "enabled": u.get("enabled", base.get("enabled", True)),
                "purpose": u.get("purpose") or base.get("purpose") or "",
                "match": u.get("match") or base.get("match") or {},
                "inject": u.get("inject") or base.get("inject") or {},
                "budget_tokens": u.get("budget_tokens", base.get("budget_tokens", 250)),
                "cooldown_turns": u.get("cooldown_turns", base.get("cooldown_turns", 8)),
                "label": u.get("label") or base.get("label") or "",
            })
            # Mark source on the raw for list_triggers (stripped at compile time)
            base["_source"] = "override"
            shipped[tid] = base
        else:
            u = _deep_copy_trigger(u)
            u["_source"] = "user"
            user_only.append(u)
    merged: list[dict] = []
    for tid in shipped_order:
        t = shipped[tid]
        t.setdefault("_source", "builtin")
        merged.append(t)
    merged.extend(user_only)
    return merged


def _raw_triggers() -> list[dict]:
    """Merged shipped + user override table (cached; auto-invalidates on disk change)."""
    global _RAW_MERGED
    _ensure_cache_fresh()
    if _RAW_MERGED is None:
        _RAW_MERGED = _merge_raw_triggers()
    return _RAW_MERGED


def load_table() -> list[dict]:
    """Compile the trigger table and cache it.

    Runtime source: shipped ``triggers.yaml`` / builtin mirror, merged with
    user overrides from ``~/.local/share/Chudware/context_injection/triggers.json``.
    Disabled triggers are compiled but skipped by :func:`match_triggers`.

    Each entry is compiled into a matcher-ready form with pre-compiled
    ``re.Pattern`` objects. Cache auto-invalidates when the user override
    file or packs dir mtime changes (cross-process GUI saves).
    """
    global _TABLE
    _ensure_cache_fresh()
    if _TABLE is not None:
        return _TABLE

    compiled: list[dict] = []
    for t in _raw_triggers():
        match_spec = t.get("match", {}) or {}
        kw_patterns: list[re.Pattern] = []
        for kw in match_spec.get("keywords", []) or []:
            try:
                kw_patterns.append(re.compile(str(kw), re.IGNORECASE))
            except re.error:
                pass  # skip malformed patterns silently

        compiled.append({
            "id": t["id"],
            "enabled": bool(t.get("enabled", True)),
            "kw_patterns": kw_patterns,
            "tool_names": set(match_spec.get("tools", []) or []),
            "events": set(match_spec.get("events", []) or []),
            "inject": t.get("inject", {}) or {},
            "budget_tokens": int(t.get("budget_tokens", 250)),
            "cooldown_turns": int(t.get("cooldown_turns", 8)),
            "purpose": str(t.get("purpose") or ""),
            "label": _label_for(t),
            "source": str(t.get("_source") or "builtin"),
        })

    _TABLE = compiled
    return _TABLE


# ---------------------------------------------------------------------------
# Pack loader + budgeter
# ---------------------------------------------------------------------------

def _read_pack_cached(path: pathlib.Path) -> Optional[str]:
    """Read *path* text, cached by ``(path, mtime)`` (None if missing/unreadable).

    A trigger can fire on every turn of a long session; without this, the
    same pack body would be re-read from disk every time.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _PACK_CACHE.pop(path, None)
        return None
    cached = _PACK_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        _PACK_CACHE.pop(path, None)
        return None
    _PACK_CACHE[path] = (mtime, text)
    return text


def _truncate_to_budget(text: str, budget_tokens: int) -> str:
    """Truncate *text* to the token heuristic without splitting a content line."""
    budget_chars = max(0, budget_tokens) * _CHARS_PER_TOKEN
    if len(text) <= budget_chars:
        return text
    marker = "[… truncated to budget]"
    if budget_chars <= len(marker):
        return marker[:budget_chars]
    content_budget = budget_chars - len(marker) - 1
    lines: list[str] = []
    used = 0
    for line in text.splitlines():
        cost = len(line) + (1 if lines else 0)
        if used + cost > content_budget:
            break
        lines.append(line)
        used += cost
    prefix = "\n".join(lines)
    return f"{prefix}\n{marker}" if prefix else marker


def _load_pack(pack_name: str, budget_tokens: int) -> str:
    """Read a context pack file and truncate to *budget_tokens* whole lines.

    User packs (``context_injection/packs/<name>.md``) shadow shipped packs of
    the same name so GUI edits take effect without rewriting the repo. Pack
    bodies are cached by ``(path, mtime)`` — see :func:`_read_pack_cached`.
    """
    if not pack_name:
        return ""
    pack_path = _PACKS_DIR / f"{pack_name}.md"
    user_path = user_packs_dir() / f"{pack_name}.md"
    if user_path.is_file():
        pack_path = user_path
    text = _read_pack_cached(pack_path)
    if text is None:
        return f"[context pack '{pack_name}' not found]"

    return _truncate_to_budget(text, budget_tokens)


# ---------------------------------------------------------------------------
# Prerun calc executor
# ---------------------------------------------------------------------------

def _extract_inputs(text: str, input_map: dict[str, str]) -> dict[str, Any]:
    """Extract calc inputs from *text* using named extraction groups.

    ``input_map`` maps calc param name → extraction-group name.
    Returns a partial dict; missing groups are omitted (caller decides skip).
    """
    extracted: dict[str, Any] = {}
    for param, group_name in input_map.items():
        if group_name == "thread_designation":
            m = _THREAD_DESIGNATION_RE.search(text)
            if m:
                extracted[param] = m.group(1).strip()
        elif group_name == "fit_designation":
            m = _FIT_DESIGNATION_RE.search(text)
            if m:
                extracted[param] = m.group(1).strip()
        elif group_name == "nominal_mm":
            m = _NOMINAL_MM_RE.search(text)
            if m:
                value = next((g for g in m.groups() if g is not None), "")
                try:
                    extracted[param] = float(value)
                except ValueError:
                    pass
    return extracted


def _run_prerun_calc(calc_spec: dict, text: str) -> Optional[dict]:
    """Execute a single prerun calc spec against matched *text*.

    Returns the aicad.calc/1 answer dict, or ``None`` when the calc cannot
    fire (missing inputs, ok=False, or exception).  Always fails soft.
    """
    calc_id = calc_spec.get("calc")
    inputs_from = calc_spec.get("inputs_from", "literal")
    input_map = calc_spec.get("input_map", {})

    if not calc_id:
        return None

    # Build inputs
    inputs: dict[str, Any] = {}
    if inputs_from == "match":
        inputs = _extract_inputs(text, input_map)
        # If required inputs not extractable, skip (advertise only).
        if not inputs:
            return None
        # Remove None-valued items from input_map (literal nulls = advertise only)
        inputs = {k: v for k, v in inputs.items() if v is not None}
        if not inputs:
            return None
    elif inputs_from == "literal":
        # Only run if ALL mapped params have non-null literal values
        inputs = {k: v for k, v in input_map.items() if v is not None}
        if not inputs:
            return None

    try:
        if calc_id == "tap_drill":
            from services.research.machining import tap_drill  # noqa: PLC0415
            result = tap_drill(inputs.get("thread", ""))
        elif calc_id == "fit_calc":
            from services.research.fits import fit_calc  # noqa: PLC0415
            nominal = inputs.get("nominal")
            fit = inputs.get("fit", "H7/g6")
            if nominal is None:
                return None
            result = fit_calc(float(nominal), str(fit))
        elif calc_id == "snap_fit":
            from services.research.calcs.snap_fit import snap_fit as _snap  # noqa: PLC0415
            result = _snap(**inputs)
        elif calc_id == "wall_thickness":
            from services.research.calcs.wall_thickness import wall_thickness as _wt  # noqa: PLC0415
            result = _wt(**inputs)
        elif calc_id == "bend_allowance":
            from services.research.calcs.bend_allowance import bend_allowance as _ba  # noqa: PLC0415
            result = _ba(**inputs)
        elif calc_id == "draft_check":
            from services.research.calcs.draft_check import draft_check as _dc  # noqa: PLC0415
            result = _dc(**inputs)
        else:
            # Try the generic calc registry
            from services.research.calcs.registry import run_calc  # noqa: PLC0415
            result = run_calc(calc_id, inputs)
    except Exception:  # noqa: BLE001
        return None

    if not result:
        return None
    # Some calcs (tap_drill) don't set ok=True explicitly — treat any result with
    # an "answer" key and no explicit ok=False as success.
    if result.get("ok") is False:
        return None
    answer = result.get("answer")
    if not answer or not isinstance(answer, dict):
        return None
    return answer


# ---------------------------------------------------------------------------
# Main matcher
# ---------------------------------------------------------------------------

def match_triggers(
    output_text: str,
    tool_names: Optional[list[str]] = None,
    events: Optional[list[str]] = None,
    seq: int = 0,
    cooldown_state: Optional[dict[str, int]] = None,
) -> tuple[list[dict], list[dict]]:
    """Evaluate all triggers against an observation turn.

    Parameters
    ----------
    output_text:
        The model's text output for this turn (and/or user text routed to the
        role).  Keyword patterns match against this string.
    tool_names:
        Tool names called this turn (tool-name trigger matching).
    events:
        Geometry event strings this turn (e.g. ``["thin_wall"]``).
    seq:
        The current advisory sequence counter (used for cooldown tracking).
    cooldown_state:
        Mutable ``{trigger_id: last_fired_seq}`` dict owned by the caller.
        Updated in-place when a trigger fires.  ``None`` → no cooldown
        (fires every time; useful in tests).

    Returns
    -------
    (context_items, ledger_events)
        *context_items* — list of advisory-item dicts (severity=info, check=trigger_id)
        ready to be appended to an advisory block's items list.
        *ledger_events* — list of ``{trigger_id, matched, tokens}`` fire records.
    """
    tool_set = set(tool_names or [])
    event_set = set(events or [])
    text = output_text or ""

    context_items: list[dict] = []
    ledger_events: list[dict] = []

    for trigger in load_table():
        tid = trigger["id"]

        # Disabled via GUI / user override — never inject.
        if not trigger.get("enabled", True):
            continue

        # ── cooldown check ──────────────────────────────────────────────────
        if cooldown_state is not None:
            last = cooldown_state.get(tid, -9999)
            if (seq - last) < trigger["cooldown_turns"]:
                continue  # still in cooldown window

        # ── match ──────────────────────────────────────────────────────────
        matched: Optional[str] = None

        # Keyword match over text (single compiled pass per pattern)
        for pat in trigger["kw_patterns"]:
            m = pat.search(text)
            if m:
                matched = m.group(0)
                break

        # Tool-name match
        if matched is None and trigger["tool_names"] & tool_set:
            matched = next(iter(trigger["tool_names"] & tool_set))

        # Event match
        if matched is None and trigger["events"] & event_set:
            matched = next(iter(trigger["events"] & event_set))

        if matched is None:
            continue  # no match — zero cost from here

        # ── prerun calcs ────────────────────────────────────────────────────
        calc_numbers: dict[str, Any] = {}
        inject = trigger["inject"]
        for calc_spec in inject.get("prerun_calcs", []):
            answer = _run_prerun_calc(calc_spec, text)
            if answer and isinstance(answer, dict):
                calc_numbers.update(answer)

        # ── pack load + budget ─────────────────────────────────────────────
        pack_name = inject.get("context_pack", "")
        exemplar = inject.get("exemplar", "")
        budget = trigger["budget_tokens"]

        pack_text = _load_pack(pack_name, budget) if pack_name else ""
        if exemplar:
            pack_text = pack_text + f"\nExample: {exemplar}" if pack_text else f"Example: {exemplar}"

        # The configured cap covers everything injected, including the context prefix and
        # exemplar. Applying it only while loading the pack let every exemplar exceed its Rule.
        finding_text = _truncate_to_budget(f"[context: {tid}] {pack_text}", budget)
        token_cost = (len(finding_text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN
        item: dict[str, Any] = {
            "id": f"ctx_{tid}",
            "severity": "info",
            "check": f"context:{tid}",
            "finding": finding_text,
            "refs": {},
            "numbers": calc_numbers,
            "candidate_fixes": [],  # info items may be empty (§3)
            "helper_tools": [],
            "source": {"kind": "table", "detail": f"trigger:{tid}"},
        }
        context_items.append(item)

        # ── update cooldown ─────────────────────────────────────────────────
        if cooldown_state is not None:
            cooldown_state[tid] = seq

        # ── ledger event ───────────────────────────────────────────────────
        ledger_events.append({
            "trigger_id": tid,
            "label": trigger.get("label") or tid,
            "matched": matched,
            "tokens": token_cost,
        })

    return context_items, ledger_events


# ---------------------------------------------------------------------------
# GUI / RPC CRUD (user overrides; never rewrites shipped triggers.yaml)
# ---------------------------------------------------------------------------

def list_triggers() -> list[dict]:
    """Merged catalog for the Context Injection settings tab.

    Each row is JSON-serializable (no compiled regexes) and includes:
    ``id, enabled, purpose, match, inject, budget_tokens, cooldown_turns, source``.
    """
    out: list[dict] = []
    for t in _raw_triggers():
        row = _normalize_trigger(t) or {}
        if not row:
            continue
        row["source"] = str(t.get("_source") or "builtin")
        out.append(row)
    return out


def get_trigger(trigger_id: str) -> dict[str, Any]:
    """Full trigger + pack body for the editor."""
    tid = str(trigger_id or "").strip()
    if not tid:
        return {"ok": False, "reason": "id required"}
    for t in _raw_triggers():
        if t.get("id") != tid:
            continue
        row = _normalize_trigger(t) or {}
        row["source"] = str(t.get("_source") or "builtin")
        pack_name = (row.get("inject") or {}).get("context_pack") or ""
        pack = get_pack(pack_name) if pack_name else {"ok": True, "name": "", "body": "", "source": ""}
        return {
            "ok": True,
            "trigger": row,
            "pack": pack if pack.get("ok") else {"ok": True, "name": pack_name, "body": "", "source": "missing"},
        }
    return {"ok": False, "reason": f"trigger not found: {tid!r}"}


def save_trigger(trigger: dict) -> dict[str, Any]:
    """Create/update a user override (or user-only trigger).

    Shipped triggers are never rewritten — user edits land in
    ``triggers.json`` and win on merge. Clears the runtime cache so the next
    ``match_triggers`` call sees the change.
    """
    norm = _normalize_trigger(trigger if isinstance(trigger, dict) else {})
    if not norm:
        return {"ok": False, "reason": "invalid trigger (id must match [A-Za-z][A-Za-z0-9_-]{0,63})"}
    tid = norm["id"]
    # Validate keyword regexes early so the GUI can surface the error.
    for kw in norm["match"]["keywords"]:
        try:
            re.compile(str(kw), re.IGNORECASE)
        except re.error as exc:
            return {"ok": False, "reason": f"bad keyword regex {kw!r}: {exc}"}

    user = _load_user_triggers()
    replaced = False
    for i, u in enumerate(user):
        if u.get("id") == tid:
            user[i] = norm
            replaced = True
            break
    if not replaced:
        user.append(norm)
    try:
        _write_user_triggers(user)
    except OSError as exc:
        return {"ok": False, "reason": f"write failed: {exc}"}
    clear_cache()
    source = "override" if any(s.get("id") == tid for s in _shipped_triggers()) else "user"
    return {"ok": True, "id": tid, "source": source}


def delete_trigger(trigger_id: str) -> dict[str, Any]:
    """Remove a user override (restores shipped) or a user-only trigger.

    Builtin rows cannot be deleted — only their overrides can be cleared.
    """
    tid = str(trigger_id or "").strip()
    if not tid:
        return {"ok": False, "reason": "id required"}
    user = _load_user_triggers()
    kept = [u for u in user if u.get("id") != tid]
    if len(kept) == len(user):
        # Nothing to delete — if it's a shipped id, say so; else not found.
        if any(s.get("id") == tid for s in _shipped_triggers()):
            return {"ok": False, "reason": "no user override to clear (shipped trigger stays)"}
        return {"ok": False, "reason": f"trigger not found: {tid!r}"}
    try:
        _write_user_triggers(kept)
    except OSError as exc:
        return {"ok": False, "reason": f"write failed: {exc}"}
    clear_cache()
    restored = any(s.get("id") == tid for s in _shipped_triggers())
    return {"ok": True, "id": tid, "restored_builtin": restored}


def list_packs() -> list[dict]:
    """Shipped + user pack names (user shadows shipped)."""
    names: dict[str, str] = {}
    if _PACKS_DIR.is_dir():
        for p in sorted(_PACKS_DIR.glob("*.md")):
            names[p.stem] = "builtin"
    ud = user_packs_dir()
    if ud.is_dir():
        for p in sorted(ud.glob("*.md")):
            names[p.stem] = "user" if names.get(p.stem) != "builtin" else "override"
    return [{"name": n, "source": s} for n, s in sorted(names.items())]


def get_pack(name: str) -> dict[str, Any]:
    """Return pack body; user pack shadows shipped."""
    pack = str(name or "").strip()
    if not pack or not _PACK_RE.match(pack):
        return {"ok": False, "reason": f"invalid pack name: {name!r}"}
    user_path = user_packs_dir() / f"{pack}.md"
    shipped_path = _PACKS_DIR / f"{pack}.md"
    if user_path.is_file():
        try:
            body = user_path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "reason": str(exc)}
        source = "override" if shipped_path.is_file() else "user"
        return {"ok": True, "name": pack, "body": body, "source": source}
    if shipped_path.is_file():
        try:
            body = shipped_path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "name": pack, "body": body, "source": "builtin"}
    return {"ok": False, "reason": f"pack not found: {pack!r}"}


def save_pack(name: str, body: str) -> dict[str, Any]:
    """Write/overwrite a user pack body (never rewrites shipped packs)."""
    pack = str(name or "").strip()
    if not pack or not _PACK_RE.match(pack):
        return {"ok": False, "reason": f"invalid pack name: {name!r}"}
    if not isinstance(body, str):
        return {"ok": False, "reason": "body must be a string"}
    dest = user_packs_dir() / f"{pack}.md"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".md.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(dest)
    except OSError as exc:
        return {"ok": False, "reason": f"write failed: {exc}"}
    # _load_pack caches bodies by (path, mtime); the atomic replace above
    # changes dest's mtime so that cache self-invalidates, and clear_cache()
    # drops it outright — also refreshes list_packs / get_trigger source chips.
    clear_cache()
    source = "override" if (_PACKS_DIR / f"{pack}.md").is_file() else "user"
    return {"ok": True, "name": pack, "source": source}


def delete_pack(name: str) -> dict[str, Any]:
    """Delete a user pack (restores shipped body if one exists)."""
    pack = str(name or "").strip()
    if not pack or not _PACK_RE.match(pack):
        return {"ok": False, "reason": f"invalid pack name: {name!r}"}
    path = user_packs_dir() / f"{pack}.md"
    if not path.is_file():
        if (_PACKS_DIR / f"{pack}.md").is_file():
            return {"ok": False, "reason": "no user pack to clear (shipped pack stays)"}
        return {"ok": False, "reason": f"pack not found: {pack!r}"}
    try:
        path.unlink()
    except OSError as exc:
        return {"ok": False, "reason": f"delete failed: {exc}"}
    clear_cache()
    restored = (_PACKS_DIR / f"{pack}.md").is_file()
    return {"ok": True, "name": pack, "restored_builtin": restored}
