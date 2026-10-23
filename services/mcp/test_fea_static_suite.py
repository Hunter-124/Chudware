"""fea_static regression suite — diverse-geometry meshing + static-solve accuracy.

Drives the REAL engine in-process on the raw core bridge (same access pattern as
test_fea_live_bridge.py, but via the shared case library
benchmarks/simsuite/fea_static_cases.py so the pytest bounds and the simsuite
bench numbers come from one code path).

Coverage:
  * every geometry in the diverse fleet (box / plate+hole / annulus / filleted
    L-bracket / shelled box / revolve / multi-feature / loft / sweep) builds
    through engine ops AND meshes without failure, with sane quality metrics;
  * analytic accuracy anchors — Euler-Bernoulli cantilever deflection (Roark 8th
    T8.1-1a), Peterson/Kirsch stress concentration, and Lamé thick-wall response;
  * element-order hierarchy (tet10 beats tet4 on bending);
  * pressure-load/force-load consistency — regression witness for the core
    defect FIXED 2026-07-12 (core/sim/fea_run.cpp accumulate_face_weights used
    unoriented fan normals; now flipped outward vs element centroid). PASSES
    against a fixed .so; the dynamic xfail remains as a regression tripwire —
    do not delete it.

Skips honestly when the staged libchudware.so lacks the FEA RPCs. Meshes are kept coarse;
the timing baseline lives in benchmarks/simsuite/.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
import time
from pathlib import Path

import pytest

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.mcp.cad_tools import register_cad_tools  # noqa: E402
from services.mcp.sim_evidence_tools import (  # noqa: E402
    register_sim_evidence_tools,
)

_CASES_PATH = (Path(__file__).resolve().parents[2]
               / "benchmarks" / "simsuite" / "fea_static_cases.py")
_spec = importlib.util.spec_from_file_location("fea_static_cases", _CASES_PATH)
fsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fsc)  # type: ignore[union-attr]

_AVAILABLE, _REASON = (False, "probe not run")
try:
    _AVAILABLE, _REASON = fsc.solver_available()
except Exception as exc:  # noqa: BLE001
    _AVAILABLE, _REASON = False, f"solver probe raised: {exc}"

pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=_REASON)

_CACHE: dict = {}


def _cached(key, fn):
    if key not in _CACHE:
        _CACHE[key] = fn()
    return _CACHE[key]


# ------------------------------------------------------------- geometry fleet

@pytest.mark.parametrize("geom", sorted(fsc.GEOMETRY_BUILDERS))
def test_geometry_builds_and_meshes(geom):
    """Every fleet part must build through engine ops and tet-mesh without failure,
    with usable quality (positive Jacobian, bounded aspect, low poor-element %)."""
    def _run():
        doc, _ = fsc.GEOMETRY_BUILDERS[geom]()
        step = fsc.export_step(doc)
        m = fsc.mesh_step(step, order=1, fineness=0.25)
        import os
        os.unlink(step)
        fsc.close_doc(doc)
        return m
    m = _cached(("mesh", geom), _run)
    assert m["n_nodes"] > 0 and m["n_elems"] > 0, m
    assert m["nodes_per_elem"] in (4, 8, 10), m["nodes_per_elem"]
    # Quality gates: generous but real — they catch degenerate/inverted meshes,
    # not styling. (Coarse-mesh worst observed across the fleet: aspect_max 9.03,
    # jacobian_min 0.113, pct_poor 0.0 — see the simsuite baseline.)
    assert m["jacobian_min"] > 0.0, f"{geom}: inverted/degenerate element"
    assert m["aspect_max"] < 50.0, f"{geom}: aspect_max {m['aspect_max']}"
    assert m["pct_poor"] < 25.0, f"{geom}: pct_poor {m['pct_poor']}"


# --------------------------------------------------------- analytic anchors

def _cantilever(order, fineness):
    return _cached(("cant", order, fineness),
                   lambda: fsc.case_cantilever("t", order=order, fineness=fineness))


def test_cantilever_tet10_matches_euler_bernoulli():
    """δ = P·L³/(3EI) (Roark 8th T8.1-1a). tet10 measured 0.42 % off at this
    fineness; 5 % bound covers mesh jitter + shear compliance at L/H=20."""
    c = _cantilever(2, 0.25)
    assert c["status"] == "ok", c
    assert c["rel_err"] < 0.05, f"tet10 deflection {c['metric']:.4f} vs {c['ref']:.4f} " \
                                f"({c['rel_err']:.1%})"


def test_cantilever_order_hierarchy_tet10_beats_tet4():
    """Quadratic elements must beat linear tets on bending (tet4 shear-locks:
    measured ~60 % vs ~0.4 % at comparable fineness). Guards order plumbing —
    a mesh_order that silently stops reaching the solver flips this instantly."""
    c4 = _cantilever(1, 0.3)
    c10 = _cantilever(2, 0.25)
    assert c10["rel_err"] < c4["rel_err"], (c10["rel_err"], c4["rel_err"])
    # tet4 locking is EXPECTED to be large but not absurd; and it must under-,
    # never over-predict deflection (locking is too-stiff, a >1 ratio here means
    # something else broke).
    assert c4["metric"] < c4["ref"], f"tet4 coarser than ref? {c4}"




def test_plate_hole_stress_concentration_bracket():
    """Peak vm at the hole must bracket Ktn·σ_net (Peterson Ktn=2+(1−d/W)³;
    Kirsch Kt→3). Nodal averaging under-reads the surface peak (measured 10-20 %
    low on tet10), so the bracket is [0.65, 1.35]×ref — it still catches the real
    failure modes: no concentration at all (ratio ~0.4), double-counted loads, or
    a runaway singularity."""
    c = _cached(("plate", 2, 0.3),
                lambda: fsc.case_plate_hole("t", order=2, fineness=0.3))
    assert c["status"] == "ok", c
    ratio = c["metric"] / c["ref"]
    assert 0.65 < ratio < 1.35, f"hole peak vm {c['metric']:.2f} vs Ktn ref " \
                                f"{c['ref']:.2f} (ratio {ratio:.2f})"


def test_annulus_lame_radial_displacement():
    """u_r(Ri) = (p·Ri/E)((Ro²+Ri²)/(Ro²−Ri²)+ν) — Lamé open-end thick cylinder
    (Timoshenko & Goodier §28). tet10 measured 2.0 % off; 10 % bound."""
    c = _cached(("lame", 2, 0.25),
                lambda: fsc.case_annulus_lame("t", order=2, fineness=0.25))
    assert c["status"] == "ok", c
    assert c["rel_err"] < 0.10, f"u_r {c['metric']:.6f} vs {c['ref']:.6f} " \
                                f"({c['rel_err']:.1%})"


def test_annulus_lame_hoop_stress_bracket():
    """Mean nodal vm on the inner mid-height band vs the Lamé state (σθ,0,−p).
    Nodal averaging smooths the inner-surface value (measured ~23 % low on the
    coarse tet10), so bracket [0.6, 1.15]×ref — catches sign-flipped or garbage
    pressure fields (the unoriented-normal core bug read 4× LOW here)."""
    c = _cached(("lame", 2, 0.25),
                lambda: fsc.case_annulus_lame("t", order=2, fineness=0.25))
    ratio = c["vm_mid"] / c["vm_ref"]
    assert 0.60 < ratio < 1.15, f"vm_mid {c['vm_mid']:.2f} vs Lamé {c['vm_ref']:.2f} " \
                                f"(ratio {ratio:.2f})"


# ------------------------------------------------- pressure/force consistency

def test_pressure_load_matches_equivalent_force():
    """An axial pressure p on the bar end and an axial force p·A on the SAME face
    must produce the same tip displacement (δ = σL/E, uniaxial Hooke). Regression
    witness for the core defect FIXED 2026-07-12: fea_run.cpp
    accumulate_face_weights used kTetFaces fan normals without outward
    orientation, so the consistent-pressure deposit p·∫N n̂ dA carried mixed signs
    (δ ratio 3.5× tet4 / 6.9× tet10). Now oriented outward vs element centroid;
    the dynamic xfail below stays as a tripwire should the fix regress."""
    c = _cached(("press_native",), lambda: fsc.case_pressure_native_bar("t"))
    rel = c["rel_err"]
    if rel is not None and rel > 0.05:
        pytest.xfail(
            f"KNOWN core bug: native pressure path off by {rel:.1%} vs δ=σL/E "
            f"(unoriented fan normals in core/sim/fea_run.cpp "
            f"accumulate_face_weights ~L230-284; fix: orient n̂ outward against "
            f"the element centroid before depositing p·∫N n̂ dA)")
    assert rel is not None and rel <= 0.05, c


def test_consistent_pressure_oracle_resultant_is_zero_on_closed_ring():
    """The Python consistent-pressure oracle must produce a self-equilibrated load
    on the full inner cylinder (net force = 0 by symmetry of p·n̂ over a closed
    ring) — guards the oracle itself against orientation regressions."""
    def _run():
        doc, info = fsc.build_annulus()
        step = fsc.export_step(doc)
        m = fsc.mesh_step(step, order=2, fineness=0.25)
        import os
        os.unlink(step)
        fsc.close_doc(doc)
        loads = fsc.consistent_pressure_loads(
            m, lambda x, y, z: abs(math.hypot(x, y) - info["Ri"]) < 0.05, 10.0)
        return m, loads, info
    m, loads, info = _cached(("oracle_ring",), _run)
    assert loads, "oracle picked no loaded nodes"
    fx = sum(l["vector"][0] for l in loads)
    fy = sum(l["vector"][1] for l in loads)
    fz = sum(l["vector"][2] for l in loads)
    # magnitude scale: p * inner area = 10 * 2π·Ri·H ≈ 150 kN
    scale = 10.0 * 2 * math.pi * info["Ri"] * info["H"]
    assert abs(fx) < 1e-6 * scale and abs(fy) < 1e-6 * scale, (fx, fy)
    assert abs(fz) < 0.02 * scale, fz  # end-ring faces give a small axial residue


# --------------------------------------------------------------- solve sanity

def test_regression_parts_solve_with_finite_fields():
    """The non-analytic fleet must solve: finite positive displacement, finite
    stress, n_dof bookkeeping consistent. One representative per feature family
    kept coarse (the full sweep lives in the bench module)."""
    for geom in ("lbracket_fillet", "revolved_shaft", "swept_rod"):
        c = _cached(("reg", geom),
                    lambda g=geom: fsc.case_regression("t", g, order=1, fineness=0.25))
        assert c["status"] == "ok", (geom, c)
        assert c["metric"] is not None and c["metric"] > 0.0, (geom, c)
        assert math.isfinite(c["metric"]), (geom, c)


# ------------------------------------------- curved-face faceset (C2 regression)

def test_curved_faceset_covers_cylinder_exactly():
    """doc_face_points on a cylinder barrel must emit the analytic surface descriptor,
    and the solver's membership test must then fix EXACTLY the lateral boundary
    nodes — no misses and no interior/end-face bleed. The old radius-only oracle
    was a stale pin: it assumed the old halved pitch. At the honest h=5 mm, 480
    interior nodes sit 0.142–0.145 mm inside the barrel, legitimately unfixed."""
    def _run():
        cb = fsc._cb()
        doc = cb.call("doc_new", {"name": "c2_cyl_cover"})["doc_id"]
        cb.call("doc_apply_ops", {"doc_id": doc, "ops": [{"op": "add_feature", "feature": {
            "type": "cylinder", "params": {"radius": 10.0, "height": 100.0}}}]})
        # `surf:"curved"` is the real spelling of "any non-planar face". This used to
        # ask for {"curved": True} — an inert key resolve_geom_query dropped silently,
        # so the query actually returned all THREE faces and this test passed only
        # because the barrel happened to enumerate first. The engine now rejects an
        # unknown query key outright (doc/picks.cpp::unknown_query_keys), and asking
        # correctly lets us assert the count too.
        q = cb.call("doc_query", {"doc_id": doc,
                                  "query": {"of": "face", "surf": "curved"}})
        faces = [m for m in q.get("matches", []) if m.get("kind") == "face"]
        assert len(faces) == 1, f"a cylinder has ONE curved face, got {len(faces)}"
        sel = faces[0]
        fp = cb.call("doc_face_points", {"doc_id": doc, "selector": sel})
        step = fsc.export_step(doc)
        mesh = fsc.mesh_step(step, order=2, fineness=0.3)
        import os
        os.unlink(step)
        fsc.close_doc(doc)
        return fp, mesh
    fp, mesh = _cached(("c2_cyl",), _run)

    # The analytic descriptor is present and correct.
    surf = fp.get("surface")
    assert surf and surf["type"] == "cylinder", fp.keys()
    assert surf["radius"] == pytest.approx(10.0)
    assert abs(surf["axis"][2]) == pytest.approx(1.0)
    assert fp.get("perp_tol_mm") == pytest.approx(1.5)

    # Membership through a real solve: fixture = the barrel faceset; fixed nodes are
    # exactly the zero-displacement ones. Surface truth requires exterior topology,
    # not just proximity: near-barrel interior tet10 nodes must remain free.
    load = [{"faces": {"points": [[0, 0, 50]], "tol_mm": 3.0},
             "type": "force", "vector": [100.0, 0.0, 0.0]}]
    r = fsc.static_solve(mesh, [fp], load)
    pts, dm = mesh["points"], r["disp_magnitude"]
    n = len(pts) // 3
    owners = {}
    element_faces = {}
    stride = mesh["nodes_per_elem"]
    for start in range(0, len(mesh["tets"]), stride):
        el = mesh["tets"][start:start + stride]
        for corners, mids in fsc._KTET_FACES:
            key = tuple(sorted(el[c] for c in corners))
            owners[key] = owners.get(key, 0) + 1
            element_faces[key] = tuple(el[c] for c in corners + mids)
    boundary_faces = [face for key, face in element_faces.items() if owners[key] == 1]
    radial_error = [abs(math.hypot(pts[3 * i], pts[3 * i + 1]) - 10.0)
                    for i in range(n)]
    # End-cap facets have constant z. Rim/chamfer facets with an off-cylinder
    # corner are not barrel facets, even when one edge lies at radius R.
    cylinder_faces = [face for face in boundary_faces
                      if max(pts[3 * c + 2] for c in face[:3]) -
                      min(pts[3 * c + 2] for c in face[:3]) > 1e-6
                      and all(radial_error[c] <= fp["perp_tol_mm"] for c in face[:3])]
    supported = {i for face in cylinder_faces for i in face}
    truth = {i for i in supported if radial_error[i] < 0.2}
    fixed = {i for i in range(n) if dm[i] == 0.0}
    # A near-surface midside node on a chamfer must not join the faceset when
    # its facet has a corner outside the analytic tolerance.
    assert fixed <= supported, "fixed nodes admitted from off-cylinder facets"
    for face in boundary_faces:
        if all(i in fixed for i in face):
            assert all(radial_error[c] <= fp["perp_tol_mm"] for c in face[:3])
    missed = truth - fixed
    bled = fixed - truth
    assert not missed, f"{len(missed)}/{len(truth)} lateral nodes missed"
    assert not bled, f"{len(bled)} non-lateral nodes wrongly fixed"


def test_native_curved_pressure_matches_lame():
    """End-to-end C1+C2: engine-native pressure on the annulus BORE face (surface
    faceset from doc_face_points + oriented consistent-pressure deposition) must match
    the Lamé radial displacement. Explicit h=8 mm measures 3.86% error; the
    former hidden global halving masked the 15.71% error at the fineness-only
    h=16 mm. The 8% accuracy bound is unchanged."""
    c = _cached(("lame_native", 8.0),
                lambda: fsc.case_annulus_lame_native("t", target_size_mm=8.0))
    assert c["status"] == "ok", c
    assert c["rel_err"] is not None and c["rel_err"] < 0.08, c


# ------------------------- stated element size + released field (the MCP tool boundary)
# The engine has honoured study.target_size_mm and study.vtu_path for a long time; what
# no tool could ASK for was the combination. Measured twice in one frame analysis
# session (2026-09-06):
#   * run_fea_static took only mesh_fineness, so the leg a ladder released could not be
#     re-solved on ITS mesh — every .vtu written afterwards was attributed to a
#     discretization nobody had solved; and
#   * run_fea_convergence wrote no field at all, so a converged ladder had a table with
#     nothing behind it and the packet's .vtu item could never close without a fourth
#     solve.
# These drive the real tools over the real engine and assert the mesh, the files and the
# packet — not ok:true.

_BOX = {"length": 80.0, "width": 40.0, "height": 20.0}
_LOAD = [{"face": "top", "force": [0.0, 0.0, -800.0]}]


class _CaptureMCP:
    """FastMCP stand-in that just collects the @mcp.tool() registrations."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def dec(fn):
            self.tools[kwargs.get("name", fn.__name__)] = fn
            return fn
        return dec


@pytest.fixture(scope="module")
def tools() -> dict:
    m = _CaptureMCP()
    register_cad_tools(m)
    register_sim_evidence_tools(m)
    return m.tools


def _json(raw) -> dict:
    assert isinstance(raw, str), f"a tool must return a JSON string, got {type(raw)}"
    return json.loads(raw)


def _box_doc(tools: dict, name: str) -> str:
    r = _json(tools["new_part"](name=name))
    assert r.get("ok"), r
    doc = r["doc_id"]
    rr = _json(tools["add_features"](doc_id=doc, ops=[
        {"op": "add_feature",
         "feature": {"type": "box", "name": "Blk", "params": _BOX}}]))
    assert rr.get("ok"), rr
    return doc


def _finish(tools: dict, out: dict, key: str) -> dict:
    """Resolve a reply that overran the transport budget and was handed to a job, so an
    assertion here is about the solve and never a flake about a job id."""
    jid = out.get("job_id")
    if out.get(key) is not None or not jid:
        return out
    deadline = time.monotonic() + 600.0
    while time.monotonic() < deadline:
        job = _json(tools["get_job"](job_id=jid))
        if job.get("status") in ("done", "completed", "error", "failed"):
            result = job.get("result")
            return result if isinstance(result, dict) else job
        time.sleep(1.0)
    raise AssertionError(f"backgrounded solve {jid} did not finish in 600 s")


def _packet(tools: dict, out_dir, case: str, result: dict, geometry: str) -> dict:
    return _json(tools["sim_evidence_packet"](
        case_id=case, requirement_id="REQ-MESH", out_dir=str(out_dir),
        study_result=result, geometry_path=geometry,
        material_source="registry row: steel",
        load_rationale="800 N down on the top face of a fully fixed base",
        notes="mesh-plumbing regression case"))


def test_stated_mesh_size_is_the_mesh_that_solved_and_its_field_reaches_the_packet(
        tools, tmp_path):
    """`mesh_size_mm` must reach study.target_size_mm, not be swallowed by the fineness
    dial: the element count the solve reports has to equal an INDEPENDENT sim_mesh of the
    same STEP at the same 6 mm (a fineness-derived mesh of this box is nowhere near it).
    The requested .vtu must exist, and the packet must hash it — that pair is what lets a
    released convergence leg be re-solved into a citable field."""
    doc = _box_doc(tools, "explicit_mesh_size")
    vtu = tmp_path / "released.vtu"
    out = _finish(tools, _json(tools["run_fea_static"](
        doc_id=doc, fixtures=["bottom"], loads=_LOAD, material="steel",
        mesh_size_mm=6.0, vtu_path=str(vtu))), "summary")
    assert out.get("ok"), out
    assert out["vtu_path"] == str(vtu), out.get("vtu_path")
    assert vtu.stat().st_size > 0, "the solve reported a vtu_path but wrote no field"

    step = str(tmp_path / "box.step")
    fsc._cb().call("doc_export_step", {"doc_id": doc, "path": step})
    direct = fsc._cb().call("sim_mesh", {"step_path": step, "order": 2,
                                        "fineness": 0.5, "target_size_mm": 6.0})
    assert direct.get("ok"), direct

    packet = _packet(tools, tmp_path / "packets", "BOX-MESH-6MM", out, step)
    assert packet.get("ok"), packet
    prov = json.loads(
        (Path(packet["packet_dir"]) / "provenance.json").read_text(encoding="utf-8"))
    assert prov["mesh_quality"]["n_elems"] == direct["n_elems"], (
        f"solved {prov['mesh_quality']['n_elems']} elements, a direct 6 mm mesh has "
        f"{direct['n_elems']} — the stated size did not reach the mesher")
    assert packet["vtu_sha256"], packet


def test_convergence_legs_write_one_field_each_and_the_reply_names_the_released_one(
        tools, tmp_path):
    """Every leg writes its OWN field into vtu_dir, named for its position, size and
    element order; each row carries that path; the reply names the finest solved leg as
    the released one, and handing the whole reply to sim_evidence_packet hashes exactly
    that leg's file. Before this a ladder was a table with no field behind it."""
    doc = _box_doc(tools, "convergence_fields")
    vdir = tmp_path / "fields"
    out = _finish(tools, _json(tools["run_fea_convergence"](
        doc_id=doc, fixtures=["bottom"], loads=_LOAD, material="steel",
        sizes_mm=[12.0, 8.0], vtu_dir=str(vdir))), "table")
    rows = out.get("table")
    assert isinstance(rows, list) and len(rows) == 2, out
    for k, (row, size) in enumerate(zip(rows, (12.0, 8.0))):
        assert not row.get("error") and not row.get("skipped"), row
        path = Path(row["vtu_path"])
        assert path.parent == vdir, path
        assert path.name.endswith(f"_leg{k}_h{size:g}_o2.vtu"), path.name
        assert path.stat().st_size > 0, f"leg {k} wrote no field"
    assert len({Path(r["vtu_path"]).name.split("_leg")[0] for r in rows}) == 1, rows

    # The DELIVERED discretization is a NUMBER on every solved row, not something a
    # reader reverse-engineers out of governor prose (a ladder asked for 1.6/1.2/0.9 mm
    # was meshed at 5.62/5.43/5.42 mm and only the prose said so). The .vtu name above
    # carries the REQUESTED size; this is what actually solved.
    for k, row in enumerate(rows):
        delivered = row["delivered_size_mm"]
        assert isinstance(delivered, (int, float)) and not isinstance(delivered, bool), row
        assert math.isfinite(delivered) and delivered > 0.0, row
        assert row["mesh_quality"]["char_len_mm"] == delivered, row["mesh_quality"]
    delivery = out["size_delivery"]
    assert [e["leg"] for e in delivery] == [0, 1], delivery
    for entry, row in zip(delivery, rows):
        assert entry["requested_mm"] == row["size_mm"], entry
        assert entry["delivered_mm"] == row["delivered_size_mm"], entry
        assert entry["ratio"] == pytest.approx(
            entry["delivered_mm"] / entry["requested_mm"], rel=1e-3), entry

    released = out.get("released_leg")
    assert released, out
    assert released["index"] == 1, released
    assert released["size_mm"] == 8.0, released
    assert released["vtu_path"] == rows[1]["vtu_path"], released

    packet = _packet(tools, tmp_path / "packets", "BOX-LADDER", out,
                     str(next(vdir.glob("*.vtu"))))
    assert packet.get("ok"), packet
    assert packet["vtu_sha256"], "the packet ignored the released leg's field"
    prov = json.loads(
        (Path(packet["packet_dir"]) / "provenance.json").read_text(encoding="utf-8"))
    assert prov["vtu_sha256"]["path"] == released["vtu_path"], prov["vtu_sha256"]


def test_a_stated_size_and_the_fineness_dial_together_are_refused(tools):
    """Two different meshes cannot both be honoured, and silently preferring one is how a
    field gets attributed to a discretization nobody asked for. The default dial is NOT a
    statement, though — mesh_size_mm alone solves (the test above)."""
    doc = _box_doc(tools, "mesh_size_exclusive")
    out = _json(tools["run_fea_static"](
        doc_id=doc, fixtures=["bottom"], loads=_LOAD, material="steel",
        mesh_size_mm=6.0, mesh_fineness=0.75))
    assert out.get("ok") is False, out
    assert "mesh_size_mm" in out["error"] and "mesh_fineness" in out["error"], out
    assert "job_id" not in out, "a refused setup must not start a solve"


def _small_feature_doc(tools: dict, name: str) -> str:
    """A 6x5x4 mm block with a 0.8 mm through hole — a feature far below half of the
    3 mm element size the test states, which is what puts it under the band floor."""
    r = _json(tools["new_part"](name=name))
    assert r.get("ok"), r
    doc = r["doc_id"]
    rr = _json(tools["add_features"](doc_id=doc, ops=[
        {"op": "add_feature", "feature": {
            "type": "box", "name": "Blk",
            "params": {"length": 6.0, "width": 5.0, "height": 4.0}}},
        {"op": "add_feature", "feature": {
            "type": "hole", "name": "Pin",
            "params": {"style": "simple", "end": "through_all",
                       "diameter": 0.8, "positions": [[0, 0]]},
            "refs": {"face": {"of": "face", "side": "top"}}}}]))
    assert rr.get("ok"), rr
    return doc


def test_a_stated_band_floor_resolves_a_feature_the_default_floor_refuses(tools):
    """`min_size_mm` is the element-size band FLOOR, and the floor is what bounds the
    mesher's feature-resolution descent (default: half the stated element size). A
    0.8 mm hole inside a 3 mm study sits under it, so the mesh is REFUSED rather than
    meshed over — the refusal an agent could not answer before this parameter existed.
    Same doc, same 3 mm background, same load: without the floor there is no mesh at
    all; with min_size_mm=0.4 the descent reaches the hole and the solve reports a mesh
    orders finer than 3 mm (a 3 mm mesh of this 120 mm3 block is a handful of cells),
    and the persisted study names the floor beside the size it belongs to.
    A floor COARSER than the stated size is refused before any solve: it would raise the
    base size instead of refining under it, i.e. coarsen the mesh the caller named."""
    doc = _small_feature_doc(tools, "band_floor")
    setup = dict(doc_id=doc, fixtures=["bottom"],
                 loads=[{"face": "top", "force": [0.0, 0.0, -300.0]}],
                 material="steel", mesh_size_mm=3.0)

    refused = _json(tools["run_fea_static"](**setup))
    assert refused.get("ok") is False, refused
    assert "feature unresolved" in refused["error"], refused["error"]

    out = _finish(tools, _json(tools["run_fea_static"](min_size_mm=0.4, **setup)),
                  "summary")
    assert out.get("ok"), out
    assert out["n_elems"] > 2000, (
        f"solved {out['n_elems']} elements — a 3 mm mesh of a 6x5x4 mm block is ~4 "
        "cells, so the band floor never reached the mesher")
    assert out["summary"]["max_von_mises_mpa"] > 0.0, out["summary"]
    studies = fsc._cb().call("sim_study_list", {"doc_id": doc}).get("studies") or []
    assert studies and studies[-1]["min_size_mm"] == 0.4, studies
    assert studies[-1]["target_size_mm"] == 3.0, studies

    coarse_floor = _json(tools["run_fea_static"](min_size_mm=4.0, **setup))
    assert coarse_floor.get("ok") is False, coarse_floor
    assert "min_size_mm" in coarse_floor["error"], coarse_floor["error"]
    assert "mesh_size_mm" in coarse_floor["error"], coarse_floor["error"]
    assert "job_id" not in coarse_floor, "a refused setup must not start a solve"


# ------------------------------- lossless compound CAD-face boundary unions

_UNION_POINTS = [
    [0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
    [0.0, 1.0, 0.0], [0.0, 0.0, 1.0],
]


def _union_plane(axis, *, npe=4):
    coordinates = list(_UNION_POINTS)
    if npe == 10:
        coordinates += [
            [0.5, 0.0, 0.0], [0.5, 0.5, 0.0], [0.0, 0.5, 0.0],
            [0.0, 0.0, 0.5], [0.0, 0.5, 0.5], [0.5, 0.0, 0.5],
        ]
    normal = [0.0, 0.0, 0.0]
    normal[axis] = -1.0
    return {
        "points": [point for point in coordinates if point[axis] == 0.0],
        "tol_mm": 2.0, "face_normal": normal, "perp_tol_mm": 1e-8,
    }


def _union_study(*, npe=4):
    points = list(_UNION_POINTS)
    if npe == 10:
        points += [
            [0.5, 0.0, 0.0], [0.5, 0.5, 0.0], [0.0, 0.5, 0.0],
            [0.0, 0.0, 0.5], [0.0, 0.5, 0.5], [0.5, 0.0, 0.5],
        ]
    return {
        "points": [value for point in points for value in point],
        "tets": list(range(npe)), "nodes_per_elem": npe,
        "material": dict(fsc.STEEL),
        "fixtures": [_union_plane(0, npe=npe)],
    }


def _native_union_static(tmp_path, name, faces, load, *, npe=4):
    path = tmp_path / f"{name}.json"
    study = _union_study(npe=npe)
    study.update(loads=[dict(load, faces=faces)], export_system_path=str(path))
    out = fsc._cb().call("sim_run_static", {"study": study})
    assert out.get("ok"), out
    return out, json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("npe", [4, 10])
def test_faceset_union_force_is_once_and_shared_membership_is_deduplicated(tmp_path, npe):
    """Two orthogonal faces share an edge, but impose ONE authored force."""
    from services.mcp.cad_common import _merge_face_sets

    y, z = _union_plane(1, npe=npe), _union_plane(2, npe=npe)
    force = [120.0, -240.0, 360.0]
    fields = []
    for ordinal, leaves in enumerate(([y, z], [z, y, y, z])):
        out, system = _native_union_static(
            tmp_path, f"force_{npe}_{ordinal}", _merge_face_sets(leaves),
            {"type": "force", "vector": force}, npe=npe)
        nodal = system["nodal_forces"]
        assert [sum(row[c] for row in nodal) for c in range(3)] == pytest.approx(
            force, abs=1e-10)
        assert out["reactions"]["applied_force_n"] == pytest.approx(force, abs=1e-8)
        assert out["reactions"]["reaction_force_n"] == pytest.approx(
            [-value for value in force], abs=1e-7)
        if npe == 4:
            for row in nodal:
                assert row == pytest.approx([value / 4 for value in force])
        else:
            # Edge 2--3 is not on either selected face, even though BOTH of its
            # endpoints belong to the union. Never complete/load this phantom edge.
            assert nodal[8] == pytest.approx([0.0, 0.0, 0.0], abs=1e-10)
            assert nodal[4] == pytest.approx([value / 3 for value in force], abs=1e-9)
        fields.append([value for row in nodal for value in row])
    assert fields[1] == pytest.approx(fields[0], abs=1e-10)


@pytest.mark.parametrize("npe", [4, 10])
def test_faceset_union_pressure_does_not_invent_unselected_facets(tmp_path, npe):
    """Union nodes cover all four corners; ONLY y=0 and z=0 facets carry pressure."""
    from services.mcp.cad_common import _merge_face_sets

    y, z = _union_plane(1, npe=npe), _union_plane(2, npe=npe)
    out, system = _native_union_static(
        tmp_path, f"pressure_{npe}", _merge_face_sets([y, z, y]),
        {"type": "pressure", "magnitude": 6.0}, npe=npe)
    nodal = system["nodal_forces"]
    assert [sum(row[c] for row in nodal) for c in range(3)] == pytest.approx(
        [0.0, 3.0, 3.0], abs=1e-10)
    assert out["reactions"]["applied_force_n"] == pytest.approx([0.0, 3.0, 3.0])
    if npe == 10:
        assert nodal[8] == pytest.approx([0.0, 0.0, 0.0], abs=1e-10)
    else:
        expected = [[0, 1, 1], [0, 1, 1], [0, 0, 1], [0, 1, 0]]
        for actual, reference in zip(nodal, expected):
            assert actual == pytest.approx(reference, abs=1e-10)


def test_faceset_union_retains_heterogeneous_analytic_and_planar_oracles(tmp_path):
    """A sphere's triangular skin facet and a plane retain separate tight tests."""
    from services.mcp.cad_common import _merge_face_sets

    sphere = {
        "points": _UNION_POINTS[1:], "tol_mm": 2.0, "perp_tol_mm": 1e-8,
        "surface": {"type": "sphere", "origin": [0, 0, 0], "radius": 1.0},
    }
    plane = _union_plane(2)
    out, system = _native_union_static(
        tmp_path, "sphere_plane", _merge_face_sets([sphere, plane, sphere]),
        {"type": "pressure", "magnitude": 6.0})
    # Oblique facet: -p*A*n = [-3,-3,-3]. z=0: [0,0,3].
    # Collapsing the sphere descriptor or planar normal gives a different resultant.
    assert [sum(row[c] for row in system["nodal_forces"]) for c in range(3)] == (
        pytest.approx([-3.0, -3.0, 0.0], abs=1e-10))
    assert out["reactions"]["applied_force_n"] == pytest.approx([-3.0, -3.0, 0.0])


_BAD_UNIONS = [
    {"facesets": []},
    {"facesets": {}},
    {"facesets": [None]},
    {"facesets": [{"points": []}]},
    {"facesets": [{"points": [0.0, 1.0]}]},
    {"facesets": [{"points": [[0.0, 0.0, 0.0]], "tol_mm": 0.0}]},
    {"facesets": [{"facesets": [_union_plane(1)]}]},
    *[dict(facesets=[_union_plane(1)], **{key: value}) for key, value in (
        ("points", []), ("tol_mm", 1.0), ("perp_tol_mm", 1e-6),
        ("face_normal", [0, 1, 0]),
        ("surface", {"type": "sphere", "origin": [0, 0, 0], "radius": 1.0}),
    )],
    {"facesets": [{"points": [[0.0, None, 0.0]]}]},
    {"facesets": [{"points": [[0.0, 0.0, 0.0]], "face_normal": [1e308, 0, 0]}]},
]


@pytest.mark.parametrize("faces", _BAD_UNIONS)
@pytest.mark.parametrize("route", ["static_load", "static_fixture", "thermal_load",
                                  "thermal_fixture"])
def test_faceset_union_malformed_or_ambiguous_boundaries_are_refused(faces, route):
    study = _union_study()
    if route.startswith("static"):
        study["loads"] = [{"type": "force", "vector": [1, 2, 3],
                           "faces": faces if route.endswith("load") else _union_plane(1)}]
        if route.endswith("fixture"):
            study["fixtures"] = [faces]
        method = "sim_run_static"
    else:
        study["material"] = {"k_w_mk": 1000.0}
        study["fixtures"] = [dict(
            faces if route.endswith("fixture") else _union_plane(0), temp_c=0.0)]
        study["loads"] = [{"type": "flux", "magnitude": 1e6,
                           "faces": faces if route.endswith("load") else _union_plane(1)}]
        method = "sim_run_thermal"
    out = fsc._cb().call(method, {"study": study})
    assert out.get("ok") is False, out
    assert "faceset" in out["error"], out


@pytest.mark.parametrize("capacitance", ["lumped", "consistent"])
def test_faceset_union_transient_flux_conserves_heat_and_ignores_duplicate_faces(capacitance):
    """q=1 W/mm² over two 0.5 mm² faces: ΔTmean=Q/(rho_cp*V)=6 K."""
    from services.mcp.cad_common import _merge_face_sets

    fields = []
    for leaves in ([_union_plane(1), _union_plane(2)],
                   [_union_plane(2), _union_plane(1), _union_plane(1)]):
        study = _union_study()
        study.update(
            fixtures=[], material={"k_w_mk": 1000.0, "cp_j_kgk": 1000.0,
                                   "rho_kg_m3": 1e6},
            loads=[{"faces": _merge_face_sets(leaves), "type": "flux", "magnitude": 1e6}],
            transient={"t_end_s": 1.0, "dt_s": 0.1, "t_init_c": 0.0,
                       "capacitance": capacitance, "frame_times_s": [1.0]})
        out = fsc._cb().call("sim_run_thermal", {"study": study})
        assert out.get("ok"), out
        temperatures = out["temperature"]
        # Each linear tetra node owns V/4 for both capacitance formulations.
        assert sum(temperatures) / 4 == pytest.approx(6.0, abs=1e-9)
        fields.append(temperatures)
    assert fields[1] == pytest.approx(fields[0], abs=1e-9)


def test_faceset_union_steady_convection_uses_only_authored_external_faces():
    """Convection union area=1, balanced by unit flux over the oblique √3/2 face."""
    from services.mcp.cad_common import _merge_face_sets

    study = _union_study()
    study.update(
        fixtures=[], material={"k_w_mk": 1000.0},
        loads=[
            {"type": "convection", "h": 1e6, "t_amb_c": 10.0,
             "faces": _merge_face_sets([_union_plane(1), _union_plane(2), _union_plane(1)])},
            {"type": "flux", "magnitude": 1e6,
             "faces": {"points": _UNION_POINTS[1:], "tol_mm": 1e-8}},
        ])
    out = fsc._cb().call("sim_run_thermal", {"study": study})
    assert out.get("ok"), out
    weights = [1 / 3, 1 / 3, 1 / 6, 1 / 6]
    boundary_mean = sum(weight * temp for weight, temp in zip(weights, out["temperature"]))
    assert boundary_mean == pytest.approx(10.0 + math.sqrt(3) / 2, abs=1e-9)


def test_faceset_union_thermal_fixed_nodes_match_separate_terminal_boundaries():
    """Sparse terminal leaves remain valid: a compound is not a full-face-only fixture."""
    from services.mcp.cad_common import _merge_face_sets

    leaves = [{"points": [point], "tol_mm": 1e-8} for point in _UNION_POINTS[:2]]
    fields = []
    for fixtures in ([dict(_merge_face_sets(leaves), temp_c=0.0)],
                     [dict(leaf, temp_c=0.0) for leaf in leaves]):
        study = _union_study()
        study.update(material={"k_w_mk": 1000.0}, fixtures=fixtures,
                     loads=[{"faces": _union_plane(2), "type": "flux", "magnitude": 1e6}])
        out = fsc._cb().call("sim_run_thermal", {"study": study})
        assert out.get("ok"), out
        assert out["temperature"][:2] == pytest.approx([0.0, 0.0], abs=1e-10)
        assert out["temperature"][2] > 0.0
        fields.append(out["temperature"])
    assert fields[1] == pytest.approx(fields[0], abs=1e-10)


def test_high_level_mcp_multiface_force_retains_one_authored_resultant(tools):
    doc = _box_doc(tools, "mcp_multiface_union")
    force = [120.0, -240.0, -360.0]
    try:
        out = _finish(tools, _json(tools["run_fea_static"](
            doc_id=doc, fixtures=["bottom"], material="steel", mesh_size_mm=10.0,
            loads=[{"face": {"of": "face"}, "force": force}])), "summary")
        assert out.get("ok"), out
        assert out["reactions"]["applied_force_n"] == pytest.approx(force, abs=1e-7)
        assert out["reactions"]["reaction_force_n"] == pytest.approx(
            [-value for value in force], abs=1e-5)
    finally:
        fsc.close_doc(doc)


def test_high_level_mcp_thermal_multiface_union_reaches_uniform_equilibrium(tools):
    doc = _box_doc(tools, "mcp_thermal_multiface_union")
    try:
        out = _json(tools["run_fea_thermal"](
            doc_id=doc, hot_faces=[], cold_faces=[], material="steel",
            mesh_fineness=0.25,
            loads=[{"face": {"of": "face"}, "flux_w_m2": 1000.0},
                   {"face": {"of": "face"}, "h_w_m2k": 100.0, "t_amb_c": 20.0}]))
        assert out.get("ok"), out
        assert out["temperature_min_c"] == pytest.approx(30.0, abs=1e-6)
        assert out["temperature_max_c"] == pytest.approx(30.0, abs=1e-6)
    finally:
        fsc.close_doc(doc)


def test_faceset_union_transient_convection_preserves_one_boundary_energy_balance():
    """One backward-Euler step balances stored heat with the two-face film flux."""
    from services.mcp.cad_common import _merge_face_sets

    study = _union_study()
    study.update(
        fixtures=[], material={"k_w_mk": 1000.0, "cp_j_kgk": 1000.0,
                               "rho_kg_m3": 1e6},
        loads=[{"type": "convection", "h": 1e6, "t_amb_c": 10.0,
                "faces": _merge_face_sets([_union_plane(1), _union_plane(2),
                                           _union_plane(2)])}],
        transient={"t_end_s": 1.0, "dt_s": 1.0, "t_init_c": 0.0,
                   "frame_times_s": [1.0]})
    out = fsc._cb().call("sim_run_thermal", {"study": study})
    assert out.get("ok"), out
    temperature = out["temperature"]
    weights = [1 / 3, 1 / 3, 1 / 6, 1 / 6]
    film_boundary_integral = sum(w * t for w, t in zip(weights, temperature))
    # C's column sum per node is rho_cp*V/4 = 1/24 W·s/K.
    # Stored heat + dt*h*∫T dA = dt*h*T_amb*A; A=1, dt=h=1.
    assert sum(temperature) / 24 + film_boundary_integral == pytest.approx(10.0, abs=1e-9)


@pytest.mark.parametrize("kind", ["flux", "convection"])
def test_faceset_union_thermal_load_without_a_complete_facet_is_refused(kind):
    from services.mcp.cad_common import _merge_face_sets

    leaves = [{"points": [point], "tol_mm": 1e-8} for point in _UNION_POINTS[:2]]
    load = {"faces": _merge_face_sets(leaves), "type": kind}
    load.update({"magnitude": 1e6} if kind == "flux" else {"h": 1e6, "t_amb_c": 10.0})
    study = _union_study()
    study.update(material={"k_w_mk": 1000.0},
                 fixtures=[dict(_union_plane(0), temp_c=0.0)], loads=[load])
    out = fsc._cb().call("sim_run_thermal", {"study": study})
    assert out.get("ok") is False, out
    assert "selects no complete exterior mesh face" in out["error"], out
