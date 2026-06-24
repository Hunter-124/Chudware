"""Live check of the rebuilt FEA mesher + export tessellation against the STAGED
libaicad.dll (the one the app loads).

Builds a 60x60x10 plate with an 8 mm hole (so curvature matters), exports a STEP, then
exercises:
  * sim_mesh: element count rises with fineness; curvature-on adds nodes vs curvature-off;
    the new quality metrics (aspect/jacobian/pct_poor/histogram) are populated.
  * export_tess_estimate: triangle count rises as the linear deflection shrinks.
  * export_file: STL ascii flag actually selects an ASCII container.

Run:  python tools/mesh_quality_verify.py
"""
import ctypes, json, os, tempfile

DIST = r"C:\Users\nigga\Desktop\AI-opencad\dist\AI-CAD"
os.add_dll_directory(DIST)
dll = ctypes.CDLL(os.path.join(DIST, "libaicad.dll"))
dll.aicad_call.restype = ctypes.c_int
dll.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
dll.aicad_free.argtypes = [ctypes.c_char_p]


def call(method, params):
    out = ctypes.c_char_p()
    rc = dll.aicad_call(method.encode(), json.dumps(params).encode(), ctypes.byref(out))
    s = out.value.decode() if out.value else ""
    if out.value:
        dll.aicad_free(out)
    try:
        return rc, (json.loads(s) if s else {})
    except Exception:
        return rc, {"_raw": s[:400]}


def dim(v):
    return {"value": v, "expr": None}


def feat(ftype, name, params=None, refs=None, **extra):
    f = {"type": ftype, "name": name, "params": params or {}, "refs": refs or {}}
    f.update(extra)
    return {"op": "add_feature", "feature": f}


results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))


# ---- build a plate with a hole + export a STEP -------------------------------------
_, doc = call("doc_new", {"kind": "part"})
doc = doc.get("doc_id")
rc, resp = call("doc_apply_ops", {"doc_id": doc, "ops": [
    feat("box", "Plate", {"length": dim(60), "width": dim(60), "height": dim(10)}),
    feat("hole", "Bore", {"style": "simple", "standard": "custom",
                          "diameter": dim(8), "end": "through_all"},
         {"face": {"of": "face", "side": "top"}})]})
check("build plate with hole", rc == 0 and not resp.get("error"), resp.get("error", ""))

tmp = tempfile.mkdtemp(prefix="meshqa_")
step = os.path.join(tmp, "plate.step")
rc, r = call("doc_export_step", {"doc_id": doc, "path": step})
check("export STEP", rc == 0 and os.path.exists(r.get("path", step)), r.get("path", ""))
step = r.get("path", step)


def mesh(**kw):
    p = {"step_path": step, "order": 1}
    p.update(kw)
    _, res = call("sim_mesh", p)
    return res


# ---- sim_mesh: fineness monotonicity ----------------------------------------------
coarse = mesh(fineness=0.15, curvature=True)
fine = mesh(fineness=0.85, curvature=True)
check("mesh ok (coarse & fine)", coarse.get("ok") and fine.get("ok"),
      f"coarse err={coarse.get('error','')} fine err={fine.get('error','')}")
check("element count rises with fineness",
      fine.get("n_elems", 0) > coarse.get("n_elems", 0),
      f"coarse={coarse.get('n_elems')} fine={fine.get('n_elems')}")

# ---- sim_mesh: curvature adds refinement ------------------------------------------
curv_off = mesh(fineness=0.5, curvature=False)
curv_on = mesh(fineness=0.5, curvature=True)
check("curvature-on adds nodes vs off",
      curv_on.get("n_nodes", 0) > curv_off.get("n_nodes", 0),
      f"off={curv_off.get('n_nodes')} on={curv_on.get('n_nodes')}")

# ---- sim_mesh: 2nd-order (tet10) + Netgen optimize (the GUI default combo) ----------
# Regression guard: a post-generate Netgen optimize pass used to wipe the elevated tet10
# elements ("no tet10 elements"). The optimizer must run during generate, not after.
_, t10 = call("sim_mesh", {"step_path": step, "order": 2, "optimize": True, "curvature": True,
                           "fineness": 0.6})
check("tet10 + optimize produces elements",
      t10.get("ok") and t10.get("n_elems", 0) > 0 and t10.get("nodes_per_elem") == 10,
      f"ok={t10.get('ok')} elems={t10.get('n_elems')} npe={t10.get('nodes_per_elem')} "
      f"err={t10.get('error','')}")

# ---- sim_mesh: quality metrics populated ------------------------------------------
q = curv_on
hist = q.get("aspect_hist", [])
check("quality metrics populated",
      q.get("aspect_avg", 0) > 0 and q.get("jacobian_min", 0) > 0 and len(hist) == 10,
      f"aspect avg={q.get('aspect_avg'):.2f} worst={q.get('aspect_max'):.2f} "
      f"jac_min={q.get('jacobian_min'):.3f} poor={q.get('pct_poor'):.1f}% hist={hist}")

# ---- structured hexahedral meshing (box) -------------------------------------------
# A box meshed with hexes is a perfectly regular grid: every cell identical, so the
# element-volume variation is ~0 (vs ~0.37 for the irregular tet pattern). This is the
# symmetry win the user asked for. Build a clean box for this (the plate has a hole).
_, bdoc = call("doc_new", {"kind": "part"})
bdoc = bdoc.get("doc_id")
call("doc_apply_ops", {"doc_id": bdoc, "ops": [
    feat("box", "Box", {"length": dim(50), "width": dim(50), "height": dim(50)})]})
bstep = os.path.join(tmp, "box.step")
_, br = call("doc_export_step", {"doc_id": bdoc, "path": bstep})
bstep = br.get("path", bstep)
_, hx = call("sim_mesh", {"step_path": bstep, "structured": True, "fineness": 0.5})
check("hex mesh on a box", hx.get("ok") and hx.get("nodes_per_elem") == 8 and hx.get("n_elems", 0) > 0,
      f"ok={hx.get('ok')} npe={hx.get('nodes_per_elem')} elems={hx.get('n_elems')} err={hx.get('error','')}")
# element-volume CoV from the hex connectivity (n0->n1 = +x edge, n0->n3 = +y, n0->n4 = +z)
HP, HT = hx.get("points", []), hx.get("tets", [])
nh = len(HT) // 8 if hx.get("ok") else 0
import statistics
hvols = []
for e in range(nh):
    c = [(HP[3*HT[e*8+k]], HP[3*HT[e*8+k]+1], HP[3*HT[e*8+k]+2]) for k in range(8)]
    hvols.append(abs((c[1][0]-c[0][0]) * (c[3][1]-c[0][1]) * (c[4][2]-c[0][2])))
hcov = (statistics.pstdev(hvols) / statistics.mean(hvols)) if hvols else 1.0
check("hex grid is exactly regular (volume CoV ~ 0)", nh > 0 and hcov < 1e-6,
      f"CoV={hcov:.6f} aspect={hx.get('aspect_avg')} jac={hx.get('jacobian_min')}")

# Non-box geometry must fall back with a clear message (the plate-with-hole).
_, hxf = call("sim_mesh", {"step_path": step, "structured": True, "fineness": 0.5})
check("hex on a non-box fails with a clear message",
      hxf.get("ok") is False and "etrahedral" in hxf.get("error", ""),
      f"ok={hxf.get('ok')} err={hxf.get('error','')[:60]}")

# ---- export_tess_estimate: triangles rise as deflection shrinks --------------------
_, est_coarse = call("export_tess_estimate", {"doc_id": doc, "lin_deflection": 0.5})
_, est_fine = call("export_tess_estimate", {"doc_id": doc, "lin_deflection": 0.02})
check("estimate: finer deflection => more triangles",
      est_fine.get("triangles", 0) > est_coarse.get("triangles", 0),
      f"coarse={est_coarse.get('triangles')} fine={est_fine.get('triangles')}")

# ---- export_file: ASCII vs binary STL ----------------------------------------------
stl_a = os.path.join(tmp, "ascii.stl")
stl_b = os.path.join(tmp, "binary.stl")
call("export_file", {"doc_id": doc, "path": stl_a, "format": "stl",
                     "lin_deflection": 0.05, "ang_deflection": 0.35, "ascii": True})
call("export_file", {"doc_id": doc, "path": stl_b, "format": "stl",
                     "lin_deflection": 0.05, "ang_deflection": 0.35, "ascii": False})
head_a = open(stl_a, "rb").read(6) if os.path.exists(stl_a) else b""
head_b = open(stl_b, "rb").read(6) if os.path.exists(stl_b) else b""
check("STL ascii flag selects ASCII container",
      head_a[:5] == b"solid" and head_b[:5] != b"solid",
      f"ascii head={head_a!r} binary head={head_b!r}")

print("-" * 60)
print(f"{sum(results)}/{len(results)} checks passed")
raise SystemExit(0 if all(results) else 1)
