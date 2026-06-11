"""Document compiler — compile an ``aicad.part/1`` dict into OCCT solids.

This is the heart of M1-WS-1A. It walks a part document's feature history in order and, for each
feature, runs its registered compiler (engine/doc/features/*) over a shared :class:`CompileContext`
to produce bodies. On top of that it provides the four cross-cutting concerns the SolidWorks-class
rebuild needs:

1. **Equations** — ``equations.globals`` are evaluated ONCE up front. Every dimension value
   ``{value, expr}`` in a feature's params is resolved against that env into a WORKING COPY
   (``expr`` wins; ``value`` is overwritten with the evaluated result) before the compiler runs.
   Resolved values are echoed back per feature as ``resolved: {param: float}``.

2. **Per-feature caching** — a feature's cache key is ``sha256(prev_key + canonical_json(feature)
   + the equation-env values that feature's exprs reference)``. A hit reuses the stored body list
   / tool body / datum and skips compilation. Hit/miss counters are exposed on the result.

3. **Error containment (SolidWorks behavior)** — a failing feature records ``ok:false`` with the
   error message (+ hint) and is SKIPPED; the history continues on the last good body. The
   compiler NEVER raises out of :func:`compile_document`.

4. **Topology owner-tagging** — after each feature, faces/edges of the current top body are mapped
   with ``TopExp.MapShapes_s`` and compared (TShape identity, via the indexed map's ``Contains``)
   against the previous feature's map. Entries absent from the previous map are tagged with the
   current feature id; unmatched leftovers keep their prior owner. This is a HEURISTIC: booleans
   regenerate some faces that geometrically belong to an older feature, so a freshly-regenerated
   coincident face may be (re)attributed to the feature that triggered the regeneration. It is the
   same diff-tag the selectors contract (selectors.md §2) consumes; the honest limitation is
   documented there ("geometric resolution, not persistent naming").

Shapes stay in-process: no file export here (C02 owns tessellation, C11 owns persistence).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedMapOfShape

from .equations import EquationError, evaluate, evaluate_globals, expr_names
from .features import CompileContext, FeatureError, get_compiler
from .features.datum import reserved_datums

# --------------------------------------------------------------------------- result types


@dataclass
class BodyOut:
    """One resolved body in the compiled result."""

    body_id: str
    name: str
    shape: Any  # build123d Solid / Compound
    color: list[float] | None = None


@dataclass
class CompileResult:
    """The full output of :func:`compile_document`.

    - ``bodies``: ordered list of :class:`BodyOut` (multi-body is real).
    - ``feature_status``: per-feature record dicts (see :func:`_status` for the shape).
    - ``face_owners`` / ``edge_owners``: per body, ``{topo_index: feature_id}`` mapping the body's
      face/edge indices (1-based, matching ``TopExp.MapShapes_s`` order) to the feature that
      created that topology. Indexed by ``body_id``.
    - ``tool_bodies``: ``{feature_id: shape}`` — the solid each feature fused/cut (patterns need
      this); absent for datums/scripts that don't contribute a single tool solid.
    - ``equations_env``: the once-evaluated global equation environment.
    - ``cache_hits`` / ``cache_misses``: per-rebuild cache counters.
    - ``equations_error``: set when the document equations themselves failed (every feature then
      compiles in error state).
    """

    bodies: list[BodyOut] = field(default_factory=list)
    feature_status: list[dict] = field(default_factory=list)
    face_owners: dict[str, dict[int, str]] = field(default_factory=dict)
    edge_owners: dict[str, dict[int, str]] = field(default_factory=dict)
    tool_bodies: dict[str, Any] = field(default_factory=dict)
    equations_env: dict[str, float] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    equations_error: str | None = None


# --------------------------------------------------------------------------- cache


@dataclass
class _CacheEntry:
    bodies: list[Any]
    tool_body: Any | None
    datum: Any | None
    status: dict
    # per-body owner maps produced by this feature (keyed by body_id string)
    face_owners: dict[str, dict[int, str]] = field(default_factory=dict)
    edge_owners: dict[str, dict[int, str]] = field(default_factory=dict)


class CompileCache:
    """Per-feature compile cache, keyed by the rolling feature key.

    The cache stores, per feature key, the body list AFTER that feature plus its tool body, datum,
    and status. ``compile_document`` threads the previous key into each feature's key so any
    upstream edit invalidates everything downstream (SolidWorks "recompute from k").
    """

    def __init__(self) -> None:
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> _CacheEntry | None:
        return self._entries.get(key)

    def put(self, key: str, entry: _CacheEntry) -> None:
        self._entries[key] = entry

    def clear(self) -> None:
        self._entries.clear()


# --------------------------------------------------------------------------- helpers


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _feature_env_subset(feat: dict, env: dict[str, float]) -> dict[str, float]:
    """The subset of the equation env that this feature's expressions actually reference.

    Walking params for dim objects with an ``expr`` and collecting their names keeps a feature's
    cache key sensitive ONLY to the globals it uses, so editing an unrelated equation doesn't
    needlessly invalidate it.
    """

    used: dict[str, float] = {}

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            expr = v.get("expr")
            if isinstance(expr, str) and expr:
                try:
                    for name in expr_names(expr):
                        if name in env:
                            used[name] = env[name]
                except EquationError:
                    pass
            for sub in v.values():
                walk(sub)
        elif isinstance(v, list):
            for sub in v:
                walk(sub)

    walk(feat.get("params", {}))
    return used


def _feature_key(prev_key: str, feat: dict, env_subset: dict[str, float]) -> str:
    h = hashlib.sha256()
    h.update(prev_key.encode("utf-8"))
    h.update(_canonical_json(feat).encode("utf-8"))
    h.update(_canonical_json(env_subset).encode("utf-8"))
    return h.hexdigest()


def _resolve_params(params: Any, env: dict[str, float]) -> tuple[Any, dict[str, float], list[str]]:
    """Deep-copy params with every dim ``{value, expr}`` resolved (expr wins) against ``env``.

    Returns ``(resolved_params, resolved_floats, errors)`` where ``resolved_floats`` maps each
    resolved dim's KEY (top-level params only) to its evaluated float for the status echo, and
    ``errors`` collects equation failures (which mark the feature errored).
    """

    errors: list[str] = []

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            if "value" in v or "expr" in v:
                expr = v.get("expr")
                out = dict(v)
                if isinstance(expr, str) and expr:
                    try:
                        out["value"] = evaluate(expr, env)
                    except EquationError as exc:
                        errors.append(str(exc))
                return out
            return {k: walk(sub) for k, sub in v.items()}
        if isinstance(v, list):
            return [walk(sub) for sub in v]
        return v

    resolved = walk(params) if isinstance(params, dict) else params
    floats: dict[str, float] = {}
    if isinstance(resolved, dict):
        for k, v in resolved.items():
            if isinstance(v, dict) and isinstance(v.get("value"), (int, float)) \
                    and not isinstance(v.get("value"), bool) and v.get("expr"):
                floats[k] = float(v["value"])
    return resolved, floats, errors


def _status(fid: str, *, ok: bool, ms: float, error: str | None = None,
            resolved: dict | None = None, skipped: bool = False,
            dangling_refs: list[str] | None = None) -> dict:
    return {
        "id": fid,
        "ok": ok,
        "ms": round(ms, 3),
        "error": error,
        "resolved": resolved or {},
        "skipped": skipped,
        "dangling_refs": dangling_refs or [],
    }


def _topo_map(shape, topo_type) -> TopTools_IndexedMapOfShape:
    m = TopTools_IndexedMapOfShape()
    if shape is not None and getattr(shape, "wrapped", None) is not None:
        TopExp.MapShapes_s(shape.wrapped, topo_type, m)
    return m


def _diff_owners(shape, prev_map, topo_type, fid: str,
                 prev_owners: dict[int, str]) -> tuple[dict[int, str], TopTools_IndexedMapOfShape]:
    """Tag the current shape's topo entities (face or edge) with their owning feature.

    An entity present in ``prev_map`` (TShape identity) inherits its previous owner if we have one;
    otherwise (new topology) it is tagged with ``fid``.
    """

    cur_map = _topo_map(shape, topo_type)
    owners: dict[int, str] = {}
    # Build a TShape->prev_owner lookup by walking the previous map's keys against prev_owners.
    for i in range(1, cur_map.Extent() + 1):
        ent = cur_map.FindKey(i)
        if prev_map is not None and prev_map.Contains(ent):
            prev_idx = prev_map.FindIndex(ent)
            owners[i] = prev_owners.get(prev_idx, fid)
        else:
            owners[i] = fid
    return owners, cur_map


def _top_body(bodies: list[Any]):
    return bodies[-1] if bodies else None


# --------------------------------------------------------------------------- compile


def compile_document(doc: dict, cache: CompileCache | None = None) -> CompileResult:
    """Compile a part document into bodies + status + topology tags.

    ``cache`` (optional) carries per-feature results across rebuilds; pass the SAME instance to
    get cache hits when only some features changed. Never raises: equation/feature failures are
    contained and reported in the result.
    """

    result = CompileResult()

    # --- equations: evaluate the global env once -------------------------
    eq = doc.get("equations") or {"globals": []}
    globals_list = eq.get("globals", []) if isinstance(eq, dict) else []
    try:
        env = evaluate_globals(globals_list)
    except EquationError as exc:
        env = {}
        result.equations_error = str(exc)
    result.equations_env = env

    features = doc.get("features") or []

    # --- context shared across features ----------------------------------
    bodies: list[Any] = []
    datums = reserved_datums()
    # body_id -> face/edge owner maps + the cached topo map for diffing the NEXT feature
    face_owners: dict[str, dict[int, str]] = {}
    edge_owners: dict[str, dict[int, str]] = {}
    body_ids: dict[int, str] = {}  # python id(shape) -> assigned body_id (stable per rebuild)
    # the topo maps of the CURRENT top body, threaded into the next feature's diff
    prev_face_map: TopTools_IndexedMapOfShape | None = None
    prev_edge_map: TopTools_IndexedMapOfShape | None = None
    prev_face_owners: dict[int, str] = {}
    prev_edge_owners: dict[int, str] = {}

    next_body_num = 1

    def _ensure_body_id(shape) -> str:
        nonlocal next_body_num
        key = id(shape)
        if key not in body_ids:
            body_ids[key] = f"b{next_body_num}"
            next_body_num += 1
        return body_ids[key]

    prev_key = doc.get("schema", "aicad.part/1")

    for feat in features:
        fid = feat.get("id", "?")
        fname = feat.get("name", fid)
        t0 = time.perf_counter()

        # Suppressed: skip, status ok/skipped, do not advance topology.
        if feat.get("suppressed"):
            result.feature_status.append(
                _status(fid, ok=True, ms=0.0, skipped=True))
            # A suppressed feature still rolls the cache key forward (its presence/edits matter).
            prev_key = _feature_key(prev_key, feat, {})
            continue

        # Resolve dims into a working copy against the equation env.
        env_subset = _feature_env_subset(feat, env)
        feat_key = _feature_key(prev_key, feat, env_subset)

        # Resolve dims (expr wins) against the env. A document-level equation failure surfaces
        # here as per-dim errors only on the dims that actually reference a now-missing name.
        resolved_params, resolved_floats, dim_errors = _resolve_params(feat.get("params", {}), env)

        # --- cache lookup ------------------------------------------------
        hit_entry = cache.get(feat_key) if cache is not None else None
        if hit_entry is not None and not dim_errors:
            result.cache_hits += 1
            bodies = list(hit_entry.bodies)
            if hit_entry.tool_body is not None:
                result.tool_bodies[fid] = hit_entry.tool_body
            if hit_entry.datum is not None:
                datums[fid] = hit_entry.datum
            result.feature_status.append(dict(hit_entry.status))
            prev_key = feat_key
            # Restore the stored owner maps for every body this feature produced.
            for bid_k, fo in hit_entry.face_owners.items():
                face_owners[bid_k] = dict(fo)
            for bid_k, eo in hit_entry.edge_owners.items():
                edge_owners[bid_k] = dict(eo)
            # Feed the top-body maps into the NEXT feature's diff.
            top = _top_body(bodies)
            bid = _ensure_body_id(top) if top is not None else None
            if top is not None:
                prev_face_owners = face_owners.get(bid, {})
                prev_edge_owners = edge_owners.get(bid, {})
                prev_face_map = _topo_map(top, TopAbs_FACE)
                prev_edge_map = _topo_map(top, TopAbs_EDGE)
            continue

        if cache is not None:
            result.cache_misses += 1

        # --- compile -----------------------------------------------------
        if dim_errors:
            ms = (time.perf_counter() - t0) * 1000.0
            result.feature_status.append(
                _status(fid, ok=False, ms=ms,
                        error="equation error: " + "; ".join(dim_errors),
                        resolved=resolved_floats))
            prev_key = feat_key
            continue

        compiler = get_compiler(feat.get("type", ""))
        if compiler is None:
            ms = (time.perf_counter() - t0) * 1000.0
            result.feature_status.append(
                _status(fid, ok=False, ms=ms,
                        error=f"no compiler for feature type '{feat.get('type')}'",
                        resolved=resolved_floats))
            prev_key = feat_key
            continue

        ctx = CompileContext(equations_env=env, bodies=bodies, datums=datums)
        # Hand the compiler a feature view with resolved params (so ctx.dim reads evaluated values).
        feat_view = dict(feat)
        feat_view["params"] = resolved_params

        try:
            out = compiler(feat_view, ctx)
        except FeatureError as exc:
            ms = (time.perf_counter() - t0) * 1000.0
            result.feature_status.append(
                _status(fid, ok=False, ms=ms, error=exc.full_message(),
                        resolved=resolved_floats, dangling_refs=exc.dangling_refs))
            prev_key = feat_key
            # Last good body unchanged; downstream continues on it.
            continue
        except Exception as exc:  # noqa: BLE001 — never let a feature crash the rebuild
            ms = (time.perf_counter() - t0) * 1000.0
            result.feature_status.append(
                _status(fid, ok=False, ms=ms,
                        error=f"{type(exc).__name__}: {exc}", resolved=resolved_floats))
            prev_key = feat_key
            continue

        # --- success: integrate output -----------------------------------
        bodies = out.bodies
        if out.tool_body is not None:
            result.tool_bodies[fid] = out.tool_body
        if out.datum is not None:
            datums[fid] = out.datum

        ms = (time.perf_counter() - t0) * 1000.0
        merged_resolved = dict(resolved_floats)
        merged_resolved.update(out.resolved)
        status = _status(fid, ok=True, ms=ms, resolved=merged_resolved)
        result.feature_status.append(status)

        # --- topology tagging on the top body ----------------------------
        top = _top_body(bodies)
        if top is not None:
            bid = _ensure_body_id(top)
            f_owners, f_map = _diff_owners(top, prev_face_map, TopAbs_FACE, fid, prev_face_owners)
            e_owners, e_map = _diff_owners(top, prev_edge_map, TopAbs_EDGE, fid, prev_edge_owners)
            face_owners[bid] = f_owners
            edge_owners[bid] = e_owners
            prev_face_map, prev_edge_map = f_map, e_map
            prev_face_owners, prev_edge_owners = f_owners, e_owners

        prev_key = feat_key

        # --- cache store -------------------------------------------------
        if cache is not None:
            # Deep-copy owner maps: _diff_owners may reuse dict references that get
            # overwritten on subsequent features, so we snapshot them here.
            stored_face = {bid_k: dict(fo) for bid_k, fo in face_owners.items()}
            stored_edge = {bid_k: dict(eo) for bid_k, eo in edge_owners.items()}
            cache.put(feat_key, _CacheEntry(
                bodies=list(bodies), tool_body=out.tool_body,
                datum=out.datum, status=dict(status),
                face_owners=stored_face, edge_owners=stored_edge))

    # --- assemble BodyOut list -------------------------------------------
    for shape in bodies:
        bid = _ensure_body_id(shape)
        result.bodies.append(BodyOut(body_id=bid, name=bid, shape=shape, color=None))
    result.face_owners = {bo.body_id: face_owners.get(bo.body_id, {}) for bo in result.bodies}
    result.edge_owners = {bo.body_id: edge_owners.get(bo.body_id, {}) for bo in result.bodies}

    return result
