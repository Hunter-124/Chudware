"""Write a SolidWorks build plan (:mod:`services.sw_export.plan`) as a SolidWorks VBA macro.

A teammate runs the macro (Tools > Macro > Run, file type "SW VBA Macros (*.swb)") in their own
SolidWorks. SolidWorks then builds the part feature by feature and saves ``<name>.SLDPRT`` next to
the macro, in THAT SolidWorks release, so the file opens for everyone on that version even when it
is older than the one Chudware could drive. At the end the macro compares the part's volume and
centre of mass with Chudware's and reports the result.

The calls and argument lists match :mod:`services.sw_export.com_build` one for one; the API
methods used exist since SolidWorks 2014.
"""

from __future__ import annotations

import math

from services.sw_export.com_build import COM_TOL_MM, VOLUME_RTOL

M = 0.001


def _n(x: float) -> str:
    """A VBA Double literal: 15 significant digits, round-off noise below 1e-12 as 0."""
    x = float(x)
    if abs(x) < 1e-12:
        return "0"
    return format(x, ".15g").replace("e", "E")


def _s(text: str) -> str:
    return '"' + str(text).replace('"', '""') + '"'


def _b(v) -> str:
    return "True" if v else "False"


_HEADER = '''\' Chudware -> SolidWorks build macro for "{name}".
' Run in SolidWorks: Tools > Macro > Run, then choose this file. SolidWorks rebuilds the part
' feature by feature and saves {name}.SLDPRT beside this macro, in your SolidWorks version.
Dim swApp As Object
Dim Part As Object
Dim FM As Object
Dim SM As Object
Dim boolstatus As Boolean
Dim errors As Long

Function Sel(nm As String, kind As String, append As Boolean, mark As Long) As Boolean
    Sel = Part.Extension.SelectByID2(nm, kind, 0, 0, 0, append, mark, Nothing, 0)
End Function

Function SelAt(kind As String, x As Double, y As Double, z As Double, append As Boolean, mark As Long) As Boolean
    SelAt = Part.Extension.SelectByID2("", kind, x, y, z, append, mark, Nothing, 0)
End Function

Function Vol() As Double
    On Error Resume Next
    Vol = 0
    Vol = Part.Extension.CreateMassProperty().Volume
End Function

Sub RenameLast(nm As String)
    On Error Resume Next
    Part.FeatureByPositionReverse(0).Name = nm
End Sub

Function SelFaceByNormal(nx As Double, ny As Double, nz As Double, append As Boolean) As Boolean
    Dim bodies As Variant, b As Variant, faces As Variant, f As Variant
    Dim best As Object, bestH As Double, n As Variant, bx As Variant, h As Double
    bestH = -1E+300
    bodies = Part.GetBodies2(0, False)
    If IsEmpty(bodies) Then Exit Function
    For Each b In bodies
        faces = b.GetFaces()
        For Each f In faces
            n = f.Normal
            If n(0) * nx + n(1) * ny + n(2) * nz > 0.999999 Then
                bx = f.GetBox()
                h = ((bx(0) + bx(3)) * nx + (bx(1) + bx(4)) * ny + (bx(2) + bx(5)) * nz) / 2
                If h > bestH Then
                    bestH = h
                    Set best = f
                End If
            End If
        Next
    Next
    If Not best Is Nothing Then SelFaceByNormal = best.Select4(append, Nothing)
End Function

Sub Fail(msg As String)
    MsgBox "Chudware export stopped: " & msg, vbCritical
    End
End Sub

Sub main()
    Dim before As Double
    Set swApp = Application.SldWorks
    Set Part = swApp.NewDocument(swApp.GetUserPreferenceStringValue(8), 0, 0, 0)
    If Part Is Nothing Then Fail "SolidWorks could not create a part (no default part template)"
    Set FM = Part.FeatureManager
    Set SM = Part.SketchManager
'''

_FOOTER = '''
    Part.ForceRebuild3 False
    Dim mp As Object, c As Variant, v As Double, dv As Double, dc As Double, report As String
    Set mp = Part.Extension.CreateMassProperty()
    v = mp.Volume * 1E+9
    c = mp.CenterOfMass
    report = "Volume " & Format(v, "0.000") & " mm^3 (Chudware {ev})"
    If {has_expected} Then
        dv = Abs(v - {ev}) / {ev_den}
        dc = Abs(c(0) * 1000 - ({cx}))
        If Abs(c(1) * 1000 - ({cy})) > dc Then dc = Abs(c(1) * 1000 - ({cy}))
        If Abs(c(2) * 1000 - ({cz})) > dc Then dc = Abs(c(2) * 1000 - ({cz}))
        If dv <= {vtol} And dc <= {ctol} Then
            report = "MATCHES the Chudware part. " & report
        Else
            report = "DOES NOT MATCH the Chudware part (volume error " & Format(dv * 100, "0.0000") & "%, centre of mass off by " & Format(dc, "0.000") & " mm). " & report
        End If
    End If
    Dim outPath As String, warnings As Long
    outPath = swApp.GetCurrentMacroPathFolder() & "\\{file}"
    boolstatus = Part.Extension.SaveAs(outPath, 0, 1, Nothing, errors, warnings)
    MsgBox report & vbCrLf & "Saved " & outPath, vbInformation
End Sub
'''


class _Writer:
    def __init__(self):
        self.lines: list[str] = []

    def w(self, line: str = "") -> None:
        self.lines.append("    " + line if line else "")

    def sketch(self, s: dict) -> None:
        p = s["plane"]
        self.w(f"' --- {s['name']}")
        self.w("Part.ClearSelection2 True")
        if p["kind"] == "principal":
            self.w(f"If Not Sel({_s(p['plane'])}, \"PLANE\", False, 0) Then Fail {_s(s['name'] + ': plane')}")
        elif p["kind"] == "offset":
            flags = 8 | (256 if p["offset"] < 0 else 0)
            self.w(f"boolstatus = Sel({_s(p['base'])}, \"PLANE\", False, 0)")
            self.w(f"If FM.InsertRefPlane({flags}, {_n(abs(p['offset']) * M)}, 0, 0, 0, 0) Is Nothing Then Fail {_s(s['name'] + ': offset plane')}")
            self.w(f"RenameLast {_s(p['name'])}")
            self.w("Part.ClearSelection2 True")
            self.w(f"If Not Sel({_s(p['name'])}, \"PLANE\", False, 0) Then Fail {_s(s['name'] + ': plane')}")
        else:
            nx, ny, nz = p["normal"]
            self.w(f"If Not SelFaceByNormal({_n(nx)}, {_n(ny)}, {_n(nz)}, False) Then Fail {_s(s['name'] + ': face')}")
        self.w("SM.InsertSketch True")
        self.w("SM.AddToDB = True")
        for e in s["entities"]:
            self.entity(e)
        self.w("SM.AddToDB = False")
        self.w("SM.InsertSketch True")
        self.w(f"RenameLast {_s(s['name'])}")

    def entity(self, e: dict) -> None:
        t, cons = e["type"], bool(e.get("construction"))
        seg = None
        if t == "line":
            (x1, y1), (x2, y2) = e["p1"], e["p2"]
            args = f"{_n(x1 * M)}, {_n(y1 * M)}, 0, {_n(x2 * M)}, {_n(y2 * M)}, 0"
            if cons:
                self.w(f"SM.CreateCenterLine {args}")
                return
            seg = f"SM.CreateLine({args})"
        elif t == "circle":
            cx, cy = e["center"]
            seg = f"SM.CreateCircleByRadius({_n(cx * M)}, {_n(cy * M)}, 0, {_n(e['radius'] * M)})"
        elif t == "arc":
            (cx, cy), r = e["center"], e["radius"]
            a0, a1 = math.radians(e["start"]), math.radians(e["end"])
            seg = (f"SM.CreateArc({_n(cx * M)}, {_n(cy * M)}, 0, "
                   f"{_n((cx + r * math.cos(a0)) * M)}, {_n((cy + r * math.sin(a0)) * M)}, 0, "
                   f"{_n((cx + r * math.cos(a1)) * M)}, {_n((cy + r * math.sin(a1)) * M)}, 0, "
                   f"{1 if e.get('ccw', True) else -1})")
        elif t == "point":
            x, y = e["p"]
            self.w(f"SM.CreatePoint {_n(x * M)}, {_n(y * M)}, 0")
            return
        elif t == "spline":
            flat = []
            for x, y in e["points"]:
                flat += [x * M, y * M, 0.0]
            self.w(f"ReDim pts({len(flat) - 1}) As Double")
            for i, v in enumerate(flat):
                self.w(f"pts({i}) = {_n(v)}")
            seg = "SM.CreateSpline2((pts), True)"
        if cons:
            self.w(f"{seg}.ConstructionGeometry = True")
        else:
            self.w(f"If {seg} Is Nothing Then Fail \"sketch entity\"")

    def _end_args(self, s: dict) -> tuple:
        end, end2 = s["end"], s["end2"]
        if end == "through_all":
            return True, 9, 0, 0.01, 0.01
        t1 = 6 if end == "mid_plane" else 0
        if end2 == "none" or end == "mid_plane":
            return True, t1, 0, s["depth"] * M, 0.01
        return False, t1, 1 if end2 == "through_all" else 0, s["depth"] * M, s["depth2"] * M

    def extrude(self, s: dict) -> None:
        sd, t1, t2, d1, d2 = self._end_args(s)
        self.w(f"' --- {s['name']}")
        self.w("Part.ClearSelection2 True")
        self.w(f"boolstatus = Sel({_s(s['sketch'])}, \"SKETCH\", False, 0)")
        head = f"{_b(sd)}, False"
        tail = f"{t1}, {t2}, {_n(d1)}, {_n(d2)}, False, False, False, False, 0, 0, False, False, False, False"
        if s["mode"] == "boss":
            self.w(f"If FM.FeatureExtrusion3({head}, {_b(s['flip'])}, {tail}, "
                   f"{_b(s.get('merge', True))}, True, True, 0, 0, False) Is Nothing Then Fail {_s(s['name'])}")
            self.w(f"RenameLast {_s(s['name'])}")
            return
        cut = (f"FM.FeatureCut4({head}, %s, {tail}, False, True, True, True, True, False, 0, 0, "
               "False, False)")
        self.w("before = Vol()")
        self.w(f"If {cut % _b(not s['flip'])} Is Nothing Then Fail {_s(s['name'])}")
        if s["end"] == "blind" and s["end2"] == "none" and not s["flip"]:
            # Chudware's blind-cut fallback: into the material when the first side cut nothing.
            self.w("If Abs(Vol() - before) < 1E-18 Then")
            self.w("    Part.ClearSelection2 True")
            self.w("    boolstatus = Sel(Part.FeatureByPositionReverse(0).Name, \"BODYFEATURE\", False, 0)")
            self.w("    Part.EditDelete")
            self.w(f"    boolstatus = Sel({_s(s['sketch'])}, \"SKETCH\", False, 0)")
            self.w(f"    If {cut % 'False'} Is Nothing Then Fail {_s(s['name'])}")
            self.w("End If")
        self.w(f"RenameLast {_s(s['name'])}")

    def revolve(self, s: dict) -> None:
        self.w(f"' --- {s['name']}")
        self.w("Part.ClearSelection2 True")
        self.w(f"boolstatus = Sel({_s(s['sketch'])}, \"SKETCH\", False, 0)")
        rev = bool(s["flip"]) ^ (s["angle"] < 0)
        self.w(f"If FM.FeatureRevolve2(True, True, False, {_b(s['mode'] == 'cut')}, {_b(rev)}, False, 0, 0, "
               f"{_n(math.radians(abs(s['angle'])))}, 0, False, False, 0, 0, 0, 0, 0, True, True, True) "
               f"Is Nothing Then Fail {_s(s['name'])}")
        self.w(f"RenameLast {_s(s['name'])}")

    def _edges(self, s: dict) -> None:
        self.w("Part.ClearSelection2 True")
        for x, y, z in s["edge_points"]:
            self.w(f"If Not SelAt(\"EDGE\", {_n(x * M)}, {_n(y * M)}, {_n(z * M)}, True, 0) Then Fail {_s(s['name'] + ': edge')}")

    def fillet(self, s: dict) -> None:
        self.w(f"' --- {s['name']}")
        self._edges(s)
        self.w(f"If FM.FeatureFillet3(195, {_n(s['size'] * M)}, 0, 0, 0, 0, 0, Nothing, Nothing, Nothing, Nothing, Nothing, Nothing, Nothing) Is Nothing Then Fail {_s(s['name'])}")
        self.w(f"RenameLast {_s(s['name'])}")

    def chamfer(self, s: dict) -> None:
        self.w(f"' --- {s['name']}")
        self._edges(s)
        self.w(f"If FM.InsertFeatureChamfer(4, 1, {_n(s['size'] * M)}, {_n(math.radians(45))}, 0, 0, 0, 0) Is Nothing Then Fail {_s(s['name'])}")
        self.w(f"RenameLast {_s(s['name'])}")

    def shell(self, s: dict) -> None:
        self.w(f"' --- {s['name']}")
        self.w("Part.ClearSelection2 True")
        for face in s["remove"]:
            if "point" in face:
                x, y, z = face["point"]
                self.w(f"If Not SelAt(\"FACE\", {_n(x * M)}, {_n(y * M)}, {_n(z * M)}, True, 0) Then Fail {_s(s['name'] + ': face')}")
            else:
                nx, ny, nz = face["normal"]
                self.w(f"If Not SelFaceByNormal({_n(nx)}, {_n(ny)}, {_n(nz)}, True) Then Fail {_s(s['name'] + ': face')}")
        self.w(f"Part.InsertFeatureShell {_n(s['thickness'] * M)}, {_b(s['outward'])}")
        self.w(f"RenameLast {_s(s['name'])}")

    def _seeds(self, seeds: list, mark: int) -> None:
        for nm in seeds:
            self.w(f"If Not Sel({_s(nm)}, \"BODYFEATURE\", True, {mark}) Then Fail {_s('seed ' + nm)}")

    def linear_pattern(self, s: dict) -> None:
        d1 = s["dirs"][0]
        d2 = s["dirs"][1] if len(s["dirs"]) > 1 else None
        self.w(f"' --- {s['name']}")
        self.w("Part.ClearSelection2 True")
        self.w(f"boolstatus = Sel({_s(d1['plane'])}, \"PLANE\", True, 1)")
        if d2:
            self.w(f"boolstatus = Sel({_s(d2['plane'])}, \"PLANE\", True, 2)")
        self._seeds(s["seeds"], 4)
        n2 = d2["count"] if d2 else 1
        s2 = (d2["spacing"] if d2 else 0.0) * M
        self.w(f"If FM.FeatureLinearPattern4({d1['count']}, {_n(d1['spacing'] * M)}, {n2}, {_n(s2)}, "
               f"{_b(d1['flip'])}, {_b(d2 and d2['flip'])}, \"\", \"\", False, False, False, False, "
               f"True, True, False, False, False, False, 0, 0) Is Nothing Then Fail {_s(s['name'])}")
        self.w(f"RenameLast {_s(s['name'])}")

    def circular_pattern(self, s: dict) -> None:
        a, b = s["axis_planes"]
        axis = f"{s['name']}_Axis"
        spacing = math.radians(s["angle"]) if s["equal_spacing"] else \
            math.radians(s["angle"]) / max(1, s["count"] - 1)
        self.w(f"' --- {s['name']}")
        self.w("Part.ClearSelection2 True")
        self.w(f"boolstatus = Sel({_s(a)}, \"PLANE\", True, 0)")
        self.w(f"boolstatus = Sel({_s(b)}, \"PLANE\", True, 0)")
        self.w("boolstatus = Part.InsertAxis2(True)")
        self.w(f"RenameLast {_s(axis)}")
        self.w("Part.ClearSelection2 True")
        self.w(f"boolstatus = Sel({_s(axis)}, \"AXIS\", True, 1)")
        self._seeds(s["seeds"], 4)
        self.w(f"If FM.FeatureCircularPattern4({s['count']}, {_n(spacing)}, {_b(s['reverse'])}, \"\", "
               f"False, {_b(s['equal_spacing'])}, False) Is Nothing Then Fail {_s(s['name'])}")
        self.w(f"RenameLast {_s(s['name'])}")

    def mirror(self, s: dict) -> None:
        self.w(f"' --- {s['name']}")
        self.w("Part.ClearSelection2 True")
        self.w(f"boolstatus = Sel({_s(s['plane'])}, \"PLANE\", False, 2)")
        self._seeds(s["seeds"], 1)
        self.w(f"If FM.InsertMirrorFeature2(False, False, True, False, 0) Is Nothing Then Fail {_s(s['name'])}")
        self.w(f"RenameLast {_s(s['name'])}")


_BODY_MACRO = '''\' Chudware -> SolidWorks body import for "{name}" (no feature tree: see the export reply).
' Keep {step} beside this macro, then run it: Tools > Macro > Run.
Dim swApp As Object
Dim Part As Object
Sub main()
    Dim errors As Long, warnings As Long, folder As String
    Set swApp = Application.SldWorks
    folder = swApp.GetCurrentMacroPathFolder()
    Set Part = swApp.LoadFile4(folder & "\\{step}", "r", Nothing, errors)
    If Part Is Nothing Then
        MsgBox "Could not open " & folder & "\\{step} (error " & errors & ")", vbCritical
        Exit Sub
    End If
    Part.Extension.SaveAs folder & "\\{file}", 0, 1, Nothing, errors, warnings
    MsgBox "Saved " & folder & "\\{file}", vbInformation
End Sub
'''


def write_body_macro(name: str, step_path: str, path: str) -> str:
    """A macro that opens the exact STEP beside it and saves it as a SolidWorks part."""
    import os
    text = _BODY_MACRO.format(name=name.replace('"', "'"), step=os.path.basename(step_path),
                              file=f"{name}.SLDPRT".replace('"', "'"))
    _write(path, text)
    return path


def _write(path: str, text: str) -> None:
    # The SolidWorks VBA editor reads macros in the ANSI code page.
    with open(path, "w", encoding="cp1252", errors="replace", newline="\r\n") as fh:
        fh.write(text)


def write_macro(plan: dict, path: str) -> str:
    """Write ``plan`` as a .swb macro to ``path``; returns the path."""
    name = plan["name"]
    w = _Writer()
    if any(e["type"] == "spline" for s in plan["steps"] if s["op"] == "sketch"
           for e in s["entities"]):
        w.w("Dim pts() As Double")
    for step in plan["steps"]:
        getattr(w, step["op"])(step)
    exp = plan.get("expected") or {}
    ev = exp.get("volume_mm3")
    com = exp.get("com_sw_mm") or [0.0, 0.0, 0.0]
    text = (_HEADER.format(name=name.replace('"', "'")) + "\n".join(w.lines) +
            _FOOTER.format(ev=_n(ev or 0.0), ev_den=_n(max(abs(ev or 0.0), 1e-9)),
                           has_expected=_b(ev is not None), cx=_n(com[0]), cy=_n(com[1]),
                           cz=_n(com[2]), vtol=_n(VOLUME_RTOL), ctol=_n(COM_TOL_MM),
                           file=f"{name}.SLDPRT".replace('"', "'")))
    _write(path, text)
    return path
