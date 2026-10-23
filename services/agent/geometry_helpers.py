"""Proactive op-aware helper / calculator registry — the HEART of "advise, don't
recall" (Stage 2 GEOMETRY REALIZATION).

Design-pipeline doc §2 Stage 2, §3.2; contract ``contracts/design_pipeline.md`` §8.

The problem this solves: a local model on a mating bore should not *recall* a fit
tolerance — the exact calculator and process data for the geometry kind it is about
to build should already be in front of it, and every number it emits must carry
provenance back to a real tool/table result.

This module is the registry. Each **geometry kind** maps to:
  * the existing ``aicad.trigger/1`` id (services/agent/advisor/triggers.yaml) that
    injects its context pack + prerun calc — the wiring is reused, not duplicated;
  * the helper **tools** the geometry_designer should call for that kind;
  * a **resolver** that pulls the *material/process-gated* numbers from the real
    calculators (``services.research.*``) and the C++ DfM process profiles
    (``core/dfm``, via ``core_bridge``) — so machining and printing yield
    *different* minimums, and every value comes back with a citation.

Nothing here invents a number. ``resolve_helpers(kind, material, process, …)``
returns a :class:`HelperResult` whose ``numbers`` and ``provenance`` are the calc /
profile outputs verbatim. A frozen frontier model may ignore the registry (helpers
are offered, not imposed — contract §10); a local model gets the full pack.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# The DFM process ids the profiles module knows (fdm/cnc_mill/lathe/sheet_metal/
# laser/injection). We map the geometry_designer's user-facing process words onto
# these canonical ids so a "machined" request and a "printed" request resolve to
# genuinely different profiles.
_PROCESS_ALIASES: dict[str, str] = {
    "machined": "cnc_mill", "machining": "cnc_mill", "cnc": "cnc_mill",
    "milled": "cnc_mill", "mill": "cnc_mill", "cnc_mill": "cnc_mill",
    # -ing forms: observed live 2026-08-04 (opus-5 called dfm_check("cnc_milling")
    # in the same run where "machining" had just been accepted).
    "milling": "cnc_mill", "cnc_milling": "cnc_mill", "cnc milling": "cnc_mill",
    "cnc machining": "cnc_mill", "cnc_machining": "cnc_mill",
    "turned": "lathe", "lathe": "lathe",
    "printed": "fdm", "3d printed": "fdm", "3dp": "fdm", "fdm": "fdm",
    "fff": "fdm", "additive": "fdm", "sla": "fdm",
    "molded": "injection", "moulded": "injection", "injection": "injection",
    "injection molded": "injection", "cast": "injection",
    "sheet": "sheet_metal", "sheet metal": "sheet_metal",
    "sheet_metal": "sheet_metal", "formed": "sheet_metal", "bent": "sheet_metal",
    "laser": "laser", "laser cut": "laser", "waterjet": "laser",
}


def canonical_process(process: Optional[str]) -> str:
    """Map a user-facing process word to a process_profiles id (default fdm)."""
    if not process:
        return "fdm"
    key = process.strip().lower()
    return _PROCESS_ALIASES.get(key, key if key in _KNOWN_PROFILE_IDS() else "fdm")


def canonical_process_strict(process: Optional[str]) -> Optional[str]:
    """Map a known process alias to its canonical id; pass anything else through.

    :func:`canonical_process` defaults an unrecognized word to ``fdm``, which is
    right for a profile lookup that must always produce something but WRONG for
    a tool argument: silently manufacturing for the wrong process is worse than
    refusing. This variant maps only what it actually knows and returns the
    input untouched otherwise, so the caller's existing "Unknown process 'x'.
    Known processes: [...]" teaching error still fires for a genuine typo.

    Live 2026-08-04 (bracket mission, BOTH opus-5 and k3-256k): the mission says
    the part "will be machined", and both models called
    ``set_process("machining")`` / ``dfm_check(process="machining")``. The alias
    table below already knew that word — nothing consulted it.
    """
    if not process:
        return process
    return _PROCESS_ALIASES.get(process.strip().lower(), process)


def _KNOWN_PROFILE_IDS() -> set[str]:
    """The DfM process ids, from the C++ core (core/dfm, ``dfm_known_processes``);
    the literal set is the offline fallback when no engine bridge is up."""
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        r = cb.call("dfm_known_processes", {})
        if r.get("ok") and isinstance(r.get("result"), list):
            return set(r["result"])
    except Exception:  # noqa: BLE001
        pass
    return {"fdm", "cnc_mill", "lathe", "sheet_metal", "laser", "injection"}

# Offline mirror of the load-bearing DfM profile fields.  These are copied verbatim
# from ``core/dfm/process_profiles.cpp::profiles`` so the agent-side helper remains
# usable on an unprovisioned gate machine where ``libchudware`` is intentionally
# absent.  The C++ RPC remains authoritative whenever it is available.
_OFFLINE_PROFILE_PARAMS: dict[str, dict[str, Any]] = {
    "fdm": {
        "min_wall_mm": 1.0, "min_feature_mm": 0.8, "hole_comp_mm": 0.20,
        "tolerance_band_mm": 0.40,
        "fit_classes_mm": {"slip": 0.40, "location": 0.20, "press": 0.10, "free": 0.50},
        "draft": {"required": False, "min_deg": 0.0},
        "overhang": {"max_deg": 45, "max_bridge_mm": 5},
        "thread": {"strategy": "heat_set_insert", "printed_ok_min": "M5"},
    },
    "cnc_mill": {
        "min_wall_mm": 0.5, "min_feature_mm": 0.5, "hole_comp_mm": 0.0,
        "tolerance_band_mm": 0.05,
        "fit_classes_mm": {"slip": 0.05, "location": 0.02, "press": -0.02, "free": 0.10},
        "draft": {"required": False, "min_deg": 0.0},
        "overhang": {"max_deg": None, "max_bridge_mm": None},
        "thread": {"strategy": "tapped", "printed_ok_min": None},
    },
    "lathe": {
        "min_wall_mm": 0.5, "min_feature_mm": 0.5, "hole_comp_mm": 0.0,
        "tolerance_band_mm": 0.05,
        "fit_classes_mm": {"slip": 0.05, "location": 0.02, "press": -0.02, "free": 0.10},
        "draft": {"required": False, "min_deg": 0.0},
        "overhang": {"max_deg": None, "max_bridge_mm": None},
        "thread": {"strategy": "single_point_or_tapped", "printed_ok_min": None},
    },
    "sheet_metal": {
        "min_wall_mm": 1.0, "min_feature_mm": 1.0, "hole_comp_mm": 0.0,
        "tolerance_band_mm": 0.10,
        "fit_classes_mm": {"slip": None, "location": None, "press": None, "free": None},
        "draft": {"required": False, "min_deg": 0.0},
        "overhang": {"max_deg": None, "max_bridge_mm": None},
        "thread": {"strategy": "clinch_or_weld_nut", "printed_ok_min": None},
    },
    "laser": {
        "min_wall_mm": 1.0, "min_feature_mm": 0.5, "hole_comp_mm": 0.0,
        "tolerance_band_mm": 0.10,
        "fit_classes_mm": {"slip": None, "location": None, "press": None, "free": None},
        "draft": {"required": False, "min_deg": 0.0},
        "overhang": {"max_deg": None, "max_bridge_mm": None},
        "thread": {"strategy": "none", "printed_ok_min": None},
    },
    "injection": {
        "min_wall_mm": 1.0, "min_feature_mm": 0.5, "hole_comp_mm": 0.0,
        "tolerance_band_mm": 0.10,
        "fit_classes_mm": {"slip": 0.10, "location": 0.05, "press": 0.02, "free": 0.20},
        "draft": {"required": True, "min_deg": 1.0},
        "overhang": {"max_deg": None, "max_bridge_mm": None},
        "thread": {"strategy": "molded", "printed_ok_min": None},
    },
}


# --------------------------------------------------------------------------- registry entries

@dataclass(frozen=True)
class HelperBinding:
    """One geometry kind → its trigger + helper tools + a resolver name.

    ``trigger_id`` is the existing ``aicad.trigger/1`` id (or the two pipeline-added
    ids) that injects the context pack for this kind. ``tools`` are the MCP tools
    the geometry_designer should call. ``resolver`` names the gating function below.
    """
    kind: str
    trigger_id: str
    tools: tuple[str, ...]
    resolver: str
    summary: str = ""


# The op-aware registry. ≥8 geometry kinds (contract §8 binding table + the two
# pipeline-added kinds), each bound to a real trigger and helper set.
_BINDINGS: dict[str, HelperBinding] = {
    "mating_bore": HelperBinding(
        "mating_bore", "bearing_seat",
        ("fit_calc", "recommend_fit"), "resolve_fit",
        "shaft seat / bearing seat / running bore — ISO 286 fit, gated by process band"),
    "press_fit": HelperBinding(
        "press_fit", "press_fit_mention",
        ("fit_calc",), "resolve_press_fit",
        "interference fit — H7/p6 band from ISO 286"),
    "threaded_hole": HelperBinding(
        "threaded_hole", "thread_mention",
        ("tap_drill", "research_thread_spec"), "resolve_thread",
        "tapped hole / stud boss — pilot Ø + engagement, gated by material"),
    "fastener_pattern": HelperBinding(
        "fastener_pattern", "fastener_pattern",
        ("tap_drill",), "resolve_clearance_hole",
        "bolt circle / clearance hole — ISO 273 pass-through"),
    "thin_wall": HelperBinding(
        "thin_wall", "thin_wall",
        ("wall_thickness",), "resolve_wall",
        "wall / shell / rib — process minimum wall, gated by process"),
    "snap_fit": HelperBinding(
        "snap_fit", "snap_fit_mention",
        ("research_eng_calc",), "resolve_snap_fit",
        "cantilever hook / latch — strain viability, gated by material"),
    "sheet_bend": HelperBinding(
        "sheet_bend", "sheet_bend",
        ("bend_allowance",), "resolve_bend",
        "sheet-metal bend — bend allowance / deduction, gated by t and r"),
    "molded_draft": HelperBinding(
        "molded_draft", "molded_draft",
        ("draft_check", "draft_analysis"), "resolve_draft",
        "molded wall — mandatory release draft, gated by finish + draw depth"),
    "material_dims": HelperBinding(
        "material_dims", "material_mention",
        ("research_material_lookup",), "resolve_material",
        "any material-gated dim — property lookup"),
    "tolerance_callout": HelperBinding(
        "tolerance_callout", "tolerance_mention",
        ("fit_calc",), "resolve_fit",
        "general fit / tolerance — ISO 286 band"),
}


def known_kinds() -> list[str]:
    """Every geometry kind the registry covers (≥8)."""
    return sorted(_BINDINGS)


def binding_for(kind: str) -> Optional[HelperBinding]:
    """The binding for a geometry kind (case/space-normalised), or None."""
    if not kind:
        return None
    key = kind.strip().lower().replace(" ", "_").replace("-", "_")
    if key in _BINDINGS:
        return _BINDINGS[key]
    # A few friendly aliases onto canonical kinds.
    aliases = {
        "bore": "mating_bore", "shaft_seat": "mating_bore",
        "bearing_seat": "mating_bore", "journal": "mating_bore",
        "interference_fit": "press_fit",
        "tapped_hole": "threaded_hole", "thread": "threaded_hole",
        "stud": "threaded_hole", "boss": "threaded_hole",
        "bolt_circle": "fastener_pattern", "clearance_hole": "fastener_pattern",
        "wall": "thin_wall", "shell": "thin_wall", "rib": "thin_wall",
        "latch": "snap_fit", "cantilever_hook": "snap_fit", "clip": "snap_fit",
        "bend": "sheet_bend", "flange": "sheet_bend", "flat_pattern": "sheet_bend",
        "draft": "molded_draft", "molded": "molded_draft",
        "tolerance": "tolerance_callout", "fit": "tolerance_callout",
    }
    alias = aliases.get(key)
    return _BINDINGS.get(alias) if alias else None


# --------------------------------------------------------------------------- resolver result

@dataclass
class HelperResult:
    """What the registry hands the geometry_designer for one micro-task.

    ``numbers`` are the calc/profile outputs verbatim; ``provenance`` is the citation
    string that MUST accompany the emitted ``geometry_spec`` (contract §3.2 requires
    a provenance field, rejected without it). ``ok`` is False when inputs were
    insufficient — the caller then asks the user rather than inventing a value.
    """
    kind: str
    trigger_id: str
    tools: list[str]
    numbers: dict[str, Any] = field(default_factory=dict)
    provenance: str = ""
    ok: bool = True
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "trigger_id": self.trigger_id, "tools": self.tools,
            "numbers": self.numbers, "provenance": self.provenance,
            "ok": self.ok, "note": self.note,
        }


def _profile(process: str) -> dict[str, Any]:
    """Return the C++ DfM profile, with its exact table as an offline fallback."""
    proc = canonical_process(process)
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        r = cb.call("dfm_get_profile", {"process": proc})
    except Exception:  # noqa: BLE001 - missing runtime core is a supported configuration
        return {"process": proc, "params": _OFFLINE_PROFILE_PARAMS[proc]}
    if not r.get("ok"):
        raise ValueError(r.get("error") or "dfm_get_profile failed")
    return r.get("result") or {}


# --------------------------------------------------------------------------- resolvers
# Each returns a HelperResult with REAL numbers + a provenance string. Gating by
# process/material happens HERE — the same geometry kind resolves to different
# minimums for machined vs printed vs molded.

def resolve_wall(*, process: str = "fdm", feature_height_mm: Optional[float] = None,
                 material: str = "", **_: Any) -> HelperResult:
    proc = canonical_process(process)
    prof = _profile(process)["params"]
    min_wall = prof.get("min_wall_mm")
    numbers = {
        "min_wall_mm": min_wall,
        "min_feature_mm": prof.get("min_feature_mm"),
        "tolerance_band_mm": prof.get("tolerance_band_mm"),
    }
    prov = (f"process_profiles.get_profile('{proc}').params.min_wall_mm = {min_wall} mm "
            f"[core/dfm/process_profiles.cpp]")
    # When a machining feature height is known, refine with the chatter-rule calc.
    if proc in ("cnc_mill", "lathe") and feature_height_mm:
        try:
            from services.research.calcs import run_calc
            r = run_calc("wall_thickness", {"process": "machining",
                                            "feature_height_mm": float(feature_height_mm),
                                            "material_family": material or "default"})
            if r.get("ok"):
                numbers["chatter_min_wall_mm"] = r["answer"]["min_wall_mm"]
                prov += (f"; wall_thickness(machining, h={feature_height_mm}) → "
                         f"{r['answer']['min_wall_mm']} mm ({r['source']['citation']})")
        except Exception:  # noqa: BLE001
            pass
    return HelperResult("thin_wall", "thin_wall", ["wall_thickness"], numbers, prov)


def resolve_fit(*, nominal_mm: Optional[float] = None, fit: str = "H7/g6",
                process: str = "fdm", **_: Any) -> HelperResult:
    proc = canonical_process(process)
    prof = _profile(process)["params"]
    fit_classes = prof.get("fit_classes_mm", {})
    numbers: dict[str, Any] = {
        "process_fit_classes_mm": fit_classes,
        "tolerance_band_mm": prof.get("tolerance_band_mm"),
        "hole_comp_mm": prof.get("hole_comp_mm"),
    }
    prov = (f"process_profiles.get_profile('{proc}').params.fit_classes_mm = {fit_classes} "
            f"[core/dfm/process_profiles.cpp]")
    if nominal_mm:
        try:
            from services.research.fits import fit_calc
            fc = fit_calc(float(nominal_mm), fit)
            if fc.get("ok"):
                numbers["iso286"] = fc["answer"]
                prov = (f"fit_calc({fit}, nominal={nominal_mm}) → "
                        f"clearance {fc['answer'].get('clearance')} mm "
                        f"[iso286.json]; " + prov)
        except Exception:  # noqa: BLE001
            pass
    return HelperResult("mating_bore", "bearing_seat", ["fit_calc", "recommend_fit"],
                        numbers, prov)


def resolve_press_fit(*, nominal_mm: Optional[float] = None,
                      process: str = "fdm", **_: Any) -> HelperResult:
    res = resolve_fit(nominal_mm=nominal_mm, fit="H7/p6", process=process)
    res.kind = "press_fit"
    res.trigger_id = "press_fit_mention"
    res.tools = ["fit_calc"]
    return res


def resolve_thread(*, thread: str = "", material: str = "", **_: Any) -> HelperResult:
    numbers: dict[str, Any] = {}
    prov_parts: list[str] = []
    if thread:
        try:
            from services.research.machining import tap_drill
            td = tap_drill(thread)
            ans = td.get("answer") if isinstance(td, dict) else None
            if ans:
                numbers["tap_drill"] = ans
                prov_parts.append(f"tap_drill({thread}) → {ans} [Machinery's Handbook]")
        except Exception:  # noqa: BLE001
            pass
    # Engagement multiple is material-gated (steel 1.0×D, Al 1.5×D, plastic 2.0×D).
    eng = {"steel": 1.0, "stainless": 1.0, "aluminum": 1.5, "aluminium": 1.5,
           "titanium": 1.25, "abs": 2.0, "pla": 2.0, "nylon": 2.0, "plastic": 2.0}
    mat_key = (material or "").strip().lower()
    mult = next((v for k, v in eng.items() if k in mat_key), None)
    if mult is not None:
        numbers["min_engagement_x_dia"] = mult
        prov_parts.append(f"engagement {mult}×D for {material} (Machinery's Handbook thread engagement)")
    ok = bool(numbers)
    return HelperResult("threaded_hole", "thread_mention",
                        ["tap_drill", "research_thread_spec"], numbers,
                        "; ".join(prov_parts), ok=ok,
                        note="" if ok else "supply thread designation + material to size")


def resolve_clearance_hole(*, thread: str = "", **_: Any) -> HelperResult:
    numbers: dict[str, Any] = {}
    prov = ""
    if thread:
        try:
            from services.research.machining import tap_drill
            td = tap_drill(thread)
            ans = td.get("answer") if isinstance(td, dict) else None
            if ans:
                numbers["tap_drill"] = ans
                prov = f"tap_drill({thread}) → {ans} [Machinery's Handbook / ISO 273]"
        except Exception:  # noqa: BLE001
            pass
    return HelperResult("fastener_pattern", "fastener_pattern", ["tap_drill"],
                        numbers, prov, ok=bool(numbers),
                        note="" if numbers else "supply thread designation for clearance hole")


def resolve_snap_fit(*, material: str = "", **_: Any) -> HelperResult:
    # The snap_fit calc needs beam geometry; here we advertise the check + the
    # material's strain limit source. The calc itself runs when geometry is known.
    prov = ("snap_fit calc (services/research/calcs/snap_fit.py): "
            "ε_allow ≈ 0.6×(yield/E); viable if ε_root ≤ ε_allow — run with the "
            "cantilever length/thickness")
    return HelperResult("snap_fit", "snap_fit_mention", ["research_eng_calc"],
                        {}, prov, ok=True,
                        note="run snap_fit calc with beam geometry to size the hook")


def resolve_bend(*, thickness_mm: Optional[float] = None,
                 inside_radius_mm: Optional[float] = None,
                 angle_deg: float = 90.0, process: str = "sheet_metal", **_: Any) -> HelperResult:
    numbers: dict[str, Any] = {}
    prov = ""
    if thickness_mm and inside_radius_mm is not None:
        try:
            from services.research.calcs import run_calc
            r = run_calc("bend_allowance", {"thickness_mm": float(thickness_mm),
                                            "inside_radius_mm": float(inside_radius_mm),
                                            "angle_deg": float(angle_deg)})
            if r.get("ok"):
                numbers = r["answer"]
                prov = (f"bend_allowance(t={thickness_mm}, r={inside_radius_mm}, "
                        f"θ={angle_deg}) → BA={r['answer']['bend_allowance_mm']} mm, "
                        f"BD={r['answer']['bend_deduction_mm']} mm ({r['source']['citation']})")
        except Exception:  # noqa: BLE001
            pass
    prof = _profile("sheet_metal")["params"]
    numbers.setdefault("min_gauge_mm", prof.get("min_wall_mm"))
    if not prov:
        prov = ("bend_allowance calc: BA=θ·(π/180)·(r+K·t) — supply t and inside "
                "radius; K from r/t table (Machinery's Handbook)")
    return HelperResult("sheet_bend", "sheet_bend", ["bend_allowance"], numbers, prov,
                        ok=bool(thickness_mm and inside_radius_mm is not None))


def resolve_draft(*, draw_depth_mm: Optional[float] = None, finish: str = "smooth",
                  process: str = "injection", **_: Any) -> HelperResult:
    prof = _profile("injection")["params"]
    draft = prof.get("draft", {})
    numbers: dict[str, Any] = {"process_min_draft_deg": draft.get("min_deg"),
                               "draft_required": draft.get("required")}
    prov = (f"process_profiles injection.draft.min_deg = {draft.get('min_deg')}° "
            f"(required={draft.get('required')}) [core/dfm/process_profiles.cpp]")
    if draw_depth_mm:
        try:
            from services.research.calcs import run_calc
            r = run_calc("draft_check", {"draw_depth_mm": float(draw_depth_mm),
                                         "finish": finish})
            if r.get("ok"):
                numbers.update(r["answer"])
                prov = (f"draft_check(depth={draw_depth_mm}, finish={finish}) → "
                        f"{r['answer']['draft_deg']}°, taper {r['answer']['offset_per_side_mm']} "
                        f"mm/side ({r['source']['citation']})")
        except Exception:  # noqa: BLE001
            pass
    return HelperResult("molded_draft", "molded_draft", ["draft_check", "draft_analysis"],
                        numbers, prov)


def _bundled_material_lookup(material: str) -> Optional[dict[str, Any]]:
    """Resolve an exact material alias from the registry when the C++ core is absent."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    explicit = os.environ.get("CHUDWARE_MATERIALS_REGISTRY")
    if explicit:
        candidates = [Path(explicit)]
    else:
        candidates: list[Path] = []
        if os.name == "nt":
            if appdata := os.environ.get("APPDATA"):
                candidates.append(Path(appdata) / "Chudware" / "materials" / "registry.json")
        elif xdg := os.environ.get("XDG_DATA_HOME"):
            candidates.append(Path(xdg) / "Chudware" / "materials" / "registry.json")
        elif home := os.environ.get("HOME"):
            candidates.append(Path(home) / ".local" / "share" / "Chudware"
                              / "materials" / "registry.json")
        candidates.append(Path(__file__).resolve().parents[2]
                          / "core" / "data" / "materials" / "registry.json")

    for path in candidates:
        try:
            materials = json.loads(path.read_text(encoding="utf-8")).get("materials", {})
        except (OSError, ValueError, TypeError):
            continue
        query = material.strip().lower()
        match: Optional[tuple[str, dict[str, Any]]] = None
        for key, record in materials.items():
            names = [key, record.get("name", ""), *(record.get("aliases") or [])]
            if query in {str(name).strip().lower() for name in names}:
                match = (key, record)
        if match is None:
            return None
        key, record = match
        properties = record.get("mechanical")
        if not isinstance(properties, dict) or not properties:
            return None
        provenance = record.get("provenance") or {}
        return {
            "key": key,
            "name": record.get("name") or key,
            "properties": properties,
            "citation": provenance.get("source"),
            "registry_path": str(path),
        }
    return None


def resolve_material(*, material: str = "", **_: Any) -> HelperResult:
    numbers: dict[str, Any] = {}
    prov = ""
    if material:
        try:
            from services.research.material_lookup import material_lookup  # type: ignore
            ml = material_lookup(material)
        except Exception:  # noqa: BLE001 - use the same bundled registry without the core
            ml = None
        if isinstance(ml, dict) and ml.get("ok"):
            props = ml.get("properties") or {}
            if props:
                numbers = dict(props)
                citation = ml.get("citation") or (ml.get("provenance") or {}).get("source")
                prov = (f"research_material_lookup({material}) → {ml.get('answer')} "
                        f"({citation or 'bundled registry provenance'})")
        elif ml is None:
            fallback = _bundled_material_lookup(material)
            if fallback is not None:
                numbers = dict(fallback["properties"])
                prov = (
                    f"research_material_lookup fallback via _bundled_material_lookup({material}) "
                    f"→ {fallback['name']} ({fallback['citation']}) "
                    f"[{fallback['registry_path']}]"
                )
    if not prov:
        prov = ("research_material_lookup: call with the material name for "
                "E / yield / density [core/data/materials/registry.json]")
    return HelperResult("material_dims", "material_mention",
                        ["research_material_lookup"], numbers, prov, ok=bool(numbers))


_RESOLVERS = {
    "resolve_wall": resolve_wall,
    "resolve_fit": resolve_fit,
    "resolve_press_fit": resolve_press_fit,
    "resolve_thread": resolve_thread,
    "resolve_clearance_hole": resolve_clearance_hole,
    "resolve_snap_fit": resolve_snap_fit,
    "resolve_bend": resolve_bend,
    "resolve_draft": resolve_draft,
    "resolve_material": resolve_material,
}


def resolve_helpers(kind: str, *, material: str = "", process: str = "fdm",
                    **inputs: Any) -> Optional[HelperResult]:
    """Front door: resolve the op-aware helper set for a geometry kind.

    Returns a :class:`HelperResult` with the correct trigger, the helper tools to
    call, and — when enough inputs are supplied — the material/process-gated numbers
    verbatim from the real calculators/profiles plus a provenance citation. Returns
    ``None`` for an unknown kind (the caller falls back to general research tools).

    The gating is FUNCTIONAL: the same ``kind`` with ``process='machined'`` vs
    ``process='printed'`` resolves to different minimums (see resolve_wall / resolve_fit).
    """
    b = binding_for(kind)
    if b is None:
        return None
    fn = _RESOLVERS.get(b.resolver)
    if fn is None:  # pragma: no cover - registry integrity guaranteed by test
        return HelperResult(b.kind, b.trigger_id, list(b.tools), ok=False,
                            note=f"no resolver {b.resolver!r}")
    return fn(material=material, process=process, **inputs)
