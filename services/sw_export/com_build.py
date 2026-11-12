"""Replay a SolidWorks build plan (:mod:`services.sw_export.plan`) through an installed SolidWorks.

The result is a real SolidWorks part: SolidWorks itself creates every sketch and feature and
saves the file, so it opens in that SolidWorks release and newer with a normal FeatureManager
tree. The call signatures and argument lists are the ones ``services/mcp/sw_fixture_author.py``
authored the import fixtures with (checked against sldworks.tlb).

After the last feature the part's mass properties are compared with Chudware's (`expected` in
the plan); a mismatch is reported (``verified: false``), never hidden.
"""

from __future__ import annotations

import math
import os
from typing import Any, Optional

from services.sw_import.sw_feature_import import SolidWorksUnavailable, _cast

M = 0.001  # mm -> m (the SolidWorks API is in metres)

# swEndConditions_e
_BLIND, _THROUGH_ALL, _MID_PLANE, _THROUGH_ALL_BOTH = 0, 1, 6, 9
_REF_PLANE_DISTANCE, _REF_PLANE_FLIP = 8, 256
_CHAMFER_ANGLE_DISTANCE = 1
# Relative volume and absolute centre-of-mass tolerances of the final comparison.
VOLUME_RTOL = 1e-5
COM_TOL_MM = 1e-2


class BuildError(RuntimeError):
    pass


def _app(visible: bool = False):
    try:
        import pythoncom
        import win32com.client as win32
    except Exception as e:  # noqa: BLE001
        raise SolidWorksUnavailable(f"pywin32 is not installed: {e}") from e
    pythoncom.CoInitialize()
    try:
        app = _cast(win32.Dispatch("SldWorks.Application"), "ISldWorks")
    except Exception as e:  # noqa: BLE001
        raise SolidWorksUnavailable(f"could not start SolidWorks over COM: {e}") from e
    app.Visible = visible
    return app


def mass_props(model) -> tuple[float, list[float]]:
    """(volume_mm3, centre of mass in SolidWorks model mm)."""
    mp = model.Extension.CreateMassProperty()
    c = mp.CenterOfMass
    return float(mp.Volume) * 1e9, [c[0] * 1000, c[1] * 1000, c[2] * 1000]


def _volume(model) -> float:
    try:
        return mass_props(model)[0]
    except Exception:  # noqa: BLE001 — no solid yet
        return 0.0


def _select(model, name: str, kind: str, append: bool = False, mark: int = 0) -> bool:
    return bool(model.Extension.SelectByID2(name, kind, 0, 0, 0, append, mark, None, 0))


def _select_point(model, kind: str, p_mm, append: bool, mark: int = 0) -> bool:
    return bool(model.Extension.SelectByID2("", kind, p_mm[0] * M, p_mm[1] * M, p_mm[2] * M,
                                            append, mark, None, 0))


def _bodies(model) -> list:
    pd = _cast(model, "IPartDoc")
    return [_cast(b, "IBody2") for b in (pd.GetBodies2(0, False) or [])]


def _select_face_by_normal(model, normal, append: bool, mark: int = 0) -> bool:
    """The outermost planar face whose outward normal is `normal` (SolidWorks model dir)."""
    best, best_h = None, -math.inf
    for body in _bodies(model):
        for fc in body.GetFaces() or []:
            face = _cast(fc, "IFace2")
            n = face.Normal
            if sum(n[i] * normal[i] for i in range(3)) < 1 - 1e-6:
                continue
            box = face.GetBox()   # [x0, y0, z0, x1, y1, z1] metres
            centre = [(box[i] + box[i + 3]) / 2 for i in range(3)]
            h = sum(centre[i] * normal[i] for i in range(3))
            if h > best_h:
                best, best_h = face, h
    return bool(best is not None and _cast(best, "IEntity").Select4(append, None))


def _rename_last(model, name: str):
    feat = model.FeatureByPositionReverse(0)
    if feat is not None:
        try:
            feat.Name = name
        except Exception:  # noqa: BLE001 — a duplicate name keeps SolidWorks' own
            pass
    return feat


class _Runner:
    def __init__(self, app, model):
        self.app, self.model = app, model
        self.fm = model.FeatureManager
        self.sm = model.SketchManager
        self.notes: list[str] = []

    # -- sketch ------------------------------------------------------------------------------
    def sketch(self, s: dict) -> None:
        m, plane = self.model, s["plane"]
        m.ClearSelection2(True)
        if plane["kind"] == "principal":
            ok = _select(m, plane["plane"], "PLANE")
        elif plane["kind"] == "offset":
            _select(m, plane["base"], "PLANE")
            off = plane["offset"]
            flags = _REF_PLANE_DISTANCE | (_REF_PLANE_FLIP if off < 0 else 0)
            if self.fm.InsertRefPlane(flags, abs(off) * M, 0, 0, 0, 0) is None:
                raise BuildError(f"{s['name']}: offset plane failed")
            _rename_last(m, plane["name"])
            m.ClearSelection2(True)
            ok = _select(m, plane["name"], "PLANE")
        else:
            ok = _select_face_by_normal(m, plane["normal"], False)
        if not ok:
            raise BuildError(f"{s['name']}: could not select the sketch plane {plane}")
        self.sm.InsertSketch(True)
        self.sm.AddToDB = True
        try:
            for e in s["entities"]:
                self.entity(e)
        finally:
            self.sm.AddToDB = False
        self.sm.InsertSketch(True)
        _rename_last(m, s["name"])

    def entity(self, e: dict) -> None:
        sm, t, cons = self.sm, e["type"], bool(e.get("construction"))
        seg = None
        if t == "line":
            (x1, y1), (x2, y2) = e["p1"], e["p2"]
            if cons:
                sm.CreateCenterLine(x1 * M, y1 * M, 0, x2 * M, y2 * M, 0)
                return
            seg = sm.CreateLine(x1 * M, y1 * M, 0, x2 * M, y2 * M, 0)
        elif t == "circle":
            cx, cy = e["center"]
            seg = sm.CreateCircleByRadius(cx * M, cy * M, 0, e["radius"] * M)
        elif t == "arc":
            (cx, cy), r = e["center"], e["radius"]
            a0, a1 = math.radians(e["start"]), math.radians(e["end"])
            seg = sm.CreateArc(cx * M, cy * M, 0,
                               (cx + r * math.cos(a0)) * M, (cy + r * math.sin(a0)) * M, 0,
                               (cx + r * math.cos(a1)) * M, (cy + r * math.sin(a1)) * M, 0,
                               1 if e.get("ccw", True) else -1)
        elif t == "point":
            x, y = e["p"]
            sm.CreatePoint(x * M, y * M, 0)
            return
        elif t == "spline":
            flat = []
            for x, y in e["points"]:
                flat += [x * M, y * M, 0.0]
            seg = sm.CreateSpline2(flat, True)
        if seg is None:
            raise BuildError(f"sketch entity {e} was not created")
        if cons:
            _cast(seg, "ISketchSegment").ConstructionGeometry = True

    # -- solid features ----------------------------------------------------------------------
    def _end_args(self, s: dict) -> tuple:
        """(Sd, T1, T2, D1, D2) for FeatureExtrusion3 / FeatureCut4."""
        end, end2 = s["end"], s["end2"]
        if end == "through_all":
            return True, _THROUGH_ALL_BOTH, 0, 0.01, 0.01
        t1 = _MID_PLANE if end == "mid_plane" else _BLIND
        if end2 == "none" or end == "mid_plane":
            return True, t1, 0, s["depth"] * M, 0.01
        t2 = _THROUGH_ALL if end2 == "through_all" else _BLIND
        return False, t1, t2, s["depth"] * M, s["depth2"] * M

    def extrude(self, s: dict) -> None:
        m = self.model
        sd, t1, t2, d1, d2 = self._end_args(s)
        if s["mode"] == "boss":
            m.ClearSelection2(True)
            _select(m, s["sketch"], "SKETCH")
            f = self.fm.FeatureExtrusion3(sd, False, s["flip"], t1, t2, d1, d2, False, False,
                                          False, False, 0, 0, False, False, False, False,
                                          bool(s.get("merge", True)), True, True, 0, 0, False)
            if f is None:
                raise BuildError(f"{s['name']}: boss extrude failed")
            _rename_last(m, s["name"])
            return
        # Chudware cuts run along the sketch normal (SolidWorks' reversed direction, Dir=True)
        # and, for an unflipped one-sided blind cut that removes nothing, try into the
        # material instead (document.cpp "BLIND-CUT DIRECTION FALLBACK"). Do the same here.
        before = _volume(m)
        simple = s["end"] == "blind" and s["end2"] == "none" and not s["flip"]
        for attempt, rev in enumerate((not s["flip"], s["flip"])):
            m.ClearSelection2(True)
            _select(m, s["sketch"], "SKETCH")
            f = self.fm.FeatureCut4(sd, False, rev, t1, t2, d1, d2, False, False, False, False,
                                    0, 0, False, False, False, False, False, True, True, True,
                                    True, False, 0, 0, False, False)
            if f is None:
                raise BuildError(f"{s['name']}: cut failed")
            if not simple or attempt or abs(_volume(m) - before) > 1e-9:
                break
            m.ClearSelection2(True)
            _select(m, f.Name, "BODYFEATURE")
            m.EditDelete()
        _rename_last(m, s["name"])

    def revolve(self, s: dict) -> None:
        m = self.model
        m.ClearSelection2(True)
        _select(m, s["sketch"], "SKETCH")
        ang = math.radians(abs(s["angle"]))
        f = self.fm.FeatureRevolve2(True, True, False, s["mode"] == "cut",
                                    bool(s["flip"]) ^ (s["angle"] < 0), False, 0, 0, ang, 0,
                                    False, False, 0, 0, 0, 0, 0, True, True, True)
        if f is None:
            raise BuildError(f"{s['name']}: revolve failed")
        _rename_last(m, s["name"])

    def _select_edges(self, s: dict) -> None:
        m = self.model
        m.ClearSelection2(True)
        missing = [p for p in s["edge_points"] if not _select_point(m, "EDGE", p, True)]
        if missing:
            raise BuildError(f"{s['name']}: no SolidWorks edge at {missing[:3]} mm")

    def fillet(self, s: dict) -> None:
        self._select_edges(s)
        if self.fm.FeatureFillet3(195, s["size"] * M, 0, 0, 0, 0, 0, None, None, None, None,
                                  None, None, None) is None:
            raise BuildError(f"{s['name']}: fillet failed")
        _rename_last(self.model, s["name"])

    def chamfer(self, s: dict) -> None:
        self._select_edges(s)
        if self.fm.InsertFeatureChamfer(4, _CHAMFER_ANGLE_DISTANCE, s["size"] * M,
                                        math.radians(45), 0, 0, 0, 0) is None:
            raise BuildError(f"{s['name']}: chamfer failed")
        _rename_last(self.model, s["name"])

    def shell(self, s: dict) -> None:
        m = self.model
        m.ClearSelection2(True)
        for face in s["remove"]:
            ok = (_select_point(m, "FACE", face["point"], True) if "point" in face
                  else _select_face_by_normal(m, face["normal"], True))
            if not ok:
                raise BuildError(f"{s['name']}: could not select shell face {face}")
        m.InsertFeatureShell(s["thickness"] * M, bool(s["outward"]))
        _rename_last(m, s["name"])

    def _select_seeds(self, seeds: list, mark: int) -> None:
        for nm in seeds:
            if not _select(self.model, nm, "BODYFEATURE", True, mark):
                raise BuildError(f"pattern seed {nm!r} not found in SolidWorks")

    def linear_pattern(self, s: dict) -> None:
        m = self.model
        m.ClearSelection2(True)
        d1 = s["dirs"][0]
        d2 = s["dirs"][1] if len(s["dirs"]) > 1 else None
        _select(m, d1["plane"], "PLANE", True, 1)
        if d2:
            _select(m, d2["plane"], "PLANE", True, 2)
        self._select_seeds(s["seeds"], 4)
        f = self.fm.FeatureLinearPattern4(
            d1["count"], d1["spacing"] * M, d2["count"] if d2 else 1,
            (d2["spacing"] if d2 else 0.0) * M, d1["flip"], bool(d2 and d2["flip"]), "", "",
            False, False, False, False, True, True, False, False, False, False, 0.0, 0.0)
        if f is None:
            raise BuildError(f"{s['name']}: linear pattern failed")
        _rename_last(m, s["name"])

    def circular_pattern(self, s: dict) -> None:
        m = self.model
        m.ClearSelection2(True)
        a, b = s["axis_planes"]
        _select(m, a, "PLANE", True)
        _select(m, b, "PLANE", True)
        m.InsertAxis2(True)
        axis = f"{s['name']}_Axis"
        _rename_last(m, axis)
        m.ClearSelection2(True)
        _select(m, axis, "AXIS", True, 1)
        self._select_seeds(s["seeds"], 4)
        spacing = math.radians(s["angle"]) if s["equal_spacing"] else \
            math.radians(s["angle"]) / max(1, s["count"] - 1)
        f = self.fm.FeatureCircularPattern4(s["count"], spacing, bool(s["reverse"]), "", False,
                                            bool(s["equal_spacing"]), False)
        if f is None:
            raise BuildError(f"{s['name']}: circular pattern failed")
        _rename_last(m, s["name"])

    def mirror(self, s: dict) -> None:
        m = self.model
        m.ClearSelection2(True)
        _select(m, s["plane"], "PLANE", False, 2)
        self._select_seeds(s["seeds"], 1)
        if self.fm.InsertMirrorFeature2(False, False, True, False, 0) is None:
            raise BuildError(f"{s['name']}: mirror failed")
        _rename_last(m, s["name"])


def verify(expected: dict, volume: float, com: list[float]) -> dict:
    out: dict[str, Any] = {"volume_mm3": volume, "com_sw_mm": com}
    ev, ec = expected.get("volume_mm3"), expected.get("com_sw_mm")
    if ev is None:
        out["verified"] = None
        return out
    dv = abs(volume - ev) / max(abs(ev), 1e-9)
    dc = max(abs(com[i] - ec[i]) for i in range(3)) if ec else 0.0
    out.update({"expected_volume_mm3": ev, "expected_com_sw_mm": ec,
                "volume_rel_error": dv, "com_error_mm": dc,
                "verified": dv <= VOLUME_RTOL and dc <= COM_TOL_MM})
    return out


def build(plan: dict, out_path: str, *, visible: bool = False, keep_open: bool = False) -> dict:
    """Build ``plan`` in SolidWorks and save it to ``out_path`` (.sldprt)."""
    app = _app(visible)
    template = app.GetUserPreferenceStringValue(8)   # swDefaultTemplatePart
    model = _cast(app.NewDocument(template, 0, 0, 0), "IModelDoc2")
    if model is None:
        raise SolidWorksUnavailable("SolidWorks could not create a new part (no part template?)")
    runner = _Runner(app, model)
    done: list[str] = []
    failure: Optional[str] = None
    try:
        for step in plan["steps"]:
            try:
                getattr(runner, step["op"])(step)
                done.append(step["name"])
            except Exception as e:  # noqa: BLE001
                failure = f"{step['op']} {step['name']!r}: {e}"
                break
        model.ForceRebuild3(False)
        vol, com = mass_props(model) if done else (0.0, [0.0, 0.0, 0.0])
        out = os.path.abspath(out_path)
        model.Extension.SaveAs(out, 0, 1, None, 0, 0)   # current version, silent
        result = {"ok": failure is None and os.path.isfile(out), "path": out,
                  "built": done, "failed_step": failure,
                  **verify(plan.get("expected") or {}, vol, com)}
        return result
    finally:
        if not keep_open:
            try:
                app.CloseDoc(model.GetTitle())
            except Exception:  # noqa: BLE001
                pass


def import_body(step_path: str, out_path: str, *, visible: bool = False) -> dict:
    """Body-only fallback: SolidWorks opens Chudware's exact STEP and saves it as a part
    (an imported body; SolidWorks FeatureWorks can recognize features on it)."""
    app = _app(visible)
    import pythoncom
    from win32com.client import VARIANT
    err = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    model = app.LoadFile4(os.path.abspath(step_path), "r", None, err)
    model = _cast(model, "IModelDoc2")
    if model is None:
        raise BuildError(f"SolidWorks could not open the STEP export (error {err.value})")
    try:
        vol, com = mass_props(model)
        out = os.path.abspath(out_path)
        model.Extension.SaveAs(out, 0, 1, None, 0, 0)
        return {"ok": os.path.isfile(out), "path": out, "volume_mm3": vol, "com_sw_mm": com}
    finally:
        try:
            app.CloseDoc(model.GetTitle())
        except Exception:  # noqa: BLE001
            pass
