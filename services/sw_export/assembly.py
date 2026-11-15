"""Chudware assembly -> a folder SolidWorks turns into a native assembly (.SLDASM).

Every distinct component source becomes one SolidWorks file in the output folder, written by
its own macro (:mod:`services.sw_export.macro`):

* a Chudware part: its feature tree, rebuilt by SolidWorks, falling back inside the macro to
  the part's exact STEP solid when SolidWorks refuses a feature or the rebuild does not match
  (or straight to that solid when the plan has unsupported features);
* a Chudware sub-assembly: its own assembly macro, recursively;
* a SolidWorks file (.sldprt/.sldasm): copied as it is; any other CAD file: its exact solid.

The assembly macro builds whatever is missing, places each component at the pose Chudware
gives it, fixes it there (Chudware mates are not re-created as SolidWorks mates), saves the
.SLDASM and checks its volume, centre of mass and inertia against Chudware's numbers. The
engine calls are injected (``call``, ``open_part``, ``edge_points``) so this stays testable.
"""

from __future__ import annotations

import os
import shutil
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from services.sw_export.macro import write_assembly_macro, write_body_macro, write_macro
from services.sw_export.plan import _T, build_plan, expected_in_sw

_NATIVE_SW = (".sldprt", ".sldasm")


def _rt(xform16) -> tuple[list[list[float]], list[float]]:
    """Row-major 4x4 (engine) -> (R, t)."""
    x = [float(v) for v in xform16]
    return [x[0:3], x[4:7], x[8:11]], [x[3], x[7], x[11]]


def _matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _matvec(a, v):
    return [sum(a[i][k] * v[k] for k in range(3)) for i in range(3)]


def _transpose(a):
    return [list(r) for r in zip(*a)]


def sw_transform(xform16) -> list[float]:
    """An engine component placement (world = R . part + t, mm) as the 16-value array of a
    SolidWorks MathTransform: SolidWorks maps a point as p . M + T, so the 3x3 block is the
    transpose of the rotation in SolidWorks axes; then T in metres, scale 1, three zeros."""
    r, t = _rt(xform16)
    a = _matmul(_matmul(_T, r), _transpose(_T))
    ts = _matvec(_T, t)
    return ([a[j][i] for i in range(3) for j in range(3)] +
            [ts[0] / 1000.0, ts[1] / 1000.0, ts[2] / 1000.0, 1.0, 0.0, 0.0, 0.0])


def _compose(outer, inner):
    """The placement ``outer . inner`` of two row-major 4x4 engine transforms."""
    ro, to = _rt(outer)
    ri, ti = _rt(inner)
    r = _matmul(ro, ri)
    t = [a + b for a, b in zip(_matvec(ro, ti), to)]
    return [r[0][0], r[0][1], r[0][2], t[0], r[1][0], r[1][1], r[1][2], t[1],
            r[2][0], r[2][1], r[2][2], t[2], 0.0, 0.0, 0.0, 1.0]


_IDENTITY = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def combine_mass(instances: list[tuple[dict, list]]) -> Optional[dict]:
    """eval_mass of placed parts -> the same for their union (engine axes), with inertia per
    unit density. ``instances`` = [(part eval_mass, xform16)]. None if any part lacks it."""
    vol, mom, rows = 0.0, [0.0, 0.0, 0.0], []
    for mass, x in instances:
        v, c, i, m = (mass or {}).get("volume_mm3"), (mass or {}).get("com"), \
            (mass or {}).get("inertia"), (mass or {}).get("mass_g")
        if not v or not c or not isinstance(i, list) or len(i) != 9 or not m:
            return None
        r, t = _rt(x)
        cw = [a + b for a, b in zip(_matvec(r, c), t)]
        ivol = [[i[3 * a + b] * v / m for b in range(3)] for a in range(3)]
        rows.append((v, cw, _matmul(_matmul(r, ivol), _transpose(r))))
        vol += v
        mom = [mom[k] + v * cw[k] for k in range(3)]
    if vol <= 0:
        return None
    com = [x / vol for x in mom]
    tot = [[0.0] * 3 for _ in range(3)]
    for v, cw, iw in rows:
        d = [cw[k] - com[k] for k in range(3)]
        dd = sum(x * x for x in d)
        for a in range(3):
            for b in range(3):
                tot[a][b] += iw[a][b] + v * ((dd if a == b else 0.0) - d[a] * d[b])
    return {"volume_mm3": vol, "mass_g": vol, "com": com,
            "inertia": [tot[a][b] for a in range(3) for b in range(3)]}


@dataclass
class _Source:
    file: str                      # SolidWorks file name in the output folder
    macro: Optional[str]           # macro that writes it (None: copied as it is)
    kind: str                      # part | assembly | native
    mass: Optional[dict] = None    # engine eval_mass in the source's own frame
    note: dict = field(default_factory=dict)
    builds: list = field(default_factory=list)   # (file, macro) this one needs, in order


class AssemblyExport:
    def __init__(self, call: Callable[[str, dict], dict],
                 open_part: Callable[[str], AbstractContextManager],
                 edge_points: Callable[[str], Callable[[str], list]], out_dir: str,
                 sketch_frames: Optional[Callable[[str], Callable]] = None):
        self.call, self.open_part, self.edge_points = call, open_part, edge_points
        self.sketch_frames = sketch_frames
        self.out_dir = out_dir
        self.sources: dict[str, _Source] = {}
        self.order: list[_Source] = []       # build order: parts before their assemblies
        self.used: set[str] = set()
        self.warnings: list[str] = []
        self.skipped: list[dict] = []        # components whose source could not be exported

    def _stem(self, path: str) -> str:
        base = os.path.splitext(os.path.basename(path))[0]
        for suffix in (".step", ".stp"):   # "x.step+" -> "x"
            if base.lower().endswith(suffix):
                base = base[: -len(suffix)]
        stem, k = base, 2
        while stem.lower() in self.used:
            stem, k = f"{base}_{k}", k + 1
        self.used.add(stem.lower())
        return stem

    def source(self, path: str) -> _Source:
        key = os.path.normcase(os.path.realpath(path))
        if key in self.sources:
            return self.sources[key]
        ext = os.path.splitext(path)[1].lower()
        stem = self._stem(path)
        if ext in _NATIVE_SW:
            src = _Source(file=stem + ext.upper(), macro=None, kind="native",
                          note={"fidelity": "solidworks_file"})
            shutil.copyfile(path, os.path.join(self.out_dir, src.file))
        elif ext == ".step+":
            with self.open_part(path) as d:
                tree = (self.call("doc_tree", {"doc_id": d}) or {}).get("tree") or {}
                if tree.get("schema") == "aicad.asm/1":
                    src = self._assembly(d, tree, path, stem)
                else:
                    src = self._part(d, tree, stem)
        else:
            d = (self.call("import_file", {"path": path}) or {}).get("doc_id")
            try:
                src = self._body(d, stem, {"fidelity": "imported_body",
                                           "reason": f"{ext} source: its exact solid"})
            finally:
                if d:
                    self.call("doc_close", {"doc_id": d})
        src.note.setdefault("source", path)
        self.sources[key] = src
        self.order.append(src)
        return src

    def _mass(self, d: str) -> Optional[dict]:
        try:
            # Uniform density: SolidWorks measures the bare geometry (no Chudware materials).
            return self.call("eval_mass", {"doc_id": d, "density_g_cm3": 1.0})
        except Exception:  # noqa: BLE001 — an empty or surface-only part has no mass
            return None

    def _step(self, d: str, stem: str) -> str:
        step = os.path.join(self.out_dir, stem + ".step")
        try:
            self.call("export_file", {"doc_id": d, "path": step, "format": "step"})
        except Exception as e:  # noqa: BLE001
            # export_file writes the file, then refuses it when a re-read misses a solid. The
            # SolidWorks macro measures what it opens, so keep it and say so.
            if not os.path.isfile(step):
                raise
            self.warnings.append(f"{stem}.step: {e}")
        return step

    def _body(self, d: str, stem: str, note: dict) -> _Source:
        step = self._step(d, stem)
        macro = os.path.join(self.out_dir, stem + ".swb")
        mass = self._mass(d)
        why = "; ".join(f"{u.get('type')} {u.get('name') or u.get('id')}: {u.get('reason')}"
                        for u in (note.get("unsupported") or [])[:3]) or note.get("reason") or             "no SolidWorks feature tree for this part"
        write_body_macro(stem, step, macro, expected=expected_in_sw(mass) if mass else None,
                         reason=why)
        return _Source(file=stem + ".SLDPRT", macro=os.path.basename(macro), kind="part",
                       mass=mass, note=note)

    def _part(self, d: str, tree: dict, stem: str) -> _Source:
        mass = self._mass(d)
        plan = build_plan(tree.get("features") or [], name=str(tree.get("name") or stem),
                          edge_points=self.edge_points(d), expected=mass,
                          sketch_frame=self.sketch_frames(d) if self.sketch_frames else None
                          ).as_dict()
        if plan["unsupported"]:
            return self._body(d, stem, {"fidelity": "imported_body",
                                        "unsupported": plan["unsupported"]})
        step = self._step(d, stem)
        macro = os.path.join(self.out_dir, stem + ".swb")
        write_macro(plan, macro, fallback_step=step)
        return _Source(file=stem + ".SLDPRT", macro=os.path.basename(macro), kind="part",
                       mass=mass, note={"fidelity": "feature_tree", "degraded": plan["degraded"]})

    def _components(self, tree: dict, asm_path: str) -> list[dict]:
        base = os.path.dirname(asm_path) if asm_path else os.getcwd()
        rows = []
        for c in tree.get("components") or []:
            if c.get("suppressed"):
                continue
            p = (c.get("source") or {}).get("path")
            if not p:
                raise ValueError(f"component {c.get('name') or c.get('id')} has no source file")
            rows.append({"name": c.get("name"), "path": p if os.path.isabs(p)
                         else os.path.normpath(os.path.join(base, p)),
                         "transform": c.get("transform") or _IDENTITY})
        return rows

    def _assembly(self, d: Optional[str], tree: dict, path: str, stem: str) -> _Source:
        comps, builds, inst = [], [], []
        for c in self._components(tree, path):
            try:
                src = self.source(c["path"])
            except Exception as e:  # noqa: BLE001 — one bad part must not lose the assembly
                self.skipped.append({"component": c["name"], "source": c["path"],
                                     "error": str(e)})
                continue
            comps.append({"file": src.file, "transform": sw_transform(c["transform"]),
                          "name": c["name"]})
            inst.append((src.mass, c["transform"]))
            for b in src.builds + ([(src.file, src.macro)] if src.macro else []):
                if b not in builds:
                    builds.append(b)
        mass = combine_mass(inst)
        macro = os.path.join(self.out_dir, stem + ".swb")
        write_assembly_macro(str(tree.get("name") or stem), macro, comps, builds,
                             expected_in_sw(mass) if mass else {})
        return _Source(file=stem + ".SLDASM", macro=os.path.basename(macro), kind="assembly",
                       mass=mass, note={"fidelity": "assembly", "components": len(comps)},
                       builds=builds)

    def export(self, doc_id: str, asm_path: str, stem: str) -> _Source:
        tree = (self.call("doc_tree", {"doc_id": doc_id}) or {}).get("tree") or {}
        self.used.add(stem.lower())
        top = self._assembly(doc_id, tree, asm_path, stem)
        self.order.append(top)
        return top


def run_all(order: list[_Source], out_dir: str, run_macro: Callable[[str], dict],
            verify: Callable[[dict, dict], dict]) -> list[dict]:
    """Run every macro in build order through an installed SolidWorks; one row per file."""
    rows = []
    for src in order:
        row: dict[str, Any] = {"file": src.file, "kind": src.kind, **src.note}
        if src.macro:
            rep = run_macro(os.path.join(out_dir, src.macro))
            row["ok"] = bool(rep.get("ok")) and os.path.isfile(os.path.join(out_dir, src.file))
            if rep.get("fidelity"):
                row["fidelity"] = rep["fidelity"]
            for k in ("error", "feature_error", "stage", "skipped"):
                if rep.get(k) and (k != "stage" or not rep.get("ok")):
                    row[k] = rep[k]
            expected = expected_in_sw(src.mass) if src.mass else {}
            if src.kind == "assembly":
                expected.pop("body_count", None)
            row.update({k: v for k, v in verify(expected, rep).items()
                        if k in ("verified", "checks", "volume_rel_error", "com_error_mm",
                                 "inertia_rel_error")})
        else:
            row["ok"] = True
        rows.append(row)
    return rows
