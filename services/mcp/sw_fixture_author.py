"""Author canonical SolidWorks test parts via COM so the importer can be validated round-trip.

Builds focused .sldprt files (one feature concept each) into a fixtures dir, returning a manifest
of {path: expected_volume_mm3 (from SolidWorks itself)} the importer test compares against.
"""
import os
import pythoncom
import win32com.client as win32
from engine.doc.sw_feature_import import _cast, _ensure_sw_module

M = 0.001  # mm -> m (SolidWorks API is metres)
OUT = os.path.join(os.path.dirname(__file__), "..", "..", "corpora", "sw_fixtures")
os.makedirs(OUT, exist_ok=True)


def _app():
    pythoncom.CoInitialize()
    app = _cast(win32.Dispatch("SldWorks.Application"), "ISldWorks")
    app.Visible = False
    return app


def _part_template(app):
    # swDefaultTemplatePart = 8
    t = app.GetUserPreferenceStringValue(8)
    return t


def new_part(app):
    tmpl = _part_template(app)
    model = app.NewDocument(tmpl, 0, 0, 0)
    return _cast(model, "IModelDoc2")


def select_plane(model, name):
    model.Extension.SelectByID2(name, "PLANE", 0, 0, 0, False, 0, None, 0)


def begin_sketch(model):
    model.SketchManager.InsertSketch(True)


def end_sketch(model):
    model.SketchManager.InsertSketch(True)


def circle(model, cx, cy, r):
    model.SketchManager.CreateCircle(cx * M, cy * M, 0, (cx + r) * M, cy * M, 0)


def line(model, x1, y1, x2, y2):
    model.SketchManager.CreateLine(x1 * M, y1 * M, 0, x2 * M, y2 * M, 0)


def centerline(model, x1, y1, x2, y2):
    sm = model.SketchManager
    sm.CreateCenterLine(x1 * M, y1 * M, 0, x2 * M, y2 * M, 0)


def volume_mm3(model):
    mp = model.Extension.CreateMassProperty()
    return float(mp.Volume) * 1e9


def mass_props(model):
    """(volume_mm3, com_mm[x,y,z]) in SolidWorks model coordinates."""
    mp = model.Extension.CreateMassProperty()
    c = mp.CenterOfMass  # metres, SW coords
    return float(mp.Volume) * 1e9, [c[0] * 1000, c[1] * 1000, c[2] * 1000]


def save(model, name):
    path = os.path.join(OUT, name)
    model.Extension.SaveAs(path, 0, 1, None, 0, 0)
    return path


def close(app, model):
    app.CloseDoc(model.GetTitle())


# --------------------------------------------------------------------------- parts
def author_extrude_front(app):
    """A 40x30 rectangle on the FRONT plane, extruded 20 — exercises a non-Top sketch plane."""
    model = new_part(app)
    select_plane(model, "Front Plane")
    begin_sketch(model)
    # rectangle centred at origin
    line(model, -20, -15, 20, -15)
    line(model, 20, -15, 20, 15)
    line(model, 20, 15, -20, 15)
    line(model, -20, 15, -20, -15)
    end_sketch(model)
    # FeatureExtrusion3 — blind, depth 20mm, single direction
    fm = model.FeatureManager
    feat = fm.FeatureExtrusion3(
        True, False, False, 0, 0, 20 * M, 0, False, False, False, False,
        0, 0, False, False, False, False, True, True, True, 0, 0, False)
    v = volume_mm3(model)
    p = save(model, "extrude_front_40x30x20.SLDPRT")
    close(app, model)
    return p, v


def author_revolve(app):
    """Revolve an L-profile around a centerline → a stepped disc/cylinder."""
    model = new_part(app)
    select_plane(model, "Front Plane")
    begin_sketch(model)
    # centerline along Y (the axis), then a closed profile offset +X from it.
    centerline(model, 0, -40, 0, 40)
    # profile: a 20(wide) x 60(tall) rectangle starting 10mm off the axis
    line(model, 10, -30, 30, -30)
    line(model, 30, -30, 30, 30)
    line(model, 30, 30, 10, 30)
    line(model, 10, 30, 10, -30)
    end_sketch(model)
    fm = model.FeatureManager
    # FeatureRevolve2(SingleDir, IsSolid, IsThin, IsCut, ReverseDir, BothDirectionUpToSameEntity,
    #   Dir1Type, Dir2Type, Dir1Angle, Dir2Angle, OffsetReverse1, OffsetReverse2, OffsetDist1,
    #   OffsetDist2, ThinType, ThinThickness1, ThinThickness2, Merge, UseFeatScope, UseAutoSelect)
    import math
    feat = fm.FeatureRevolve2(True, True, False, False, False, False,
                              0, 0, 2 * math.pi, 0, False, False, 0, 0,
                              0, 0, 0, True, True, True)
    v = volume_mm3(model)
    p = save(model, "revolve_ring.SLDPRT")
    close(app, model)
    return p, v, feat is not None


def _asym_rect_extrude(app, plane, name):
    """An OFFSET rectangle (0,0)->(40,20) extruded 10 — asymmetric in every axis, so the COM
    pins down the SW->engine coordinate mapping (no symmetry to hide a flip)."""
    model = new_part(app)
    select_plane(model, plane)
    begin_sketch(model)
    line(model, 0, 0, 40, 0)
    line(model, 40, 0, 40, 20)
    line(model, 40, 20, 0, 20)
    line(model, 0, 20, 0, 0)
    end_sketch(model)
    fm = model.FeatureManager
    fm.FeatureExtrusion3(True, False, False, 0, 0, 10 * M, 0, False, False, False, False,
                         0, 0, False, False, False, False, True, True, True, 0, 0, False)
    vol, com = mass_props(model)
    p = save(model, name)
    close(app, model)
    return p, vol, com


def _box(model, lx, ly, lz):
    """A box centred at origin on the Top plane, extruded mid-plane in Z."""
    select_plane(model, "Top Plane")
    begin_sketch(model)
    line(model, -lx / 2, -ly / 2, lx / 2, -ly / 2)
    line(model, lx / 2, -ly / 2, lx / 2, ly / 2)
    line(model, lx / 2, ly / 2, -lx / 2, ly / 2)
    line(model, -lx / 2, ly / 2, -lx / 2, -ly / 2)
    end_sketch(model)
    model.FeatureManager.FeatureExtrusion3(
        True, False, False, 0, 0, lz * M, 0, False, False, False, False,
        0, 0, False, False, False, False, True, True, True, 0, 0, False)


def _select_all_edges(model):
    """Select every edge of the (single) solid body. Returns the count selected."""
    pd = _cast(model, "IPartDoc")
    body = _cast(pd.GetBodies2(0, False)[0], "IBody2")   # swSolidBody=0
    edges = body.GetEdges()
    model.ClearSelection2(True)
    n = 0
    for e in edges:
        ent = _cast(e, "IEntity")
        if ent.Select4(True, None):
            n += 1
    return n


def author_fillet_box(app):
    model = new_part(app)
    _box(model, 40, 30, 20)
    _select_all_edges(model)
    # InsertFeatureFillet variant via FeatureManager.FeatureFillet3
    import pythoncom
    R = 3 * M
    fm = model.FeatureManager
    feat = fm.FeatureFillet3(195, R, 0, 0, 0, 0, 0, None, None, None, None, None, None, None)
    vol, com = mass_props(model)
    p = save(model, "fillet_box.SLDPRT")
    close(app, model)
    return p, vol, com


def author_chamfer_box(app):
    model = new_part(app)
    _box(model, 40, 30, 20)
    _select_all_edges(model)
    fm = model.FeatureManager
    # InsertFeatureChamfer(Width, Angle, Flags, ChamferType, OtherDist, VertexChamDist1,...)
    feat = fm.InsertFeatureChamfer(4, 1, 3 * M, 0.785398, 0, 0, 0, 0)
    vol, com = mass_props(model)
    p = save(model, "chamfer_box.SLDPRT")
    close(app, model)
    return p, vol, com


def _select_face_normal(model, target, append=False, mark=None):
    """Select the body face whose outward normal ~ target (SW model dir). Returns True if found."""
    pd = _cast(model, "IPartDoc")
    body = _cast(pd.GetBodies2(0, False)[0], "IBody2")
    best, bestdot, bestpt = None, -2, None
    for fc in body.GetFaces():
        face = _cast(fc, "IFace2")
        nrm = face.Normal  # [nx,ny,nz]
        dot = sum(nrm[i] * target[i] for i in range(3))
        if dot > bestdot:
            bestdot, best = dot, face
    if best is None:
        return False
    if mark is not None:
        # midpoint of the face for SelectByID2 with a mark
        pts = []
        for e in best.GetEdges():
            cp = _cast(e, "IEdge").GetCurveParams2()
            pts.append([(cp[0] + cp[3]) / 2, (cp[1] + cp[4]) / 2, (cp[2] + cp[5]) / 2])
        c = [sum(p[i] for p in pts) / len(pts) for i in range(3)]
        return model.Extension.SelectByID2("", "FACE", c[0], c[1], c[2], append, mark, None, 0)
    return _cast(best, "IEntity").Select4(append, None)


def _select_feature(model, name, append=True, mark=0):
    return model.Extension.SelectByID2(name, "BODYFEATURE", 0, 0, 0, append, mark, None, 0)


def _select_edge_dir(model, axis_letter, mark=1):
    """Select a body edge aligned with X/Y/Z (for a pattern direction), with the given mark."""
    idx = {"x": 0, "y": 1, "z": 2}[axis_letter]
    pd = _cast(model, "IPartDoc")
    body = _cast(pd.GetBodies2(0, False)[0], "IBody2")
    for e in body.GetEdges():
        ed = _cast(e, "IEdge")
        cp = ed.GetCurveParams2()
        d = [cp[3] - cp[0], cp[4] - cp[1], cp[5] - cp[2]]
        if abs(d[idx]) > 1e-4 and all(abs(d[j]) < 1e-6 for j in range(3) if j != idx):
            ent = _cast(ed, "IEntity")
            mp = [(cp[0] + cp[3]) / 2, (cp[1] + cp[4]) / 2, (cp[2] + cp[5]) / 2]
            model.Extension.SelectByID2("", "EDGE", mp[0], mp[1], mp[2], True, mark, None, 0)
            return True
    return False


def author_shell(app):
    model = new_part(app)
    _box(model, 40, 30, 20)
    model.ClearSelection2(True)
    _select_face_normal(model, (0, 1, 0))      # remove the top (+Y) face
    model.InsertFeatureShell(2 * M, False)      # 2mm wall, hollow inward
    vol, com = mass_props(model)
    p = save(model, "shell_box.SLDPRT")
    close(app, model)
    return p, vol, com


def author_linpat(app):
    model = new_part(app)
    _box(model, 80, 40, 20)
    # a 8mm hole near the -X end, on the top face
    _select_face_normal(model, (0, 1, 0))
    begin_sketch(model)
    circle(model, -30, 0, 4)
    end_sketch(model)
    model.FeatureManager.FeatureCut4(
        True, False, False, 1, 0, 0.1, 0.1, False, False, False, False, 0, 0,
        False, False, False, False, False, True, True, True, True, False, 0, 0, False, False)
    cut_name = model.FeatureByPositionReverse(0).Name
    # Direction edge along X (mark 1) + the seed cut feature (mark 4), then linear-pattern.
    model.ClearSelection2(True)
    _select_edge_dir(model, "x", mark=1)
    _select_feature(model, cut_name, append=True, mark=4)
    feat = model.FeatureManager.FeatureLinearPattern4(
        4, 20 * M, 1, 0.0, True, False, "", "", False, False, False, False,
        True, True, False, False, False, False, 0.0, 0.0)
    vol, com = mass_props(model)
    p = save(model, "linpat_holes.SLDPRT")
    close(app, model)
    return p, vol, com


def author_mirror(app):
    model = new_part(app)
    _box(model, 60, 40, 20)
    # a hole off-centre on +X side, on top face, then mirror across the Right plane (YZ)
    _select_face_normal(model, (0, 1, 0))
    begin_sketch(model)
    circle(model, 20, 0, 5)
    end_sketch(model)
    model.FeatureManager.FeatureCut4(
        True, False, False, 1, 0, 0.1, 0.1, False, False, False, False, 0, 0,
        False, False, False, False, False, True, True, True, True, False, 0, 0, False, False)
    cut_name = model.FeatureByPositionReverse(0).Name
    model.ClearSelection2(True)
    # Mirror plane selected with mark 2; the seed feature with mark 1.
    model.Extension.SelectByID2("Right Plane", "PLANE", 0, 0, 0, False, 2, None, 0)
    _select_feature(model, cut_name, append=True, mark=1)
    feat = model.FeatureManager.InsertMirrorFeature2(False, False, True, False, 0)
    vol, com = mass_props(model)
    p = save(model, "mirror_holes.SLDPRT")
    close(app, model)
    return p, vol, com


def author_draft(app):
    model = new_part(app)
    _box(model, 40, 30, 40)   # taller box so draft is visible
    # neutral plane = the bottom (-Y) face; draft the 4 side faces inward by 5 deg
    model.ClearSelection2(True)
    _select_face_normal(model, (0, -1, 0), append=True, mark=1)   # neutral plane = mark 1
    for tgt in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):     # faces to draft = mark 0
        _select_face_normal(model, tgt, append=True, mark=0)
    import math as _m
    feat = model.InsertMfDraft2(5 * _m.pi / 180.0, False, False, 0, False)
    vol, com = mass_props(model)
    p = save(model, "draft_box.SLDPRT")
    close(app, model)
    return p, vol, com, (feat is not None)


def author_cirpat(app):
    model = new_part(app)
    # disc r40 x 10 on the Top plane
    select_plane(model, "Top Plane")
    begin_sketch(model)
    circle(model, 0, 0, 40)
    end_sketch(model)
    model.FeatureManager.FeatureExtrusion3(
        True, False, False, 0, 0, 10 * M, 0, False, False, False, False,
        0, 0, False, False, False, False, True, True, True, 0, 0, False)
    # a 5mm hole at radius 25 on the top face
    _select_face_normal(model, (0, 1, 0))
    begin_sketch(model)
    circle(model, 25, 0, 5)
    end_sketch(model)
    model.FeatureManager.FeatureCut4(
        True, False, False, 1, 0, 0.1, 0.1, False, False, False, False, 0, 0,
        False, False, False, False, False, True, True, True, True, False, 0, 0, False, False)
    cut_name = model.FeatureByPositionReverse(0).Name
    # axis = the cylindrical (round) face of the disc, seed = the cut
    model.ClearSelection2(True)
    _select_face_normal_radial(model, mark=1)
    _select_feature(model, cut_name, append=True, mark=4)
    # 6 instances at 60° step (NOT equal-spaced over a full circle, to avoid the 360°=0° overlap):
    # holes at 0,60,120,180,240,300 -> 6 distinct.
    model.FeatureManager.FeatureCircularPattern4(
        6, 1.0471975512, False, "", False, False, False)
    vol, com = mass_props(model)
    p = save(model, "cirpat_holes.SLDPRT")
    close(app, model)
    return p, vol, com


def _select_face_normal_radial(model, mark=1):
    """Select the disc's cylindrical face (used as a circular-pattern axis)."""
    pd = _cast(model, "IPartDoc")
    body = _cast(pd.GetBodies2(0, False)[0], "IBody2")
    for fc in body.GetFaces():
        face = _cast(fc, "IFace2")
        surf = _cast(face.GetSurface(), "ISurface")
        if surf.IsCylinder():
            ent = _cast(face, "IEntity")
            # a point on the cylinder: use a face param point
            pt = face.GetClosestPointOn(0.040, 0, 0.005)
            model.Extension.SelectByID2("", "FACE", pt[0], pt[1], pt[2], True, mark, None, 0)
            return True
    return False


def author_asym_top(app):
    return _asym_rect_extrude(app, "Top Plane", "asym_top.SLDPRT")


def author_asym_front(app):
    return _asym_rect_extrude(app, "Front Plane", "asym_front.SLDPRT")


def author_asym_right(app):
    return _asym_rect_extrude(app, "Right Plane", "asym_right.SLDPRT")


if __name__ == "__main__":
    app = _app()
    manifest = {}
    for fn in (author_extrude_front, author_revolve,
               author_asym_top, author_asym_front, author_asym_right):
        try:
            res = fn(app)
            path, vol = res[0], res[1]
            com = res[2] if (len(res) > 2 and isinstance(res[2], list)) else None
            manifest[path] = vol
            extra = f" com_sw={[round(c,3) for c in com]}" if com else ""
            print(f"OK {os.path.basename(path):34} vol={vol:.4f}{extra}")
        except Exception as e:
            print(f"FAIL {fn.__name__}: {e}")
