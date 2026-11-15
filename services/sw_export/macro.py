"""Write a SolidWorks build plan (:mod:`services.sw_export.plan`) as a SolidWorks VBA macro.

The macro is the ONE implementation of the SolidWorks rebuild. A teammate runs it (Tools > Macro >
Run, file type "SW VBA Macros (*.swb)") in their own SolidWorks; Chudware runs the same file in an
installed SolidWorks over COM (:func:`services.sw_export.com_build.run_macro`). SolidWorks builds
the part feature by feature and saves ``<stem>.SLDPRT`` next to the macro, in THAT SolidWorks
release, so the file opens for everyone on that version even when it is older than Chudware's.

At the end the macro compares the part with Chudware's (volume, centre of mass, body count and
the centroidal inertia diagonal) and writes ``<stem>.report.txt`` beside itself (key=value lines);
it shows the result in a message box unless ``<stem>.silent`` exists (the COM runner's marker).

Bodies are named after the Chudware feature that owns them (``Track``), so a cut subtracts from
the ONE body the engine cuts, and transforms and booleans address bodies the way the engine does.
"""

from __future__ import annotations

import math
import os

from services.sw_export.plan import arc_points

M = 0.001
VOLUME_RTOL = 1e-5        # relative volume tolerance of the final comparison
COM_TOL_MM = 1e-2         # absolute centre-of-mass tolerance (mm)
INERTIA_RTOL = 1e-4       # inertia diagonal, relative to its largest entry
# "close": tolerances x CLOSE_FACTOR. Chudware and SolidWorks booleans can differ by ~1e-4 of
# the volume on oblique intersections (FRAME-520: 64 mm^3 of 820 000); that keeps the tree.
CLOSE_FACTOR = 100.0


_CHUNK = 150                  # sketch entities per VBA procedure
_SPLIT = "'--split--"


def _n(x: float) -> str:
    """A VBA Double literal: 15 significant digits, round-off noise below 1e-12 as 0."""
    x = float(x)
    if abs(x) < 1e-12:
        return "0"
    s = format(x, ".15g").replace("e", "E")
    return s if any(c in s for c in ".E") else s + "#"


def _m(x: float) -> str:
    return _n(x * M)


def _s(text) -> str:
    return '"' + str(text).replace('"', '""') + '"'


def _b(v) -> str:
    return "True" if v else "False"


_LIBRARY = r'''' Chudware -> SolidWorks build macro for "{name}".
' Run in SolidWorks: Tools > Macro > Run, then choose this file. SolidWorks rebuilds the part
' feature by feature, saves {file} beside this macro in your SolidWorks version, and reports
' whether it matches the Chudware part.
Dim swApp As Object
Dim Part As Object
Dim FM As Object
Dim SM As Object
Dim MU As Object
Dim XF As Object
Dim seg As Object
Dim p1 As Variant, p2 As Variant, p3 As Variant
Dim boolstatus As Boolean
Dim errors As Long, warnings As Long
Dim Own As Object
Dim Planes As Object
Dim Cur As String
Dim Stage As String
Dim Built As Long
Dim Folder As String
Dim Silent As Boolean
Dim Fallback As String
Dim Notes As String

Function Num(x As Double) As String
    Num = Trim(Str(x))
End Function

Sub Report(ok As Boolean, msg As String)
    Dim fh As Integer
    fh = FreeFile
    Open Folder & "\{stem}.report.txt" For Output As #fh
    Print #fh, "ok=" & IIf(ok, "1", "0")
    Print #fh, "stage=" & Stage
    Print #fh, "built=" & Built
    Print #fh, msg
    Close #fh
End Sub

' Stop the build: raise to main's handler. (A VBA `End` here makes RunMacro2 run the macro
' again, and that second run leaves SolidWorks unable to sketch until it restarts.)
Sub Fail(msg As String)
    Err.Raise vbObjectError + 513, "Chudware", msg
End Sub

Sub Abort(msg As String)
    Report False, "error=" & msg
    If Not Silent Then MsgBox "Chudware export stopped at " & Stage & ": " & msg, vbCritical
    If Silent And Not Part Is Nothing Then swApp.CloseDoc Part.GetTitle()
End Sub

Function Sel(nm As String, kind As String, append As Boolean, mark As Long) As Boolean
    Sel = Part.Extension.SelectByID2(nm, kind, 0, 0, 0, append, mark, Nothing, 0)
End Function

Sub SelSketch(nm As String)
    Part.ClearSelection2 True
    If Not Sel(nm, "SKETCH", False, 0) Then Fail "sketch " & nm & " not found"
End Sub

Sub RenameLast(nm As String)
    On Error Resume Next
    Part.FeatureByPositionReverse(0).Name = nm
End Sub

Sub DeleteFeature(nm As String)
    Part.ClearSelection2 True
    If Sel(nm, "BODYFEATURE", False, 0) Then Part.EditDelete
End Sub

Function Vol() As Double
    On Error Resume Next
    Vol = 0
    Vol = Part.Extension.CreateMassProperty().Volume
End Function

Function Bodies() As Variant
    Bodies = Part.GetBodies2(0, False)
End Function

Function BodyCount() As Long
    Dim v As Variant
    v = Bodies()
    If IsEmpty(v) Then BodyCount = 0 Else BodyCount = UBound(v) - LBound(v) + 1
End Function

Function BodyByName(nm As String) As Object
    Dim v As Variant, b As Variant
    v = Bodies()
    If IsEmpty(v) Then Exit Function
    For Each b In v
        If b.Name = nm Then
            Set BodyByName = b
            Exit Function
        End If
    Next
End Function

' Body name -> (volume, centre of mass) before a feature, to see what the feature changed.
Function TakeSnap() As Object
    Dim d As Object, v As Variant, b As Variant, mp As Variant
    Set d = CreateObject("Scripting.Dictionary")
    v = Bodies()
    If Not IsEmpty(v) Then
        For Each b In v
            mp = b.GetMassProperties(1#)
            d(b.Name) = Array(mp(3), mp(0), mp(1), mp(2))
        Next
    End If
    Set TakeSnap = d
End Function

Function Changed(ByVal b As Object, ByVal s As Variant) As Boolean
    Dim mp As Variant
    mp = b.GetMassProperties(1#)
    Changed = Abs(mp(3) - s(0)) > 1E-09 * Abs(s(0)) + 1E-18 Or Abs(mp(0) - s(1)) > 1E-09 _
        Or Abs(mp(1) - s(2)) > 1E-09 Or Abs(mp(2) - s(3)) > 1E-09
End Function

Function NewBody(snap As Object) As Object
    Dim v As Variant, b As Variant
    v = Bodies()
    If IsEmpty(v) Then Exit Function
    For Each b In v
        If Not snap.Exists(b.Name) Then
            Set NewBody = b
            Exit Function
        End If
    Next
End Function

Function NewBodies(snap As Object) As Variant
    Dim v As Variant, b As Variant, hits As New Collection, out() As Object, i As Long
    v = Bodies()
    If IsEmpty(v) Then Exit Function
    For Each b In v
        If Not snap.Exists(b.Name) Then hits.Add b
    Next
    If hits.Count = 0 Then Exit Function
    ReDim out(hits.Count - 1)
    For i = 1 To hits.Count
        Set out(i - 1) = hits(i)
    Next
    NewBodies = out
End Function

Sub Repoint(old As String, nm As String)
    Dim k As Variant
    For Each k In Own.Keys
        If Own(k) = old Then Own(k) = nm
    Next
    If Cur = old Then Cur = nm
End Sub

' Name the bodies the last feature created or changed after the Chudware feature `fid`, and
' point every Chudware body key at the body it now lives in (the engine's merge/consume rule).
Sub Track(fid As String, before As Object)
    Dim v As Variant, b As Variant, hit As New Collection, gone As Object, k As Variant
    Dim nm As String, i As Long, old As String
    Set gone = CreateObject("Scripting.Dictionary")
    For Each k In before.Keys
        gone(k) = True
    Next
    v = Bodies()
    If Not IsEmpty(v) Then
        For Each b In v
            If before.Exists(b.Name) Then
                gone.Remove b.Name
                If Changed(b, before(b.Name)) Then hit.Add b
            Else
                hit.Add b
            End If
        Next
    End If
    If hit.Count = 0 Then
        If Cur <> "" Then Own(fid) = Cur
        Exit Sub
    End If
    For i = 1 To hit.Count
        If i = 1 Then nm = fid Else nm = fid & "#" & (i - 1)
        old = hit(i).Name
        If old <> nm Then
            hit(i).Name = nm
            Repoint old, nm
        End If
    Next
    For Each k In gone.Keys
        Repoint CStr(k), fid
    Next
    Own(fid) = fid
    Cur = fid
End Sub

Function Target(key As String) As Object
    Dim nm As String
    nm = Cur
    If key <> "" Then
        If Not Own.Exists(key) Then Fail "body " & key & " was not built"
        nm = Own(key)
    End If
    Set Target = BodyByName(nm)
    If Target Is Nothing Then Fail "body " & nm & " (Chudware " & key & ") not found"
End Function

' The EDGE or FACE through (x, y, z) metres. A SelectByID2 pick at coordinates is a view pick
' and misses in a hidden window, so search the bodies geometrically.
Function SelNear(kind As String, x As Double, y As Double, z As Double, append As Boolean, mark As Long) As Boolean
    Dim v As Variant, b As Variant, ents As Variant, e As Variant, q As Variant
    Dim d As Double, bestD As Double, best As Object
    bestD = 1E-12
    v = Bodies()
    If IsEmpty(v) Then Exit Function
    For Each b In v
        If kind = "EDGE" Then ents = b.GetEdges() Else ents = b.GetFaces()
        If Not IsEmpty(ents) Then
            For Each e In ents
                q = Empty
                On Error Resume Next   ' some edge kinds refuse a closest-point query
                q = e.GetClosestPointOn(x, y, z)
                On Error GoTo 0
                If Not IsEmpty(q) Then
                    ' squared distance: SolidWorks VBA raises error 5 on Sqr(0)
                    d = (q(0) - x) * (q(0) - x) + (q(1) - y) * (q(1) - y) + (q(2) - z) * (q(2) - z)
                    If d < bestD Then
                        bestD = d
                        Set best = e
                    End If
                End If
            Next
        End If
    Next
    If best Is Nothing Then Exit Function
    SelNear = best.Select2(append, mark)
End Function

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

' Select a plane through (ox, oy, oz) with normal (nx, ny, nz), metres: a default plane, an
' offset of one (reused), or a plane normal to a 3D-sketch line for any other direction.
Sub PlaneFor(nm As String, ox As Double, oy As Double, oz As Double, nx As Double, ny As Double, nz As Double)
    Dim base As String, d As Double, key As String, f As Object, sd As Object
    Part.ClearSelection2 True
    If Abs(Abs(nx) - 1) < 1E-09 Then
        base = "Right Plane"
        d = ox
    ElseIf Abs(Abs(ny) - 1) < 1E-09 Then
        base = "Top Plane"
        d = oy
    ElseIf Abs(Abs(nz) - 1) < 1E-09 Then
        base = "Front Plane"
        d = oz
    End If
    If base <> "" Then
        If Abs(d) < 1E-12 Then
            If Not Sel(base, "PLANE", False, 0) Then Fail base
            Exit Sub
        End If
        key = base & "|" & CStr(Round(d * 1000000000#))
        If Not Planes.Exists(key) Then
            boolstatus = Sel(base, "PLANE", False, 0)
            If d < 0 Then
                Set f = FM.InsertRefPlane(8 Or 256, -d, 0, 0, 0, 0)
            Else
                Set f = FM.InsertRefPlane(8, d, 0, 0, 0, 0)
            End If
            If f Is Nothing Then Fail "offset plane " & nm
            RenameLast nm
            Planes(key) = Part.FeatureByPositionReverse(0).Name
        End If
        Part.ClearSelection2 True
        If Not Sel(Planes(key), "PLANE", False, 0) Then Fail "plane " & Planes(key)
        Exit Sub
    End If
    SM.AddToDB = True
    SM.Insert3DSketch True
    Set seg = SM.CreateLine(ox, oy, oz, ox + nx * 0.01, oy + ny * 0.01, oz + nz * 0.01)
    SM.Insert3DSketch True
    SM.AddToDB = False
    If seg Is Nothing Then Fail "plane normal line " & nm
    RenameLast nm & "_Normal"
    Part.ClearSelection2 True
    Set sd = Part.SelectionManager.CreateSelectData
    sd.Mark = 0
    boolstatus = seg.Select4(False, sd)
    sd.Mark = 1
    boolstatus = seg.GetStartPoint2().Select4(True, sd)
    Set f = FM.InsertRefPlane(2, 0, 4, 0, 0, 0)
    If f Is Nothing Then Fail "plane normal to " & nm
    RenameLast nm
    Part.ClearSelection2 True
    If Not Sel(Part.FeatureByPositionReverse(0).Name, "PLANE", False, 0) Then Fail "plane " & nm
End Sub

' A reference axis through (ox, oy, oz) along (dx, dy, dz), metres, from a 3D-sketch line.
Function MakeAxis(nm As String, ox As Double, oy As Double, oz As Double, dx As Double, dy As Double, dz As Double) As String
    SM.AddToDB = True
    SM.Insert3DSketch True
    Set seg = SM.CreateLine(ox, oy, oz, ox + dx * 0.01, oy + dy * 0.01, oz + dz * 0.01)
    SM.Insert3DSketch True
    SM.AddToDB = False
    If seg Is Nothing Then Fail "axis line " & nm
    RenameLast nm & "_Line"
    Part.ClearSelection2 True
    boolstatus = seg.Select4(False, Nothing)
    boolstatus = Part.InsertAxis2(True)
    RenameLast nm
    MakeAxis = Part.FeatureByPositionReverse(0).Name
End Function

' A 3D-sketch line from the origin along (dx, dy, dz): a pattern direction (start -> end).
Function DirLine(nm As String, dx As Double, dy As Double, dz As Double) As Object
    SM.AddToDB = True
    SM.Insert3DSketch True
    Set DirLine = SM.CreateLine(0, 0, 0, dx * 0.01, dy * 0.01, dz * 0.01)
    SM.Insert3DSketch True
    SM.AddToDB = False
    If DirLine Is Nothing Then Fail "direction line " & nm
    RenameLast nm
End Function

' Begin a sketch on the selected plane; SP maps model points (metres) into it.
Sub BeginSketch()
    SM.InsertSketch True
    Set XF = SM.ActiveSketch.ModelToSketchTransform
    SM.AddToDB = True
End Sub

Sub EndSketch(nm As String)
    SM.AddToDB = False
    SM.InsertSketch True
    RenameLast nm
End Sub

Function SP(x As Double, y As Double, z As Double) As Variant
    Dim a(2) As Double, pt As Object
    a(0) = x
    a(1) = y
    a(2) = z
    ' (a): a late-bound call must get the array by value, or SolidWorks reads zeros.
    Set pt = MU.CreatePoint((a))
    Set pt = pt.MultiplyTransform(XF)
    SP = pt.ArrayData
End Function

Sub Note(msg As String)
    If Notes <> "" Then Notes = Notes & " | "
    Notes = Notes & msg
End Sub

Sub Need(ok As Boolean, what As String)
    If Not ok Then Fail what
End Sub

Sub Construction()
    If seg Is Nothing Then Fail "sketch entity"
    seg.ConstructionGeometry = True
End Sub

' Cut by a tool body + Combine: the engine cuts ONE body, a SolidWorks cut every body it meets.
Sub CutTool(sk As String, nm As String, isRev As Boolean, sd As Boolean, flip As Boolean, t1 As Long, t2 As Long, d1 As Double, d2 As Double, ang As Double, key As String, simple As Boolean)
    Dim tname As String, attempt As Long, s0 As Object, ft As Object, fc As Object
    Dim tools As Variant, v0 As Double, dirflip As Boolean
    tname = Target(key).Name
    v0 = Vol()
    For attempt = 0 To 1
        dirflip = flip Xor (attempt = 1)
        Set s0 = TakeSnap()
        SelSketch sk
        If isRev Then
            Set ft = FM.FeatureRevolve2(True, True, False, False, dirflip, False, 0, 0, ang, 0, False, False, 0, 0, 0, 0, 0, False, True, True)
        Else
            Set ft = FM.FeatureExtrusion3(sd, False, dirflip, t1, t2, d1, d2, False, False, False, False, 0, 0, False, False, False, False, False, True, True, 0, 0, False)
        End If
        If ft Is Nothing Then Fail "cut tool"
        RenameLast nm & "_Tool"
        ' every new body: a sketch of several closed contours makes one tool body each
        tools = NewBodies(s0)
        If IsEmpty(tools) Then Fail "cut tool made no body"
        Set fc = FM.InsertCombineFeature(15902, BodyByName(tname), tools)
        If Not fc Is Nothing Then
            If (Not simple) Or attempt = 1 Or Abs(Vol() - v0) > 1E-15 Then
                RenameLast nm
                Exit Sub
            End If
            DeleteFeature Part.FeatureByPositionReverse(0).Name
        End If
        DeleteFeature nm & "_Tool"
        If Not simple Then Exit For
    Next
    ' Like Chudware, a cut that removes nothing is a warning, not a failure.
    Note "cut " & nm & " removes nothing from body " & tname
End Sub

' Chudware fuses a boss into EVERY body it touches; SolidWorks leaves a body that only shares
' a face (or a tangent cylinder) with the new feature separate. Fuse those too: a body whose
' temporary union with the feature's body is one solid touches it.
Sub MergeTouching(fid As String, nm As String, before As Object)
    Dim h As Object, v As Variant, b As Variant, hb As Variant, bb As Variant, res As Variant
    Dim e As Long, hits As New Collection, tools() As Object, i As Long, f As Object
    Set h = BodyByName(fid)
    If h Is Nothing Then Exit Sub
    hb = h.GetBodyBox()
    v = Bodies()
    If IsEmpty(v) Then Exit Sub
    For Each b In v
        If b.Name <> fid Then
            bb = b.GetBodyBox()
            If bb(0) <= hb(3) + 1E-07 And hb(0) <= bb(3) + 1E-07 And bb(1) <= hb(4) + 1E-07 _
                And hb(1) <= bb(4) + 1E-07 And bb(2) <= hb(5) + 1E-07 And hb(2) <= bb(5) + 1E-07 Then
                res = Empty
                On Error Resume Next
                res = h.Copy().Operations2(15903, b.Copy(), e)
                On Error GoTo 0
                If Not IsEmpty(res) Then
                    If UBound(res) - LBound(res) = 0 Then hits.Add b
                End If
            End If
        End If
    Next
    If hits.Count = 0 Then Exit Sub
    ReDim tools(hits.Count)
    Set tools(0) = h
    For i = 1 To hits.Count
        Set tools(i) = hits(i)
    Next
    Set f = FM.InsertCombineFeature(15903, Nothing, (tools))
    If f Is Nothing Then Fail "fuse " & nm & " with the bodies it touches"
    RenameLast nm & "_Fuse"
    Track fid, before
End Sub

Sub Extr(sk As String, fid As String, nm As String, sd As Boolean, flip As Boolean, t1 As Long, t2 As Long, d1 As Double, d2 As Double, mode As String, key As String, simple As Boolean)
    Dim before As Object, f As Object, v0 As Double, attempt As Long, rev As Boolean
    Set before = TakeSnap()
    If mode <> "cut" Then
        SelSketch sk
        Set f = FM.FeatureExtrusion3(sd, False, flip, t1, t2, d1, d2, False, False, False, False, 0, 0, False, False, False, False, mode = "merge", True, True, 0, 0, False)
        If f Is Nothing Then Fail "extrude"
        RenameLast nm
    ElseIf BodyCount() > 1 Then
        CutTool sk, nm, False, sd, flip, t1, t2, d1, d2, 0, key, simple
    Else
        ' Chudware cuts run along the sketch normal (SolidWorks' reversed cut direction) and an
        ' unflipped one-sided blind cut that removes nothing goes into the material instead
        ' (document.cpp "BLIND-CUT DIRECTION FALLBACK"). SolidWorks refuses an empty cut.
        v0 = Vol()
        For attempt = 0 To 1
            rev = flip Xor (attempt = 0)
            SelSketch sk
            Set f = FM.FeatureCut4(sd, False, rev, t1, t2, d1, d2, False, False, False, False, 0, 0, False, False, False, False, False, True, True, True, True, False, 0, 0, False, False)
            If Not f Is Nothing Then
                If (Not simple) Or attempt = 1 Or Abs(Vol() - v0) > 1E-15 Then Exit For
                DeleteFeature f.Name
            ElseIf (Not simple) Or attempt = 1 Then
                ' Like Chudware, a cut that removes nothing is a warning, not a failure.
                Note "cut " & nm & " removes nothing"
                Exit For
            End If
        Next
        RenameLast nm
    End If
    Track fid, before
    If mode = "merge" Then MergeTouching fid, nm, before
End Sub

Sub Revo(sk As String, fid As String, nm As String, mode As String, key As String, ang As Double, rev As Boolean)
    Dim before As Object, f As Object
    Set before = TakeSnap()
    If mode = "cut" And BodyCount() > 1 Then
        CutTool sk, nm, True, True, rev, 0, 0, 0, 0, ang, key, False
    Else
        SelSketch sk
        Set f = FM.FeatureRevolve2(True, True, False, mode = "cut", rev, False, 0, 0, ang, 0, False, False, 0, 0, 0, 0, 0, mode <> "new", True, True)
        If f Is Nothing Then Fail "revolve"
        RenameLast nm
    End If
    Track fid, before
    If mode = "merge" Then MergeTouching fid, nm, before
End Sub

Sub MoveBody(fid As String, nm As String, key As String, ByVal copy As Boolean, hasRot As Boolean, a As Double, b As Double, c As Double, px As Double, py As Double, pz As Double, hasT As Boolean, tx As Double, ty As Double, tz As Double)
    Dim before As Object, s0 As Object, tname As String, f As Object, moved As Object
    Set before = TakeSnap()
    tname = Target(key).Name
    If Not (hasRot Or hasT) Then
        Own(fid) = tname
        Cur = tname
        Exit Sub
    End If
    If hasRot Then
        Set s0 = TakeSnap()
        Part.ClearSelection2 True
        If Not Sel(tname, "SOLIDBODY", False, 1) Then Fail "select body " & tname
        Set f = FM.InsertMoveCopyBody2(0, 0, 0, 0, px, py, pz, a, b, c, copy, 1)
        If f Is Nothing Then Fail "rotate body " & tname
        If hasT Then RenameLast nm & "_Rotate" Else RenameLast nm
        Set moved = NewBody(s0)
        If Not moved Is Nothing Then tname = moved.Name
        copy = False
    End If
    If hasT Then
        Part.ClearSelection2 True
        If Not Sel(tname, "SOLIDBODY", False, 1) Then Fail "select body " & tname
        Set f = FM.InsertMoveCopyBody2(tx, ty, tz, 0, 0, 0, 0, 0, 0, 0, copy, 1)
        If f Is Nothing Then Fail "move body " & tname
        RenameLast nm
    End If
    Track fid, before
End Sub

Sub Comb(fid As String, nm As String, code As Long, ka As String, kb As String)
    Dim before As Object, f As Object, a As Object, b As Object
    Dim one(0) As Object, two(1) As Object
    Set before = TakeSnap()
    Set a = Target(ka)
    Set b = Target(kb)
    If code = 15902 Then
        Set one(0) = b
        Set f = FM.InsertCombineFeature(code, a, (one))
    Else
        Set two(0) = a
        Set two(1) = b
        Set f = FM.InsertCombineFeature(code, Nothing, (two))
    End If
    If f Is Nothing Then Fail "combine " & ka & " with " & kb
    RenameLast nm
    Track fid, before
End Sub

' Measured values (key=value lines) and whether they match Chudware's part.
' `ok`: within the verification tolerances. `near`: within CLOSE_FACTOR of them, which kernel
' differences between Chudware and SolidWorks booleans stay inside; only a rebuild that misses
' `near` falls back to the exact solid.
Function Measure(ByRef ok As Boolean, ByRef near As Boolean) As String
    Dim mp As Object, c As Variant, v As Double, n As Long, mo As Variant, k As Double
    Set mp = Part.Extension.CreateMassProperty()
    v = mp.Volume * 1E+09
    c = mp.CenterOfMass
    n = BodyCount()
    mo = mp.GetMomentOfInertia(0)
    k = 0
    If mp.Mass > 0 Then k = mp.Volume / mp.Mass * 1E+15
    Measure = "volume_mm3=" & Num(v) & vbCrLf & "com_sw_mm=" & Num(c(0) * 1000) & "," & Num(c(1) * 1000) & "," & Num(c(2) * 1000) _
        & vbCrLf & "body_count=" & n & vbCrLf & "inertia_sw_mm5=" & Num(mo(0) * k) & "," & Num(mo(4) * k) & "," & Num(mo(8) * k)
    ok = True
{checks}
    near = True
{near_checks}
    Measure = Measure & vbCrLf & "close=" & IIf(near, "1", "0")
End Function

Sub SaveAndReport(msg As String, ok As Boolean, fidelity As String)
    Dim saved As String
    saved = Folder & "\{file}"
    boolstatus = Part.Extension.SaveAs(saved, 0, 1, Nothing, errors, warnings)
    If Notes <> "" Then msg = msg & vbCrLf & "notes=" & Notes
    msg = msg & vbCrLf & "match=" & IIf(ok, "1", "0") & vbCrLf & "fidelity=" & fidelity & vbCrLf & "saved=" & saved & vbCrLf & "save_ok=" & IIf(boolstatus, "1", "0")
    Report True, msg
    If Not Silent Then
        If ok Then
            MsgBox "MATCHES the Chudware part (" & fidelity & ")." & vbCrLf & "Saved " & saved, vbInformation
        Else
            MsgBox "DOES NOT MATCH the Chudware part: " & msg, vbExclamation
        End If
    End If
    If Silent Then swApp.CloseDoc Part.GetTitle()
End Sub

Sub Finish()
    Dim msg As String, ok As Boolean, near As Boolean
    Stage = "verify"
    Part.ForceRebuild3 False
    msg = Measure(ok, near)
    If (Not near) And Fallback <> "" Then
        BodyFallback "the feature rebuild does not match the Chudware part (" & Replace(msg, vbCrLf, "; ") & ")"
        Exit Sub
    End If
    SaveAndReport msg, ok, "feature_tree"
End Sub

' The exact solid instead of the feature tree: Chudware's STEP export beside this macro.
' SolidWorks keeps a STEP file's coordinates; Chudware is Z-up and every rebuilt part maps
' engine (x, y, z) to SolidWorks (x, z, -y). Turn the imported bodies the same way: -90 deg
' about X (InsertMoveCopyBody2 rotates about Z, Y, then X).
Sub ToSolidWorksAxes()
    Dim v As Variant, b As Variant, f As Object
    v = Bodies()
    If IsEmpty(v) Then Fail "the STEP file has no solid body"
    Part.ClearSelection2 True
    For Each b In v
        boolstatus = Sel(b.Name, "SOLIDBODY", True, 1)
    Next
    Set f = Part.FeatureManager.InsertMoveCopyBody2(0, 0, 0, 0, 0, 0, 0, 0, 0, -1.5707963267949, False, 1)
    If f Is Nothing Then Fail "could not turn the imported bodies Z-up -> Y-up"
    RenameLast "Chudware Z-up to Y-up"
    Part.ClearSelection2 True
End Sub

Sub BodyFallback(reason As String)
    Dim msg As String, ok As Boolean, near As Boolean
    On Error GoTo bad
    Stage = "body fallback"
    If Not Part Is Nothing Then swApp.CloseDoc Part.GetTitle()
    Set Part = swApp.LoadFile4(Folder & "\" & Fallback, "r", Nothing, errors)
    If Part Is Nothing Then
        Abort "could not open " & Fallback & " (error " & errors & ") after: " & reason
        Exit Sub
    End If
    ToSolidWorksAxes
    msg = Measure(ok, near) & vbCrLf & "feature_error=" & reason
    SaveAndReport msg, ok, "imported_body"
    Exit Sub
bad:
    Abort "body fallback failed (" & Err.Description & ") after: " & reason
End Sub
'''

_MAIN = r'''
Sub main()
    Set swApp = Application.SldWorks
    Folder = swApp.GetCurrentMacroPathFolder()
    Silent = Dir(Folder & "\{stem}.silent") <> ""
    Stage = "start"
    Fallback = "{fallback}"
    If Fallback <> "" And Dir(Folder & "\" & Fallback) = "" Then Fallback = ""
    If "{body_only}" <> "" Then
        If Fallback = "" Then
            Abort "{fallback} is not beside this macro"
        Else
            BodyFallback "{body_only}"
        End If
        Exit Sub
    End If
    On Error GoTo crash
    Set MU = swApp.GetMathUtility()
    Set Own = CreateObject("Scripting.Dictionary")
    Set Planes = CreateObject("Scripting.Dictionary")
    Set Part = swApp.NewDocument(swApp.GetUserPreferenceStringValue(8), 0, 0, 0)
    If Part Is Nothing Then Fail "SolidWorks could not create a part (no default part template)"
    Set FM = Part.FeatureManager
    Set SM = Part.SketchManager
{calls}
    Finish
    Exit Sub
crash:
    Dim why As String
    If Err.Number = vbObjectError + 513 Then
        why = Err.Description
    Else
        why = "SolidWorks error " & Err.Number & ": " & Err.Description
    End If
    If Fallback <> "" Then
        BodyFallback Stage & ": " & why
    Else
        Abort why
    End If
End Sub
'''


def _checks(expected: dict, flag: str = "ok", scale: float = 1.0) -> str:
    """VBA lines that clear ``flag`` when a measured value misses Chudware's by more than the
    tolerances times ``scale``."""
    out = []
    ev = expected.get("volume_mm3")
    if ev is not None:
        out.append(f"    If Abs(v - {_n(ev)}) > {_n(VOLUME_RTOL * scale)} * "
                   f"{_n(max(abs(ev), 1e-9))} Then {flag} = False")
    com = expected.get("com_sw_mm")
    if com:
        for i in range(3):
            out.append(f"    If Abs(c({i}) * 1000 - ({_n(com[i])})) > {_n(COM_TOL_MM * scale)} "
                       f"Then {flag} = False")
    if expected.get("body_count") is not None:
        out.append(f"    If n <> {int(expected['body_count'])} Then {flag} = False")
    ine = expected.get("inertia_sw_mm5")
    if ine:
        imax = max(abs(x) for x in ine) or 1.0
        for i, j in ((0, 0), (1, 4), (2, 8)):
            out.append(f"    If Abs(mo({j}) * k - ({_n(ine[i])})) > "
                       f"{_n(INERTIA_RTOL * scale * imax)} Then {flag} = False")
    return "\n".join(out)


class _Writer:
    def __init__(self):
        self.subs: list[list[str]] = []
        self.lines: list[str] = []

    def w(self, line: str = "") -> None:
        self.lines.append("    " + line if line else "")

    def begin(self, s: dict) -> None:
        self.lines = []
        self.w(f"Stage = {_s(s['name'])}")

    def end(self) -> None:
        self.w("Built = Built + 1")
        self.subs.append(self.lines)

    # -- sketches ----------------------------------------------------------------------------
    def sketch(self, s: dict) -> None:
        """A sketch on a model face (side query): entities in the face's own 2D frame."""
        nx, ny, nz = s["plane"]["normal"]
        self.w("Part.ClearSelection2 True")
        self.w(f"If Not SelFaceByNormal({_n(nx)}, {_n(ny)}, {_n(nz)}, False) Then "
               f"Fail {_s('sketch face')}")
        self.w("SM.InsertSketch True")
        self.w("SM.AddToDB = True")
        for e in s["entities"]:
            self.entity2d(e)
        self.w(f"EndSketch {_s(s['name'])}")

    def entity2d(self, e: dict) -> None:
        t, cons = e["type"], bool(e.get("construction"))
        if t == "line":
            (x1, y1), (x2, y2) = e["p1"], e["p2"]
            fn = "CreateCenterLine" if cons else "CreateLine"
            self.w(f"Set seg = SM.{fn}({_m(x1)}, {_m(y1)}, 0, {_m(x2)}, {_m(y2)}, 0)")
            self.w("Need Not seg Is Nothing, \"sketch line\"")
            return
        if t == "circle":
            cx, cy = e["center"]
            self.w(f"Set seg = SM.CreateCircleByRadius({_m(cx)}, {_m(cy)}, 0, {_m(e['radius'])})")
        elif t == "arc":
            a, b, m = arc_points(e)
            self.w(f"Set seg = SM.Create3PointArc({_m(a[0])}, {_m(a[1])}, 0, {_m(b[0])}, "
                   f"{_m(b[1])}, 0, {_m(m[0])}, {_m(m[1])}, 0)")
        elif t == "point":
            x, y = e["p"]
            self.w(f"SM.CreatePoint {_m(x)}, {_m(y)}, 0")
            return
        elif t == "spline":
            flat = []
            for x, y in e["points"]:
                flat += [x * M, y * M, 0.0]
            self.spline(flat)
        self.w("Construction" if cons else "Need Not seg Is Nothing, \"sketch entity\"")

    def spline(self, flat: list[float]) -> None:
        self.w(f"ReDim pts({len(flat) - 1}) As Double")
        for i, v in enumerate(flat):
            self.w(f"pts({i}) = {_n(v)}")
        self.w("Set seg = SM.CreateSpline2((pts), True)")

    def _p(self, var: str, q) -> None:
        self.w(f"{var} = SP({_m(q[0])}, {_m(q[1])}, {_m(q[2])})")

    def sketch3(self, s: dict) -> None:
        """A sketch whose entities are given as 3D model points (mapped into the sketch)."""
        o, n = s["plane"]["origin"], s["plane"]["normal"]
        self.w(f"PlaneFor {_s(s['plane'].get('name') or s['name'] + '_Plane')}, {_m(o[0])}, {_m(o[1])}, {_m(o[2])}, "
               f"{_n(n[0])}, {_n(n[1])}, {_n(n[2])}")
        self.w("BeginSketch")
        self._p("p1", o)
        self.w(f"If Abs(p1(2)) > 1E-08 Then Fail \"sketch plane misses its origin by \" & "
               f"Num(CDbl(p1(2))) & \" m\"")
        for i, e in enumerate(s["entities"]):
            if i and i % _CHUNK == 0:
                self.w(_SPLIT)   # continue in another procedure: VBA caps one at 64 KB
            t, cons = e["type"], bool(e.get("construction"))
            if t == "line":
                self._p("p1", e["p1"])
                self._p("p2", e["p2"])
                fn = "CreateCenterLine" if cons else "CreateLine"
                self.w(f"Set seg = SM.{fn}(p1(0), p1(1), 0, p2(0), p2(1), 0)")
                self.w("Need Not seg Is Nothing, \"sketch line\"")
                continue
            if t == "circle":
                self._p("p1", e["center"])
                self.w(f"Set seg = SM.CreateCircleByRadius(p1(0), p1(1), 0, {_m(e['radius'])})")
            elif t == "arc3":
                self._p("p1", e["p1"])
                self._p("p2", e["p2"])
                self._p("p3", e["pm"])
                self.w("Set seg = SM.Create3PointArc(p1(0), p1(1), 0, p2(0), p2(1), 0, "
                       "p3(0), p3(1), 0)")
            elif t == "point":
                self._p("p1", e["p"])
                self.w("SM.CreatePoint p1(0), p1(1), 0")
                continue
            elif t == "spline":
                self.w(f"ReDim pts({3 * len(e['points']) - 1}) As Double")
                for i, q in enumerate(e["points"]):
                    self._p("p1", q)
                    self.w(f"pts({3 * i}) = p1(0): pts({3 * i + 1}) = p1(1): "
                           f"pts({3 * i + 2}) = 0")
                self.w("Set seg = SM.CreateSpline2((pts), True)")
            self.w("Construction" if cons else "Need Not seg Is Nothing, \"sketch entity\"")
        self.w(f"EndSketch {_s(s['name'])}")

    # -- solid features ----------------------------------------------------------------------
    @staticmethod
    def _end_args(s: dict) -> tuple:
        """(Sd, T1, T2, D1, D2) for FeatureExtrusion3 / FeatureCut4."""
        end, end2 = s["end"], s["end2"]
        if end == "through_all":
            if s.get("on_face"):
                # From a model face the engine's two-way cut only removes material on one
                # side: one direction, flipped by the fallback when it meets nothing (a
                # pattern of a two-direction cut gives SolidWorks 2026 a zero-volume body).
                return True, 1, 0, 0.01, 0.01
            # Through all, both directions (SolidWorks refuses swEndCondThroughAllBoth here).
            return False, 1, 1, 0.01, 0.01
        t1 = 6 if end == "mid_plane" else 0     # swEndCondMidPlane / Blind
        if end2 == "none" or end == "mid_plane":
            return True, t1, 0, s["depth"] * M, 0.01
        return False, t1, 1 if end2 == "through_all" else 0, s["depth"] * M, s["depth2"] * M

    def extrude(self, s: dict) -> None:
        sd, t1, t2, d1, d2 = self._end_args(s)
        body = s["body"]
        simple = (s["end"] == "blind" or (s["end"] == "through_all" and s.get("on_face")))             and s["end2"] == "none" and not s["flip"]
        self.w(f"Extr {_s(s['sketch'])}, {_s(s['id'])}, {_s(s['name'])}, {_b(sd)}, "
               f"{_b(s['flip'])}, {t1}, {t2}, {_n(d1)}, {_n(d2)}, {_s(body['mode'])}, "
               f"{_s(body.get('target') or '')}, {_b(simple)}")

    def revolve(self, s: dict) -> None:
        body = s["body"]
        rev = bool(s["flip"]) ^ (s["angle"] < 0)
        self.w(f"Revo {_s(s['sketch'])}, {_s(s['id'])}, {_s(s['name'])}, {_s(body['mode'])}, "
               f"{_s(body.get('target') or '')}, {_n(math.radians(abs(s['angle'])))}, {_b(rev)}")

    def move_body(self, s: dict) -> None:
        rot = next((m for m in s["moves"] if "rotate" in m), None)
        tr = next((m for m in s["moves"] if "translate" in m), None)
        a, b, c = (math.radians(x) for x in (rot["rotate"] if rot else (0, 0, 0)))
        pv = rot["pivot"] if rot else [0, 0, 0]
        t = tr["translate"] if tr else [0, 0, 0]
        self.w(f"MoveBody {_s(s['id'])}, {_s(s['name'])}, {_s(s.get('target') or '')}, "
               f"{_b(s['copy'])}, {_b(rot)}, {_n(a)}, {_n(b)}, {_n(c)}, {_m(pv[0])}, "
               f"{_m(pv[1])}, {_m(pv[2])}, {_b(tr)}, {_m(t[0])}, {_m(t[1])}, {_m(t[2])}")

    def combine(self, s: dict) -> None:
        self.w(f"Comb {_s(s['id'])}, {_s(s['name'])}, {s['code']}, {_s(s['a'])}, {_s(s['b'])}")

    def _tracked(self, s: dict, body_lines) -> None:
        self.w("Dim before As Object")
        self.w("Set before = TakeSnap()")
        body_lines()
        self.w(f"RenameLast {_s(s['name'])}")
        self.w(f"Track {_s(s['id'])}, before")

    def _edges(self, s: dict) -> None:
        self.w("Part.ClearSelection2 True")
        for x, y, z in s["edge_points"]:
            self.w(f"If Not SelNear(\"EDGE\", {_m(x)}, {_m(y)}, {_m(z)}, True, 0) Then "
                   f"Fail {_s('no edge at ' + str([x, y, z]))}")

    def fillet(self, s: dict) -> None:
        def go():
            self._edges(s)
            self.w(f"If FM.FeatureFillet3(195, {_m(s['size'])}, 0, 0, 0, 0, 0, Nothing, Nothing, "
                   "Nothing, Nothing, Nothing, Nothing, Nothing) Is Nothing Then Fail \"fillet\"")
        self._tracked(s, go)

    def chamfer(self, s: dict) -> None:
        def go():
            self._edges(s)
            self.w(f"If FM.InsertFeatureChamfer(4, 1, {_m(s['size'])}, {_n(math.radians(45))}, "
                   "0, 0, 0, 0) Is Nothing Then Fail \"chamfer\"")
        self._tracked(s, go)

    def shell(self, s: dict) -> None:
        def go():
            self.w("Part.ClearSelection2 True")
            for face in s["remove"]:
                if "point" in face:
                    x, y, z = face["point"]
                    self.w(f"If Not SelNear(\"FACE\", {_m(x)}, {_m(y)}, {_m(z)}, True, 0) Then "
                           "Fail \"shell face\"")
                else:
                    nx, ny, nz = face["normal"]
                    self.w(f"If Not SelFaceByNormal({_n(nx)}, {_n(ny)}, {_n(nz)}, True) Then "
                           "Fail \"shell face\"")
            self.w(f"Part.InsertFeatureShell {_m(s['thickness'])}, {_b(s['outward'])}")
        self._tracked(s, go)

    def _seeds(self, seeds: list, mark: int) -> None:
        for nm in seeds:
            self.w(f"If Not Sel({_s(nm)}, \"BODYFEATURE\", True, {mark}) Then "
                   f"Fail {_s('seed ' + nm)}")

    def linear_pattern(self, s: dict) -> None:
        d1 = s["dirs"][0]
        d2 = s["dirs"][1] if len(s["dirs"]) > 1 else None

        def go():
            # Each direction is a 3D-sketch line from the origin along it (start -> end).
            for i, d in enumerate(s["dirs"], 1):
                v = d["dir"]
                self.w(f"Dim dl{i} As Object")
                self.w(f"Set dl{i} = DirLine({_s(s['name'] + '_Dir' + str(i))}, {_n(v[0])}, "
                       f"{_n(v[1])}, {_n(v[2])})")
            self.w("Part.ClearSelection2 True")
            self.w("boolstatus = dl1.Select2(True, 1)")
            if d2:
                self.w("boolstatus = dl2.Select2(True, 2)")
            self._seeds(s["seeds"], 4)
            n2 = d2["count"] if d2 else 1
            s2 = (d2["spacing"] if d2 else 0.0)
            self.w(f"If FM.FeatureLinearPattern4({d1['count']}, {_m(d1['spacing'])}, {n2}, "
                   f"{_m(s2)}, False, False, \"\", \"\", False, False, False, False, True, True, "
                   "False, False, False, False, 0, 0) Is Nothing Then Fail \"linear pattern\"")
        self._tracked(s, go)

    def circular_pattern(self, s: dict) -> None:
        o, d = s["axis"]["origin"], s["axis"]["dir"]
        spacing = math.radians(s["angle"]) if s["equal_spacing"] else \
            math.radians(s["angle"]) / max(1, s["count"] - 1)

        def go():
            self.w("Dim ax As String")
            self.w(f"ax = MakeAxis({_s(s['name'] + '_Axis')}, {_m(o[0])}, {_m(o[1])}, "
                   f"{_m(o[2])}, {_n(d[0])}, {_n(d[1])}, {_n(d[2])})")
            self.w("Part.ClearSelection2 True")
            self.w("boolstatus = Sel(ax, \"AXIS\", True, 1)")
            self._seeds(s["seeds"], 4)
            self.w(f"If FM.FeatureCircularPattern4({s['count']}, {_n(spacing)}, "
                   f"{_b(s['reverse'])}, \"\", False, {_b(s['equal_spacing'])}, False) Is "
                   "Nothing Then Fail \"circular pattern\"")
        self._tracked(s, go)

    def mirror(self, s: dict) -> None:
        def go():
            self.w("Part.ClearSelection2 True")
            self.w(f"boolstatus = Sel({_s(s['plane'])}, \"PLANE\", False, 2)")
            self._seeds(s["seeds"], 1)
            self.w("If FM.InsertMirrorFeature2(False, False, True, False, 0) Is Nothing Then "
                   "Fail \"mirror\"")
        self._tracked(s, go)


def write_macro(plan: dict, path: str, fallback_step: str = "", body_only: str = "") -> str:
    """Write ``plan`` as a .swb macro to ``path``; it saves ``<stem>.SLDPRT`` beside itself.

    ``fallback_step``: a STEP file name beside the macro. When SolidWorks cannot build a
    feature, or the result does not match, the macro saves that exact solid instead
    (``fidelity=imported_body`` in its report, with ``feature_error``)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    w = _Writer()
    for step in plan["steps"]:
        w.begin(step)
        getattr(w, step["op"])(step)
        w.end()
    subs = []
    for i, body in enumerate(w.subs, 1):
        parts: list[list[str]] = [[]]
        for ln in body:
            if ln.strip() == _SPLIT:
                parts.append([])
            else:
                parts[-1].append(ln)
        head = parts[0] + [f"    Step{i}_{k}" for k in range(2, len(parts) + 1)]
        subs.append(f"Sub Step{i}()\n    Dim pts() As Double\n" + "\n".join(head) + "\nEnd Sub")
        for k, part in enumerate(parts[1:], 2):
            subs.append(f"Sub Step{i}_{k}()\n    Dim pts() As Double\n" + "\n".join(part) +
                        "\nEnd Sub")
    calls = "\n".join(f"    Step{i}" for i in range(1, len(w.subs) + 1))
    fmt = {"name": str(plan["name"]).replace('"', "'"), "stem": stem,
           "file": f"{stem}.SLDPRT", "fallback": os.path.basename(fallback_step),
           "body_only": " ".join(body_only.replace('"', "'").split())}
    exp = plan.get("expected") or {}
    text = (_LIBRARY.format(checks=_checks(exp), near_checks=_checks(exp, "near", CLOSE_FACTOR),
                            **fmt) + "\n" +
            "\n\n".join(subs) + "\n" + _MAIN.format(calls=calls, **fmt))
    _write(path, text)
    return path


_ASM_MACRO = r'''' Chudware -> SolidWorks assembly macro for "{name}".
' Keep the whole folder together and run this macro: Tools > Macro > Run. It builds every part
' and sub-assembly whose SolidWorks file is not in the folder yet (their own macros, in your
' SolidWorks version), places each component at its Chudware position, fixes it there, saves
' {file} and reports whether the assembly matches Chudware's.
Dim swApp As Object
Dim Asm As Object
Dim MU As Object
Dim errors As Long, warnings As Long
Dim Folder As String
Dim Silent As Boolean
Dim Stage As String
Dim Opened As Collection
Dim Comps As Collection

Function Num(x As Double) As String
    Num = Trim(Str(x))
End Function

Sub Report(ok As Boolean, msg As String)
    Dim fh As Integer
    fh = FreeFile
    Open Folder & "\{stem}.report.txt" For Output As #fh
    Print #fh, "ok=" & IIf(ok, "1", "0")
    Print #fh, "stage=" & Stage
    Print #fh, msg
    Close #fh
End Sub

Sub Fail(msg As String)
    Err.Raise vbObjectError + 513, "Chudware", msg
End Sub

' Run a part or sub-assembly macro unless its SolidWorks file is already in the folder.
Sub EnsureBuilt(file As String, macro As String)
    Dim stem As String, fh As Integer, ok As Boolean, rep As String, ln As String
    If Dir(Folder & "\" & file) <> "" Then Exit Sub
    Stage = "build " & file
    stem = Left(macro, Len(macro) - 4)
    fh = FreeFile
    Open Folder & "\" & stem & ".silent" For Output As #fh
    Print #fh, "run by {stem}"
    Close #fh
    ok = swApp.RunMacro2(Folder & "\" & macro, "", "main", 1, errors)
    Kill Folder & "\" & stem & ".silent"
    If Dir(Folder & "\" & file) = "" Then
        rep = ""
        If Dir(Folder & "\" & stem & ".report.txt") <> "" Then
            fh = FreeFile
            Open Folder & "\" & stem & ".report.txt" For Input As #fh
            Do While Not EOF(fh)
                Line Input #fh, ln
                If Left(ln, 6) = "error=" Then rep = Mid(ln, 7)
            Loop
            Close #fh
        End If
        Fail macro & " did not save " & file & ": " & rep
    End If
End Sub

' The default assembly template, or the first .asmdot beside the part template (some installs
' point the default at a file that is not there).
Function AsmTemplate() As String
    Dim t As String, d As String, f As String, sf As Variant
    t = swApp.GetUserPreferenceStringValue(9)
    If t <> "" Then
        If Dir(t) <> "" Then
            AsmTemplate = t
            Exit Function
        End If
    End If
    d = swApp.GetUserPreferenceStringValue(8)
    d = Left(d, InStrRev(d, "\"))
    For Each sf In Array("", "MBD\")
        f = Dir(d & sf & "*.asmdot")
        If f <> "" Then
            AsmTemplate = d & sf & f
            Exit Function
        End If
    Next
    AsmTemplate = t
End Function

Sub OpenComp(file As String)
    Dim d As Object, kind As Long
    If LCase(Right(file, 7)) = ".sldasm" Then kind = 2 Else kind = 1
    Set d = swApp.OpenDoc6(Folder & "\" & file, kind, 1, "", errors, warnings)
    If d Is Nothing Then Fail "could not open " & file & " (error " & errors & ")"
    Opened.Add d.GetTitle()
End Sub

' Add one component and set its placement: a SolidWorks MathTransform array (rotation rows
' are the images of the axes, then the translation in metres, then the scale).
Sub Place(file As String, t As Variant)
    Dim c As Object, a(15) As Double, i As Long
    Stage = "place " & file
    Set c = Asm.AddComponent5(Folder & "\" & file, 0, "", False, "", 0, 0, 0)
    If c Is Nothing Then Fail "could not add " & file
    For i = 0 To 15
        a(i) = t(i)
    Next
    c.Transform2 = MU.CreateTransform((a))
    Comps.Add c
End Sub

Sub main()
    Dim mp As Object, c As Variant, v As Double, mo As Variant, k As Double, msg As String
    Dim ok As Boolean, saved As String, title As Variant, boolstatus As Boolean
    Set swApp = Application.SldWorks
    Folder = swApp.GetCurrentMacroPathFolder()
    Silent = Dir(Folder & "\{stem}.silent") <> ""
    Set Opened = New Collection
    Set Comps = New Collection
    Stage = "start"
    On Error GoTo crash
    Set MU = swApp.GetMathUtility()
{builds}
    Stage = "open components"
{opens}
    Set Asm = swApp.NewDocument(AsmTemplate(), 0, 0, 0)
    If Asm Is Nothing Then Fail "SolidWorks could not create an assembly (no assembly template found)"
{places}
    Stage = "fix components"
    Asm.ClearSelection2 True
    For Each c In Comps
        boolstatus = c.Select4(True, Nothing, False)
    Next
    Asm.FixComponent
    Asm.ClearSelection2 True
    Asm.ForceRebuild3 False
    Stage = "verify"
    Set mp = Asm.Extension.CreateMassProperty()
    v = mp.Volume * 1E+09
    c = mp.CenterOfMass
    mo = mp.GetMomentOfInertia(0)
    k = 0
    If mp.Mass > 0 Then k = mp.Volume / mp.Mass * 1E+15
    msg = "volume_mm3=" & Num(v) & vbCrLf & "com_sw_mm=" & Num(c(0) * 1000) & "," & Num(c(1) * 1000) & "," & Num(c(2) * 1000) _
        & vbCrLf & "inertia_sw_mm5=" & Num(mo(0) * k) & "," & Num(mo(4) * k) & "," & Num(mo(8) * k) & vbCrLf & "component_count=" & Comps.Count
    ok = True
{checks}
    saved = Folder & "\{file}"
    boolstatus = Asm.Extension.SaveAs(saved, 0, 1, Nothing, errors, warnings)
    msg = msg & vbCrLf & "match=" & IIf(ok, "1", "0") & vbCrLf & "saved=" & saved & vbCrLf & "save_ok=" & IIf(boolstatus, "1", "0")
    Report True, msg
    If Silent Then
        swApp.CloseDoc Asm.GetTitle()
        For Each title In Opened
            swApp.CloseDoc CStr(title)
        Next
    ElseIf ok Then
        MsgBox "MATCHES the Chudware assembly (" & Comps.Count & " components)." & vbCrLf & "Saved " & saved, vbInformation
    Else
        MsgBox "DOES NOT MATCH the Chudware assembly: " & msg, vbExclamation
    End If
    Exit Sub
crash:
    If Err.Number = vbObjectError + 513 Then msg = Err.Description Else msg = "SolidWorks error " & Err.Number & ": " & Err.Description
    Report False, "error=" & msg
    If Silent Then
        On Error Resume Next
        If Not Asm Is Nothing Then swApp.CloseDoc Asm.GetTitle()
        For Each title In Opened
            swApp.CloseDoc CStr(title)
        Next
    Else
        MsgBox "Chudware assembly export stopped at " & Stage & ": " & msg, vbCritical
    End If
End Sub
'''


def write_assembly_macro(name: str, path: str, components: list[dict],
                         builds: list[tuple[str, str]], expected: dict) -> str:
    """An assembly macro saving ``<stem>.SLDASM`` beside itself.

    ``builds`` = [(SolidWorks file, its macro)] to build first when missing, in order
    (sub-assemblies after their parts); ``components`` = [{"file", "transform"}] with the
    16-value SolidWorks placement array (:func:`services.sw_export.assembly.sw_transform`).
    ``expected``: volume, centre of mass and inertia diagonal (``expected_in_sw``)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    files: list[str] = []
    for c in components:
        if c["file"] not in files:
            files.append(c["file"])
    b = "\n".join(f"    EnsureBuilt {_s(f)}, {_s(m)}" for f, m in builds)
    o = "\n".join(f"    OpenComp {_s(f)}" for f in files)
    pl = "\n".join(f"    Place {_s(c['file'])}, Array(" +
                   ", ".join(_n(x) for x in c["transform"]) + ")" for c in components)
    checks = _checks({k: v for k, v in expected.items() if k != "body_count"})
    text = _ASM_MACRO.format(name=name.replace('"', "'"), stem=stem, file=f"{stem}.SLDASM",
                             builds=b, opens=o, places=pl, checks=checks)
    _write(path, text)
    return path


def write_body_macro(name: str, step_path: str, path: str, expected: dict | None = None,
                     reason: str = "no SolidWorks feature tree for this part") -> str:
    """A macro that opens the exact STEP beside it, turns it into SolidWorks axes, checks it
    against ``expected`` and saves it as ``<stem>.SLDPRT`` (``fidelity=imported_body``)."""
    return write_macro({"name": name, "steps": [], "expected": expected or {}}, path,
                       fallback_step=step_path, body_only=reason)


def _write(path: str, text: str) -> None:
    # The SolidWorks VBA editor reads macros in the ANSI code page.
    with open(path, "w", encoding="cp1252", errors="replace", newline="\r\n") as fh:
        fh.write(text)
