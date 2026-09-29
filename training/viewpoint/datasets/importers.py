"""Per-dataset face-label readers, the CANON mapping, and the engine face correspondence.

A source dataset labels the faces of ITS OWN reading of a STEP file. Our training records label
the faces of OUR engine's import of that file (``import_file`` -> ``doc_query`` ordinals), so every
label crosses a correspondence step that is verified by geometry, never by enumeration order:

1. the source faces are enumerated independently (OCP reads the STEP; GSL faces come from the
   generating engine doc) with their surface type, area and centroid;
2. the STEP is imported with the real engine and its faces enumerated with ``doc_query``;
3. each source face is matched to the engine face with the same surface type whose centroid and
   area agree within tolerance (one-to-one, cheapest pairs first);
4. a part below ``MIN_MATCH_RATE`` matched faces (either side) is dropped and reported.

Matched faces take the CANON class of their source label (tables below and
``contracts/geom_labels.md``). Faces are grouped into feature instances — the dataset's own
instances where it has them (MFInstSeg), otherwise connected faces of one class family over the
engine's face adjacency (split by owning feature for GSL) — and each instance gets its centroid,
axis, opening side and through/blind state.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import pickle
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

SCHEMA = "chudware.geomlabel/1"
UNITS = "mm"
MIN_MATCH_RATE = 0.98
POS_TOL_REL = 1e-3       # centroid tolerance as a fraction of the part's bbox diagonal
POS_TOL_MIN_MM = 1e-3
AREA_TOL_REL = 1e-2
AREA_TOL_ABS_MM2 = 1e-4
ADJ_QUANT_MM = 1e-4      # shared tessellation vertices are bit-identical; this only de-noises

CANON = (
    "hole_through", "hole_blind", "passage", "pocket", "slot_through", "slot_blind",
    "step_through", "step_blind", "o_ring", "boss", "rib", "fillet", "chamfer", "stock",
    "extrude_side", "extrude_end", "cut_side", "cut_end", "revolve_side", "revolve_end", "other",
)
UNMATCHED = "unmatched"      # source_class of an engine face no source face matched (class other)
HOLE_UNRESOLVED = "hole"     # GSL hole faces until their instance's through/blind is measured

# MFCAD++ and MFInstSeg share one class-index table: Utils/parameters.py `feat_names` in both
# generators (gitlab.com/CAPP-SRC/mfcad2-dataset, github.com/whjdark/AAGNet/dataset).
MFCAD_CLASSES = (
    "chamfer", "through_hole", "triangular_passage", "rectangular_passage", "6sides_passage",
    "triangular_through_slot", "rectangular_through_slot", "circular_through_slot",
    "rectangular_through_step", "2sides_through_step", "slanted_through_step", "Oring",
    "blind_hole", "triangular_pocket", "rectangular_pocket", "6sides_pocket",
    "circular_end_pocket", "rectangular_blind_slot", "v_circular_end_blind_slot",
    "h_circular_end_blind_slot", "triangular_blind_step", "circular_blind_step",
    "rectangular_blind_step", "round", "stock",
)
MFCAD_TO_CANON = {
    "chamfer": "chamfer",
    "through_hole": "hole_through",
    "triangular_passage": "passage",
    "rectangular_passage": "passage",
    "6sides_passage": "passage",
    "triangular_through_slot": "slot_through",
    "rectangular_through_slot": "slot_through",
    "circular_through_slot": "slot_through",
    "rectangular_through_step": "step_through",
    "2sides_through_step": "step_through",
    "slanted_through_step": "step_through",
    "Oring": "o_ring",
    "blind_hole": "hole_blind",
    "triangular_pocket": "pocket",
    "rectangular_pocket": "pocket",
    "6sides_pocket": "pocket",
    "circular_end_pocket": "pocket",
    "rectangular_blind_slot": "slot_blind",
    "v_circular_end_blind_slot": "slot_blind",
    "h_circular_end_blind_slot": "slot_blind",
    "triangular_blind_step": "step_blind",
    "circular_blind_step": "step_blind",
    "rectangular_blind_step": "step_blind",
    "round": "fillet",
    "stock": "stock",
}

# Original MFCAD (github.com/hducg/MFCAD): `FEAT_NAMES` in occ/dataset_visualizer.py. Every name
# is also an MFCAD++ class with the same meaning, so the CANON map is the MFCAD++ one.
MFCAD16_CLASSES = (
    "rectangular_through_slot", "triangular_through_slot", "rectangular_passage",
    "triangular_passage", "6sides_passage", "rectangular_through_step", "2sides_through_step",
    "slanted_through_step", "rectangular_blind_step", "triangular_blind_step",
    "rectangular_blind_slot", "rectangular_pocket", "triangular_pocket", "6sides_pocket",
    "chamfer", "stock",
)
MFCAD16_TO_CANON = {c: MFCAD_TO_CANON[c] for c in MFCAD16_CLASSES}
MFCAD_URL = "https://github.com/hducg/MFCAD"

# Fusion 360 Gallery segmentation `segment_names.json` order.
FUSION_SEG_CLASSES = ("ExtrudeSide", "ExtrudeEnd", "CutSide", "CutEnd", "Fillet", "Chamfer",
                      "RevolveSide", "RevolveEnd")
FUSION_TO_CANON = {
    "ExtrudeSide": "extrude_side", "ExtrudeEnd": "extrude_end",
    "CutSide": "cut_side", "CutEnd": "cut_end",
    "Fillet": "fillet", "Chamfer": "chamfer",
    "RevolveSide": "revolve_side", "RevolveEnd": "revolve_end",
}

# GSL source classes are produced by gsl_source_classes() below; anything else maps to other.
GSL_TO_CANON = {
    "stock": "stock",
    "boss": "boss",
    "hole": HOLE_UNRESOLVED,
    "extrude.boss/side": "extrude_side",
    "extrude.boss/end": "extrude_end",
    "extrude.cut/side": "cut_side",
    "extrude.cut/end": "cut_end",
    "revolve/side": "revolve_side",
    "revolve/end": "revolve_end",
    "rib": "rib",
    "fillet": "fillet",
    "chamfer": "chamfer",
    "shell": "pocket",
}
GSL_LICENSE = "Chudware-generated (tools/gsl_dataset_gen.py on the Chudware engine)"
GSL_URL = "repo:tools/gsl_dataset_gen.py"
_GSL_PRIMITIVES = frozenset({"box", "cylinder", "sphere", "cone", "torus", "wedge"})
_GSL_HOLES = frozenset({"bolt_circle", "hole", "hole_wizard", "cbore", "csk", "counterbore",
                        "countersink", "tapped_hole", "hole_pattern"})
_GSL_PATTERNS = frozenset({"circular_pattern", "linear_pattern"})
_DATUM_AXES = {"axis.x": (1.0, 0.0, 0.0), "axis.y": (0.0, 1.0, 0.0), "axis.z": (0.0, 0.0, 1.0)}

_THROUGH = frozenset({"hole_through", "slot_through", "step_through", "passage"})
_BLIND = frozenset({"hole_blind", "slot_blind", "step_blind", "pocket"})
_FAMILY = {
    "hole_through": "hole", "hole_blind": "hole", HOLE_UNRESOLVED: "hole",
    "slot_through": "slot", "slot_blind": "slot",
    "step_through": "step", "step_blind": "step",
    "extrude_side": "extrude", "extrude_end": "extrude",
    "cut_side": "cut", "cut_end": "cut",
    "revolve_side": "revolve", "revolve_end": "revolve",
}
_OP_FAMILIES = frozenset({"extrude", "cut", "revolve"})
_AXIS_FAMILIES = frozenset({"hole", "slot", "step", "passage", "pocket", "o_ring", "boss",
                            "extrude", "cut", "revolve"})
_SIDE_FAMILIES = frozenset({"hole", "slot", "step", "passage", "pocket", "o_ring", "boss",
                            "rib", "extrude", "cut"})
_NO_INSTANCE = frozenset({"stock"})


class LabelError(RuntimeError):
    """A part whose labels cannot be read or mapped (reported, never silently relabelled)."""


# ------------------------------------------------------------------------------ data types

@dataclass(frozen=True)
class FaceGeom:
    surf: str                              # plane|cylinder|cone|sphere|torus|bspline|other
    area: float                            # mm^2
    centroid: tuple                        # mm
    normal: Optional[tuple] = None         # signed outward normal (plane) / engine rep_dir
    axis: Optional[tuple] = None           # cylinder/cone axis (unsigned), plane normal


@dataclass(frozen=True)
class SourceFace:
    index: int
    geom: FaceGeom
    source_class: str


@dataclass
class SourcePart:
    dataset: str
    original_id: str
    file: Path
    license: str
    url: str
    faces: list
    instances: Optional[list] = None       # groups of source-face indices (dataset ground truth)
    owners: Optional[list] = None          # per source face owning feature id (GSL)


@dataclass(frozen=True)
class EngineFace:
    ordinal: int
    body: int
    face_id: int
    geom: FaceGeom


@dataclass
class MatchResult:
    pairs: dict                            # source index -> engine ordinal
    unmatched_source: list
    unmatched_engine: list
    rate: float


@dataclass
class LabelOutcome:
    record: Optional[dict]
    report: dict


# ---------------------------------------------------------------------------- CANON maps

def to_canon(dataset: str, source_class: str) -> str:
    """CANON class of a source label (HOLE_UNRESOLVED for GSL holes until measured)."""
    if dataset in ("mfcadpp", "mfinstseg", "mfcadpp_gen"):
        table = MFCAD_TO_CANON
    elif dataset == "mfcad":
        table = MFCAD16_TO_CANON
    elif dataset == "fusion360_seg":
        table = FUSION_TO_CANON
    elif dataset == "gsl":
        return GSL_TO_CANON.get(source_class, "other")
    else:
        raise LabelError(f"no CANON table for dataset {dataset!r}")
    if source_class not in table:
        raise LabelError(f"{dataset}: unknown source class {source_class!r}")
    return table[source_class]


def family(cls: str) -> str:
    return _FAMILY.get(cls, cls)


def split_of(part_id: str) -> str:
    """Deterministic whole-part split: sha1(part_id) mod 100 -> <80 train, <90 val, else test."""
    b = int(hashlib.sha1(part_id.encode("utf-8")).hexdigest(), 16) % 100
    return "train" if b < 80 else ("val" if b < 90 else "test")


# --------------------------------------------------------------------------- STEP readers

def read_step_faces(path: Path) -> tuple:
    """(faces, names): every unique face of the STEP in TopExp_Explorer order (the order the
    MFCAD++/MFInstSeg/BRepNet tooling indexes labels in), with the STEP entity name of each."""
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepGProp import BRepGProp
        from OCP.GeomAbs import GeomAbs_SurfaceType as ST
        from OCP.GProp import GProp_GProps
        from OCP.IFSelect import IFSelect_ReturnStatus
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopAbs import TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS
        from OCP.TopTools import TopTools_IndexedMapOfShape
    except ImportError as e:  # reported by name; the engine path does not need OCP
        raise LabelError(f"OCP (OpenCASCADE Python bindings) is required to read source "
                         f"STEP labels: {e}") from e
    surf_names = {ST.GeomAbs_Plane: "plane", ST.GeomAbs_Cylinder: "cylinder",
                  ST.GeomAbs_Cone: "cone", ST.GeomAbs_Sphere: "sphere",
                  ST.GeomAbs_Torus: "torus", ST.GeomAbs_BSplineSurface: "bspline"}
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise LabelError(f"{path}: STEP read failed")
    reader.TransferRoots()
    shape = reader.OneShape()
    transfer = reader.WS().TransferReader()
    seen = TopTools_IndexedMapOfShape()
    faces, names = [], []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        exp.Next()
        if seen.Contains(face):
            continue
        seen.Add(face)
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        c = props.CentreOfMass()
        faces.append(FaceGeom(surf_names.get(BRepAdaptor_Surface(face).GetType(), "other"),
                              float(props.Mass()), (c.X(), c.Y(), c.Z())))
        entity = transfer.EntityFromShapeResult(face, 1)
        name = ""
        if entity is not None and hasattr(entity, "Name") and entity.Name() is not None:
            name = entity.Name().ToCString()
        names.append(name)
    if not faces:
        raise LabelError(f"{path}: no faces")
    return faces, names


def read_mfcadpp(step: Path, original_id: Optional[str] = None) -> SourcePart:
    """MFCAD++: the class index of every face is its ADVANCED_FACE name."""
    faces, names = read_step_faces(step)
    out = []
    for i, (g, n) in enumerate(zip(faces, names)):
        try:
            k = int(n)
        except ValueError:
            raise LabelError(f"{step}: face {i} has no class index (name {n!r})") from None
        if not 0 <= k < len(MFCAD_CLASSES):
            raise LabelError(f"{step}: face {i} class index {k} out of range")
        out.append(SourceFace(i, g, MFCAD_CLASSES[k]))
    return SourcePart("mfcadpp", original_id or step.stem, step, "CC-BY-4.0",
                      "https://doi.org/10.17034/d1fec5a0-8c10-4630-b02e-b92dc81df823", out)


class _PlainDataUnpickler(pickle.Unpickler):
    """Unpickles builtin containers and scalars only; any class/function reference is refused,
    so a downloaded label file cannot execute code."""

    def find_class(self, module, name):
        raise LabelError(f"label pickle references {module}.{name}; only plain data is accepted")


def read_face_truth(path: Path) -> list:
    """MFCAD `<stem>.face_truth`: a pickled list, class index per face id."""
    with open(path, "rb") as f:
        try:
            raw = _PlainDataUnpickler(f).load()
        except (pickle.UnpicklingError, EOFError, ValueError) as e:
            raise LabelError(f"{path}: unreadable label pickle: {e}") from e
    if not isinstance(raw, (list, tuple)) or not all(
            isinstance(k, int) and not isinstance(k, bool) for k in raw):
        raise LabelError(f"{path}: expected a list of class indices, got {type(raw).__name__}")
    return list(raw)


def read_mfcad(step: Path, face_truth: Path, original_id: Optional[str] = None) -> SourcePart:
    """MFCAD: every ADVANCED_FACE is named with its face id; `face_truth[id]` is its class
    index into MFCAD16_CLASSES."""
    faces, names = read_step_faces(step)
    truth = read_face_truth(face_truth)
    if len(truth) != len(faces):
        raise LabelError(f"{step}: {len(faces)} faces but {len(truth)} labels")
    out, seen = [], set()
    for i, (g, n) in enumerate(zip(faces, names)):
        try:
            fid = int(n)
        except ValueError:
            raise LabelError(f"{step}: face {i} has no face id (name {n!r})") from None
        if not 0 <= fid < len(truth) or fid in seen:
            raise LabelError(f"{step}: face {i} id {fid} out of range or repeated")
        seen.add(fid)
        k = truth[fid]
        if not 0 <= k < len(MFCAD16_CLASSES):
            raise LabelError(f"{step}: face id {fid} class index {k} out of range")
        out.append(SourceFace(i, g, MFCAD16_CLASSES[k]))
    return SourcePart("mfcad", original_id or step.stem, step, "MIT", MFCAD_URL, out)


def read_mfinstseg(step: Path, labels: Path, original_id: Optional[str] = None) -> SourcePart:
    """MFInstSeg: `seg` {face index: class index}, `inst` dense same-instance matrix."""
    raw = json.loads(labels.read_text())
    rec = raw[0][1] if isinstance(raw, list) else raw
    seg, inst = rec["seg"], rec["inst"]
    faces, _ = read_step_faces(step)
    if len(seg) != len(faces) or len(inst) != len(faces):
        raise LabelError(f"{step}: {len(faces)} faces but {len(seg)} labels")
    out = []
    for i, g in enumerate(faces):
        k = int(seg[str(i)])
        if not 0 <= k < len(MFCAD_CLASSES):
            raise LabelError(f"{step}: face {i} class index {k} out of range")
        out.append(SourceFace(i, g, MFCAD_CLASSES[k]))
    groups, assigned = [], set()
    for i, row in enumerate(inst):
        if not row[i] or i in assigned:
            continue
        members = [j for j, v in enumerate(row) if v]
        assigned.update(members)
        groups.append(members)
    return SourcePart("mfinstseg", original_id or step.stem, step,
                      "unstated (AAGNet code MIT)", "https://github.com/whjdark/AAGNet", out,
                      instances=groups)


def read_mfcadpp_gen(step: Path, original_id: Optional[str] = None) -> SourcePart:
    """A part from our port of the MFCAD++ generator (mfcadpp_gen/): every ADVANCED_FACE is
    named with its face id; the `<seed>.labels.json` sidecar gives each id its class (the
    MFCAD_CLASSES table), generator feature instance and the generator's own face area and
    centroid, which must agree with the STEP as read back (§5 tolerances) — a guard on the
    name -> face join. The surface type is not compared: a STEP round trip may re-type a
    surface (a fillet torus reads back as a surface of revolution); matching uses the read."""
    from .mfcadpp_gen import (GENERATOR_VERSION, LICENSE, SIDECAR_SCHEMA,  # noqa: PLC0415
                              UPSTREAM_URL, sidecar_path)
    side_file = sidecar_path(step)
    if not side_file.exists():
        raise LabelError(f"{step}: no label sidecar {side_file.name}")
    side = json.loads(side_file.read_text())
    if side.get("schema") != SIDECAR_SCHEMA or side.get("generator_version") != GENERATOR_VERSION:
        raise LabelError(f"{side_file}: schema/version {side.get('schema')}/"
                         f"{side.get('generator_version')} is not "
                         f"{SIDECAR_SCHEMA}/{GENERATOR_VERSION}")
    by_id = {int(f["id"]): f for f in side["faces"]}
    faces, names = read_step_faces(step)
    if len(by_id) != len(faces) or len(side["faces"]) != len(faces):
        raise LabelError(f"{step}: {len(faces)} faces but {len(side['faces'])} sidecar labels")
    lo = [min(g.centroid[k] for g in faces) for k in range(3)]
    hi = [max(g.centroid[k] for g in faces) for k in range(3)]
    pos_tol = max(POS_TOL_REL * _norm(tuple(h - l for l, h in zip(lo, hi))), POS_TOL_MIN_MM)
    out, seen, groups = [], set(), defaultdict(list)
    for i, (g, n) in enumerate(zip(faces, names)):
        try:
            fid = int(n)
        except ValueError:
            raise LabelError(f"{step}: face {i} has no face id (name {n!r})") from None
        if fid not in by_id or fid in seen:
            raise LabelError(f"{step}: face {i} id {fid} unknown or repeated")
        seen.add(fid)
        f = by_id[fid]
        if f["class"] not in MFCAD_CLASSES:
            raise LabelError(f"{step}: face id {fid} class {f['class']!r} not in the MFCAD++ table")
        d = _norm(tuple(a - b for a, b in zip(g.centroid, f["centroid"])))
        if d > pos_tol or abs(f["area"] - g.area) > (AREA_TOL_REL * max(f["area"], g.area)
                                                     + AREA_TOL_ABS_MM2):
            raise LabelError(f"{step}: face id {fid} read back with {g.area:.4f} mm² at "
                             f"{d:.4g} mm from the generated {f['area']:.4f} mm²")
        out.append(SourceFace(i, g, f["class"]))
        if int(f["instance"]) >= 0:
            groups[int(f["instance"])].append(i)
    return SourcePart("mfcadpp_gen", original_id or str(side["seed"]), step, LICENSE, UPSTREAM_URL,
                      out, instances=[groups[k] for k in sorted(groups)])


def read_fusion_seg(step: Path, seg: Path, segment_names: Optional[Path] = None,
                    original_id: Optional[str] = None) -> SourcePart:
    """Fusion 360 Gallery segmentation: one segment index per face per line of the .seg file."""
    names = FUSION_SEG_CLASSES
    if segment_names is not None:
        names = tuple(json.loads(segment_names.read_text()))
    idx = [int(t) for t in seg.read_text().split()]
    faces, _ = read_step_faces(step)
    if len(idx) != len(faces):
        raise LabelError(f"{step}: {len(faces)} faces but {len(idx)} segment labels")
    out = [SourceFace(i, g, names[k]) for i, (g, k) in enumerate(zip(faces, idx))]
    return SourcePart("fusion360_seg", original_id or step.stem, step,
                      "Fusion 360 Gallery Dataset License (non-commercial)",
                      "https://github.com/AutodeskAILab/Fusion360GalleryDataset", out)


# ---------------------------------------------------------------------------- vector math

def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a) -> Optional[tuple]:
    n = _norm(a)
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else None


def _positive_dominant(a) -> tuple:
    k = max(range(3), key=lambda i: abs(a[i]))
    return a if a[k] >= 0 else (-a[0], -a[1], -a[2])


def _side_label(v) -> Optional[str]:
    k = max(range(3), key=lambda i: abs(v[i]))
    if abs(v[k]) < 0.5:
        return None
    return ("+" if v[k] > 0 else "-") + "XYZ"[k]


def _principal(faces: list, largest: bool) -> Optional[tuple]:
    """Largest/smallest eigenvector of sum(area * n n^T) over faces with a normal."""
    import numpy as np  # noqa: PLC0415
    m = np.zeros((3, 3))
    for g in faces:
        if g.normal is not None:
            n = np.asarray(g.normal, dtype=float)
            m += g.area * np.outer(n, n)
    if not m.any():
        return None
    w, v = np.linalg.eigh(m)
    vec = v[:, -1 if largest else 0]
    return tuple(float(x) for x in vec)


# ---------------------------------------------------------------------------- matching

def match_faces(src: Sequence[FaceGeom], eng: Sequence[FaceGeom], *, diag: float,
                pos_tol_rel: float = POS_TOL_REL, area_tol_rel: float = AREA_TOL_REL) -> MatchResult:
    """One-to-one geometric correspondence: same surface type, centroid within
    max(pos_tol_rel * diag, POS_TOL_MIN_MM), area within area_tol_rel; cheapest pairs first.
    Enumeration order plays no part."""
    pos_tol = max(pos_tol_rel * diag, POS_TOL_MIN_MM)
    by_surf = defaultdict(list)
    for j, e in enumerate(eng):
        by_surf[e.surf].append(j)
    cands = []
    for i, s in enumerate(src):
        for j in by_surf.get(s.surf, ()):
            e = eng[j]
            d = _norm((s.centroid[0] - e.centroid[0], s.centroid[1] - e.centroid[1],
                       s.centroid[2] - e.centroid[2]))
            if d > pos_tol:
                continue
            da = abs(s.area - e.area)
            if da > area_tol_rel * max(s.area, e.area) + AREA_TOL_ABS_MM2:
                continue
            rel = da / max(s.area, e.area, AREA_TOL_ABS_MM2)
            cands.append((d / pos_tol + rel / area_tol_rel, i, j))
    cands.sort()
    pairs, used = {}, set()
    for _, i, j in cands:
        if i in pairs or j in used:
            continue
        pairs[i] = j
        used.add(j)
    n = max(len(src), len(eng), 1)
    return MatchResult(pairs=pairs,
                       unmatched_source=[i for i in range(len(src)) if i not in pairs],
                       unmatched_engine=[j for j in range(len(eng)) if j not in used],
                       rate=len(pairs) / n)


# ------------------------------------------------------------------ instances + geometry

def _components(members: set, adjacency: Sequence[set]) -> list:
    """Connected components of `members` over `adjacency`, each sorted, in ordinal order."""
    left, out = set(members), []
    for start in sorted(members):
        if start not in left:
            continue
        comp, stack = [], [start]
        left.discard(start)
        while stack:
            f = stack.pop()
            comp.append(f)
            for n in adjacency[f]:
                if n in left:
                    left.discard(n)
                    stack.append(n)
        out.append(sorted(comp))
    return out


def feature_geometry(members: Sequence[int], classes: Sequence[str], eng: Sequence[EngineFace],
                     adjacency: Sequence[set]) -> dict:
    """centroid / axis / opening side / geometric through-ness of one feature instance.

    net = sum of area * outward normal over the instance's PLANAR faces. A removal feature's
    faces point into the removed void, so by the divergence theorem their vector area equals the
    net vector area of the openings: a blind feature has a non-zero net pointing out through its
    opening, a through hole/passage nets to ~0. Geometric through = the instance borders
    non-member faces facing opposite ways (it opens on two opposite sides).

    axis: holes/o-rings/revolves -> the largest cylinder/cone axis; extrude/cut -> the end faces'
    principal normal; pockets/slots/steps/bosses -> the floor (largest planar face within 60° of
    net) normal; otherwise the direction all planar walls are perpendicular to. Signed toward the
    opening when net is significant, else positive along its dominant component."""
    mset = set(members)
    geoms = [eng[f].geom for f in members]
    total = sum(g.area for g in geoms) or 1.0
    centroid = tuple(sum(g.area * g.centroid[k] for g in geoms) / total for k in range(3))
    planar = [g for g in geoms if g.surf == "plane" and g.normal is not None]
    planar_area = sum(g.area for g in planar)
    net = tuple(sum(g.area * g.normal[k] for g in planar) for k in range(3))
    net_sig = planar_area > 0 and _norm(net) > 0.05 * planar_area
    nbr_normals = []
    for f in members:
        for n in adjacency[f]:
            if n not in mset and eng[n].geom.normal is not None:
                nbr_normals.append(eng[n].geom.normal)
    through_geo = any(_dot(a, b) < -0.9 for a, b in itertools.combinations(nbr_normals, 2))

    fam = family(classes[0])
    axis = None
    if fam in _AXIS_FAMILIES:
        curved = [g for g in geoms if g.surf in ("cylinder", "cone") and g.axis is not None]
        if fam in ("hole", "o_ring", "revolve") and curved:
            axis = max(curved, key=lambda g: g.area).axis
        elif fam in ("extrude", "cut"):
            ends = [eng[f].geom for f, c in zip(members, classes) if c.endswith("_end")]
            axis = _principal(ends, largest=True) if ends else _principal(planar, largest=False)
        elif net_sig:
            # "facing along net" = within 60°: side walls whose vector areas do not cancel
            # exactly lean slightly into net (dot > 0) and can outweigh a small floor
            net_len = _norm(net)
            floors = [g for g in planar if _dot(g.normal, net) >= 0.5 * net_len]
            axis = max(floors, key=lambda g: g.area).normal if floors else net
        elif curved and fam == "boss":
            axis = max(curved, key=lambda g: g.area).axis
        else:
            axis = _principal(planar, largest=False)
        if axis is not None:
            axis = _unit(axis)
        if axis is not None:
            if net_sig:
                if _dot(axis, net) < 0:
                    axis = (-axis[0], -axis[1], -axis[2])
            else:
                axis = _positive_dominant(axis)
    side = _side_label(_unit(net)) if fam in _SIDE_FAMILIES and net_sig else None
    return {"centroid": [round(x, 6) for x in centroid],
            "axis": [round(x, 6) for x in axis] if axis is not None else None,
            "opening_side": side, "through_geometric": through_geo}


def _feature_class(member_classes: list, geoms: list) -> str:
    fam = family(member_classes[0])
    if fam in _OP_FAMILIES:
        return f"{fam}_side"
    area = defaultdict(float)
    for c, g in zip(member_classes, geoms):
        area[c] += g.area
    return max(sorted(area), key=lambda c: area[c])


def build_record(part: SourcePart, eng: Sequence[EngineFace], adjacency: Sequence[set],
                 match: MatchResult, bbox: dict) -> dict:
    """The chudware.geomlabel/1 record for one matched part (contracts/geom_labels.md)."""
    n = len(eng)
    cls = ["other"] * n
    src_cls = [UNMATCHED] * n
    owner = [None] * n
    for i, j in match.pairs.items():
        sf = part.faces[i]
        cls[j] = to_canon(part.dataset, sf.source_class)
        src_cls[j] = sf.source_class
        if part.owners is not None:
            owner[j] = part.owners[i]
    matched = set(match.pairs.values())

    groups: list = []
    grouped: set = set()
    if part.instances is not None:
        for g in part.instances:
            ords = sorted({match.pairs[i] for i in g if i in match.pairs})
            ords = [o for o in ords if cls[o] not in _NO_INSTANCE]
            if ords:
                groups.append(ords)
                grouped.update(ords)
    keyed = defaultdict(set)
    for j in range(n):
        if j in grouped or j not in matched or cls[j] in _NO_INSTANCE:
            continue
        keyed[(owner[j] or "", family(cls[j]))].add(j)
    for key in sorted(keyed):
        groups.extend(_components(keyed[key], adjacency))
    groups.sort(key=lambda g: g[0])

    instance = [-1] * n
    features = []
    for k, members in enumerate(groups):
        member_classes = [cls[j] for j in members]
        geo = feature_geometry(members, member_classes, eng, adjacency)
        if family(member_classes[0]) == "hole" and any(c == HOLE_UNRESOLVED
                                                       for c in member_classes):
            resolved = "hole_through" if geo["through_geometric"] else "hole_blind"
            for j in members:
                if cls[j] == HOLE_UNRESOLVED:
                    cls[j] = resolved
            member_classes = [cls[j] for j in members]
        fcls = _feature_class(member_classes, [eng[j].geom for j in members])
        if fcls in _THROUGH:
            through = True
        elif fcls in _BLIND:
            through = False
        elif fcls == "cut_side":
            through = geo["through_geometric"]
        else:
            through = None
        for j in members:
            instance[j] = k
        features.append({"instance": k, "class": fcls, "faces": list(members),
                         "through": through, "opening_side": geo["opening_side"],
                         "centroid": geo["centroid"], "axis": geo["axis"]})

    part_id = f"{part.dataset}/{part.original_id}"
    return {
        "schema": SCHEMA,
        "part_id": part_id,
        "split": split_of(part_id),
        "source": {"dataset": part.dataset, "license": part.license, "url": part.url,
                   "original_id": part.original_id},
        "file": str(Path(part.file).resolve()),
        "units": UNITS,
        "faces": [{"ordinal": e.ordinal, "body": e.body, "face_id": e.face_id,
                   "class": cls[e.ordinal], "source_class": src_cls[e.ordinal],
                   "instance": instance[e.ordinal]} for e in eng],
        "features": features,
        "stats": {"n_faces": n, "n_bodies": len({e.body for e in eng}),
                  "bbox": {"min": list(bbox["min"]), "max": list(bbox["max"])},
                  "match_rate": round(match.rate, 6),
                  "n_unmatched": len(match.unmatched_engine)},
    }


# ------------------------------------------------------------------------------- engine

def _cb():
    from services.mcp import core_bridge  # noqa: PLC0415 — engine only where it is needed
    return core_bridge


def _geom_from_match(m: dict) -> FaceGeom:
    def vec(v):
        return tuple(float(x) for x in v) if isinstance(v, (list, tuple)) and len(v) == 3 else None
    return FaceGeom(str(m.get("surf") or "other"), float(m.get("area_or_len") or 0.0),
                    vec(m.get("centroid")) or (0.0, 0.0, 0.0), vec(m.get("normal")),
                    vec(m.get("axis")))


def import_step(path: Path) -> str:
    """Import a STEP exactly the way every consumer of the manifest does."""
    return _cb().call("import_file", {"path": str(path), "format": "step"})["doc_id"]


def engine_faces(doc_id: str) -> list:
    """Every face of the document in doc_query order (ordinal = running index)."""
    q = _cb().call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}})
    out = []
    for i, m in enumerate(q.get("matches") or []):
        body, topo = m.get("body"), m.get("topo")
        if not isinstance(body, int) or not isinstance(topo, int):
            raise LabelError(f"doc_query face {i} lacks the body/topo join keys")
        out.append(EngineFace(i, body, topo, _geom_from_match(m)))
    return out


def engine_adjacency(doc_id: str, faces: Sequence[EngineFace]) -> list:
    """Face adjacency of EVERY body from the engine tessellation: two faces of one body are
    adjacent when they share at least two tessellation vertices (a common B-rep edge — BRepMesh
    discretises each edge once for both faces). doc_adjacency only covers the last body."""
    import numpy as np  # noqa: PLC0415
    from services.mcp import cad_common  # noqa: PLC0415
    mesh = cad_common._doc_mesh(doc_id)
    if mesh is None:
        raise LabelError(f"{doc_id}: no tessellation to derive adjacency from")
    ordinal_of = {(f.body, f.face_id): f.ordinal for f in faces}
    mesh_ord = np.asarray([ordinal_of.get((mf["body"], mf["face_id"]), -1)
                           for mf in mesh["faces"]] + [-1], dtype=np.int64)
    tri_face = np.asarray(mesh["tri_face"], dtype=np.int64)
    tri_ord = mesh_ord[np.where(tri_face >= 0, tri_face, len(mesh_ord) - 1)]
    vert = np.asarray(mesh["idx"], dtype=np.int64).reshape(-1)
    vord = np.repeat(tri_ord, 3)
    keep = vord >= 0
    vert, vord = vert[keep], vord[keep]
    body = np.asarray([f.body for f in faces], dtype=np.int64)[vord]
    key = np.round(np.asarray(mesh["pos"], dtype=np.float64)[vert] / ADJ_QUANT_MM).astype(np.int64)
    rows = np.unique(np.column_stack([body, key, vord]), axis=0)
    adjacency = [set() for _ in faces]
    shared = defaultdict(int)
    if len(rows):
        brk = np.flatnonzero(np.any(rows[1:, :4] != rows[:-1, :4], axis=1)) + 1
        starts = np.concatenate([[0], brk])
        ends = np.concatenate([brk, [len(rows)]])
        for s, e in zip(starts[ends - starts > 1].tolist(), ends[ends - starts > 1].tolist()):
            for a, b in itertools.combinations(rows[s:e, 4].tolist(), 2):
                shared[(a, b)] += 1
    for (a, b), count in shared.items():
        if count >= 2:
            adjacency[a].add(b)
            adjacency[b].add(a)
    return adjacency


def label_part(part: SourcePart, *, min_match: float = MIN_MATCH_RATE) -> LabelOutcome:
    """Import `part.file` with the engine, match faces by geometry, build the record.

    Returns the record (None when dropped) and a report with the match rate and the unmatched
    faces on both sides."""
    cb = _cb()
    doc = import_step(part.file)
    try:
        eng = engine_faces(doc)
        bbox = cb.call("doc_bbox", {"doc_id": doc})
        diag = _norm(tuple(bbox["size"]))
        match = match_faces([f.geom for f in part.faces], [e.geom for e in eng], diag=diag)
        report = {"part_id": f"{part.dataset}/{part.original_id}",
                  "n_source_faces": len(part.faces), "n_engine_faces": len(eng),
                  "match_rate": round(match.rate, 6),
                  "unmatched_source": match.unmatched_source,
                  "unmatched_engine": match.unmatched_engine, "dropped": None}
        if match.rate < min_match:
            report["dropped"] = f"match_rate {match.rate:.3f} < {min_match}"
            return LabelOutcome(None, report)
        adjacency = engine_adjacency(doc, eng)
        return LabelOutcome(build_record(part, eng, adjacency, match, bbox), report)
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc})
        except cb.CoreError:
            pass


# ------------------------------------------------------------------------------------ GSL

def _feature_mode(feat: dict) -> str:
    return str(feat.get("mode") or (feat.get("params") or {}).get("mode") or "boss")


def _sketch_normal(doc_id: str, sketch_id: str) -> Optional[tuple]:
    frame = _cb().call("doc_sketch_overview", {"doc_id": doc_id, "sketch": sketch_id}).get(
        "plane_frame")
    if not isinstance(frame, list) or len(frame) != 16:
        return None
    return _unit((frame[2], frame[6], frame[10]))  # row-major; column 2 is the plane normal


def _ref_axis(refs: dict) -> Optional[tuple]:
    ax = refs.get("axis")
    if isinstance(ax, dict) and ax.get("kind") == "datum":
        return _DATUM_AXES.get(str(ax.get("feature")))
    if isinstance(ax, str):
        return _DATUM_AXES.get(ax)
    return None


def gsl_source_classes(doc_id: str, tree: dict, owners: Sequence[Optional[str]],
                       geoms: Sequence[FaceGeom]) -> list:
    """GSL source class per face from the owning feature (GSL_TO_CANON keys, or the raw type).

    extrude side/end: a planar face whose normal is parallel to the profile sketch normal is an
    end, every other face a side. revolve end: a planar face whose normal is perpendicular to the
    revolve axis (a partial revolve's profile planes). A circular-pattern copy keeps its seed's
    classification only when the pattern axis is parallel to the seed's extrude direction."""
    feats = [f for f in ((tree.get("tree") or {}).get("features") or []) if isinstance(f, dict)]
    by_id = {f.get("id"): f for f in feats}
    base = None
    for f in feats:
        t = f.get("type")
        if t in _GSL_PRIMITIVES or (t in ("extrude", "revolve", "sweep", "loft")
                                    and _feature_mode(f) != "cut"):
            base = f.get("id")
            break
    direction_cache: dict = {}

    def extrude_dir(f: dict) -> Optional[tuple]:
        fid = f.get("id")
        if fid not in direction_cache:
            prof = ((f.get("refs") or {}).get("profile") or [{}])
            sk = prof[0].get("sketch") if isinstance(prof, list) and prof else None
            direction_cache[fid] = _sketch_normal(doc_id, sk) if isinstance(sk, str) else None
        return direction_cache[fid]

    out = []
    for fid, g in zip(owners, geoms):
        f = by_id.get(fid) if fid else None
        if f is None:
            out.append("import_or_base")
            continue
        pattern_axis = None
        seen = set()
        while f.get("type") in _GSL_PATTERNS and f.get("id") not in seen:
            seen.add(f.get("id"))
            refs = f.get("refs") or {}
            if f.get("type") == "circular_pattern":
                pattern_axis = _ref_axis(refs)
                if pattern_axis is None:
                    break
            seeds = refs.get("features") or []
            nxt = by_id.get(seeds[0]) if seeds and isinstance(seeds[0], str) else None
            if nxt is None:
                break
            f = nxt
        t = f.get("type")
        if t in _GSL_PATTERNS:
            out.append(str(t))
            continue
        if t in _GSL_PRIMITIVES:
            out.append("stock" if f.get("id") == base else "boss")
        elif t in _GSL_HOLES:
            out.append("hole")
        elif t == "extrude":
            d = extrude_dir(f)
            if d is None or (pattern_axis is not None and abs(_dot(d, pattern_axis)) < 0.99):
                out.append("extrude")
                continue
            mode = "cut" if _feature_mode(f) == "cut" else "boss"
            end = g.surf == "plane" and g.normal is not None and abs(_dot(g.normal, d)) > 0.95
            out.append(f"extrude.{mode}/{'end' if end else 'side'}")
        elif t == "revolve":
            ax = _ref_axis(f.get("refs") or {})
            end = (ax is not None and g.surf == "plane" and g.normal is not None
                   and abs(_dot(g.normal, ax)) < 0.05)
            out.append("revolve/end" if end else "revolve/side")
        else:
            out.append(str(t))
    return out


def gsl_sidecar(step: Path) -> Path:
    return step.with_suffix(".labels.json")


def gsl_source_part(family_name: str, seed: int, out_dir: Path) -> SourcePart:
    """Build GSL sample (family, seed) on the engine, export it to STEP under
    out_dir/<family>/<seed>.step with a label sidecar, and return it as a SourcePart whose
    faces carry the generating doc's geometry and owner-feature labels."""
    from tools.gsl_dataset_gen import FAMILIES, GENERATOR_VERSION, _label_faces  # noqa: PLC0415
    if family_name not in FAMILIES:
        raise LabelError(f"unknown GSL family {family_name!r}")
    cb = _cb()
    ops = FAMILIES[family_name](random.Random(seed))
    doc = cb.call("doc_new", {"kind": "part", "name": f"viewnet_{family_name}_{seed}"})["doc_id"]
    try:
        applied = cb.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
        failed = [s for s in (applied.get("rebuild") or {}).get("feature_status", [])
                  if not s.get("ok", True)]
        if failed:
            raise LabelError(f"gsl {family_name}/{seed}: feature(s) failed to build: {failed}")
        tree = cb.call("doc_tree", {"doc_id": doc})
        matches = _label_faces(doc, tree)["matches"]
        geoms = [_geom_from_match(m) for m in matches]
        owners = [m.get("owner_feature_id") for m in matches]
        classes = gsl_source_classes(doc, tree, owners, geoms)
        step = out_dir / family_name / f"{seed}.step"
        step.parent.mkdir(parents=True, exist_ok=True)
        cb.call("doc_export_step", {"doc_id": doc, "path": str(step)})
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc})
        except cb.CoreError:
            pass
    gsl_sidecar(step).write_text(json.dumps({
        "schema": "chudware.viewnet.gsl-source/1", "family": family_name, "seed": seed,
        "generator_version": GENERATOR_VERSION,
        "faces": [{"surf": g.surf, "area": g.area, "centroid": list(g.centroid),
                   "normal": list(g.normal) if g.normal else None,
                   "axis": list(g.axis) if g.axis else None,
                   "source_class": c, "owner": o} for g, c, o in zip(geoms, classes, owners)],
    }, indent=1))
    return load_gsl_part(step)


def load_gsl_part(step: Path) -> SourcePart:
    """A previously generated GSL sample, from its STEP + label sidecar."""
    side = json.loads(gsl_sidecar(step).read_text())
    faces, owners = [], []
    for i, f in enumerate(side["faces"]):
        g = FaceGeom(f["surf"], float(f["area"]), tuple(f["centroid"]),
                     tuple(f["normal"]) if f.get("normal") else None,
                     tuple(f["axis"]) if f.get("axis") else None)
        faces.append(SourceFace(i, g, f["source_class"]))
        owners.append(f.get("owner"))
    return SourcePart("gsl", f"{side['family']}/{side['seed']}", step, GSL_LICENSE, GSL_URL,
                      faces, owners=owners)
