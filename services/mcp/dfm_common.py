"""Shared plumbing for geometry-derived DfM rows (``dfm_check``) and verified fixes
(``dfm_reinforce``).

Everything here is evidence plumbing, not engineering rules: a row producer reads the
document through :class:`DfmContext`, reads its numeric limits from the bound
ProcessProfile ``params.rules`` (``ctx.rule(key)``; every limit therefore carries the
citation the C++ profile recorded in ``params.rule_sources``) and returns rows built
with :func:`make_row`.

Row contract (every producer, every process):

* ``check``       row name (stable id, e.g. ``"sharp_internal_corners"``)
* ``status``      ``pass | fail | warn | not_evaluated | not_applicable``; ``warn`` rows are
                  advice (they pass the verdict), ``not_evaluated`` is NEVER a pass.
* ``ok``          False for fail, True for pass/warn, None otherwise (derived)
* ``note``        one human sentence with the measured number and the limit
* ``basis``       how it was computed ("mesh inscribed sphere", "B-rep concave edge", ...)
* ``confidence``  ``measured`` | ``geometry_heuristic`` | ``table`` | ``authored_params``
* ``assumptions`` every input the row had to assume, each a short sentence
* ``metrics`` / ``limits``  numbers (mm, deg, ratio) with the unit in the key suffix
* ``regions``     at most ``ctx.limit`` entries, worst first; each carries ``faces`` (sids),
                  ``built_by`` (feature ids) and a model-frame ``centroid`` / ``at``
* ``fix``         human sentences; ``fix_ops``  structured proposals (see :func:`fix_proposal`)

Nothing here ever returns ``pass`` when nothing was evaluated: a producer that cannot
measure raises :class:`DfmEvidenceError` (or returns :func:`not_evaluated`) and the
dispatcher turns that into a ``not_evaluated`` row carrying the reason.
"""
from __future__ import annotations

import contextlib
import math
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from services.mcp import core_bridge as cb

STATUSES = ("pass", "fail", "warn", "not_evaluated", "not_applicable")
CONFIDENCES = ("measured", "geometry_heuristic", "table", "authored_params")


class DfmEvidenceError(RuntimeError):
    """The geometry/engine evidence a row needs is unavailable (row becomes not_evaluated)."""


class MissingRule(DfmEvidenceError):
    """The bound ProcessProfile has no ``params.rules[key]`` for this process."""

    def __init__(self, key: str, process: str):
        super().__init__(
            f"ProcessProfile '{process}' has no rule '{key}' (params.rules). The profile "
            f"table in core/dfm/process_profiles.cpp must carry it, with a source, before "
            f"this row can be evaluated.")
        self.key = key
        self.process = process


# ----------------------------------------------------------------------------- numbers

def num(value: Any) -> Optional[float]:
    """One finite float from a plain number or a ``{"value": n, "expr": ...}`` dim object."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def r3(value: Any) -> Any:
    """Round a float (or nested list of floats) to 3 decimals for stable JSON."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 3) if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [r3(v) for v in value]
    return value


def unit(vector) -> list[float]:
    """Unit vector of a finite nonzero 3-vector; ValueError otherwise."""
    if not isinstance(vector, (list, tuple)) or len(vector) != 3 or any(
            isinstance(x, bool) for x in vector):
        raise ValueError("direction must be a finite nonzero [x, y, z] vector")
    try:
        comps = [float(x) for x in vector]
    except (TypeError, ValueError, OverflowError):
        raise ValueError("direction must be a finite nonzero [x, y, z] vector") from None
    mag = math.hypot(*comps)
    if not math.isfinite(mag) or mag == 0:
        raise ValueError("direction must be a finite nonzero [x, y, z] vector")
    return [x / mag for x in comps]


# ----------------------------------------------------------------------------- rows

def make_row(check: str, status: str, note: str, *, basis: str, confidence: str,
             assumptions: Optional[list] = None, metrics: Optional[dict] = None,
             limits: Optional[dict] = None, regions: Optional[list] = None,
             fix: Optional[list] = None, fix_ops: Optional[list] = None,
             omitted: int = 0, detail: bool = False, rule: Optional[str] = None,
             error: Optional[str] = None, **extra: Any) -> dict:
    """One DfM row in the shared shape (see the module docstring).

    ``rule`` is the citation of the limit the row applied (copy it from
    ``ctx.rule_source(key)``). ``regions`` must already be capped/sorted by the caller
    (``ctx.limit``); ``omitted`` counts the regions that were cut.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown row status {status!r}")
    if confidence not in CONFIDENCES:
        raise ValueError(f"unknown row confidence {confidence!r}")
    if omitted:
        note += f" {omitted} more regions are not shown."
        if not detail:
            note += " To show up to 50 regions, set detail=true."
    row: dict = {
        "check": check, "status": status,
        "ok": False if status == "fail" else True if status in ("pass", "warn") else None,
        "note": note, "basis": basis, "confidence": confidence,
        "assumptions": list(assumptions or []),
        "metrics": metrics or {}, "limits": limits or {},
        "regions": regions or [], "regions_omitted": int(omitted),
        "fix": list(fix or []), "fix_ops": list(fix_ops or []),
    }
    if rule:
        row["rule"] = rule
    if error:
        row["error"] = error
    row.update(extra)
    return row


def not_evaluated(check: str, error: str, *, basis: str = "not evaluated",
                  assumptions: Optional[list] = None, **extra: Any) -> dict:
    """A row that records WHY nothing was measured (never a pass)."""
    return make_row(check, "not_evaluated", error, basis=basis, confidence="measured",
                    assumptions=assumptions, error=error, **extra)


def not_applicable(check: str, why: str) -> dict:
    return make_row(check, "not_applicable", why, basis="process rule scope",
                    confidence="table")


def region(*, faces=None, built_by=None, centroid=None, **numbers: Any) -> dict:
    """A located finding: face sids, building feature ids, model-frame centroid + numbers."""
    out: dict = {"faces": sorted({str(f) for f in (faces or []) if f}),
                 "built_by": sorted({str(b) for b in (built_by or []) if b})}
    if centroid is not None:
        out["centroid"] = r3([float(c) for c in centroid])
    out.update({k: r3(v) for k, v in numbers.items()})
    return out


# ----------------------------------------------------------------------------- fix proposals

def dim(value: float) -> dict:
    return {"value": float(value), "expr": None}


def op_edit_param(feature_id: str, **params: float) -> dict:
    """``edit_feature`` changing dimension parameters, e.g. ``op_edit_param("f3", thickness=1.35)``."""
    return {"op": "edit_feature", "id": feature_id,
            "params": {k: dim(v) for k, v in params.items()}}


def op_sketch_dim(sketch_id: str, constraint_id: str, value: float) -> dict:
    """``sketch_set_dim`` driving constraint ``constraint_id`` of sketch ``sketch_id``."""
    return {"op": "sketch_set_dim", "sketch": sketch_id, "id": constraint_id,
            "value": float(value)}


def op_fillet(name: str, radius_mm: float, edge_points: list) -> dict:
    """Constant-radius fillet on the edges nearest each model-frame point (one selector per
    point, ``count:1``). The engine clamps an out-of-range radius to the nearest valid
    one and reports it in ``features[i].data.fillet``."""
    return {"op": "add_feature", "feature": {
        "type": "fillet", "name": name,
        "params": {"type": "constant", "radius": dim(radius_mm)},
        "refs": {"edges": [{"of": "edge", "near": r3([float(c) for c in p]), "count": 1}
                           for p in edge_points]}}}


def fix_proposal(label: str, ops: list, *, rule: str, kind: str,
                 effect: Optional[str] = None, verified: Optional[bool] = None,
                 **extra: Any) -> dict:
    """One concrete, applyable fix (``ops`` are ``add_features`` ops, engine dialect).

    ``verified`` is ``None`` for an unverified proposal, True/False once a shadow-clone
    build + re-measure ran. ``rule`` is the cited rule that motivates the change.
    """
    out = {"label": label, "kind": kind, "ops": ops, "rule": rule, "verified": verified}
    if effect:
        out["expected_effect"] = effect
    out.update(extra)
    return out


# ----------------------------------------------------------------------------- context

def _flatten_features(tree_result: Any) -> list[dict]:
    root = tree_result.get("tree", tree_result) if isinstance(tree_result, dict) else {}
    found: list[dict] = []
    seen: set[int] = set()

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen.add(id(node))
        if node.get("type") and isinstance(node.get("params"), dict):
            found.append(node)
        for key in ("features", "children"):
            visit(node.get(key))

    visit(root)
    return found


@dataclass
class DfmContext:
    """What a row producer may read. All geometry reads are cached for one dfm_check call."""

    doc_id: str
    process: str
    profile: dict
    direction: list                 # unit build-up (additive) / pull (molding, casting) vector
    direction_source: str           # "caller" | "profile" | "default"
    detail: bool = False
    wanted: Optional[set] = None
    options: dict = field(default_factory=dict)   # tool-call options (target_wall_mm, printer, ...)
    _cache: dict = field(default_factory=dict, repr=False)

    # -- profile ----------------------------------------------------------------------
    @property
    def params(self) -> dict:
        return self.profile.get("params") or {}

    @property
    def rules(self) -> dict:
        r = self.params.get("rules")
        return r if isinstance(r, dict) else {}

    def rule(self, key: str) -> Any:
        """``params.rules[key]``; MissingRule (-> not_evaluated row) when the profile lacks it."""
        value = self.rules.get(key)
        if value is None:
            raise MissingRule(key, self.process)
        return value

    def rule_or_none(self, key: str) -> Any:
        return self.rules.get(key)

    def rule_source(self, key: str) -> Optional[str]:
        sources = self.params.get("rule_sources")
        return sources.get(key) if isinstance(sources, dict) else None

    def want(self, name: str) -> bool:
        return self.wanted is None or name in self.wanted

    @property
    def limit(self) -> int:
        return 50 if self.detail else 5

    # -- document geometry ------------------------------------------------------------
    def _memo(self, key: str, make: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    def tree(self) -> dict:
        """Raw ``doc_tree`` response ({tree:{features..}, owner_map, ...})."""
        return self._memo("tree", lambda: cb.call("doc_tree", {"doc_id": self.doc_id}) or {})

    def features(self) -> list[dict]:
        """Every feature row of the tree (id, name, type, params, refs), in history order."""
        return self._memo("features", lambda: _flatten_features(self.tree()))

    def feature(self, feature_id: Optional[str]) -> Optional[dict]:
        if not feature_id:
            return None
        return next((f for f in self.features() if f.get("id") == feature_id), None)

    def faces(self) -> list[dict]:
        """Every face as ``doc_query`` reports it: sid, surf, centroid, normal, axis,
        radius, area_or_len, concavity, body, topo, owner_feature (all model-frame mm)."""
        def make() -> list[dict]:
            res = cb.call("doc_query", {"doc_id": self.doc_id, "query": {"of": "face"}}) or {}
            return [m for m in (res.get("matches") or []) if isinstance(m, dict)]
        return self._memo("faces", make)

    def face(self, sid: str) -> Optional[dict]:
        by_sid = self._memo("faces_by_sid", lambda: {str(f.get("sid")): f for f in self.faces()})
        return by_sid.get(str(sid))

    def bbox(self) -> dict:
        """``doc_bbox`` of the active document (min/max model-frame mm)."""
        return self._memo("bbox", lambda: cb.call("doc_bbox", {"doc_id": self.doc_id}) or {})

    def mesh(self):
        """The display tessellation joined to doc_query sids: ``services.dfam.PrintMesh``
        (vertices, triangles, tri_face, faces[FaceInfo(sid, body, surf_type, owner_feature)])."""
        def make():
            from services.mcp import print_tools as pt  # noqa: PLC0415
            return pt.load_doc(self.doc_id)[0]
        return self._memo("mesh", make)

    def edge_analysis(self, min_dihedral_dev_deg: float = 5.0, body: Optional[int] = None,
                      kinds: tuple = ("concave", "convex")) -> dict:
        """Contract C1 ``eval_edge_analysis`` (all pages merged; see contracts/worker_rpc.md):
        ``{edges:[{sid, body, topo, kind, dihedral_deg (interior angle through the material,
        90 = box edge, 270 = sharp inside corner), length_mm, mid, dir, faces:[sid,sid],
        radius_mm?, owner_feature}], concave_blends:[{face_sid, surf, radius_mm, arc_deg?,
        axis?, adjacent_faces, owner_feature}]}``.
        Raises DfmEvidenceError when the engine RPC is unavailable or errors."""
        def make() -> dict:
            edges: list = []
            blends: list = []
            cursor = None
            for _ in range(40):
                params: dict = {"doc_id": self.doc_id, "limit": 2000,
                                "min_dihedral_dev_deg": min_dihedral_dev_deg,
                                "kinds": list(kinds)}
                if body is not None:
                    params["body"] = body
                if cursor is not None:
                    params["cursor"] = cursor
                try:
                    page = cb.call("eval_edge_analysis", params)
                except Exception as exc:  # noqa: BLE001
                    raise DfmEvidenceError(f"eval_edge_analysis unavailable: {exc}") from exc
                if not isinstance(page, dict) or page.get("ok") is False:
                    raise DfmEvidenceError("eval_edge_analysis failed: "
                                           f"{(page or {}).get('error', 'no result')}")
                if page.get("complete") is False or page.get("source_failures"):
                    raise DfmEvidenceError(f"edge analysis incomplete: {page.get('source_failures')}")
                if page.get("blends_truncated") or page.get("unclassified_count", 0):
                    raise DfmEvidenceError("edge analysis omitted blends or has unclassified edges; a complete corner assessment is unavailable")
                edges.extend(page.get("edges") or [])
                blends.extend(page.get("concave_blends") or [])
                cursor = page.get("next_cursor")
                if not page.get("truncated"):
                    return {"edges": edges, "concave_blends": blends, "truncated": False}
                if cursor is None:
                    raise DfmEvidenceError("edge analysis truncated without a continuation cursor")
            raise DfmEvidenceError("edge analysis exceeded the 40-page evidence budget")
        return self._memo(f"edges:{min_dihedral_dev_deg}:{body}:{','.join(kinds)}", make)


# ----------------------------------------------------------------------------- shadow clone

class ShadowSource:
    """A saved snapshot of the live document from which throw-away clones are opened.

    Clones are INTERNAL (never mirrored into the user's GUI), detached from any path and
    always closed. Never apply a candidate op to the live document from a DfM tool.
    """

    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self._path: Optional[str] = None

    def __enter__(self) -> "ShadowSource":
        fd, path = tempfile.mkstemp(prefix="chudware_dfm_", suffix=".step+")
        os.close(fd)
        self._path = path
        try:
            cb.call("doc_save", {"doc_id": self.doc_id, "path": path, "preserve_path": True})
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._path:
            with contextlib.suppress(OSError):
                os.unlink(self._path)
            self._path = None

    @contextlib.contextmanager
    def clone(self) -> Iterator[str]:
        if not self._path:
            raise RuntimeError("ShadowSource used outside its with-block")
        opened = cb.call("doc_open", {"path": self._path, "detach_path": True,
                                      "internal": True}) or {}
        clone_id = opened.get("doc_id")
        if not clone_id:
            raise DfmEvidenceError(opened.get("error") or "shadow clone open returned no doc_id")
        try:
            yield clone_id
        finally:
            with contextlib.suppress(Exception):
                cb.call("doc_close", {"doc_id": clone_id})


def apply_to_clone(clone_id: str, ops: list) -> dict:
    """Apply ``ops`` to a shadow clone. Returns ``{ok, error?, failed_features, statuses}``.

    ok is False when the engine refuses the batch or any feature fails to rebuild.
    """
    try:
        resp = cb.call("doc_apply_ops", {"doc_id": clone_id, "ops": ops, "with_tess": False})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "failed_features": [], "statuses": []}
    statuses = ((resp or {}).get("rebuild") or {}).get("feature_status") or []
    failed = [s for s in statuses if isinstance(s, dict) and not s.get("ok", True)]
    if (resp or {}).get("rejected_tree") or failed:
        msg = "; ".join(f"{s.get('id')}: {s.get('error') or 'rebuild failed'}" for s in failed[:5])
        return {"ok": False, "error": msg or "op batch rejected", "failed_features": failed,
                "statuses": statuses}
    return {"ok": True, "failed_features": [], "statuses": statuses}
