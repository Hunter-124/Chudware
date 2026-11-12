"""Chudware part feature tree -> a SolidWorks build plan (the inverse of services.sw_import).

The plan is a plain list of steps in SolidWorks MODEL coordinates (millimetres, degrees), so
the two backends stay thin: :mod:`services.sw_export.com_build` replays it through an installed
SolidWorks, :mod:`services.sw_export.macro` writes it as a SolidWorks VBA macro that a teammate
runs in their own SolidWorks release (which is what makes the result open in older versions:
SolidWorks itself writes the file, in that teammate's version).

Strict-fidelity contract (same as the importer): a feature or parameter this cannot rebuild
exactly is listed in ``unsupported`` with the Chudware feature id/name/type and the reason; the
caller refuses a parametric export with a non-empty list rather than write a quietly different
part. ``degraded`` lists what is rebuilt with the exact geometry but less design intent (e.g.
sketch relations and dimensions are not re-created yet).

Coordinates: a sketch on a principal plane keeps its 2D coordinates unchanged (the importer copies
them verbatim; validated by SolidWorks center-of-mass on asymmetric parts). A world point maps
engine (x, y, z) -> SolidWorks (x, z, -y): engine is Z-up, SolidWorks Y-up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Chudware principal datum -> SolidWorks default plane. Each pair's 2D sketch frames coincide.
SW_PLANE = {"plane.top": "Top Plane", "plane.front": "Front Plane", "plane.right": "Right Plane"}
# SolidWorks plane whose normal is +/- each engine world axis (for pattern directions).
_AXIS_PLANE = {(1, 0, 0): ("Right Plane", False), (-1, 0, 0): ("Right Plane", True),
               (0, 0, 1): ("Top Plane", False), (0, 0, -1): ("Top Plane", True),
               (0, -1, 0): ("Front Plane", False), (0, 1, 0): ("Front Plane", True)}
# The two default planes whose intersection is each engine world axis through the origin.
_AXIS_PLANES = {0: ("Top Plane", "Front Plane"), 1: ("Top Plane", "Right Plane"),
                2: ("Front Plane", "Right Plane")}
# Face-side words (contracts/selectors.md) -> engine outward normal.
_SIDE_NORMAL = {"top": (0, 0, 1), "bottom": (0, 0, -1), "front": (0, -1, 0),
                "back": (0, 1, 0), "right": (1, 0, 0), "left": (-1, 0, 0)}
_SKETCH_ENTITIES = {"line", "circle", "arc", "point", "spline"}


def _principal(ref: Any) -> Optional[str]:
    """"plane.top" for the bare id or the {"kind":"datum","feature":"plane.top"} form."""
    if isinstance(ref, dict) and ref.get("kind") == "datum":
        ref = ref.get("feature")
    return ref if isinstance(ref, str) and ref in SW_PLANE else None


def to_sw(p) -> list[float]:
    """Engine world point/direction (mm) -> SolidWorks model coordinates (mm)."""
    return [float(p[0]), float(p[2]), -float(p[1])]


@dataclass
class Plan:
    name: str
    steps: list[dict] = field(default_factory=list)
    unsupported: list[dict] = field(default_factory=list)
    degraded: list[dict] = field(default_factory=list)
    expected: dict = field(default_factory=dict)   # {volume_mm3, com_sw_mm}

    def as_dict(self) -> dict:
        return {"name": self.name, "steps": self.steps, "unsupported": self.unsupported,
                "degraded": self.degraded, "expected": self.expected}


def _dim(v: Any) -> Optional[float]:
    if isinstance(v, dict):
        v = v.get("value")
    return float(v) if isinstance(v, (int, float)) else None


def _unit_axis(d) -> Optional[tuple[int, int, int]]:
    """The signed principal axis a direction lies on, or None."""
    if not isinstance(d, (list, tuple)) or len(d) != 3:
        return None
    n = math.sqrt(sum(float(x) * float(x) for x in d))
    if n < 1e-12:
        return None
    u = [float(x) / n for x in d]
    for i in range(3):
        if abs(abs(u[i]) - 1.0) < 1e-9:
            out = [0, 0, 0]
            out[i] = 1 if u[i] > 0 else -1
            return tuple(out)
    return None


class _Builder:
    def __init__(self, features: list[dict], edge_points: Callable[[str], list],
                 name: str):
        self.features = [f for f in features if not f.get("suppressed")]
        self.edge_points = edge_points
        self.plan = Plan(name=name)
        self.by_id = {f.get("id"): f for f in self.features}
        self.sw_name: dict[str, str] = {}      # chudware feature id -> SolidWorks feature name
        self.sketch_ids: set[str] = set()
        self.used_names: set[str] = set()

    # -- reporting -------------------------------------------------------------------------
    def refuse(self, f: dict, reason: str) -> None:
        self.plan.unsupported.append({"id": f.get("id"), "name": f.get("name"),
                                      "type": f.get("type"), "reason": reason})

    def degrade(self, f: dict, reason: str) -> None:
        self.plan.degraded.append({"id": f.get("id"), "name": f.get("name"),
                                   "type": f.get("type"), "reason": reason})

    def name_for(self, f: dict) -> str:
        base = str(f.get("name") or f.get("id") or f.get("type"))
        name, k = base, 2
        while name in self.used_names:
            name, k = f"{base}_{k}", k + 1
        self.used_names.add(name)
        self.sw_name[f.get("id")] = name
        return name

    def ref_id(self, ref: Any) -> Optional[str]:
        """A feature id from a ref: "f3", "$3"-style op refs are not stored in documents."""
        if isinstance(ref, str):
            return ref if ref in self.by_id else None
        if isinstance(ref, dict):
            for k in ("feature", "sketch", "id"):
                if isinstance(ref.get(k), str) and ref[k] in self.by_id:
                    return ref[k]
        return None

    # -- planes ------------------------------------------------------------------------------
    def sketch_plane(self, f: dict) -> Optional[dict]:
        ref = (f.get("refs") or {}).get("plane")
        if _principal(ref):
            return {"kind": "principal", "plane": SW_PLANE[_principal(ref)]}
        if isinstance(ref, dict) and ref.get("of") == "face" and ref.get("side") in _SIDE_NORMAL:
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
                return {"kind": "offset", "base": SW_PLANE[base], "offset": off,
                        "name": f"{datum.get('name') or pid}"}
            self.refuse(f, "sketch is on a datum plane that is not a plain offset of "
                           "Top/Front/Right")
            return None
        self.refuse(f, f"sketch plane {ref!r} is not Top/Front/Right, an offset of one, or a "
                       "principal-facing model face")
        return None

    # -- features ----------------------------------------------------------------------------
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
        return self.sw_name[next(iter(ids))]

    def extrude(self, f: dict) -> None:
        p = f.get("params") or {}
        sketch = self.profile_sketch(f)
        if sketch is None:
            return
        mode = p.get("mode", "boss")
        if mode not in ("boss", "cut", "new", "add"):
            return self.refuse(f, f"extrude mode {mode!r} has no SolidWorks export")
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
        self.plan.steps.append({
            "op": "extrude", "id": f.get("id"), "name": self.name_for(f), "sketch": sketch,
            "mode": "cut" if mode == "cut" else "boss", "end": end, "depth": depth,
            "flip": bool(p.get("flip")), "end2": end2,
            "depth2": 10.0 if depth2 is None else depth2, "merge": p.get("merge", True)})

    def revolve(self, f: dict) -> None:
        p = f.get("params") or {}
        sketch_name = self.profile_sketch(f)
        if sketch_name is None:
            return
        sid = next(k for k, v in self.sw_name.items() if v == sketch_name)
        sk = self.by_id[sid]
        lines = [e for e in (sk.get("params") or {}).get("entities") or []
                 if e.get("type") == "line" and e.get("construction")]
        if len(lines) != 1:
            return self.refuse(f, "revolve axis must be the one construction line of its sketch "
                                  f"(found {len(lines)})")
        angle = _dim(p.get("angle"))
        angle = 360.0 if angle is None else angle
        if abs(angle) < 360.0 - 1e-9:
            self.degrade(f, "partial revolve: the side SolidWorks sweeps is checked by the "
                            "volume comparison")
        self.plan.steps.append({"op": "revolve", "id": f.get("id"), "name": self.name_for(f),
                                "sketch": sketch_name, "mode": p.get("mode", "boss"),
                                "angle": angle, "flip": bool(p.get("flip"))})

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

    def axis_of(self, ref: Any) -> Optional[tuple[int, int, int]]:
        if isinstance(ref, dict) and ref.get("kind") == "axis":
            if any(abs(float(x)) > 1e-9 for x in ref.get("origin") or [0, 0, 0]):
                return None
            return _unit_axis(ref.get("axis"))
        if isinstance(ref, str) and ref in ("axis.x", "axis.y", "axis.z"):
            return {"axis.x": (1, 0, 0), "axis.y": (0, 1, 0), "axis.z": (0, 0, 1)}[ref]
        return None

    def linear_pattern(self, f: dict) -> None:
        p, r = f.get("params") or {}, f.get("refs") or {}
        seeds = self.seeds(f)
        if seeds is None:
            return
        dirs = []
        for key, ck, sk in (("dir1", "count1", "spacing1"), ("dir2", "count2", "spacing2")):
            count = int(p.get(ck, 1 if key == "dir2" else 2))
            if key == "dir2" and (count <= 1 or r.get("dir2") is None):
                break
            axis = self.axis_of(r.get(key))
            spacing = _dim(p.get(sk))
            if axis is None or spacing is None:
                return self.refuse(f, f"pattern {key} must be a world axis (X/Y/Z) direction")
            plane, flip = _AXIS_PLANE[axis]
            if spacing < 0:
                spacing, flip = -spacing, not flip
            dirs.append({"plane": plane, "flip": flip, "count": count, "spacing": spacing})
        self.plan.steps.append({"op": "linear_pattern", "id": f.get("id"),
                                "name": self.name_for(f), "seeds": seeds, "dirs": dirs})

    def circular_pattern(self, f: dict) -> None:
        p, r = f.get("params") or {}, f.get("refs") or {}
        seeds = self.seeds(f)
        if seeds is None:
            return
        axis = self.axis_of(r.get("axis"))
        if axis is None:
            return self.refuse(f, "circular pattern axis must be a world axis through the origin")
        angle = _dim(p.get("angle"))
        angle = 360.0 if angle is None else angle
        idx = max(range(3), key=lambda i: abs(axis[i]))
        # engine axis +/-X, +/-Y, +/-Z -> SolidWorks axis direction sign (Y maps to -Z).
        sw_dir = to_sw(axis)
        self.plan.steps.append({"op": "circular_pattern", "id": f.get("id"),
                                "name": self.name_for(f), "seeds": seeds,
                                "axis_planes": list(_AXIS_PLANES[idx]),
                                "axis_sw": sw_dir, "count": int(p.get("count", 4)),
                                "angle": angle,
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
        return self.plan


def build_plan(features: list[dict], *, name: str,
               edge_points: Callable[[str], list] = lambda _fid: [],
               expected: Optional[dict] = None) -> Plan:
    """Build the SolidWorks plan for a part document's ``features`` (doc_schema §2).

    ``edge_points(feature_id)`` returns engine-space points ON each edge a fillet/chamfer
    selects (doc_feature_edges ``mid``). ``expected`` is the part's eval_mass
    ({volume_mm3, com}) for the backends' verification."""
    plan = _Builder(features, edge_points, name).run()
    if expected:
        com = expected.get("com") or expected.get("com_mm")
        plan.expected = {"volume_mm3": expected.get("volume_mm3"),
                         "com_sw_mm": to_sw(com) if com else None}
    return plan
