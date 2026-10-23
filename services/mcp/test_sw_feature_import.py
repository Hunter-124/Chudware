"""End-to-end test for SolidWorks feature-tree import (services.sw_import.sw_feature_import + sw_translate).

Reconstructs a set of reference SolidWorks parts into the C++ engine and checks the resulting
volume matches what SolidWorks itself reports — proving the feature tree (sketches with relations,
reference planes, extrude/cut/loft) was rebuilt faithfully, not just imported as a frozen body.

Requirements (else the whole module SKIPS): SOLIDWORKS installed + pywin32 (COM extraction) and a
staged ``dist/Chudware/libchudware.dll`` (the engine). Runnable two ways:
    python -m services.mcp.test_sw_feature_import      # script style, prints a table
    pytest services/mcp/test_sw_feature_import.py       # parametrised
The sample parts are not in the repo; set CHUDWARE_SW_SAMPLES to the folder holding them.
When it is unset, or a file is missing, the affected tests skip individually.
"""

from __future__ import annotations

import os

import pytest

from services.sw_import.sw_feature_import import solidworks_available
from services.sw_import.sw_translate import resolve
from services.mcp import core_bridge as cb

_SAMPLES = os.environ.get("CHUDWARE_SW_SAMPLES", "")

# file name -> volume (mm^3) SolidWorks reports for it (ground truth, captured via COM).
_PART_VOLUMES = {
    "25mm-diameter-circle-extruded-100mm.SLDPRT": 49087.3852,
    "50mm-circle-on-top-face-50mm-tall-extrusion-&-25mm-cordial-circular-"
    "extrude-cut-from-top-face-through-all.SLDPRT": 73631.0778,
    "4inx8in-2inx4in-loft-4in-tall.SLDPRT": 1223567.4453,
}
_ASSEMBLY = ("assembly-(cylinder inside cylinder with bore, faces mated at one end "
             "and concentric mate added)..SLDASM")

_skip = pytest.mark.skipif(
    not (solidworks_available() and cb.available()),
    reason="needs SOLIDWORKS (COM) + a staged libchudware.dll")


def _build_volume(path: str):
    plan = resolve(path)
    assert not plan["blocking"], f"unexpected blocking features: {plan['blocking']}"
    d = cb.call("doc_new", {"name": plan["name"]}).get("doc_id")
    try:
        cb.call("doc_apply_ops", {"doc_id": d, "ops": plan["ops"]})
        mass = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": 7.85})
        assert mass.get("body_count", 0) >= 1, f"no body built: {mass}"
        return mass.get("volume_mm3"), plan
    finally:
        try:
            cb.call("doc_close", {"doc_id": d})
        except Exception:  # noqa: BLE001
            pass


@_skip
@pytest.mark.parametrize("fname,expected", list(_PART_VOLUMES.items()))
def test_part_reconstructs_to_solidworks_volume(fname, expected):
    path = os.path.join(_SAMPLES, fname)
    if not os.path.isfile(path):
        pytest.skip(f"sample part not present: {path}")
    vol, _ = _build_volume(path)
    assert vol is not None, "engine reported no volume"
    err = abs(vol - expected) / expected
    assert err < 1e-4, f"{fname}: engine {vol:.4f} vs SolidWorks {expected:.4f} ({err*100:.3f}%)"


def _sw_mass(path):
    """Ground-truth (volume_mm3, com_mm[x,y,z]) from SolidWorks itself, in SW model coords."""
    import pythoncom
    import win32com.client as win32
    from services.sw_import.sw_feature_import import _cast
    pythoncom.CoInitialize()
    app = _cast(win32.Dispatch("SldWorks.Application"), "ISldWorks")
    app.Visible = False
    ret = app.OpenDoc6(path, 1, 1, "", 0, 0)
    model = _cast(ret[0] if isinstance(ret, tuple) else ret, "IModelDoc2")
    try:
        mp = model.Extension.CreateMassProperty()
        c = mp.CenterOfMass
        return float(mp.Volume) * 1e9, [c[0] * 1000, c[1] * 1000, c[2] * 1000]
    finally:
        try:
            app.CloseDoc(model.GetTitle())
        except Exception:  # noqa: BLE001
            pass


def _T(p):
    """SolidWorks model coords -> engine world coords (SW Y-up -> engine Z-up)."""
    return [p[0], -p[2], p[1]]


# Authored fixtures (engine.doc.sw_fixture_author writes them) — one per feature concept. Each is
# validated by comparing the engine's reconstructed volume AND center-of-mass to SolidWorks' own.
_FIXTURE_DIR = os.environ.get(
    "CHUDWARE_SW_FIXTURES", os.path.join(os.path.dirname(__file__), "..", "..", "corpora", "sw_fixtures"))
_FIXTURES = [
    "asym_top.SLDPRT", "asym_front.SLDPRT", "asym_right.SLDPRT", "extrude_front_40x30x20.SLDPRT",
    "revolve_ring.SLDPRT", "fillet_box.SLDPRT", "chamfer_box.SLDPRT", "shell_box.SLDPRT",
    "linpat_holes.SLDPRT", "cirpat_holes.SLDPRT", "mirror_holes.SLDPRT",
]


@_skip
@pytest.mark.parametrize("fname", _FIXTURES)
def test_authored_fixture_matches_solidworks(fname):
    """Each feature concept reconstructs to SolidWorks' exact volume AND center-of-mass."""
    path = os.path.join(_FIXTURE_DIR, fname)
    if not os.path.isfile(path):
        pytest.skip(f"fixture not generated: {path} (run engine.doc.sw_fixture_author)")
    vol, plan = _build_volume(path)
    swv, swc = _sw_mass(path)
    assert abs(vol - swv) / swv < 1e-4, f"{fname}: vol {vol:.3f} vs SW {swv:.3f}"
    # center-of-mass confirms orientation (no flips) — symmetric parts wouldn't catch that on volume.
    d = cb.call("doc_new", {"name": "com"}).get("doc_id")
    try:
        cb.call("doc_apply_ops", {"doc_id": d, "ops": plan["ops"]})
        com = cb.call("eval_mass", {"doc_id": d}).get("com")
    finally:
        try:
            cb.call("doc_close", {"doc_id": d})
        except Exception:  # noqa: BLE001
            pass
    exp = _T(swc)
    assert max(abs(a - b) for a, b in zip(com, exp)) < 0.05, \
        f"{fname}: engine COM {com} vs SW(T) {exp} — orientation/placement off"


@_skip
def test_assembly_reconstructs_and_places_components():
    """An assembly resolves to per-component build plans + 4x4 placements; building each component
    and placing it via asm_insert_component reproduces the SolidWorks component positions."""
    import tempfile
    path = os.path.join(_SAMPLES, _ASSEMBLY)
    if not os.path.isfile(path):
        pytest.skip(f"sample assembly not present: {path}")
    plan = resolve(path)
    assert not plan["blocking"], f"unexpected blocking: {plan['blocking']}"
    assert plan.get("is_assembly") and len(plan["components"]) == 2
    sidecar = tempfile.mkdtemp(prefix="swasm_test_")
    built, total_vol = {}, 0.0
    for c in plan["components"]:
        key = c["part_key"]
        if key not in built:
            cd = cb.call("doc_new", {"name": os.path.basename(key)})["doc_id"]
            cb.call("doc_apply_ops", {"doc_id": cd, "ops": c["ops"]})
            total_vol += cb.call("eval_mass", {"doc_id": cd}).get("volume_mm3", 0.0)
            cpath = os.path.join(sidecar, os.path.splitext(os.path.basename(key))[0] + ".step+")
            cb.call("doc_save", {"doc_id": cd, "path": cpath})
            cb.call("doc_close", {"doc_id": cd})
            built[key] = cpath
    # total component volume = the SolidWorks assembly volume.
    assert abs(total_vol - 122718.463) / 122718.463 < 1e-4, total_vol
    ad = cb.call("doc_new", {"name": "asm", "kind": "assembly"})["doc_id"]
    try:
        cb.call("doc_apply_ops", {"doc_id": ad, "ops": [
            {"op": "asm_insert_component", "name": c["name"],
             "source": {"path": built[c["part_key"]]}, "transform": c["xform16"]}
            for c in plan["components"]]})
        bodies = cb.call("asm_world_bodies", {"doc_id": ad, "explode": False})["bodies"]
        assert len(bodies) == 2
        # both components are placed at the SolidWorks X,Y (the engine T-map of the SW position).
        for b in bodies:
            bb = b["bbox"]
            cx = (bb["min"][0] + bb["max"][0]) / 2
            cy = (bb["min"][1] + bb["max"][1]) / 2
            assert abs(cx - 664.329) < 0.5 and abs(cy - (-1310.564)) < 0.5, (cx, cy)
    finally:
        try:
            cb.call("doc_close", {"doc_id": ad})
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":  # script-style run with a readable table
    if not (solidworks_available() and cb.available()):
        print("SKIP: needs SOLIDWORKS (COM) + staged libchudware.dll")
        raise SystemExit(0)
    print(f"{'part':60} {'engine mm^3':>14} {'SolidWorks':>14}  verdict")
    ok = True
    for fname, expected in _PART_VOLUMES.items():
        path = os.path.join(_SAMPLES, fname)
        if not os.path.isfile(path):
            print(f"{fname[:60]:60} {'(missing)':>14}")
            continue
        vol, plan = _build_volume(path)
        err = abs(vol - expected) / expected
        passed = err < 1e-4
        ok = ok and passed
        print(f"{fname[:60]:60} {vol:14.4f} {expected:14.4f}  "
              f"{'PASS' if passed else 'FAIL'} ({err*100:.3f}%)"
              + (f"  [{len(plan['degraded'])} degraded]" if plan["degraded"] else ""))
    print("ALL PASS" if ok else "FAILURES ABOVE")
