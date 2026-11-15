"""Chudware part feature tree -> a SolidWorks build plan (the inverse of services.sw_import).

The plan is a plain list of steps in SolidWorks MODEL coordinates (millimetres, degrees).
:mod:`services.sw_export.macro` writes it as one SolidWorks VBA macro; Chudware runs that macro
in an installed SolidWorks (:mod:`services.sw_export.com_build`) and a teammate runs the same file
in their own SolidWorks release (SolidWorks itself writes the part, in that teammate's version).

Strict-fidelity contract (same as the importer): a feature or parameter this cannot rebuild
exactly is listed in ``unsupported`` with the Chudware feature id/name/type and the reason; the
caller refuses a parametric export with a non-empty list rather than write a quietly different
part. ``degraded`` lists what is rebuilt with the exact geometry but less design intent (e.g.
sketch relations and dimensions are not re-created yet).

Bodies: every solid step carries ``body = {"mode": "merge"|"new"|"cut", "target": key|None}``
with the engine's body policy (contracts/doc_schema.md "Solid feature modes"): a merging
feature fuses into every body it touches, ``new`` (or ``merge:false``) adds a body, and a cut
subtracts from ONE body, its ``body`` param or else the current (last-built) one. The macro
names each SolidWorks body after the Chudware feature that owns it, so ``target`` is the
Chudware feature id.

Coordinates: a sketch on a principal plane keeps its 2D coordinates unchanged (the importer copies
them verbatim; validated by SolidWorks center-of-mass on asymmetric parts). A world point maps
engine (x, y, z) -> SolidWorks (x, z, -y): engine is Z-up, SolidWorks Y-up. Primitive profiles
(``sketch3``) are given as 3D model points; the macro maps them into the sketch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Chudware principal datum -> SolidWorks default plane. Each pair's 2D sketch frames coincide.
SW_PLANE = {"plane.top": "Top Plane", "plane.front": "Front Plane", "plane.right": "Right Plane"}
# Engine 2D sketch frame (u, v) of each principal plane, in engine world axes.
_PLANE_UV = {"plane.top": ((1, 0, 0), (0, 1, 0)), "plane.front": ((1, 0, 0), (0, 0, 1)),
             "plane.right": ((0, 1, 0), (0, 0, 1))}
_PLANE_N = {"plane.top": (0, 0, 1), "plane.front": (0, -1, 0), "plane.right": (1, 0, 0)}
# Face-side words (contracts/selectors.md) -> engine outward normal.
_SIDE_NORMAL = {"top": (0, 0, 1), "bottom": (0, 0, -1), "front": (0, -1, 0),
                "back": (0, 1, 0), "right": (1, 0, 0), "left": (-1, 0, 0)}
_SKETCH_ENTITIES = {"line", "circle", "arc", "point", "spline"}
_PRIMITIVES = {"box", "cylinder", "cone", "sphere", "torus"}
# swBodyOperationType_e for the Combine feature.
COMBINE_OP = {"fuse": 15903, "cut": 15902, "common": 15901}
_BOOLEAN_ALIAS = {"combine": "fuse", "union": "fuse", "add": "fuse", "subtract": "cut",
                  "intersect": "common"}
_SIGNED_AXIS = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}
SNAP_MM = 1e-2   # sketch endpoints closer than this become one point (Chudware closes
                 # profiles across such gaps; a SolidWorks contour needs them joined)


def _frame(plane: str, offset: float) -> dict:
    """A default plane shifted ``offset`` along its normal, as an explicit frame (engine)."""
    n = [float(x) for x in _PLANE_N[plane]]
    u, v = ([float(x) for x in a] for a in _PLANE_UV[plane])
    return {"o": _scale(n, offset), "u": u, "v": v, "n": n}


def point_normal_frame(origin, normal) -> dict:
    """The engine's frame of a plane through ``origin`` with unit ``normal``
    (geom/plane.cpp SketchPlane::from): u = +X projected onto the plane (+Y when the normal
    is along X), v = normal x u."""
    cand = [1.0, 0.0, 0.0] if abs(normal[0]) <= 1.0 - 1e-9 else [0.0, 1.0, 0.0]
    u = _unit([cand[i] - _dot(cand, normal) * normal[i] for i in range(3)])
    return {"o": [float(x) for x in origin], "u": u, "v": _cross(normal, u),
            "n": [float(x) for x in normal]}


def _datum(ref: Any) -> Any:
    """{"kind": "datum", "feature": "axis.z"} -> "axis.z"; any other ref unchanged."""
    if isinstance(ref, dict) and ref.get("kind") == "datum" and isinstance(ref.get("feature"), str):
        return ref["feature"]
    return ref


def _principal(ref: Any) -> Optional[str]:
    """"plane.top" for the bare id or the {"kind":"datum","feature":"plane.top"} form."""
    if isinstance(ref, dict) and ref.get("kind") == "datum":
        ref = ref.get("feature")
    return ref if isinstance(ref, str) and ref in SW_PLANE else None


def to_sw(p) -> list[float]:
    """Engine world point/direction (mm) -> SolidWorks model coordinates (mm)."""
    return [float(p[0]), float(p[2]), -float(p[1])]


# Engine -> SolidWorks frame change as a matrix: sw = T . engine.
_T = ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))


def _mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _mv(a, v):
    return [sum(a[i][k] * v[k] for k in range(3)) for i in range(3)]


def _add(*vs):
    return [sum(v[i] for v in vs) for i in range(3)]


def _scale(v, s):
    return [x * s for x in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _unit(v) -> Optional[list[float]]:
    n = math.sqrt(_dot(v, v))
    return [x / n for x in v] if n > 1e-12 else None


def axis_angle(axis, angle_rad: float):
    """Rotation matrix about a unit ``axis`` (right hand)."""
    x, y, z = axis
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    t = 1 - c
    return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c]]


def rot_z_to(axis) -> list[list[float]]:
    """The engine's primitive orientation: the minimal rotation of local +Z onto ``axis``
    (a half turn about +X for -Z), exactly as document.cpp builds it."""
    cosang = axis[2]
    if cosang >= 1.0 - 1e-12:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    if cosang <= -1.0 + 1e-12:
        return axis_angle((1.0, 0.0, 0.0), math.pi)
    return axis_angle(_unit(_cross((0.0, 0.0, 1.0), axis)),
                      math.acos(max(-1.0, min(1.0, cosang))))


def sw_euler(r_sw) -> tuple[float, float, float]:
    """(a, b, c) radians with ``r_sw = Rx(c) . Ry(b) . Rz(a)``: the angle arguments of
    InsertMoveCopyBody2, which rotates about Z, then Y, then X (measured on SolidWorks 2026)."""
    b = math.asin(max(-1.0, min(1.0, r_sw[0][2])))
    if abs(math.cos(b)) > 1e-9:
        a = math.atan2(-r_sw[0][1], r_sw[0][0])
        c = math.atan2(-r_sw[1][2], r_sw[2][2])
    else:   # gimbal lock: fold the X turn into Z
        a, c = math.atan2(r_sw[1][0], r_sw[1][1]), 0.0
    return a, b, c


def engine_rotation_to_sw(r_eng):
    """The same rigid rotation expressed in SolidWorks axes: T . R . T^-1."""
    t_inv = [list(r) for r in zip(*_T)]
    return _mm(_mm(_T, r_eng), t_inv)


@dataclass
class Plan:
    name: str
    steps: list[dict] = field(default_factory=list)
    unsupported: list[dict] = field(default_factory=list)
    degraded: list[dict] = field(default_factory=list)
    expected: dict = field(default_factory=dict)   # {volume_mm3, com_sw_mm, body_count, ...}

    def as_dict(self) -> dict:
        return {"name": self.name, "steps": self.steps, "unsupported": self.unsupported,
                "degraded": self.degraded, "expected": self.expected}


def _dim(v: Any) -> Optional[float]:
    if isinstance(v, dict):
        v = v.get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _vec3(v) -> Optional[list[float]]:
    if isinstance(v, (list, tuple)) and len(v) >= 3 and \
            all(isinstance(x, (int, float)) for x in v[:3]):
        return [float(x) for x in v[:3]]
    return None


def _parse_axis(v) -> Optional[list[float]]:
    """[x,y,z] or "x"/"+y"/"-z" (document.cpp parse_signed_axis) -> unit vector."""
    if isinstance(v, str):
        s = v.strip().lower()
        sign = -1.0 if s.startswith("-") else 1.0
        s = s.lstrip("+-")
        return _scale(list(_SIGNED_AXIS[s]), sign) if s in _SIGNED_AXIS else None
    vec = _vec3(v)
    return _unit(vec) if vec else None


class _Builder:
    def __init__(self, features: list[dict], edge_points: Callable[[str], list],
                 name: str):
        self.features = [f for f in features if not f.get("suppressed")]
        self.edge_points = edge_points
        self.plan = Plan(name=name)
        self.by_id = {f.get("id"): f for f in self.features}
        self.sw_name: dict[str, str] = {}      # chudware feature id -> SolidWorks feature name
        self.sketch_ids: set[str] = set()
        self.sketch_frames: dict[str, Optional[dict]] = {}   # sketch id -> frame (o, u, v, n)
        self.path_kind: dict[str, str] = {}    # path feature id -> SolidWorks selection type
        self.used_names: set[str] = set()

    # -- reporting -------------------------------------------------------------------------
    def refuse(self, f: dict, reason: str) -> None:
        self.plan.unsupported.append({"id": f.get("id"), "name": f.get("name"),
                                      "type": f.get("type"), "reason": reason})

    def degrade(self, f: dict, reason: str) -> None:
        self.plan.degraded.append({"id": f.get("id"), "name": f.get("name"),
                                   "type": f.get("type"), "reason": reason})

    def name_for(self, f: dict, suffix: str = "") -> str:
        base = str(f.get("name") or f.get("id") or f.get("type")) + suffix
        name, k = base, 2
        while name in self.used_names:
            name, k = f"{base}_{k}", k + 1
        self.used_names.add(name)
        if not suffix:
            self.sw_name[f.get("id")] = name
        return name

    def ref_id(self, ref: Any) -> Optional[str]:
        """A feature id from a ref: "f3", "$3"-style op refs are not stored in documents."""
        if isinstance(ref, str):
            return ref if ref in self.by_id else None
        if isinstance(ref, dict):
            for k in ("feature", "sketch", "id", "owner_feature"):
                if isinstance(ref.get(k), str) and ref[k] in self.by_id:
                    return ref[k]
        return None

    def body_key(self, f: dict, ref: Any) -> Optional[str]:
        """A body reference as the macro's body key (the owning feature id, "<fid>#k")."""
        if isinstance(ref, dict) and ref.get("kind") == "body":
            ref = ref.get("owner_feature")
        if isinstance(ref, str):
            base = ref.split("#", 1)[0]
            if base in self.by_id:
                return ref
        self.refuse(f, f"body reference {ref!r} does not name a body-owning feature")
        return None

    def body_policy(self, f: dict, default_mode: str = "boss") -> Optional[dict]:
        """The engine's body policy for a solid feature (document.cpp solid_mode)."""
        p = f.get("params") or {}
        mode = p.get("mode", default_mode)
        if mode not in ("new", "boss", "add", "cut"):
            self.refuse(f, f"mode {mode!r} has no SolidWorks export")
            return None
        if not p.get("merge", True) and mode != "cut":
            mode = "new"
        if mode == "cut":
            target = None
            if p.get("body") is not None:
                target = self.body_key(f, p["body"])
                if target is None:
                    return None
            return {"mode": "cut", "target": target}
        return {"mode": "new" if mode == "new" else "merge", "target": None}

    # -- planes ------------------------------------------------------------------------------
    def sketch_plane(self, f: dict) -> Optional[dict]:
        ref = (f.get("refs") or {}).get("plane")
        # The sketch's own `offset` shifts its plane along the normal (document.cpp).
        extra = _dim((f.get("params") or {}).get("offset")) or 0.0
        if _principal(ref):
            self.sketch_frames[f.get("id")] = _frame(_principal(ref), extra)
            return {"kind": "principal", "plane": SW_PLANE[_principal(ref)]}
        if isinstance(ref, dict) and ref.get("of") == "face" and ref.get("side") in _SIDE_NORMAL:
            if extra:
                self.refuse(f, "a sketch offset from a model face is not exported yet")
                return None
            return {"kind": "face", "normal": to_sw(_SIDE_NORMAL[ref["side"]])}
        pid = self.ref_id(ref)
        datum = self.by_id.get(pid) if pid else None
        if datum and datum.get("type") == "plane":
            p, r = datum.get("params") or {}, datum.get("refs") or {}
            base = _principal(r.get("base"))
            off = _dim(p.get("offset"))
            if p.get("method", "offset") == "offset" and base and off is not None:
                if p.get("flip"):
                    off = -off
                off += extra
                self.sketch_frames[f.get("id")] = _frame(base, off)
                return {"kind": "offset", "base": SW_PLANE[base], "offset": off,
                        "name": f"{datum.get('name') or pid}"}
            if p.get("method") == "point_normal":
                o, n = _vec3(p.get("origin") or r.get("origin")),                     _vec3(p.get("normal") or r.get("normal"))
                n = _unit(n) if n else None
                if o is not None and n is not None:
                    fr = point_normal_frame(o, n)
                    fr["o"] = _add(fr["o"], _scale(n, extra))
                    self.sketch_frames[f.get("id")] = fr
                    return {"kind": "offset", "name": f"{datum.get('name') or pid}"}
            self.refuse(f, "sketch is on a datum plane that is not an offset of Top/Front/"
                           "Right or a point-normal plane")
            return None
        self.refuse(f, f"sketch plane {ref!r} is not Top/Front/Right, an offset of one, or a "
                       "principal-facing model face")
        return None

    # -- sketched features -------------------------------------------------------------------
    def sketch(self, f: dict) -> None:
        plane = self.sketch_plane(f)
        if plane is None:
            return
        params = f.get("params") or {}
        ents = []
        for e in params.get("entities") or []:
            t = e.get("type")
            if t not in _SKETCH_ENTITIES:
                self.refuse(f, f"sketch entity {e.get('id')} of type {t!r} has no SolidWorks "
                               "export yet (line, circle, arc, point and spline do)")
                return
            ents.append({k: e[k] for k in ("type", "construction", "p1", "p2", "center",
                                           "radius", "start", "end", "ccw", "p", "points")
                         if k in e})
        if params.get("constraints"):
            self.degrade(f, "sketch relations and dimensions are not re-created in SolidWorks "
                            "(the sketch geometry is exact, but under-defined there)")
        if params.get("external_refs"):
            self.degrade(f, "converted (external) entities are exported as plain geometry")
        self.sketch_ids.add(f.get("id"))
        self.plan.steps.append({"op": "sketch", "id": f.get("id"), "name": self.name_for(f),
                                "plane": plane, "entities": ents})

    def profile_sketch(self, f: dict) -> Optional[str]:
        prof = (f.get("refs") or {}).get("profile")
        rows = prof if isinstance(prof, list) else [prof]
        ids = {self.ref_id(r) for r in rows if r is not None}
        ids.discard(None)
        if len(ids) != 1 or next(iter(ids)) not in self.sketch_ids:
            self.refuse(f, "profile must be regions of exactly one exported sketch")
            return None
        if len(rows) > 1:
            self.degrade(f, "several profile regions: SolidWorks uses every closed contour of "
                            "the sketch")
        return next(iter(ids))

    def extrude(self, f: dict) -> None:
        p = f.get("params") or {}
        sid = self.profile_sketch(f)
        if sid is None:
            return
        body = self.body_policy(f)
        if body is None:
            return
        end = p.get("end", "blind")
        end2 = p.get("end2", "none")
        if end not in ("blind", "through_all", "mid_plane") or end2 not in ("none", "blind",
                                                                              "through_all"):
            return self.refuse(f, f"end condition {end!r}/{end2!r} has no SolidWorks export "
                                  "(blind, through_all, mid_plane do)")
        for k in ("draft", "start_offset"):
            if (_dim(p.get(k)) or 0.0) != 0.0:
                return self.refuse(f, f"extrude {k} is not exported yet")
        if p.get("start", "sketch_plane") != "sketch_plane" or p.get("thin") or p.get("dir"):
            return self.refuse(f, "extrude start offset, thin walls and explicit directions are "
                                  "not exported yet")
        depth = _dim(p.get("depth"))
        depth = 10.0 if depth is None else depth
        depth2 = _dim(p.get("depth2"))
        on_face = any(st.get("id") == sid and st["op"] == "sketch" and
                      st["plane"]["kind"] == "face" for st in self.plan.steps)
        self.plan.steps.append({
            "op": "extrude", "id": f.get("id"), "name": self.name_for(f), "on_face": on_face,
            "sketch": self.sw_name[sid], "mode": "cut" if body["mode"] == "cut" else "boss",
            "body": body, "end": end, "depth": depth, "flip": bool(p.get("flip")),
            "end2": end2, "depth2": 10.0 if depth2 is None else depth2})

    def _axis_line_2d(self, sid: str, ref: Any) -> Optional[list]:
        """A world axis as a 2D centerline in a principal (or offset) sketch, or None."""
        frame = self.sketch_frames.get(sid)
        if frame is None:
            return None
        ref = _datum(ref)
        if isinstance(ref, str) and ref in ("axis.x", "axis.y", "axis.z"):
            d, o = list(_SIGNED_AXIS[ref[-1]]), [0.0, 0.0, 0.0]
        elif isinstance(ref, dict) and ref.get("kind") == "axis":
            d, o = _parse_axis(ref.get("axis")), _vec3(ref.get("origin") or [0, 0, 0])
            if d is None or o is None:
                return None
        else:
            return None
        n, u, v = frame["n"], frame["u"], frame["v"]
        rel = [o[i] - frame["o"][i] for i in range(3)]
        # The axis must lie in the sketch plane: along it, through a point on it.
        if abs(_dot(d, n)) > 1e-9 or abs(_dot(rel, n)) > 1e-6:
            return None
        p1 = [_dot(rel, u), _dot(rel, v)]
        p2 = [p1[0] + 10 * _dot(d, u), p1[1] + 10 * _dot(d, v)]
        return [p1, p2]

    def revolve(self, f: dict) -> None:
        p, r = f.get("params") or {}, f.get("refs") or {}
        sid = self.profile_sketch(f)
        if sid is None:
            return
        body = self.body_policy(f)
        if body is None:
            return
        step = next(s for s in self.plan.steps if s.get("id") == sid and s["op"] == "sketch")
        lines = [e for e in step["entities"] if e["type"] == "line" and e.get("construction")]
        axis_line = self._axis_line_2d(sid, r.get("axis")) if r.get("axis") is not None \
            else None
        if axis_line is not None:
            # The revolve names its axis: make it the sketch's only centerline.
            step["entities"] = [e for e in step["entities"] if not e.get("construction")
                                or e["type"] != "line"]
            step["entities"].append({"type": "line", "construction": True,
                                     "p1": axis_line[0], "p2": axis_line[1]})
        elif len(lines) != 1:
            return self.refuse(f, "revolve axis must be a world axis in the sketch plane or the "
                                  f"one construction line of its sketch (found {len(lines)})")
        angle = _dim(p.get("angle"))
        angle = 360.0 if angle is None else angle
        if abs(angle) < 360.0 - 1e-9:
            self.degrade(f, "partial revolve: the side SolidWorks sweeps is checked by the "
                            "volume comparison")
        self.plan.steps.append({"op": "revolve", "id": f.get("id"), "name": self.name_for(f),
                                "sketch": self.sw_name[sid], "body": body,
                                "angle": angle, "flip": bool(p.get("flip"))})

    # -- paths, sweeps and lofts -------------------------------------------------------------
    def sketch3d(self, f: dict) -> None:
        """A 3D sketch (lines and arcs through a mid point) as a SolidWorks 3D sketch."""
        params = f.get("params") or {}
        reps: list[list[float]] = []

        def snap(q):   # join endpoints Chudware treats as connected (see SNAP_MM)
            q = [float(x) for x in q]
            for r in reps:
                if all(abs(r[i] - q[i]) <= SNAP_MM for i in range(3)):
                    return r
            reps.append(q)
            return q
        ents = []
        for e in params.get("entities") or []:
            t = e.get("type")
            if e.get("construction"):
                continue
            if t == "line" and _vec3(e.get("p1")) and _vec3(e.get("p2")):
                ents.append({"type": "line", "p1": to_sw(snap(e["p1"])),
                             "p2": to_sw(snap(e["p2"]))})
            elif t == "arc" and all(_vec3(e.get(k)) for k in ("p1", "p2", "pm")):
                ents.append({"type": "arc3", "p1": to_sw(snap(e["p1"])),
                             "p2": to_sw(snap(e["p2"])), "pm": to_sw(e["pm"])})
            elif t == "spline" and len(e.get("points") or []) >= 2 and                     all(_vec3(q) for q in e["points"]):
                pts = [to_sw(q) for q in e["points"]]
                ents.append({"type": "spline", "points": pts, "p1": pts[0], "p2": pts[-1]})
                self.degrade(f, "a 3D spline is re-interpolated by SolidWorks through the same "
                                "points (its shape between them is checked by the volume)")
            else:
                return self.refuse(f, f"3D sketch entity {e.get('id')} of type {t!r} has no "
                                      "SolidWorks export yet (lines and 3-point arcs do)")
        if not ents:
            return self.refuse(f, "3D sketch has no exportable entities")
        ents = tangent_fillets(ents)
        if params.get("constraints"):
            self.degrade(f, "3D sketch relations are not re-created in SolidWorks")
        self.sketch_ids.add(f.get("id"))
        self.path_kind[f.get("id")] = "3DSKETCH"
        self.plan.steps.append({"op": "sketch3d", "id": f.get("id"), "name": self.name_for(f),
                                "entities": ents})

    def helix(self, f: dict) -> None:
        p, r = dict(f.get("refs") or {}, **(f.get("params") or {})), f.get("refs") or {}
        for k in ("pitch_table", "taper", "taper_angle", "spiral", "shape", "radius2"):
            if p.get(k):
                return self.refuse(f, f"helix {k} is not exported yet (constant-pitch helix is)")
        base = _principal(r.get("base") if r.get("base") is not None else "plane.top")
        if base is None:
            return self.refuse(f, "helix base must be Top/Front/Right")
        radius, pitch, height = (_dim(p.get(k)) for k in ("radius", "pitch", "height"))
        if not (radius and pitch and height):
            return self.refuse(f, "helix needs radius, pitch and height")
        fr = _frame(base, 0.0)
        start = math.radians(_dim(p.get("start_angle")) or 0.0)
        p0 = _add(fr["o"], _scale(fr["u"], radius * math.cos(start)),
                  _scale(fr["v"], radius * math.sin(start)))
        self.path_kind[f.get("id")] = "REFERENCECURVES"
        self.sketch_ids.add(f.get("id"))
        self.plan.steps.append({"op": "helix", "id": f.get("id"), "name": self.name_for(f),
                                "plane": SW_PLANE[base], "radius": radius, "pitch": pitch,
                                "height": height, "ccw": bool(p.get("ccw", True)),
                                "start": to_sw(p0), "centre": to_sw(fr["o"]),
                                "normal": to_sw(fr["n"])})

    def sweep(self, f: dict) -> None:
        p, r = f.get("params") or {}, f.get("refs") or {}
        for k in ("guides", "twist", "orientation"):
            if r.get(k) or p.get(k):
                return self.refuse(f, f"sweep {k} is not exported yet")
        sid = self.profile_sketch(f)
        if sid is None:
            return
        pid = self.ref_id(r.get("path"))
        if pid is None or pid not in self.sketch_ids:
            return self.refuse(f, "sweep path must be an exported sketch, 3D sketch or helix")
        body = self.body_policy(f)
        if body is None:
            return
        self.plan.steps.append({"op": "sweep", "id": f.get("id"), "name": self.name_for(f),
                                "profile": self.sw_name[sid], "path": self.sw_name[pid],
                                "path_kind": self.path_kind.get(pid, "SKETCH"), "body": body})

    def loft(self, f: dict) -> None:
        p, r = f.get("params") or {}, f.get("refs") or {}
        for k in ("guides", "closed", "start_tangent", "end_tangent"):
            if r.get(k) or p.get(k):
                return self.refuse(f, f"loft {k} is not exported yet")
        rows = r.get("sections") or r.get("profiles") or []
        ids = [self.ref_id(x) for x in rows]
        if len(ids) < 2 or any(i is None or i not in self.sketch_ids for i in ids):
            return self.refuse(f, "loft sections must be two or more exported sketches")
        if len(ids) > 2 and p.get("ruled"):
            return self.refuse(f, "a ruled loft through more than two sections is not "
                                  "exported yet")
        body = self.body_policy(f, "new")
        if body is None:
            return
        self.plan.steps.append({"op": "loft", "id": f.get("id"), "name": self.name_for(f),
                                "sections": [self.sw_name[i] for i in ids], "body": body})

    # -- primitives --------------------------------------------------------------------------
    def primitive(self, f: dict) -> None:
        """box/cylinder/cone/sphere/torus -> a profile sketch on a plane through the solid's
        centre + a mid-plane extrude or a 360-degree revolve, at the engine's exact pose."""
        t = f.get("type")
        p = dict(f.get("refs") or {}, **(f.get("params") or {}))
        body = self.body_policy(f)
        if body is None:
            return
        at = p.get("at")
        if at is None:
            at = [0.0, 0.0, 0.0]
        elif _vec3(at) is None:
            return self.refuse(f, "relational `at` placement (a face/edge query) is not "
                                  "exported yet; give `at` as [x, y, z]")
        at = _vec3(at)
        off = _vec3(p.get("at_offset")) or [0.0, 0.0, 0.0]
        axis = [0.0, 0.0, 1.0]
        for k in ("axis", "axis_dir"):
            if p.get(k) is not None:
                axis = _parse_axis(p[k])
                if axis is None:
                    return self.refuse(f, f"{t} axis {p[k]!r} is not a direction")
                break
        if t == "sphere":
            axis = [0.0, 0.0, 1.0]
        rot = rot_z_to(axis)
        ex, ey, ez = (_mv(rot, e) for e in ((1, 0, 0), (0, 1, 0), (0, 0, 1)))
        dims = {}
        need = {"box": (("length", 20), ("width", 20), ("height", 20)),
                "cylinder": (("radius", 10), ("height", 20)),
                "cone": (("r1", 10), ("r2", 0), ("height", 20)),
                "sphere": (("radius", 10),), "torus": (("major_r", 15), ("minor_r", 3))}[t]
        for k, dflt in need:
            v = _dim(p.get(k)) if p.get(k) is not None else float(dflt)
            if v is None:
                return self.refuse(f, f"{t} {k} {p.get(k)!r} is not a number")
            dims[k] = v
        base = [0.0, 0.0, 0.0]
        if not p.get("centered", True):
            if t == "box":
                base = [dims["length"] / 2, dims["width"] / 2, dims["height"] / 2]
            elif t in ("cylinder", "cone"):
                base = [0.0, 0.0, dims["height"] / 2]
        c = _add(at, _mv(rot, base), off)
        ents: list[dict] = []
        if t == "box":
            hl, hw = dims["length"] / 2, dims["width"] / 2
            q = [_add(c, _scale(ex, sx * hl), _scale(ey, sy * hw))
                 for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            ents = [{"type": "line", "p1": to_sw(q[i]), "p2": to_sw(q[(i + 1) % 4])}
                    for i in range(4)]
            normal = ez
        elif t == "cylinder":
            ents = [{"type": "circle", "center": to_sw(c), "radius": dims["radius"]}]
            normal = ez
        else:
            normal = ex   # the profile plane contains the axis (ez) and ey
            h = dims.get("height", 0.0)
            if t == "cone":
                p0, p3 = _add(c, _scale(ez, -h / 2)), _add(c, _scale(ez, h / 2))
                p1, p2 = _add(p0, _scale(ey, dims["r1"])), _add(p3, _scale(ey, dims["r2"]))
                loop = [p0, p1, p2, p3] if dims["r2"] > 0 else [p0, p1, p3]
                if dims["r1"] <= 0:
                    loop = [p0, p2, p3]
                ents = [{"type": "line", "p1": to_sw(loop[i]),
                         "p2": to_sw(loop[(i + 1) % len(loop)])} for i in range(len(loop))]
                axis_pts = (p0, p3)
            elif t == "sphere":
                r = dims["radius"]
                p0, p1 = _add(c, _scale(ez, -r)), _add(c, _scale(ez, r))
                ents = [{"type": "arc3", "p1": to_sw(p0), "p2": to_sw(p1),
                         "pm": to_sw(_add(c, _scale(ey, r)))},
                        {"type": "line", "p1": to_sw(p1), "p2": to_sw(p0)}]
                axis_pts = (p0, p1)
            else:   # torus
                if dims["minor_r"] >= dims["major_r"]:
                    return self.refuse(f, "self-intersecting torus (minor_r >= major_r) is not "
                                          "exported")
                ents = [{"type": "circle", "center": to_sw(_add(c, _scale(ey, dims["major_r"]))),
                         "radius": dims["minor_r"]}]
                axis_pts = (_add(c, _scale(ez, -dims["major_r"])),
                            _add(c, _scale(ez, dims["major_r"])))
            ents.append({"type": "line", "construction": True, "p1": to_sw(axis_pts[0]),
                         "p2": to_sw(axis_pts[1])})
        sketch = self.name_for(f, "_Sketch")
        self.plan.steps.append({"op": "sketch3", "id": None, "name": sketch,
                                "plane": {"origin": to_sw(c), "normal": to_sw(normal)},
                                "entities": ents})
        common = {"id": f.get("id"), "name": self.name_for(f), "sketch": sketch, "body": body}
        if t in ("box", "cylinder"):
            self.plan.steps.append({"op": "extrude", **common,
                                    "mode": "cut" if body["mode"] == "cut" else "boss",
                                    "end": "mid_plane", "depth": dims["height"], "flip": False,
                                    "end2": "none", "depth2": 10.0})
        else:
            self.plan.steps.append({"op": "revolve", **common, "angle": 360.0, "flip": False})

    # -- body operations ---------------------------------------------------------------------
    def transform(self, f: dict) -> None:
        p = dict(f.get("refs") or {}, **(f.get("params") or {}))
        target = None
        if p.get("body") is not None:
            target = self.body_key(f, p["body"])
            if target is None:
                return
        moves = []
        angle = _dim(p.get("angle")) or 0.0
        if abs(angle) > 1e-12:
            ax = _parse_axis(p.get("rotate") if p.get("rotate") is not None
                             else p.get("axis", [0, 0, 1]))
            if ax is None:
                return self.refuse(f, "transform rotation axis is not a direction")
            pivot = _vec3(p.get("pivot") or [0, 0, 0])
            if pivot is None:
                return self.refuse(f, "transform pivot must be [x, y, z]")
            r_sw = engine_rotation_to_sw(axis_angle(ax, math.radians(angle)))
            moves.append({"rotate": [math.degrees(a) for a in sw_euler(r_sw)],
                          "pivot": to_sw(pivot)})
        tr = _vec3(p.get("translate") or [0, 0, 0])
        if tr is None:
            return self.refuse(f, "transform translate must be [x, y, z]")
        if any(abs(x) > 1e-12 for x in tr):
            moves.append({"translate": to_sw(tr)})
        self.plan.steps.append({"op": "move_body", "id": f.get("id"), "name": self.name_for(f),
                                "target": target, "copy": bool(p.get("copy")),
                                "moves": moves})

    def boolean(self, f: dict) -> None:
        p = dict(f.get("refs") or {}, **(f.get("params") or {}))
        op = _BOOLEAN_ALIAS.get(p.get("op"), p.get("op"))
        if op not in COMBINE_OP:
            return self.refuse(f, f"boolean op {p.get('op')!r} has no SolidWorks export")
        if p.get("keep_tools"):
            return self.refuse(f, "keep_tools booleans are not exported (SolidWorks Combine "
                                  "consumes its tool bodies)")
        a, b = p.get("a"), p.get("b")
        if a is None or b is None:
            return self.refuse(f, "boolean needs both operands (a, b)")
        ka, kb = self.body_key(f, a), self.body_key(f, b)
        if ka is None or kb is None:
            return
        self.plan.steps.append({"op": "combine", "id": f.get("id"), "name": self.name_for(f),
                                "code": COMBINE_OP[op], "a": ka, "b": kb})

    # -- dress-ups and patterns --------------------------------------------------------------
    def edge_op(self, f: dict, op: str, size_key: str) -> None:
        p = f.get("params") or {}
        size = _dim(p.get(size_key))
        if size is None:
            return self.refuse(f, f"{op} has no {size_key}")
        if op == "fillet" and _dim(p.get("radius2")) not in (None, 0.0, size):
            return self.refuse(f, "variable-radius fillets are not exported yet")
        if op == "chamfer" and p.get("kind", "equal") != "equal":
            return self.refuse(f, "only equal-distance chamfers are exported")
        pts = self.edge_points(f.get("id"))
        if not pts:
            return self.refuse(f, f"could not resolve the {op} edges on the model")
        self.plan.steps.append({"op": op, "id": f.get("id"), "name": self.name_for(f),
                                "size": size, "edge_points": [to_sw(q) for q in pts]})

    def shell(self, f: dict) -> None:
        p, r = f.get("params") or {}, f.get("refs") or {}
        t = _dim(p.get("thickness"))
        if t is None:
            return self.refuse(f, "shell has no thickness")
        rows = r.get("remove") or []
        rows = rows if isinstance(rows, list) else [rows]
        faces = []
        for q in rows:
            if isinstance(q, dict) and q.get("of") == "face" and isinstance(q.get("near"), list):
                faces.append({"point": to_sw(q["near"])})
            elif isinstance(q, dict) and q.get("of") == "face" and q.get("side") in _SIDE_NORMAL:
                faces.append({"normal": to_sw(_SIDE_NORMAL[q["side"]])})
            else:
                return self.refuse(f, f"shell face {q!r} is not a near-point or side query")
        self.plan.steps.append({"op": "shell", "id": f.get("id"), "name": self.name_for(f),
                                "thickness": t, "outward": bool(p.get("outward")),
                                "remove": faces})

    def seeds(self, f: dict) -> Optional[list[str]]:
        out = []
        for ref in (f.get("refs") or {}).get("features") or []:
            fid = self.ref_id(ref)
            if fid not in self.sw_name or fid in self.sketch_ids:
                self.refuse(f, f"pattern/mirror seed {ref!r} is not an exported solid feature")
                return None
            out.append(self.sw_name[fid])
        if not out:
            self.refuse(f, "pattern/mirror has no seed features (body patterns not exported)")
            return None
        return out

    def axis_line(self, ref: Any) -> Optional[tuple[list[float], list[float]]]:
        """(origin, unit direction) in engine coordinates for an axis ref."""
        ref = _datum(ref)
        if isinstance(ref, dict) and ref.get("kind") == "axis":
            d, o = _parse_axis(ref.get("axis")), _vec3(ref.get("origin") or [0, 0, 0])
            return (o, d) if d is not None and o is not None else None
        if isinstance(ref, str) and ref in ("axis.x", "axis.y", "axis.z"):
            return [0.0, 0.0, 0.0], list(_SIGNED_AXIS[ref[-1]])
        return None

    def linear_pattern(self, f: dict) -> None:
        p, r = f.get("params") or {}, f.get("refs") or {}
        seeds = self.seeds(f)
        if seeds is None:
            return
        dirs = []
        for key, ck, sk in (("dir1", "count1", "spacing1"), ("dir2", "count2", "spacing2")):
            count = int(p.get(ck, 1 if key == "dir2" else 2))
            if key == "dir2" and (count <= 1 or r.get(key) is None):
                break
            line = self.axis_line(r.get(key))
            spacing = _dim(p.get(sk))
            if line is None or spacing is None:
                return self.refuse(f, f"pattern {key} must be an axis direction with a spacing")
            d = line[1]
            if spacing < 0:
                spacing, d = -spacing, _scale(d, -1.0)
            dirs.append({"dir": to_sw(d), "count": count, "spacing": spacing})
        self.plan.steps.append({"op": "linear_pattern", "id": f.get("id"),
                                "name": self.name_for(f), "seeds": seeds, "dirs": dirs})

    def circular_pattern(self, f: dict) -> None:
        p, r = f.get("params") or {}, f.get("refs") or {}
        seeds = self.seeds(f)
        if seeds is None:
            return
        line = self.axis_line(r.get("axis"))
        if line is None:
            return self.refuse(f, "circular pattern axis must be an explicit axis "
                                  "(origin + direction) or axis.x/y/z")
        angle = _dim(p.get("angle"))
        angle = 360.0 if angle is None else angle
        self.plan.steps.append({"op": "circular_pattern", "id": f.get("id"),
                                "name": self.name_for(f), "seeds": seeds,
                                "axis": {"origin": to_sw(line[0]), "dir": to_sw(line[1])},
                                "count": int(p.get("count", 4)), "angle": angle,
                                "equal_spacing": bool(p.get("equal_spacing", True)),
                                "reverse": bool(p.get("reverse"))})

    def mirror(self, f: dict) -> None:
        r = f.get("refs") or {}
        if r.get("bodies"):
            return self.refuse(f, "body mirrors are not exported yet (feature mirrors are)")
        seeds = self.seeds(f)
        if seeds is None:
            return
        plane = _principal(r.get("plane"))
        if not plane:
            return self.refuse(f, "mirror plane must be Top/Front/Right")
        self.plan.steps.append({"op": "mirror", "id": f.get("id"), "name": self.name_for(f),
                                "seeds": seeds, "plane": SW_PLANE[plane]})

    def run(self) -> Plan:
        used_datums = set()
        for f in self.features:
            if f.get("type") == "sketch":
                pid = self.ref_id((f.get("refs") or {}).get("plane"))
                if pid:
                    used_datums.add(pid)
        for f in self.features:
            t = f.get("type")
            if t == "sketch":
                self.sketch(f)
            elif t == "extrude":
                self.extrude(f)
            elif t == "revolve":
                self.revolve(f)
            elif t in _PRIMITIVES:
                self.primitive(f)
            elif t == "transform":
                self.transform(f)
            elif t == "sketch3d":
                self.sketch3d(f)
            elif t == "helix":
                self.helix(f)
            elif t == "sweep":
                self.sweep(f)
            elif t == "loft":
                self.loft(f)
            elif t == "boolean":
                self.boolean(f)
            elif t == "fillet":
                self.edge_op(f, "fillet", "radius")
            elif t == "chamfer":
                self.edge_op(f, "chamfer", "length")
            elif t == "shell":
                self.shell(f)
            elif t == "linear_pattern":
                self.linear_pattern(f)
            elif t == "circular_pattern":
                self.circular_pattern(f)
            elif t == "mirror":
                self.mirror(f)
            elif t == "plane" and f.get("id") in used_datums:
                continue  # folded into the sketch that uses it
            elif t in ("plane", "axis", "point", "csys"):
                self.degrade(f, "unused reference geometry is not exported")
            else:
                self.refuse(f, f"feature type {t!r} has no SolidWorks export yet")
        steps = [self.to_3d(s) for s in self.plan.steps]
        # Paths first: a 3D sketch drawn after solids exist snaps its points onto their edges
        # (SolidWorks 2026 builds metres-long arcs where a brake line runs beside another),
        # and no path depends on a solid.
        self.plan.steps = [s for s in steps if s["op"] == "sketch3d"] +             [s for s in steps if s["op"] != "sketch3d"]
        return self.plan

    def to_3d(self, s: dict) -> dict:
        """A sketch on a default or offset plane as a ``sketch3`` step: its 2D entities placed
        as 3D model points (exact whichever 2D frame SolidWorks gives the plane)."""
        frame = self.sketch_frames.get(s.get("id")) if s["op"] == "sketch" else None
        if frame is None:
            return s
        n, u, v, origin = frame["n"], frame["u"], frame["v"], frame["o"]

        # Chudware closes a profile within its own tolerance; a SolidWorks sketch needs the
        # endpoints to coincide, so endpoints closer than SNAP_MM share one point.
        reps: list[list[float]] = []

        def snap(q):
            for r in reps:
                if abs(r[0] - q[0]) <= SNAP_MM and abs(r[1] - q[1]) <= SNAP_MM:
                    return r
            reps.append([float(q[0]), float(q[1])])
            return reps[-1]
        for e in s["entities"]:
            if e["type"] == "line":
                snap(e["p1"])
                snap(e["p2"])

        def p3(q):
            return to_sw(_add(origin, _scale(u, q[0]), _scale(v, q[1])))
        ents = []
        for e in s["entities"]:
            t, cons = e["type"], bool(e.get("construction"))
            if t == "line":
                ents.append({"type": "line", "construction": cons, "p1": p3(snap(e["p1"])),
                             "p2": p3(snap(e["p2"]))})
            elif t == "circle":
                ents.append({"type": "circle", "construction": cons,
                             "center": p3(e["center"]), "radius": float(e["radius"])})
            elif t == "arc":
                a, b, m = arc_points(e)
                ents.append({"type": "arc3", "construction": cons, "p1": p3(snap(a)),
                             "p2": p3(snap(b)), "pm": p3(m)})
            elif t == "point":
                ents.append({"type": "point", "p": p3(e["p"])})
            elif t == "spline":
                ents.append({"type": "spline", "construction": cons,
                             "points": [p3(q) for q in e["points"]]})
        plane = {"origin": to_sw(origin), "normal": to_sw(n)}
        if s["plane"]["kind"] == "offset":
            plane["name"] = s["plane"]["name"]
        return {"op": "sketch3", "id": s["id"], "name": s["name"], "plane": plane,
                "entities": ents}


def _arc_start_tangent(a, m, b) -> Optional[list[float]]:
    """Unit tangent at ``a`` of the circle through ``a``, ``m``, ``b``, toward ``m``."""
    n = _unit(_cross(_sub(m, a), _sub(b, a)))
    if n is None:
        return None
    # centre: intersection of the perpendicular bisectors of a-m and a-b in the plane
    am, ab = _sub(m, a), _sub(b, a)
    d = 2 * _dot(_cross(am, ab), _cross(am, ab))
    c = _add(a, _scale(_add(_scale(_cross(ab, _cross(am, ab)), _dot(am, am)),
                            _scale(_cross(_cross(am, ab), am), _dot(ab, ab))), 1 / d))
    t = _unit(_cross(n, _sub(a, c)))
    if t is None:
        return None
    return t if _dot(t, am) > 0 else _scale(t, -1.0)


def _tangent_arc(a, t, b, pm_hint) -> Optional[tuple]:
    """The circular arc from ``a`` (leaving along unit ``t``) to ``b``, on the side of
    ``pm_hint``: (mid point, unit tangent at b), or None if degenerate."""
    ab = _sub(b, a)
    n = _unit(_sub(ab, _scale(t, _dot(ab, t))))
    if n is None or _dot(ab, n) < 1e-12:
        return None
    r = _dot(ab, ab) / (2 * _dot(ab, n))
    c = _add(a, _scale(n, r))
    e1, e2 = _unit(_sub(a, c)), t
    cb = _sub(b, c)
    th = math.atan2(_dot(cb, e2), _dot(cb, e1)) % (2 * math.pi)

    def at(x):
        return _add(c, _scale(e1, r * math.cos(x)), _scale(e2, r * math.sin(x)))
    pm = at(th / 2)
    if math.dist(pm, pm_hint) > 0.05 * max(r, 1.0):
        return None
    tb = _add(_scale(e1, -math.sin(th)), _scale(e2, math.cos(th)))
    return pm, tb


def _arc_shape(e: dict) -> Optional[tuple]:
    """(radius, sweep angle, unit start tangent, unit start-to-centre) of a 3-point arc."""
    t0 = _arc_start_tangent(e["p1"], e["pm"], e["p2"])
    if t0 is None:
        return None
    ab = _sub(e["p2"], e["p1"])
    n = _unit(_sub(ab, _scale(t0, _dot(ab, t0))))
    if n is None or _dot(ab, n) < 1e-12:
        return None
    r = _dot(ab, ab) / (2 * _dot(ab, n))
    c = _add(e["p1"], _scale(n, r))
    e1, cb = _unit(_sub(e["p1"], c)), _sub(e["p2"], c)
    th = math.atan2(_dot(cb, t0), _dot(cb, e1)) % (2 * math.pi)
    return r, th, t0, n


def _end_tangents(e: dict) -> tuple:
    """(start, end) unit tangents of a line or a 3-point arc, or (None, None)."""
    if e["type"] == "line":
        d = _unit(_sub(e["p2"], e["p1"]))
        return d, d
    t0 = _arc_start_tangent(e["p1"], e["pm"], e["p2"])
    fit = _tangent_arc(e["p1"], t0, e["p2"], e["pm"]) if t0 else None
    return t0, (fit[1] if fit else None)


def tangent_fillets(ents: list[dict]) -> list[dict]:
    """Make a line/arc path exactly tangent where it is meant to be.

    Stored coordinates are rounded, so arcs meet their lines a few thousandths of a degree
    off tangent; Chudware sweeps such a path but SolidWorks refuses it. Joints within 0.05
    degrees in the stored path are smooth: walking the chain, an arc after a smooth joint is
    made tangent to the direction it starts from, and a line after one leaves along that
    direction (same length). Real corners are kept. The repair is dropped if any point moves
    more than 0.05 mm."""
    if any(e["type"] not in ("line", "arc3") for e in ents):
        return [dict(e) for e in ents]
    tans = [_end_tangents(e) for e in ents]
    smooth = [False] + [
        tans[i - 1][1] is not None and tans[i][0] is not None and
        _dot(tans[i - 1][1], tans[i][0]) > math.cos(math.radians(0.05))
        for i in range(1, len(ents))]
    out = [dict(e) for e in ents]
    cur = None   # unit tangent at the end of the previous (repaired) entity
    for i, e in enumerate(out):
        if i:
            e["p1"] = out[i - 1]["p2"]
        if e["type"] == "line":
            if smooth[i] and cur is not None:
                e["p2"] = _add(e["p1"], _scale(cur, math.dist(ents[i]["p1"], ents[i]["p2"])))
            cur = _unit(_sub(e["p2"], e["p1"]))
        else:
            # keep the arc's radius, sweep and bend direction; start it where the path now is
            own = _arc_shape(ents[i])
            if own is None:
                return [dict(x) for x in ents]
            r, th, t_own, n_own = own
            t0 = cur if smooth[i] and cur is not None else t_own
            n = _unit(_sub(n_own, _scale(t0, _dot(n_own, t0))))
            if n is None:
                return [dict(x) for x in ents]
            c = _add(e["p1"], _scale(n, r))
            e1 = _scale(n, -1.0)

            def at(x, c=c, e1=e1, t0=t0, r=r):
                return _add(c, _scale(e1, r * math.cos(x)), _scale(t0, r * math.sin(x)))
            e["pm"], e["p2"] = at(th / 2), at(th)
            cur = _add(_scale(e1, -math.sin(th)), _scale(t0, math.cos(th)))
    pts = lambda es: [q for x in es for q in (x["p1"], x["p2"])]   # noqa: E731
    if max((math.dist(q0, q1) for q0, q1 in zip(pts(ents), pts(out))), default=0.0) > 0.05:
        return [dict(e) for e in ents]
    return out


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def arc_points(e: dict):
    """A 2D sketch arc (center, radius, start/end degrees, ccw) as (start, end, mid) points."""
    (cx, cy), r = e["center"], float(e["radius"])
    a0, a1 = float(e["start"]), float(e["end"])
    if e.get("ccw", True):
        am = a0 + ((a1 - a0) % 360.0 or 360.0) / 2
    else:
        am = a0 - ((a0 - a1) % 360.0 or 360.0) / 2

    def pt(a):
        return [cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))]
    return pt(a0), pt(a1), pt(am)


def build_plan(features: list[dict], *, name: str,
               edge_points: Callable[[str], list] = lambda _fid: [],
               expected: Optional[dict] = None) -> Plan:
    """Build the SolidWorks plan for a part document's ``features`` (doc_schema §2).

    ``edge_points(feature_id)`` returns engine-space points ON each edge a fillet/chamfer
    selects (doc_feature_edges ``mid``). ``expected`` is the part's eval_mass
    ({volume_mm3, com, body_count, inertia, mass_g}) for the macro's verification."""
    plan = _Builder(features, edge_points, name).run()
    if expected:
        plan.expected = expected_in_sw(expected)
    return plan


def expected_in_sw(mass: dict) -> dict:
    """eval_mass -> the macro's check values in SolidWorks axes: volume, centre of mass, body
    count and the centroidal inertia diagonal per unit density (mm^5, density-independent)."""
    com = mass.get("com") or mass.get("com_mm")
    out = {"volume_mm3": mass.get("volume_mm3"), "com_sw_mm": to_sw(com) if com else None}
    if isinstance(mass.get("body_count"), int):
        out["body_count"] = mass["body_count"]
    inertia, m, v = mass.get("inertia"), mass.get("mass_g"), mass.get("volume_mm3")
    if isinstance(inertia, list) and len(inertia) == 9 and m and v:
        k = v / m
        # SolidWorks x, y, z = engine x, z, -y: the diagonal is (Ixx, Izz, Iyy).
        out["inertia_sw_mm5"] = [inertia[0] * k, inertia[8] * k, inertia[4] * k]
    return out
