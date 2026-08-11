"""Round-10 fix verification (post user's additional fixes + TEXT feature).

Drives the engine via core_bridge against whatever libchudware.dll CHUDWARE_DIST points at
(defaults to the build-tree bin so it tests the freshest build). Run:
  CHUDWARE_DIST set automatically below; from repo root:
  PYTHONPATH=. <runtime-venv-python> tools/verify_round10.py

Checks (each prints PASS/FAIL with the observed value):
  1. TEXT engrave + emboss on a planar face (real geometry, valid solid)
  2. bowtie/self-intersecting profile extrude is REJECTED (no zero-volume body)
  3. draft with a FACE-QUERY neutral plane resolves
  4. side:bottom on an uncentred tube -> planar cap (not the perpendicular bore) + no regression
  5. IGES export of a curved part round-trips as a valid closed solid (honest validation)
  6. inspect_section area on a HOLLOW (multi-loop) section is correct (not 0)
  7. inspect_section in-plane Ixx/Iyy correct for a FRONT (non-XY) section
"""
import os, sys
os.environ.setdefault("CHUDWARE_DIST", os.path.abspath("core/build-occt/bin/Release"))
import tools.chudware_drive as d
from services.mcp import core_bridge as cb

PI = 3.141592653589793
results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, "|", detail)

def feat_failed(r):
    for k in ("features", "statuses"):
        for f in (r.get(k) or []):
            if f.get("ok") is False or f.get("error"):
                return f.get("error") or "(failed)"
    # also look inside rebuild status blocks
    rb = r.get("rebuild") or {}
    for f in (rb.get("statuses") or rb.get("features") or []):
        if f.get("ok") is False or f.get("error"):
            return f.get("error") or "(failed)"
    return None

def vol(doc):
    try:
        return d.measure(doc).get("volume_mm3")
    except cb.CoreError:
        return None

# ---- 1. TEXT engrave + emboss ------------------------------------------------
doc = d.new_part("t_text")
d.ops(doc, [d.box("B", 60, 40, 15)])
base = vol(doc)
d.ops(doc, [d.feat("text", "eng", {"text": "PN-7", "height": 8, "depth": 1.2, "mode": "engrave"},
                   {"target_face": {"of": "face", "side": "top"}})])
v_eng = vol(doc)
d.ops(doc, [d.feat("text", "emb", {"text": "OK", "height": 6, "depth": 1.0, "mode": "emboss"},
                   {"target_face": {"of": "face", "side": "bottom"}})])
v_emb = vol(doc)
c = d.check(doc)
check("TEXT engrave removes material", v_eng is not None and v_eng < base,
      f"{base}->{v_eng}")
check("TEXT emboss adds material", v_emb is not None and v_emb > v_eng,
      f"{v_eng}->{v_emb}")
check("TEXT part stays watertight", not c.get("issues"), f"issues={c.get('issues')}")
d.close(doc)

# ---- 2. bowtie extrude rejected ----------------------------------------------
doc = d.new_part("t_bow")
d.ops(doc, [d.box("Keep", 20, 20, 10)])
d.ops(doc, [{"op": "add_feature", "feature": {"type": "sketch", "name": "S", "params": {
    "plane": "plane.top", "entities": [
        {"id": "l1", "type": "line", "p1": [0, 0], "p2": [10, 10]},
        {"id": "l2", "type": "line", "p1": [10, 10], "p2": [10, 0]},
        {"id": "l3", "type": "line", "p1": [10, 0], "p2": [0, 10]},
        {"id": "l4", "type": "line", "p1": [0, 10], "p2": [0, 0]}]}}}])
d.ops(doc, [d.feat("extrude", "E", {"mode": "new", "end": "blind", "depth": {"value": 5, "expr": None}},
                   {"profile": [{"kind": "region", "sketch": "$2", "centroid": [5, 5], "hint_index": 0}]})])
# the box must survive at 4000 and NO degenerate zero-volume body may have been added
check("bowtie extrude adds no zero-volume body", abs((vol(doc) or 0) - 4000.0) < 0.01,
      f"vol={vol(doc)} (want 4000, box survived + no degenerate body)")
d.close(doc)

# ---- 3. draft with face-query neutral ----------------------------------------
doc = d.new_part("t_draft")
d.ops(doc, [d.box("B", 30, 30, 20)])
r = d.ops(doc, [d.feat("draft", "D", {"angle": 5},
                       {"neutral": {"of": "face", "side": "bottom"},
                        "faces": {"of": "face", "side": "front"}})])
err = feat_failed(r)
check("draft face-query neutral resolves", err is None and vol(doc) not in (None, 0),
      f"err={err} vol={vol(doc)}")
d.close(doc)

# ---- 4. side:bottom on uncentred tube ----------------------------------------
doc = d.new_part("t_tube")
d.ops(doc, [d.feat("cylinder", "C", {"radius": {"value": 20, "expr": None},
                                     "height": {"value": 8, "expr": None}, "at": [0, 0, 4]})])
d.ops(doc, [d.feat("cylinder", "bore", {"radius": {"value": 10, "expr": None},
                                        "height": {"value": 40, "expr": None}, "mode": "cut", "at": [0, 0, 4]})])
def surf_z(q):
    m = d.query(doc, q).get("matches", [])
    return (m[0].get("surf"), round(m[0].get("centroid", [0, 0, 9])[2], 2)) if m else (None, None)
sb = surf_z({"of": "face", "side": "bottom"})
st = surf_z({"of": "face", "side": "top"})
check("tube side:bottom -> planar cap z~0", sb == ("plane", 0.0), f"{sb}")
check("tube side:top -> planar cap z~8", st == ("plane", 8.0), f"{st}")
d.close(doc)
# regression: cylinder
doc = d.new_part("t_cyl")
d.ops(doc, [d.cylinder("C", 15, 30)])
ct = surf_z2 = (lambda q: (lambda m: (m[0].get("surf"), round(m[0].get("centroid", [0, 0, 0])[2], 2)) if m else (None, None))(d.query(doc, q).get("matches", [])))
check("cyl side:top -> plane (no regression)", ct({"of": "face", "side": "top"}) == ("plane", 15.0),
      f"{ct({'of':'face','side':'top'})}")
check("cyl side:front -> cylinder (no regression)", ct({"of": "face", "side": "front"})[0] == "cylinder",
      f"{ct({'of':'face','side':'front'})}")
d.close(doc)

# ---- 5. IGES round-trip + honest validation ----------------------------------
doc = d.new_part("t_iges")
d.ops(doc, [d.cylinder("C", 20, 30)])
d.ops(doc, [d.feat("fillet", "F", {"radius": {"value": 4, "expr": None}},
                   {"edges": {"of": "edge", "surf": "round"}})])
base = vol(doc)
out = os.path.abspath("tools/_r10.iges")
try:
    exp = d.export(doc, out, "iges")
    igok = bool(exp.get("validated") or exp.get("valid_solids"))
except cb.CoreError as e:
    igok = False
    exp = {"error": str(e)[:60]}
# re-open and compare volume
rtv = None
if os.path.exists(out):
    try:
        doc2 = cb.call("doc_open", {"path": out}).get("doc_id")
        rtv = d.measure(doc2).get("volume_mm3"); d.close(doc2)
    except cb.CoreError:
        rtv = None
check("IGES round-trips a valid solid (vol preserved)",
      rtv is not None and base and abs(rtv - base) / base < 0.02,
      f"export={exp.get('validated', exp)} base={base} reread={rtv}")
d.close(doc)
try: os.remove(out)
except OSError: pass

# ---- 6. inspect_section hollow (multi-loop) area -----------------------------
doc = d.new_part("t_sec")  # centred tube OD40 ID20, height 8 -> 'top' cut = annulus
d.ops(doc, [d.cylinder("C", 20, 8)])
d.ops(doc, [d.feat("cylinder", "bore", {"radius": {"value": 10, "expr": None},
                                        "height": {"value": 40, "expr": None}, "mode": "cut"})])
sec = cb.call("eval_section_props", {"doc_id": doc, "plane": "top"})
want = PI * (20 * 20 - 10 * 10)  # 942.48
a = sec.get("area_mm2", 0.0)
check("inspect_section hollow area (multi-loop)", a and abs(a - want) / want < 0.02,
      f"area={a:.6g} want={want:.6g}")
d.close(doc)

# ---- 7. inspect_section non-XY in-plane inertia ------------------------------
doc = d.new_part("t_in")  # box x=40, y=10, z=20 centred -> 'front'(XZ) section = 40x20
d.ops(doc, [d.box("B", 40, 10, 20)])
sec = cb.call("eval_section_props", {"doc_id": doc, "plane": "front"})
# in-plane second moments of a 40(horiz)x20(vert) rectangle about its centroid:
#   about horizontal axis: 40*20^3/12 = 26666.67 ; about vertical: 20*40^3/12 = 106666.67
ixx, iyy = sec.get("Ixx", 0.0), sec.get("Iyy", 0.0)
pair = sorted([ixx, iyy])
want_pair = sorted([40 * 20**3 / 12, 20 * 40**3 / 12])
okin = all(w and abs(p - w) / w < 0.03 for p, w in zip(pair, want_pair))
check("inspect_section front-plane in-plane Ixx/Iyy", okin,
      f"got={[round(x,1) for x in pair]} want={[round(x,1) for x in want_pair]}")
d.close(doc)

print("\n== %d/%d checks passed ==" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
