"""StructureDoc → PyNite FE model + member-force/deflection analysis (T-D2).

Consumes the frozen ``aicad.structure/1`` contract (``structural_schema.md``) and PyNite (MIT)
to compute member forces and deflections under ASCE 7 load combinations (:mod:`load_cases`).

Unit system — consistent **N · mm · MPa** throughout (matches the contract: forces in N,
distributed loads N/mm, lengths mm). ``E``/``G`` are therefore N/mm² (MPa), ``I`` mm⁴, ``A``
mm², so deflections come out in mm and moments in N·mm — exactly the §3 ``AnalysisResult`` units.

Node topology — the v1 schema stores each element as a placed extrusion (``transform`` +
``length_mm``), *not* a node graph. Each element becomes one PyNite member from
``transform·(0,0,0)`` to ``transform·(0,0,length_mm)``; endpoints coincident within a micron are
merged into a shared node, so elements that physically touch form a connected frame. Members are
named by ``element.id`` so :func:`run_analysis` results key straight back to the doc.

Sign convention — the contract says ``axial_n`` is **+tension**; PyNite's ``member.axial()`` is
**+compression**, so we negate. See :func:`run_analysis`.

Supports — nodes of a ``footing`` element are fully fixed; if a doc has no footing at all, the
single lowest node is fixed as ground so a lone post/beam is still solvable. (v1 idealisation;
pinned/roller variants and per-load element targeting are noted in ``work/questions/T-D2.md``.)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional, Union

from engine.structural.brep import _NAMED_I_SHAPES, _DEFAULT_SECTION, _dims
from engine.structural.elements import StructuralElement, StructureDoc
from engine.structural.load_cases import LoadCombo, get_combo

# --------------------------------------------------------------------------- materials

# E, G, nu in MPa (N/mm²). Public-domain reference values (NDS Supplement Table 4A for wood;
# AISC / textbook for steel & concrete) — the numbers are facts, not the copyrighted tables.
_MATERIALS: dict[str, tuple[float, float, float]] = {
    #  name          E        G       nu
    "cedar":       (12000.0,  750.0, 0.30),   # task-specified western red cedar
    "df":          (11000.0,  690.0, 0.30),   # Douglas Fir-Larch No.2 (E≈1.6e6 psi)
    "spf":         ( 9500.0,  590.0, 0.30),   # Spruce-Pine-Fir
    "steel":       (200000.0, 77000.0, 0.30),  # A36 / A992 structural steel
    "concrete":    (25000.0,  10400.0, 0.20),  # ~25 MPa normal-weight (Ec≈4700√f'c)
    "aluminum":    (69000.0,  26000.0, 0.33),
}
_DEFAULT_MATERIAL = (9000.0, 560.0, 0.30)  # generic softwood fallback


def material_properties(material: str) -> tuple[float, float, float]:
    """``(E, G, nu)`` in MPa for a material key (``cedar``, ``steel_a36``, ``df_no2`` …).

    Matched by prefix so registry keys like ``steel_a36`` / ``df_no2`` / ``concrete_25mpa`` map to
    the right family. ``nu`` is nominal (frame bending/axial use only E; G carries torsion) — the
    wood ``E``/``G`` pairs are *not* related by the isotropic ``G=E/2(1+nu)`` identity and must not
    be back-derived through it.
    """
    key = (material or "").strip().lower()
    for fam, props in _MATERIALS.items():
        if key == fam or key.startswith(fam):
            return props
    if key.startswith(("douglas", "fir", "lumber", "wood")):
        return _MATERIALS["df"]
    if key.startswith("alu"):
        return _MATERIALS["aluminum"]
    return _DEFAULT_MATERIAL


# --------------------------------------------------------------------------- section geometry


def _rect_props(w: float, h: float) -> tuple[float, float, float, float]:
    """``(A, Iy, Iz, J)`` mm-units for a solid rectangle (width ``w`` along local z, height ``h``
    along local y — matches ``brep._rect_wire`` orientation after +Z extrusion)."""
    A = w * h
    Iz = w * h ** 3 / 12.0          # strong-ish axis: bending in the local-y (height) direction
    Iy = h * w ** 3 / 12.0
    a, b = (h, w) if h >= w else (w, h)   # a = long side, b = short side
    J = a * b ** 3 * (1.0 / 3.0 - 0.21 * (b / a) * (1.0 - b ** 4 / (12.0 * a ** 4)))
    return A, Iy, Iz, J


def _i_props(h: float, b: float, tw: float, tf: float) -> tuple[float, float, float, float]:
    """``(A, Iy, Iz, J)`` for an I-shape (total height ``h``, flange width ``b``, web ``tw``,
    flange thickness ``tf``). ``Iz`` = strong axis (about the horizontal centroidal axis)."""
    hw = h - 2.0 * tf                          # clear web height
    A = 2.0 * b * tf + hw * tw
    Iz = b * h ** 3 / 12.0 - (b - tw) * hw ** 3 / 12.0
    Iy = 2.0 * (tf * b ** 3 / 12.0) + hw * tw ** 3 / 12.0
    J = (2.0 * b * tf ** 3 + (h - tf) * tw ** 3) / 3.0   # open thin-wall approximation
    return A, Iy, Iz, J


def section_properties(section_name: str, material: str = "cedar",
                       *, element_type: Optional[str] = None) -> dict:
    """Full member-stiffness property set for a section spec + material.

    Returns ``{E, G, nu, A, Iy, Iz, J}`` (MPa / mm units). Section geometry is parsed with the same
    grammar as ``brep.section_profile`` (``WxH`` rectangle, ``I h×b×tw×tf`` / named AISC ``W`` shape,
    ``RHS`` → outer rectangle); unparseable names fall back to the element type's default section.
    NOTE: unlike ``brep.element_to_brep`` (T-BL21), this still uses the solid outer rectangle for
    ``RHS`` stiffness/D-C-ratio properties — voiding ``A``/``Iy``/``Iz``/``J`` for hollow sections is
    a separate, not-yet-scoped change to the analysis path.
    """
    E, G, nu = material_properties(material)
    raw = (section_name or "").strip()
    key = raw.upper().replace(" ", "")

    if key in _NAMED_I_SHAPES:
        A, Iy, Iz, J = _i_props(*_NAMED_I_SHAPES[key])
    elif key.startswith("I") and len(_dims(key)) >= 4:
        d = _dims(key)
        A, Iy, Iz, J = _i_props(d[0], d[1], d[2], d[3])
    elif key.startswith("RHS") and len(_dims(key)) >= 2:
        d = _dims(key)
        A, Iy, Iz, J = _rect_props(d[0], d[1])   # solid outer rectangle (see NOTE above)
    else:
        d = _dims(key)
        if len(d) >= 2:
            A, Iy, Iz, J = _rect_props(d[0], d[1])
        elif len(d) == 1:
            A, Iy, Iz, J = _rect_props(d[0], d[0])
        else:
            default = _DEFAULT_SECTION.get(element_type or "", "100x100")
            return section_properties(default, material, element_type=None)
    return {"E": E, "G": G, "nu": nu, "A": A, "Iy": Iy, "Iz": Iz, "J": J}


# --------------------------------------------------------------------------- geometry helpers

_MERGE_DECIMALS = 3   # coincidence tolerance: 1e-3 mm (a micron)


def _apply_transform(transform: list[float], x: float, y: float, z: float) -> tuple[float, float, float]:
    """Apply a row-major 4×4 ``transform`` to point ``(x,y,z)`` (translation in the 4th column)."""
    m = transform
    return (
        m[0] * x + m[1] * y + m[2] * z + m[3],
        m[4] * x + m[5] * y + m[6] * z + m[7],
        m[8] * x + m[9] * y + m[10] * z + m[11],
    )


def element_endpoints(element: StructuralElement) -> tuple[tuple, tuple]:
    """``(start, end)`` global coords: the element's local ``(0,0,0)`` and ``(0,0,length_mm)``
    run through its ``transform`` (identity ⇒ a vertical member up +Z)."""
    t = element.transform
    p0 = _apply_transform(t, 0.0, 0.0, 0.0)
    p1 = _apply_transform(t, 0.0, 0.0, float(element.length_mm))
    return p0, p1


def _node_key(p: tuple) -> tuple:
    return (round(p[0], _MERGE_DECIMALS), round(p[1], _MERGE_DECIMALS), round(p[2], _MERGE_DECIMALS))


# --------------------------------------------------------------------------- model build


def build_pynite_model(structure_doc: StructureDoc):
    """Convert a :class:`StructureDoc` into a PyNite ``FEModel3D`` (nodes, members, materials,
    sections, supports) — **no loads**. Members are named by ``element.id``.

    Returns the ``FEModel3D``. Raises ``ImportError`` if PyNite is unavailable (callers at the tool
    boundary translate that into the ``{code,message,detail}`` error envelope)."""
    from Pynite import FEModel3D

    model = FEModel3D()
    node_names: dict[tuple, str] = {}
    footing_nodes: set[str] = set()
    footing_ids: set[str] = set()
    all_node_coords: dict[str, tuple] = {}
    added_mats: set[str] = set()
    added_secs: set[str] = set()

    def _node(p: tuple) -> str:
        k = _node_key(p)
        name = node_names.get(k)
        if name is None:
            name = f"N{len(node_names) + 1}"
            node_names[k] = name
            all_node_coords[name] = p
            model.add_node(name, p[0], p[1], p[2])
        return name

    for el in structure_doc.elements:
        p0, p1 = element_endpoints(el)
        if _node_key(p0) == _node_key(p1):
            raise ValueError(f"element {el.id} is zero-length (length_mm={el.length_mm}) — no member")
        n0, n1 = _node(p0), _node(p1)

        props = section_properties(el.section_name, el.material, element_type=el.element_type)
        mat_name = f"mat_{el.material or 'default'}"
        sec_name = f"sec_{(el.section_name or 'default')}_{el.element_type}"
        if mat_name not in added_mats:
            model.add_material(mat_name, props["E"], props["G"], props["nu"], 1e-9)
            added_mats.add(mat_name)
        if sec_name not in added_secs:
            model.add_section(sec_name, props["A"], props["Iy"], props["Iz"], props["J"])
            added_secs.add(sec_name)
        model.add_member(el.id, n0, n1, mat_name, sec_name)

        if el.element_type == "footing":
            footing_nodes.update((n0, n1))
            footing_ids.add(el.id)

    # Supports: footing nodes fixed; else the single lowest node fixed as ground.
    if footing_nodes:
        support_nodes = footing_nodes
    elif all_node_coords:
        lowest = min(all_node_coords, key=lambda n: (all_node_coords[n][2],
                                                      all_node_coords[n][1],
                                                      all_node_coords[n][0]))
        support_nodes = {lowest}
    else:
        support_nodes = set()
    for n in support_nodes:
        model.def_support(n, True, True, True, True, True, True)

    model._footing_ids = footing_ids   # consumed by apply_load_cases (skip line loads on footings)
    return model


# --------------------------------------------------------------------------- load application

_AXES = ("X", "Y", "Z")


def _gravity_dir(gravity: Optional[list]) -> tuple[str, float]:
    """Reduce a gravity/load direction vector to (dominant global axis 'FX'|'FY'|'FZ', sign)."""
    g = gravity or [0.0, 0.0, -1.0]
    idx = max(range(3), key=lambda i: abs(g[i]))
    sign = -1.0 if g[idx] < 0 else 1.0
    if g[idx] == 0:
        return "FZ", -1.0
    return f"F{_AXES[idx]}", sign


def apply_load_cases(model, load_cases: list[dict]) -> list[str]:
    """Translate the doc's contract load cases (``structural_schema.md`` §3) onto the model as
    PyNite load *cases* named by ``kind``. Returns the distinct kind names applied.

    v1 mapping (documented in ``work/questions/T-D2.md``): a load case has no element target, so its
    ``distributed``/``point`` load is applied to **every non-footing member** in the direction of
    ``gravity`` (scaled by the case's own ``factor``). ``point.p_n`` at ``point.at_mm`` becomes a
    member point load — for a vertical post that is the axial DoD case; for a horizontal beam it is
    a transverse load.
    """
    line_members = [m for m in model.members]  # all members; footings carry no line load in v1
    footing_ids = getattr(model, "_footing_ids", set())
    line_members = [m for m in line_members if m not in footing_ids]
    kinds: list[str] = []
    for lc in load_cases or []:
        kind = lc.get("kind") or "dead"
        if kind not in kinds:
            kinds.append(kind)
        direction, sign = _gravity_dir(lc.get("gravity"))
        factor = float(lc.get("factor", 1.0))
        dist = lc.get("distributed") or {}
        point = lc.get("point") or {}
        w = dist.get("w_n_per_mm")
        p = point.get("p_n")
        for mname in line_members:
            if w is not None:
                mag = sign * abs(float(w)) * factor
                model.add_member_dist_load(mname, direction, mag, mag, case=kind)
            if p is not None:
                mem = model.members[mname]
                at = point.get("at_mm")
                x = float(at) if at is not None else mem.L()
                mag = sign * abs(float(p)) * factor
                model.add_member_pt_load(mname, direction, mag, x, case=kind)
    return kinds


# --------------------------------------------------------------------------- analysis result


@dataclass
class AnalysisResult:
    """One row of ``structural_schema.md`` §3 — a member's demand under one combo. ``dc_ratio`` /
    ``pass_`` are left ``None`` here; T-D3 (D/C ratio) fills them. ``combo`` is added for provenance
    (JSON key stays ``pass`` via :meth:`to_dict`)."""

    element_id: str
    combo: str
    axial_n: float
    shear_n: float
    moment_nmm: float
    deflection_mm: float
    dc_ratio: Optional[float] = None
    pass_: Optional[bool] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pass"] = d.pop("pass_")   # contract field name is `pass`
        return d


def _max_mag(*vals: float) -> float:
    """The value with the largest absolute magnitude (sign preserved); 0.0 if none finite."""
    best = 0.0
    for v in vals:
        if v is None:
            continue
        v = float(v)
        if abs(v) > abs(best):
            best = v
    return best


def _member_extract(mem, combo: str) -> tuple[float, float, float, float]:
    """(axial_n[+tension], shear_n, moment_nmm, deflection_mm) for one member under one combo."""
    L = mem.L()
    # axial: PyNite is +compression along the member; negate for the contract's +tension.
    axial_pynite = _max_mag(mem.axial(0.0, combo), mem.axial(L, combo))
    axial_n = -axial_pynite

    def _both(getter_max, getter_min, arg):
        vals = []
        f = getattr(mem, getter_max, None)
        if f:
            vals.append(f(arg, combo))
        f = getattr(mem, getter_min, None)
        if f:
            vals.append(f(arg, combo))
        return vals

    shear = _max_mag(*_both("max_shear", "min_shear", "Fy"), *_both("max_shear", "min_shear", "Fz"))
    moment = _max_mag(*_both("max_moment", "min_moment", "Mz"), *_both("max_moment", "min_moment", "My"))
    defl = _max_mag(*_both("max_deflection", "min_deflection", "dy"),
                    *_both("max_deflection", "min_deflection", "dz"))
    return float(axial_n), float(shear), float(moment), float(defl)


def run_analysis(model, load_combos: Union[LoadCombo, list]) -> list[AnalysisResult]:
    """Register ``load_combos`` on the model, run a first-order analysis, and extract member forces
    and deflections for **every member × every combo**.

    ``load_combos`` may be a single :class:`LoadCombo`, a list of them, or combo *names* (resolved
    against :data:`load_cases.PRESETS`). Returns one :class:`AnalysisResult` per member per combo.
    """
    if isinstance(load_combos, (LoadCombo, str)):
        load_combos = [load_combos]
    combos: list[LoadCombo] = [c if isinstance(c, LoadCombo) else get_combo(c) for c in load_combos]

    for c in combos:
        factors = c.factors() or {"dead": 1.0}   # an empty combo still needs one case key
        model.add_load_combo(c.name, factors)

    model.analyze(check_statics=False)

    results: list[AnalysisResult] = []
    for mname in model.members:
        mem = model.members[mname]
        for c in combos:
            axial, shear, moment, defl = _member_extract(mem, c.name)
            results.append(AnalysisResult(
                element_id=mname, combo=c.name,
                axial_n=axial, shear_n=shear, moment_nmm=moment, deflection_mm=defl,
            ))
    return results


# --------------------------------------------------------------------------- high-level entry


def analyze_doc(structure_doc: StructureDoc,
                load_combo: Union[str, LoadCombo, list, None] = None) -> dict:
    """End-to-end: build the model from ``structure_doc``, apply its contract load cases, run the
    requested combo(s), and return a JSON-serialisable ``{"results": [AnalysisResult, ...]}``.

    ``load_combo`` defaults to :data:`load_cases.DEAD_LIVE`. Every value is cast to a plain ``float``
    so the result survives ``json.dumps`` (numpy scalars are not JSON-serialisable)."""
    model = build_pynite_model(structure_doc)
    apply_load_cases(model, structure_doc.load_cases)
    if load_combo is None:
        from engine.structural.load_cases import DEAD_LIVE
        load_combo = DEAD_LIVE
    results = run_analysis(model, load_combo)
    return {"results": [r.to_dict() for r in results]}
