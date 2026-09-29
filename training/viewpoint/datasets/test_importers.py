"""ViewNet label importers: geometric face correspondence, CANON mapping, instances, through/blind.

Engine-free tests always run. STEP fixtures are built in the test with OCP (skipped without it);
the engine tests import them with libchudware (skipped when the engine is unavailable).
"""
from __future__ import annotations

import dataclasses
import random
import zipfile
from collections import Counter

import pytest

from training.viewpoint.datasets import fetch
from training.viewpoint.datasets import importers as im

try:
    import OCP  # noqa: F401
    HAVE_OCP = True
except ImportError:
    HAVE_OCP = False

try:
    from services.mcp import core_bridge as cb
    HAVE_ENGINE = cb.available()
except Exception:  # noqa: BLE001 — any import/load failure means no engine
    HAVE_ENGINE = False

needs_ocp = pytest.mark.skipif(not HAVE_OCP, reason="OCP (OpenCASCADE bindings) not installed")
needs_engine = pytest.mark.skipif(not (HAVE_OCP and HAVE_ENGINE),
                                  reason="libchudware or OCP unavailable")

# A 40x30x10 plate with a r3 through hole at (10,15) and a r4, 6 deep blind hole at (30,15):
# (surf, area, centroid, outward normal, axis) exactly as the engine's doc_query reports them.
PLATE = [
    ("plane", 300.0, (0.0, 15.0, 5.0), (-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    ("plane", 400.0, (20.0, 0.0, 5.0), (0.0, -1.0, 0.0), (0.0, 1.0, 0.0)),
    ("plane", 1121.460184, (19.803906, 15.0, 10.0), (0.0, 0.0, 1.0), (0.0, 0.0, 1.0)),
    ("plane", 400.0, (20.0, 30.0, 5.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
    ("plane", 1171.725666, (20.241305, 15.0, 0.0), (0.0, 0.0, -1.0), (0.0, 0.0, 1.0)),
    ("plane", 300.0, (40.0, 15.0, 5.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    ("cylinder", 188.495559, (10.0, 15.0, 5.0), (0.0, 0.0, 1.0), (0.0, 0.0, 1.0)),
    ("cylinder", 150.796447, (30.0, 15.0, 7.0), (0.0, 0.0, 1.0), (0.0, 0.0, 1.0)),
    ("plane", 50.265482, (30.0, 15.0, 4.0), (0.0, 0.0, 1.0), (0.0, 0.0, 1.0)),
]
PLATE_ADJ = [(0, 1), (0, 2), (0, 3), (0, 4), (1, 2), (1, 4), (1, 5), (2, 3), (2, 5), (2, 6),
             (2, 7), (3, 4), (3, 5), (4, 5), (4, 6), (7, 8)]
PLATE_DIAG = 51.0
THROUGH, BLIND_WALL, BLIND_FLOOR = 6, 7, 8


def _geom(row) -> im.FaceGeom:
    return im.FaceGeom(*row)


def _engine(order):
    """EngineFaces for PLATE enumerated in `order` (engine ordinal k holds PLATE[order[k]])."""
    return [im.EngineFace(k, 1, k + 1, _geom(PLATE[i])) for k, i in enumerate(order)]


def _adjacency(order):
    pos = {i: k for k, i in enumerate(order)}
    adj = [set() for _ in order]
    for a, b in PLATE_ADJ:
        adj[pos[a]].add(pos[b])
        adj[pos[b]].add(pos[a])
    return adj


def _part(dataset, classes):
    faces = [im.SourceFace(i, _geom(r), classes[i]) for i, r in enumerate(PLATE)]
    return im.SourcePart(dataset, "plate", fetch.data_root() / "plate.step", "test", "test",
                         faces)


BBOX = {"min": [0.0, 0.0, 0.0], "max": [40.0, 30.0, 10.0], "size": [40.0, 30.0, 10.0]}


# ------------------------------------------------------------------------ engine-free

def test_matching_follows_geometry_not_enumeration_order():
    order = list(range(len(PLATE)))
    random.Random(3).shuffle(order)
    assert order != sorted(order)
    eng = [dataclasses.replace(e.geom, centroid=tuple(c + 1e-5 for c in e.geom.centroid))
           for e in _engine(order)]
    m = im.match_faces([_geom(r) for r in PLATE], eng, diag=PLATE_DIAG)
    assert m.rate == 1.0
    assert m.pairs == {i: order.index(i) for i in range(len(PLATE))}


def test_matching_reports_faces_whose_type_or_position_disagree():
    eng = [_geom(r) for r in PLATE]
    eng[BLIND_WALL] = dataclasses.replace(eng[BLIND_WALL], surf="bspline")
    eng[0] = dataclasses.replace(eng[0], centroid=(0.0, 15.0, 5.5))  # 0.5 mm > 0.051 mm tol
    m = im.match_faces([_geom(r) for r in PLATE], eng, diag=PLATE_DIAG)
    assert m.unmatched_source == [0, BLIND_WALL]
    assert m.unmatched_engine == [0, BLIND_WALL]
    assert m.rate == pytest.approx(7 / 9) and m.rate < im.MIN_MATCH_RATE


def test_measured_holes_resolve_through_and_blind_with_axis_and_opening():
    order = [8, 2, 6, 0, 4, 7, 1, 5, 3]
    classes = ["stock"] * 6 + ["hole", "hole", "hole"]
    part = _part("gsl", classes)
    eng = _engine(order)
    m = im.match_faces([f.geom for f in part.faces], [e.geom for e in eng], diag=PLATE_DIAG)
    rec = im.build_record(part, eng, _adjacency(order), m, BBOX)
    by_src = {order[f["ordinal"]]: f for f in rec["faces"]}
    assert by_src[THROUGH]["class"] == "hole_through"
    assert by_src[BLIND_WALL]["class"] == by_src[BLIND_FLOOR]["class"] == "hole_blind"
    assert all(by_src[i]["class"] == "stock" and by_src[i]["instance"] == -1 for i in range(6))
    feats = {f["class"]: f for f in rec["features"]}
    assert set(feats) == {"hole_through", "hole_blind"}
    thru, blind = feats["hole_through"], feats["hole_blind"]
    assert thru["through"] is True and thru["opening_side"] is None
    assert blind["through"] is False and blind["opening_side"] == "+Z"
    assert thru["axis"] == pytest.approx([0, 0, 1]) and blind["axis"] == pytest.approx([0, 0, 1])
    assert sorted(order[o] for o in blind["faces"]) == [BLIND_WALL, BLIND_FLOOR]
    assert blind["centroid"][0] == pytest.approx(30.0)
    assert by_src[BLIND_FLOOR]["instance"] == by_src[BLIND_WALL]["instance"] == blind["instance"]


def test_mfcad_labels_keep_their_own_through_state():
    classes = ["stock"] * 6 + ["through_hole", "blind_hole", "blind_hole"]
    part = _part("mfcadpp", classes)
    order = list(range(len(PLATE)))
    eng = _engine(order)
    m = im.match_faces([f.geom for f in part.faces], [e.geom for e in eng], diag=PLATE_DIAG)
    rec = im.build_record(part, eng, _adjacency(order), m, BBOX)
    assert [f["class"] for f in rec["faces"]][6:] == ["hole_through", "hole_blind", "hole_blind"]
    assert [f["source_class"] for f in rec["faces"]][6:] == classes[6:]
    assert {f["class"]: f["through"] for f in rec["features"]} == {"hole_through": True,
                                                                  "hole_blind": False}


def test_same_class_instances_split_on_connectivity_and_op_faces_merge():
    # Two through holes of one class that do not touch are two instances; a pocket's cut walls
    # and cut floor are one instance whose feature class is cut_side.
    geoms = [
        im.FaceGeom("plane", 5000.0, (0, 0, 20), (0, 0, 1), (0, 0, 1)),           # 0 top
        im.FaceGeom("cylinder", 100.0, (-20, 0, 10), (0, 0, 1), (0, 0, 1)),       # 1 hole A
        im.FaceGeom("cylinder", 100.0, (20, 0, 10), (0, 0, 1), (0, 0, 1)),        # 2 hole B
        im.FaceGeom("plane", 5000.0, (0, 0, 0), (0, 0, -1), (0, 0, 1)),           # 3 bottom
        im.FaceGeom("plane", 40.0, (-5, 10, 18), (1, 0, 0), (1, 0, 0)),           # 4 wall -x
        im.FaceGeom("plane", 40.0, (5, 10, 18), (-1, 0, 0), (1, 0, 0)),           # 5 wall +x
        im.FaceGeom("plane", 40.0, (0, 5, 18), (0, 1, 0), (0, 1, 0)),             # 6 wall -y
        im.FaceGeom("plane", 40.0, (0, 15, 18), (0, -1, 0), (0, 1, 0)),           # 7 wall +y
        im.FaceGeom("plane", 100.0, (0, 10, 16), (0, 0, 1), (0, 0, 1)),           # 8 floor
    ]
    classes = ["stock", "through_hole", "through_hole", "stock"]
    fusion = ["ExtrudeSide", "CutSide", "CutSide", "ExtrudeEnd", "CutSide", "CutSide",
              "CutSide", "CutSide", "CutEnd"]
    edges = [(0, 1), (0, 2), (3, 1), (3, 2), (0, 4), (0, 5), (0, 6), (0, 7), (4, 6), (6, 5),
             (5, 7), (7, 4), (8, 4), (8, 5), (8, 6), (8, 7)]
    adj = [set() for _ in geoms]
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    eng = [im.EngineFace(k, 1, k + 1, g) for k, g in enumerate(geoms)]
    pairs = im.MatchResult({k: k for k in range(len(geoms))}, [], [], 1.0)
    bbox = {"min": [-50, -50, 0], "max": [50, 50, 20]}

    mf = im.SourcePart("mfcadpp", "p", fetch.data_root() / "p.step", "t", "t",
                       [im.SourceFace(k, g, c) for k, (g, c) in enumerate(zip(geoms, classes))])
    pairs4 = im.MatchResult({k: k for k in range(4)}, [], list(range(4, 9)), 4 / 9)
    rec = im.build_record(mf, eng, adj, pairs4, bbox)
    holes = [f for f in rec["features"] if f["class"] == "hole_through"]
    assert sorted(f["faces"] for f in holes) == [[1], [2]]
    assert [f["class"] for f in rec["faces"]][4:] == ["other"] * 5
    assert [f["source_class"] for f in rec["faces"]][4:] == [im.UNMATCHED] * 5

    fu = im.SourcePart("fusion360_seg", "p", fetch.data_root() / "p.step", "t", "t",
                       [im.SourceFace(k, g, c) for k, (g, c) in enumerate(zip(geoms, fusion))])
    rec = im.build_record(fu, eng, adj, pairs, bbox)
    cuts = [f for f in rec["features"] if f["class"] == "cut_side"]
    pocket = next(f for f in cuts if 8 in f["faces"])
    assert pocket["faces"] == [4, 5, 6, 7, 8]
    assert pocket["through"] is False and pocket["opening_side"] == "+Z"
    assert pocket["axis"] == pytest.approx([0, 0, 1])
    through_cuts = [f for f in cuts if 8 not in f["faces"]]
    assert sorted(f["faces"] for f in through_cuts) == [[1], [2]]
    assert all(f["through"] is True for f in through_cuts)


def test_every_source_class_maps_into_canon():
    canon = set(im.CANON)
    assert {im.to_canon("mfcadpp", c) for c in im.MFCAD_CLASSES} <= canon
    assert {im.to_canon("mfcad", c) for c in im.MFCAD16_CLASSES} <= canon
    assert {im.to_canon("fusion360_seg", c) for c in im.FUSION_SEG_CLASSES} <= canon
    assert set(im.GSL_TO_CANON.values()) <= canon | {im.HOLE_UNRESOLVED}
    assert im.to_canon("gsl", "sweep") == "other"
    with pytest.raises(im.LabelError):
        im.to_canon("mfcadpp", "not_a_class")


def test_split_is_a_stable_80_10_10_hash_bucket():
    ids = [f"mfcadpp/{i}" for i in range(20000)]
    counts = Counter(im.split_of(i) for i in ids)
    assert abs(counts["train"] / 20000 - 0.8) < 0.015
    assert abs(counts["val"] / 20000 - 0.1) < 0.01
    assert [im.split_of(i) for i in ids[:50]] == [im.split_of(i) for i in ids[:50]]


def test_subset_selection_skips_unlabelled_parts_and_extends_as_a_prefix():
    src = fetch.SOURCES["mfinstseg"]
    names = ["steps/", "labels/"]
    for i in range(8):
        names.append(f"steps/p{i}.step")
        if i != 5:
            names.append(f"labels/p{i}.json")
    three = fetch.select_parts(src, names, 3)
    all_ = fetch.select_parts(src, names, None)
    assert all_[:3] == three
    assert "steps/p5.step" not in [p for p, _ in all_] and len(all_) == 7
    assert all(comps == [f"labels/{p[6:-5]}.json"] for p, comps in all_)


def test_extracted_members_are_crc_verified_and_repaired(tmp_path):
    arc = tmp_path / "a.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("steps/x.step", b"ISO-10303-21;" * 100)
    out = tmp_path / "raw"
    with zipfile.ZipFile(arc) as z:
        assert fetch.extract_member(z, "steps/x.step", out) is True
        assert fetch.extract_member(z, "steps/x.step", out) is False
        dest = out / "steps" / "x.step"
        dest.write_bytes(b"ISO-10303-21;" * 99 + b"ISO-10303-2X;")
        assert fetch.extract_member(z, "steps/x.step", out) is True
        assert dest.read_bytes() == b"ISO-10303-21;" * 100
        with pytest.raises(fetch.FetchError), zipfile.ZipFile(_evil_zip(tmp_path)) as evil:
            fetch.extract_member(evil, "../evil", out)


def _evil_zip(tmp_path):
    p = tmp_path / "evil.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("../evil", b"x")
    return p


def test_git_members_are_blob_verified_and_repaired(tmp_path, monkeypatch):
    src = fetch.SOURCES["mfcad"]
    good = b"ISO-10303-21;" * 100
    served = {"body": good[:-1] + b"X"}
    monkeypatch.setattr(fetch, "_get", lambda url, headers=None: served["body"])
    member, blob = "dataset/step/p.step", fetch.git_blob_sha1(good)
    dest = tmp_path / member
    with pytest.raises(fetch.FetchError):
        fetch.fetch_git_member(src, member, blob, tmp_path)
    assert not dest.exists()
    served["body"] = good
    assert fetch.fetch_git_member(src, member, blob, tmp_path) == len(good)
    assert fetch.fetch_git_member(src, member, blob, tmp_path) == 0
    dest.write_bytes(good[:-1] + b"Y")
    assert fetch.fetch_git_member(src, member, blob, tmp_path) == len(good)
    assert dest.read_bytes() == good


# ------------------------------------------------------------------------ OCP fixtures

def _mfcad_index(name: str) -> int:
    return im.MFCAD_CLASSES.index(name)


def _write_plate_step(path, *, reorder: bool) -> None:
    """The PLATE part as a STEP whose ADVANCED_FACE names carry MFCAD++ class indices assigned
    from each face's own geometry; `reorder` rebuilds the solid with its faces shuffled."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.GeomAbs import GeomAbs_SurfaceType as ST
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.STEPConstruct import STEPConstruct
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TCollection import TCollection_HAsciiString
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Shell, TopoDS_Solid

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 30, 10).Shape()
    thru = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(10, 15, -1), gp_Dir(0, 0, 1)), 3, 12).Shape()
    blind = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(30, 15, 4), gp_Dir(0, 0, 1)), 4, 7).Shape()
    shape = BRepAlgoAPI_Cut(BRepAlgoAPI_Cut(box, thru).Shape(), blind).Shape()
    faces = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        faces.append(TopoDS.Face_s(exp.Current()))
        exp.Next()
    if reorder:
        random.Random(11).shuffle(faces)
        bld = BRep_Builder()
        shell = TopoDS_Shell()
        bld.MakeShell(shell)
        for f in faces:
            bld.Add(shell, f)
        shell.Closed(True)
        solid = TopoDS_Solid()
        bld.MakeSolid(solid)
        bld.Add(solid, shell)
        shape = solid
    writer = STEPControl_Writer()
    finder = writer.WS().TransferWriter().FinderProcess()
    writer.Transfer(shape, STEPControl_AsIs)
    for f in faces:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, props)
        area, z = props.Mass(), props.CentreOfMass().Z()
        if BRepAdaptor_Surface(f).GetType() == ST.GeomAbs_Cylinder:
            label = "through_hole" if abs(z - 5.0) < 1e-6 else "blind_hole"
        elif abs(area - 50.265482) < 0.01 and abs(z - 4.0) < 1e-6:
            label = "blind_hole"
        else:
            label = "stock"
        entity = STEPConstruct.FindEntity_s(finder, f)
        entity.SetName(TCollection_HAsciiString(str(_mfcad_index(label))))
    writer.Write(str(path))


def _expected_class(geom: im.FaceGeom) -> str:
    if geom.surf == "cylinder":
        return "hole_through" if abs(geom.centroid[2] - 5.0) < 1e-3 else "hole_blind"
    if abs(geom.area - 50.265482) < 1e-3:
        return "hole_blind"
    return "stock"


@needs_ocp
def test_mfcadpp_face_names_are_read_as_class_labels(tmp_path):
    step = tmp_path / "plate.step"
    _write_plate_step(step, reorder=False)
    part = im.read_mfcadpp(step)
    assert len(part.faces) == len(PLATE)
    for f in part.faces:
        assert im.to_canon("mfcadpp", f.source_class) == _expected_class(f.geom)


SLOT_X, SLOT_FLOOR_Z = (15.0, 25.0), 6.0


def _write_mfcad_slot_block(step, truth) -> dict:
    """A 40x30x10 block with a 10 wide, 4 deep rectangular through slot along Y, written the
    way MFCAD is: each ADVANCED_FACE named with a shuffled face id, `<stem>.face_truth` a pickled
    list of MFCAD16 class indices by face id. Returns {face id: expected CANON class}."""
    import pickle

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.STEPConstruct import STEPConstruct
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TCollection import TCollection_HAsciiString
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 30, 10).Shape()
    cutter = BRepPrimAPI_MakeBox(gp_Pnt(SLOT_X[0], -1, SLOT_FLOOR_Z), 10, 32, 5).Shape()
    shape = BRepAlgoAPI_Cut(box, cutter).Shape()
    faces = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        faces.append(TopoDS.Face_s(exp.Current()))
        exp.Next()
    ids = list(range(len(faces)))
    random.Random(3).shuffle(ids)
    assert ids != sorted(ids)
    writer = STEPControl_Writer()
    finder = writer.WS().TransferWriter().FinderProcess()
    writer.Transfer(shape, STEPControl_AsIs)
    labels, expected = [None] * len(faces), {}
    for f, fid in zip(faces, ids):
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, props)
        c = props.CentreOfMass()
        slot = min(abs(c.X() - x) for x in SLOT_X) < 1e-6 or abs(c.Z() - SLOT_FLOOR_Z) < 1e-6
        name = "rectangular_through_slot" if slot else "stock"
        labels[fid] = im.MFCAD16_CLASSES.index(name)
        expected[fid] = "slot_through" if slot else "stock"
        STEPConstruct.FindEntity_s(finder, f).SetName(TCollection_HAsciiString(str(fid)))
    writer.Write(str(step))
    truth.write_bytes(pickle.dumps(labels))
    assert sorted(expected.values()).count("slot_through") == 3
    return expected


@needs_ocp
def test_mfcad_labels_are_looked_up_by_face_id_not_face_order(tmp_path):
    import pickle

    step, truth = tmp_path / "0-23.step", tmp_path / "0-23.face_truth"
    expected = _write_mfcad_slot_block(step, truth)
    part = im.read_mfcad(step, truth)
    _, names = im.read_step_faces(step)
    assert part.dataset == "mfcad" and part.license == "MIT" and part.original_id == "0-23"
    assert [im.to_canon("mfcad", f.source_class) for f in part.faces] == \
        [expected[int(n)] for n in names]

    truth.write_bytes(pickle.dumps(pickle.loads(truth.read_bytes())[:-1]))
    with pytest.raises(im.LabelError, match="labels"):
        im.read_mfcad(step, truth)
    truth.write_bytes(pickle.dumps(dataclasses.replace))  # a global reference, not plain data
    with pytest.raises(im.LabelError, match="plain data"):
        im.read_mfcad(step, truth)


@needs_engine
def test_mfcad_slot_becomes_one_through_slot_feature(tmp_path):
    step, truth = tmp_path / "s.step", tmp_path / "s.face_truth"
    _write_mfcad_slot_block(step, truth)
    out = im.label_part(im.read_mfcad(step, truth))
    assert out.report["match_rate"] == 1.0 and out.record is not None
    (slot,) = out.record["features"]
    assert slot["class"] == "slot_through" and slot["through"] is True
    assert len(slot["faces"]) == 3 and slot["opening_side"] == "+Z"


@needs_engine
def test_engine_labels_follow_geometry_through_a_reordered_step(tmp_path):
    natural, shuffled = tmp_path / "natural.step", tmp_path / "shuffled.step"
    _write_plate_step(natural, reorder=False)
    _write_plate_step(shuffled, reorder=True)
    # Source labels from one face order, engine import of the other file.
    part = dataclasses.replace(im.read_mfcadpp(natural), file=shuffled)
    doc = im.import_step(shuffled)
    try:
        eng = im.engine_faces(doc)
    finally:
        cb.call("doc_close", {"doc_id": doc})
    assert [round(e.geom.area, 3) for e in eng] != [round(f.geom.area, 3) for f in part.faces]

    out = im.label_part(part)
    assert out.report["match_rate"] == 1.0 and out.record is not None
    rec = out.record
    assert len(rec["faces"]) == len(PLATE)
    for face, e in zip(rec["faces"], eng):
        assert face["ordinal"] == e.ordinal and face["face_id"] == e.face_id
        assert face["class"] == _expected_class(e.geom)
    feats = {f["class"]: f for f in rec["features"]}
    assert feats["hole_through"]["through"] is True
    assert feats["hole_blind"]["through"] is False
    assert feats["hole_blind"]["opening_side"] == "+Z"
    assert len(feats["hole_blind"]["faces"]) == 2


@needs_engine
def test_gsl_labels_join_each_body_on_its_own_face_ids():
    from services.mcp.core_bridge import dim, feat
    from tools.gsl_dataset_gen import _label_faces
    ops = [feat("box", "Base", {"length": dim(60), "width": dim(40), "height": dim(10)}),
           feat("cylinder", "Peg", {"radius": dim(5), "height": dim(20)}, {},
                at=[100, 0, 0], mode="boss", merge=False)]
    doc = cb.call("doc_new", {"kind": "part", "name": "viewnet_two_body"})["doc_id"]
    try:
        cb.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
        matches = _label_faces(doc, cb.call("doc_tree", {"doc_id": doc}))["matches"]
    finally:
        cb.call("doc_close", {"doc_id": doc})
    assert {m["body"] for m in matches} == {1, 2}
    assert all(m["label"] == ("box" if m["body"] == 1 else "cylinder") for m in matches)


@needs_engine
def test_gsl_bolt_circle_becomes_one_through_hole_instance_per_bolt(tmp_path):
    from tools.gsl_dataset_gen import FAMILIES
    bolts = FAMILIES["plate_bolt_circle"](random.Random(4))[1]["feature"]["params"]["count"]
    part = im.gsl_source_part("plate_bolt_circle", 4, tmp_path)
    reloaded = im.load_gsl_part(part.file)
    assert [f.source_class for f in reloaded.faces] == [f.source_class for f in part.faces]
    out = im.label_part(reloaded)
    assert out.record is not None, out.report
    holes = [f for f in out.record["features"] if f["class"] == "hole_through"]
    assert len(holes) == bolts == len(out.record["features"])
    assert all(h["through"] is True and h["axis"] == pytest.approx([0, 0, 1]) for h in holes)
    assert Counter(f["class"] for f in out.record["faces"])["stock"] == 6
