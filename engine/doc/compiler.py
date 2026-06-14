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
    # Body kind — "solid" (default, every classic feature) or "surface" (open sheet body). Stamped
    # in the assembly loop from the per-feature ``body_kinds`` map; downstream (tessellate /
    # mass-props / export) branches on it. Defaults keep solid bodies byte-identical.
    kind: str = "solid"


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
    # Weldments cut-list side-channel: the per-feature structural-member records the weld_member
    # compiler stashes on ``ctx.datums["_weld_members"]`` (``{fid: {profile, dims, members:[…]}}``),
    # surfaced onto the result so a worker RPC can read it WITHOUT poking inside the compiler. Empty
    # for docs with no weld_member features. NOTE: a CACHE-HIT weld_member does not repopulate the
    # side-channel, so callers that need this should compile with a fresh cache (see
    # engine.doc.cutlist).
    weld_members: dict[str, Any] = field(default_factory=dict)
    # Sheet-metal FLAT-PATTERN side-channel: the per-feature flat-pattern records the
    # ``sm_flat_pattern`` compiler stashes on ``ctx.datums["_sheet_metal_flat"]``
    # (``{fid: {bend_lines:[…], outline:[…], flat_area, thickness, n_base, …}}``), surfaced onto the
    # result so the flat-DXF export RPC can read it WITHOUT poking inside the compiler. Empty for docs
    # with no flat pattern. SAME cache caveat as ``weld_members``: a CACHE-HIT sm_flat_pattern does
    # not repopulate the side-channel, so callers that need this should compile with a fresh cache
    # (see engine.doc.export2d.flat_pattern_for_session).
    sheet_metal_flat: dict[str, Any] = field(default_factory=dict)
    # CONFIG version bump — the configuration this rebuild compiled (the active config's name, or
    # "Default" when the document carries no configurations). Pure echo; absent-config docs report
    # "Default" and are otherwise unchanged.
    active_configuration: str = "Default"


# --------------------------------------------------------------------------- cache


@dataclass
class _CacheEntry:
    bodies: list[Any]
    tool_body: Any | None
    datum: Any | None
    status: dict
    op: str | None = None
    # per-body owner maps produced by this feature (keyed by body_id string)
    face_owners: dict[str, dict[int, str]] = field(default_factory=dict)
    edge_owners: dict[str, dict[int, str]] = field(default_factory=dict)
    # the kind ("solid"|"surface") of the body this feature produced/replaced, keyed by id(shape)
    # so a cache-hit restores the per-body kind without recompiling.
    body_kinds: dict[int, str] = field(default_factory=dict)


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
            dangling_refs: list[str] | None = None,
            rolled_back: bool = False) -> dict:
    return {
        "id": fid,
        "ok": ok,
        "ms": round(ms, 3),
        "error": error,
        "resolved": resolved or {},
        "skipped": skipped,
        "dangling_refs": dangling_refs or [],
        "rolled_back": rolled_back,
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


# --------------------------------------------------------------------------- configurations

DEFAULT_CONFIG_NAME = "Default"


def resolve_active_config(doc: dict) -> tuple[str, dict | None]:
    """Pick the active configuration: ``(name, config_dict | None)``.

    A document with no ``configurations`` (or an empty list) compiles as the single implicit
    ``"Default"`` config (returns ``(None_name, None)``-equivalent ``("Default", None)``). When
    configurations exist, ``active_configuration`` selects one by name; if that pointer is
    missing/unknown the FIRST configuration is used (SolidWorks always has exactly one active
    config). The returned config dict (when not None) is the verbatim stored config — overlay logic
    reads its ``param_overrides`` + ``suppressions``."""
    configs = doc.get("configurations")
    if not isinstance(configs, list) or not configs:
        return DEFAULT_CONFIG_NAME, None
    active_name = doc.get("active_configuration")
    by_name = {c.get("name"): c for c in configs if isinstance(c, dict)}
    if isinstance(active_name, str) and active_name in by_name:
        return active_name, by_name[active_name]
    first = configs[0] if isinstance(configs[0], dict) else None
    name = first.get("name") if first else DEFAULT_CONFIG_NAME
    return (name if isinstance(name, str) and name else DEFAULT_CONFIG_NAME), first


def _config_effective_features(features: list[dict], config: dict | None) -> list[dict]:
    """Overlay ``config`` onto ``features`` and return a NEW effective feature list.

    Never mutates the stored features. For each feature it produces a shallow overlay where:
      * ``suppressed`` becomes ``base.suppressed OR config.suppressions[fid]``;
      * each ``params[key]`` is replaced by ``config.param_overrides["fid.key"]`` when present.

    When ``config`` is None (the implicit Default) the features are returned UNCHANGED (the exact
    same dict objects), so a config-less document compiles byte-identically to before — no copy,
    no overlay, no behavioral difference."""
    if config is None:
        return features
    overrides = config.get("param_overrides") or {}
    suppressions = config.get("suppressions") or {}
    if not overrides and not suppressions:
        return features

    # Bucket overrides by feature id: {fid: {param_key: value}}.
    by_fid: dict[str, dict] = {}
    for key, val in overrides.items():
        if isinstance(key, str) and "." in key:
            fid, _, pkey = key.partition(".")
            by_fid.setdefault(fid, {})[pkey] = val

    out: list[dict] = []
    for feat in features:
        if not isinstance(feat, dict):
            out.append(feat)
            continue
        fid = feat.get("id")
        param_ov = by_fid.get(fid)
        sup_ov = suppressions.get(fid)
        if param_ov is None and sup_ov is None:
            out.append(feat)  # untouched feature: reuse the original object (cache-stable)
            continue
        ef = dict(feat)
        if sup_ov is not None:
            ef["suppressed"] = bool(feat.get("suppressed")) or bool(sup_ov)
        if param_ov:
            params = dict(feat.get("params") or {})
            for pkey, pval in param_ov.items():
                params[pkey] = pval
            ef["params"] = params
        out.append(ef)
    return out


def apply_configuration_to_doc(doc: dict) -> tuple[str, dict]:
    """Produce ``(active_name, effective_doc)``: a shallow overlay of ``doc`` with the active
    configuration applied to BOTH ``features`` (param overrides + feature suppression) and, for
    assemblies, ``components`` (suppression — a config's ``suppressions[<component_id>]`` toggles a
    component out of the assembly). The original document is NEVER mutated; ``effective_doc`` shares
    every untouched sub-object with the original. A config-less document returns ``("Default", doc)``
    — the exact same object, no copy — preserving byte-identical behavior.

    The part compiler (:func:`compile_document`) applies feature overlay itself; this helper is for
    callers (the assembly rebuild) that don't route through :func:`compile_document` and need the
    component-suppression overlay too."""
    active_name, config = resolve_active_config(doc)
    if config is None:
        return DEFAULT_CONFIG_NAME, doc

    suppressions = config.get("suppressions") or {}
    eff = dict(doc)
    eff["features"] = _config_effective_features(doc.get("features") or [], config)

    # Component suppression (assemblies): a suppression keyed by a component id (k<N>) suppresses
    # that component. Feature ids (f<N>) are handled by the feature overlay above; keys that match
    # neither simply do nothing (the validator already flags unknown ids).
    comps = doc.get("components")
    if isinstance(comps, list) and suppressions:
        new_comps = []
        changed = False
        for c in comps:
            if isinstance(c, dict) and c.get("id") in suppressions:
                sc = dict(c)
                sc["suppressed"] = bool(c.get("suppressed")) or bool(suppressions[c["id"]])
                new_comps.append(sc)
                changed = changed or sc["suppressed"] != bool(c.get("suppressed"))
            else:
                new_comps.append(c)
        if changed:
            eff["components"] = new_comps
    return active_name, eff


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

    # --- configurations: overlay the active config onto the feature list -
    # The active config's param overrides + suppressions are applied to a NON-MUTATING overlay of
    # the stored features (see _config_effective_features). A config-less document yields the exact
    # original feature objects, so it compiles byte-identically to before this version. The active
    # config name is echoed on the result AND seeded into the rolling cache key, so switching
    # configs produces a disjoint key chain (caches stay per-config; switching back is a hit).
    base_features = doc.get("features") or []
    active_name, active_config = resolve_active_config(doc)
    result.active_configuration = active_name
    features = _config_effective_features(base_features, active_config)

    # --- rollback bar (SolidWorks semantics) -----------------------------
    # doc["rollback_after"] = <feature_id> | null. Features AFTER the bar are NOT compiled; they get
    # status "rolled_back" (distinct from an error — no error icon) and contribute no geometry. The
    # bar is identified by a feature id; null (or a dangling id) means the bar is at the end. We
    # compute the cut INDEX (number of features to actually compile) once up front.
    rollback_after = doc.get("rollback_after")
    rollback_cut = len(features)  # default: compile everything
    if rollback_after:
        for i, feat in enumerate(features):
            if isinstance(feat, dict) and feat.get("id") == rollback_after:
                rollback_cut = i + 1  # compile through (and including) the bar feature
                break

    # --- context shared across features ----------------------------------
    bodies: list[Any] = []
    datums = reserved_datums()
    # Per-feature tool solids + their boolean op, threaded into every CompileContext so a
    # pattern/mirror feature can replicate the features it references. result.tool_bodies is the
    # source of truth for the solids; feature_ops mirrors it with "add"/"cut".
    feature_ops: dict[str, str] = {}
    # body_id -> face/edge owner maps + the cached topo map for diffing the NEXT feature
    face_owners: dict[str, dict[int, str]] = {}
    edge_owners: dict[str, dict[int, str]] = {}
    body_ids: dict[int, str] = {}  # python id(shape) -> assigned body_id (stable per rebuild)
    # python id(shape) -> body kind ("solid"|"surface"); mirrors body_ids. Only surface bodies are
    # recorded here (a missing entry defaults to "solid"), so solid rebuilds stay byte-identical.
    body_kinds: dict[int, str] = {}
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

    # Seed the rolling cache key with the schema, and (only when the document HAS configurations)
    # the active config name. A config-less document keeps the exact pre-CONFIG seed, so every
    # per-feature key — and the whole rebuild — is unchanged (the regression-guard byte-identity
    # holds, caches included). When configs exist, the seed differs per active config, so two
    # configs cache on disjoint key chains and never cross-contaminate (switching back is a hit).
    prev_key = doc.get("schema", "aicad.part/1")
    if active_config is not None:
        prev_key += "\x00cfg:" + active_name

    for feat_index, feat in enumerate(features):
        fid = feat.get("id", "?")
        fname = feat.get("name", fid)
        t0 = time.perf_counter()

        # Rollback bar: features at or beyond the cut are NOT compiled (geometry reflects only the
        # prefix). They report status "rolled_back" (no error, no geometry) and do not advance the
        # rolling cache key — so moving the bar forward later is a pure prefix-extension (the prefix
        # keys are unchanged → cache hits) and the rolled-back tail stays out of the body list.
        if feat_index >= rollback_cut:
            result.feature_status.append(
                _status(fid, ok=True, ms=0.0, rolled_back=True))
            continue

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
                feature_ops[fid] = hit_entry.op or "add"
            if hit_entry.datum is not None:
                datums[fid] = hit_entry.datum
            result.feature_status.append(dict(hit_entry.status))
            prev_key = feat_key
            # Restore the stored owner maps for every body this feature produced.
            for bid_k, fo in hit_entry.face_owners.items():
                face_owners[bid_k] = dict(fo)
            for bid_k, eo in hit_entry.edge_owners.items():
                edge_owners[bid_k] = dict(eo)
            # Restore per-body kinds (surface bodies); re-key onto the LIVE shape ids so the
            # assembly loop's id(shape) lookup hits. The cache stored kinds keyed by the cached
            # shape's id; the cached shapes ARE the live ones (list copied by reference), so the
            # ids already match — but we re-stamp by walking this feature's body list to be safe.
            for shp in bodies:
                k = hit_entry.body_kinds.get(id(shp))
                if k is not None and k != "solid":
                    body_kinds[id(shp)] = k
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

        ctx = CompileContext(equations_env=env, bodies=bodies, datums=datums,
                             tool_bodies=result.tool_bodies, feature_ops=feature_ops)
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
            feature_ops[fid] = out.op or "add"
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
        # Record the body kind this feature produced/replaced (the top body it just built). Only
        # non-solid kinds are stored; absent => "solid", keeping classic features byte-identical.
        if top is not None and out.kind != "solid":
            body_kinds[id(top)] = out.kind

        prev_key = feat_key

        # --- cache store -------------------------------------------------
        if cache is not None:
            # Deep-copy owner maps: _diff_owners may reuse dict references that get
            # overwritten on subsequent features, so we snapshot them here.
            stored_face = {bid_k: dict(fo) for bid_k, fo in face_owners.items()}
            stored_edge = {bid_k: dict(eo) for bid_k, eo in edge_owners.items()}
            # Snapshot per-body kinds for the bodies this feature produced (keyed by id(shape),
            # matching how the cache-hit path re-stamps them). Only non-solid bodies appear.
            stored_kinds = {id(shp): body_kinds[id(shp)]
                            for shp in bodies if id(shp) in body_kinds}
            cache.put(feat_key, _CacheEntry(
                bodies=list(bodies), tool_body=out.tool_body,
                datum=out.datum, status=dict(status), op=out.op,
                face_owners=stored_face, edge_owners=stored_edge,
                body_kinds=stored_kinds))

    # --- assemble BodyOut list -------------------------------------------
    for shape in bodies:
        bid = _ensure_body_id(shape)
        kind = body_kinds.get(id(shape), "solid")
        result.bodies.append(BodyOut(body_id=bid, name=bid, shape=shape, color=None, kind=kind))
    result.face_owners = {bo.body_id: face_owners.get(bo.body_id, {}) for bo in result.bodies}
    result.edge_owners = {bo.body_id: edge_owners.get(bo.body_id, {}) for bo in result.bodies}

    # --- Weldments cut-list side-channel ---------------------------------
    # weld_member stashes per-feature member records on a sentinel Datum keyed "_weld_members".
    # Surface its obj (a {fid: {profile, dims, members}} dict) so the cut-list RPC can read it.
    wm = datums.get("_weld_members")
    if wm is not None and isinstance(getattr(wm, "obj", None), dict):
        result.weld_members = wm.obj

    # --- Sheet-metal flat-pattern side-channel -----------------------------
    # sm_flat_pattern stashes per-feature flat records on a sentinel Datum keyed "_sheet_metal_flat"
    # ({fid: {bend_lines, outline, flat_area, thickness, n_base, …}}). Surface it so the flat-DXF RPC
    # can read the outline + bend lines.
    smf = datums.get("_sheet_metal_flat")
    if smf is not None and isinstance(getattr(smf, "obj", None), dict):
        result.sheet_metal_flat = smf.obj

    return result
