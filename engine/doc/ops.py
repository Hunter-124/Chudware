"""Document ops — validate-then-apply the closed mutation vocabulary (docs/contracts/doc_ops.md).

The GUI and (M9) the AI modeler emit exactly these ops. :func:`apply_ops` is the single entry
point: it takes a document dict + an op batch and returns ``(new_doc, id_map)`` or raises
:class:`OpError` with a structured ``{code, message, detail}`` payload (doc_ops.md §5).

Transactionality (doc_ops.md §2):
  1. Validation is atomic — the WHOLE batch is structurally + referentially checked first (and the
     RESULTING document is run through the schema validator). Any failure rejects the batch with
     ``invalid_op {index, reason}`` and NO state change.
  2. Application is atomic — all ops apply to a working copy; the caller swaps it in and bumps the
     revision by exactly 1.
  3. Rebuild failures are NOT op errors (a feature that fails to compile keeps its edited
     definition) — that is the session/compiler layer's concern, not this module's.

Id placeholders (doc_ops.md §3): an op that creates an object may be referenced by a LATER op in
the same batch as ``"$<op-index>"``. Inside ``sketch_add_entity.constraints``, ``"$self"`` refers
to the entity being added. Placeholders are resolved against assigned ids before the resulting
document is validated.

Ids are assigned monotonically per document: features ``f<N>``, sketch entities ``e<N>``,
constraints ``c<N>``, components ``k<N>``, mates ``m<N>``. Counters never reuse a freed id.
"""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any

from engine.doc.schema import (
    FEATURE_ID_RE,
    RESERVED_DATUMS,
    validate_document,
)

# --------------------------------------------------------------------------- error envelope


class OpError(Exception):
    """A structured op error (doc_ops.md §5). Carries ``code`` + ``detail`` for the RPC envelope."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_envelope(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def _invalid(index: int, reason: str) -> OpError:
    return OpError("invalid_op", f"op[{index}]: {reason}", {"index": index, "reason": reason})


# --------------------------------------------------------------------------- id counters


_ID_RE = {
    "f": re.compile(r"^f(\d+)$"),
    "e": re.compile(r"^e(\d+)$"),
    "c": re.compile(r"^c(\d+)$"),
    "k": re.compile(r"^k(\d+)$"),
    "m": re.compile(r"^m(\d+)$"),
}


def _scan_max(doc: dict) -> dict[str, int]:
    """Find the current max numeric suffix per id family so new ids are monotonic."""
    hi = {"f": 0, "e": 0, "c": 0, "k": 0, "m": 0}

    def bump(prefix: str, value: Any) -> None:
        if isinstance(value, str):
            m = _ID_RE[prefix].match(value)
            if m:
                hi[prefix] = max(hi[prefix], int(m.group(1)))

    for feat in doc.get("features", []) or []:
        if not isinstance(feat, dict):
            continue
        bump("f", feat.get("id"))
        if feat.get("type") == "sketch":
            params = feat.get("params", {}) or {}
            for ent in params.get("entities", []) or []:
                if isinstance(ent, dict):
                    bump("e", ent.get("id"))
            for con in params.get("constraints", []) or []:
                if isinstance(con, dict):
                    bump("c", con.get("id"))
    for comp in doc.get("components", []) or []:
        if isinstance(comp, dict):
            bump("k", comp.get("id"))
    for mate in doc.get("mates", []) or []:
        if isinstance(mate, dict):
            bump("m", mate.get("id"))
    return hi


class _IdGen:
    """Monotonic id generator seeded from the document's existing ids."""

    def __init__(self, doc: dict) -> None:
        self._hi = _scan_max(doc)

    def next(self, prefix: str) -> str:
        self._hi[prefix] += 1
        return f"{prefix}{self._hi[prefix]}"


# --------------------------------------------------------------------------- doc helpers


def _features(doc: dict) -> list[dict]:
    return doc.setdefault("features", [])


def _find_feature(doc: dict, fid: str) -> tuple[int, dict] | tuple[None, None]:
    for i, f in enumerate(_features(doc)):
        if isinstance(f, dict) and f.get("id") == fid:
            return i, f
    return None, None


def _find_sketch(doc: dict, fid: str) -> dict:
    _, f = _find_feature(doc, fid)
    if f is None:
        raise OpError("invalid_op", f"sketch '{fid}' not found", {"reason": f"unknown sketch {fid}"})
    if f.get("type") != "sketch":
        raise OpError("invalid_op", f"feature '{fid}' is not a sketch",
                      {"reason": f"{fid} is not a sketch"})
    return f


def _patch(target: dict, patch: dict) -> None:
    """Apply patch semantics: present keys replace; a ``None`` value deletes the key."""
    for k, v in patch.items():
        if v is None:
            target.pop(k, None)
        else:
            target[k] = v


# --------------------------------------------------------------------------- reference scan


def _walk_selector_owners(obj: Any, out: set[str]) -> None:
    """Collect every ``owner_feature`` / datum ``feature`` / region ``sketch`` referenced anywhere
    inside *obj* (a ref value, a selector, a list of selectors, ...)."""
    if isinstance(obj, dict):
        kind = obj.get("kind")
        if kind in ("face", "edge", "vertex", "body"):
            ow = obj.get("owner_feature")
            if isinstance(ow, str):
                out.add(ow)
        elif kind == "datum":
            fe = obj.get("feature")
            if isinstance(fe, str):
                out.add(fe)
        elif kind == "region":
            sk = obj.get("sketch")
            if isinstance(sk, str):
                out.add(sk)
        for v in obj.values():
            _walk_selector_owners(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_selector_owners(v, out)


def _feature_dependents(doc: dict, fid: str) -> list[str]:
    """Feature ids that reference *fid* (via selectors, datum refs, region refs, or feature-list
    refs like patterns/mirror). Used to refuse a non-cascade delete/reorder."""
    deps: list[str] = []
    for f in _features(doc):
        if not isinstance(f, dict) or f.get("id") == fid:
            continue
        refs = f.get("refs", {}) or {}
        referenced: set[str] = set()
        _walk_selector_owners(refs, referenced)
        # feature-id-list refs (linear_pattern/circular_pattern/mirror features slot)
        for slot_val in refs.values():
            if isinstance(slot_val, list):
                for item in slot_val:
                    if isinstance(item, str):
                        referenced.add(item)
            elif isinstance(slot_val, str):
                referenced.add(slot_val)
        if fid in referenced:
            deps.append(f["id"])
    return deps


def _eq_referencing(doc: dict, name: str) -> list[str]:
    """Feature ids (or 'equations.<other>') whose expressions reference equation *name*."""
    from engine.doc.equations import EquationError, expr_names

    refs: list[str] = []

    def uses(expr: Any) -> bool:
        if not isinstance(expr, str) or not expr:
            return False
        try:
            return name in expr_names(expr)
        except EquationError:
            return False

    # Other globals
    for g in (doc.get("equations", {}) or {}).get("globals", []) or []:
        if isinstance(g, dict) and g.get("name") != name and uses(g.get("expr")):
            refs.append(f"equations.{g.get('name')}")

    # Feature dim exprs
    def walk(v: Any, where: str) -> None:
        if isinstance(v, dict):
            if uses(v.get("expr")):
                refs.append(where)
            for k, sub in v.items():
                walk(sub, where)
        elif isinstance(v, list):
            for sub in v:
                walk(sub, where)

    for f in _features(doc):
        if isinstance(f, dict):
            walk(f.get("params", {}), f.get("id", "?"))
    return refs


# --------------------------------------------------------------------------- op application


def _new_feature_envelope(feature: dict, fid: str) -> dict:
    """Normalize an inbound ``add_feature.feature`` (no id) into a full feature envelope."""
    out = copy.deepcopy(feature)
    out["id"] = fid
    out.setdefault("name", fid)
    out.setdefault("suppressed", False)
    out.setdefault("params", {})
    out.setdefault("refs", {})
    return out


def _resolve_placeholder(value: Any, id_map: dict[str, str], index: int) -> Any:
    """Resolve a ``"$N"`` placeholder string to its assigned id. Non-placeholders pass through.
    A ``"$N"`` referring to an op that produced no id (or a later op) is an error."""
    if isinstance(value, str) and value.startswith("$") and value != "$self":
        if value not in id_map:
            raise _invalid(index, f"placeholder '{value}' does not refer to an earlier created object")
        return id_map[value]
    return value


def _resolve_placeholders_deep(obj: Any, id_map: dict[str, str], index: int) -> Any:
    """Recursively resolve ``$N`` placeholders that appear as the ``sketch`` field of an op or as a
    feature-id inside a feature-list ref. We only substitute strings that look like ``$N``; ``$self``
    is left for the sketch-entity constraint pass."""
    if isinstance(obj, dict):
        return {k: _resolve_placeholders_deep(v, id_map, index) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_placeholders_deep(v, id_map, index) for v in obj]
    return _resolve_placeholder(obj, id_map, index)


def _apply_one(doc: dict, op: dict, index: int, idgen: _IdGen, id_map: dict[str, str]) -> None:
    """Apply a single op to *doc* in place. Records any created id in *id_map* under ``$<index>``."""
    if not isinstance(op, dict):
        raise _invalid(index, "op must be an object")
    kind = op.get("op")
    if not isinstance(kind, str):
        raise _invalid(index, "op.op (the op name) is required")

    handler = _OP_HANDLERS.get(kind)
    if handler is None:
        raise OpError("unsupported", f"op[{index}]: unknown op '{kind}'",
                      {"index": index, "reason": f"unsupported op '{kind}'"})
    handler(doc, op, index, idgen, id_map)


# ---- part + general ----


def _op_add_feature(doc, op, index, idgen, id_map):
    feature = op.get("feature")
    if not isinstance(feature, dict):
        raise _invalid(index, "add_feature requires a 'feature' object")
    fid = idgen.next("f")
    feat = _new_feature_envelope(feature, fid)
    # Resolve $N placeholders inside the feature's refs (e.g. region sketch placeholders).
    feat["refs"] = _resolve_placeholders_deep(feat.get("refs", {}), id_map, index)
    feats = _features(doc)
    before = op.get("before")
    if before is not None:
        pos, _ = _find_feature(doc, before)
        if pos is None:
            raise _invalid(index, f"before='{before}' is not an existing feature")
        feats.insert(pos, feat)
    else:
        # SolidWorks behavior: if the rollback bar is NOT at the end, a new feature is inserted
        # immediately AFTER the bar position, and the bar then moves to sit after the new feature
        # (so the new feature is the last rolled-IN feature). With the bar at the end (or unset),
        # the feature simply appends.
        rb = doc.get("rollback_after")
        bar_pos, bar_feat = (_find_feature(doc, rb) if rb else (None, None))
        if bar_feat is not None and bar_pos < len(feats) - 1:
            feats.insert(bar_pos + 1, feat)
            doc["rollback_after"] = fid  # bar follows the freshly-inserted feature
        else:
            feats.append(feat)
    id_map[f"${index}"] = fid


def _op_edit_feature(doc, op, index, idgen, id_map):
    fid = op.get("id")
    _, feat = _find_feature(doc, fid)
    if feat is None:
        raise _invalid(index, f"edit_feature: '{fid}' not found")
    if "params" in op and isinstance(op["params"], dict):
        feat.setdefault("params", {})
        _patch(feat["params"], op["params"])
    if "refs" in op and isinstance(op["refs"], dict):
        feat.setdefault("refs", {})
        _patch(feat["refs"], _resolve_placeholders_deep(op["refs"], id_map, index))
    if "name" in op and op["name"] is not None:
        feat["name"] = op["name"]


def _op_delete_feature(doc, op, index, idgen, id_map):
    fid = op.get("id")
    pos, feat = _find_feature(doc, fid)
    if feat is None:
        raise _invalid(index, f"delete_feature: '{fid}' not found")
    cascade = bool(op.get("cascade", False))
    deps = _feature_dependents(doc, fid)
    if deps and not cascade:
        raise OpError("dependency_exists",
                      f"feature '{fid}' is referenced by {deps}",
                      {"dependents": deps})
    to_delete = {fid}
    if cascade:
        # Transitively collect dependents.
        changed = True
        while changed:
            changed = False
            for f in _features(doc):
                if isinstance(f, dict) and f["id"] not in to_delete:
                    if _feature_dependents_subset(doc, f["id"], to_delete):
                        to_delete.add(f["id"])
                        changed = True
    # If the rollback bar sits on a feature about to be deleted, move it to the nearest surviving
    # PRECEDING feature (or null = end) so the bar stays valid (SolidWorks semantics).
    rb = doc.get("rollback_after")
    if rb in to_delete:
        feats_now = _features(doc)
        bar_pos = next((i for i, f in enumerate(feats_now)
                        if isinstance(f, dict) and f.get("id") == rb), None)
        new_bar = None
        if bar_pos is not None:
            for f in reversed(feats_now[:bar_pos]):
                if isinstance(f, dict) and f.get("id") not in to_delete:
                    new_bar = f.get("id")
                    break
        if new_bar:
            doc["rollback_after"] = new_bar
        else:
            doc.pop("rollback_after", None)
    doc["features"] = [f for f in _features(doc) if not (isinstance(f, dict) and f["id"] in to_delete)]


def _feature_dependents_subset(doc: dict, fid: str, deleted: set[str]) -> bool:
    """True if feature *fid* references any feature already marked for deletion."""
    _, feat = _find_feature(doc, fid)
    if feat is None:
        return False
    referenced: set[str] = set()
    refs = feat.get("refs", {}) or {}
    _walk_selector_owners(refs, referenced)
    for slot_val in refs.values():
        if isinstance(slot_val, list):
            referenced.update(x for x in slot_val if isinstance(x, str))
        elif isinstance(slot_val, str):
            referenced.add(slot_val)
    return bool(referenced & deleted)


def _feature_references(feat: dict) -> set[str]:
    """Feature ids referenced by *feat* through selectors, datum refs, or feature-id slots."""
    referenced: set[str] = set()
    refs = feat.get("refs", {}) or {}
    _walk_selector_owners(refs, referenced)
    for slot_val in refs.values():
        if isinstance(slot_val, list):
            referenced.update(x for x in slot_val if isinstance(x, str))
        elif isinstance(slot_val, str):
            referenced.add(slot_val)
    referenced -= set(RESERVED_DATUMS)
    return referenced


def _validate_feature_order(doc: dict, feats: list[dict], index: int) -> None:
    """Validate the full feature list so no feature appears before its dependencies."""
    all_ids = {f.get("id") for f in feats if isinstance(f, dict)}
    preceding: set[str] = set(RESERVED_DATUMS)
    for feat in feats:
        if not isinstance(feat, dict):
            continue
        fid = feat.get("id", "?")
        missing = sorted(r for r in _feature_references(feat)
                         if r in all_ids and r not in preceding)
        if missing:
            raise OpError(
                "dependency_exists",
                f"reorder would place '{fid}' before its dependencies {missing}",
                {"feature": fid, "dependents": missing},
            )
        if isinstance(fid, str):
            preceding.add(fid)


def _op_reorder_feature(doc, op, index, idgen, id_map):
    fid = op.get("id")
    pos, feat = _find_feature(doc, fid)
    if feat is None:
        raise _invalid(index, f"reorder_feature: '{fid}' not found")
    feats = _features(doc)
    feats.pop(pos)
    before = op.get("before")
    if before is None:
        feats.append(feat)
        new_pos = len(feats) - 1
    else:
        bpos, _ = _find_feature(doc, before)
        if bpos is None:
            raise _invalid(index, f"reorder_feature: before='{before}' not found")
        feats.insert(bpos, feat)
        new_pos = bpos
    _validate_feature_order(doc, feats, index)


def _op_set_suppressed(doc, op, index, idgen, id_map):
    fid = op.get("id")
    _, feat = _find_feature(doc, fid)
    if feat is None:
        raise _invalid(index, f"set_suppressed: '{fid}' not found")
    feat["suppressed"] = bool(op.get("value"))


def _op_rename(doc, op, index, idgen, id_map):
    fid = op.get("id")
    _, feat = _find_feature(doc, fid)
    if feat is None:
        raise _invalid(index, f"rename: '{fid}' not found")
    name = op.get("name")
    if not isinstance(name, str) or not name:
        raise _invalid(index, "rename requires a non-empty 'name'")
    feat["name"] = name


def _op_set_folder(doc, op, index, idgen, id_map):
    """Set (or clear) a feature's organizational folder tag. UI-only metadata: the compiler
    ignores it, but it round-trips through save/open so the FeatureManager can group features."""
    fid = op.get("id")
    _, feat = _find_feature(doc, fid)
    if feat is None:
        raise _invalid(index, f"set_folder: '{fid}' not found")
    folder = op.get("folder")
    if folder is None or folder == "":
        feat.pop("folder", None)
    elif isinstance(folder, str):
        feat["folder"] = folder
    else:
        raise _invalid(index, "set_folder requires a string 'folder' (or null/empty to clear)")


def _op_set_material(doc, op, index, idgen, id_map):
    key = op.get("key")
    if key is None:
        doc["material"] = None
    elif isinstance(key, str):
        doc["material"] = {"key": key}
    else:
        raise _invalid(index, "set_material requires a string 'key' (or null)")


def _op_set_appearance(doc, op, index, idgen, id_map):
    """Set (or clear) a body's display appearance (C26 color + C32-A material). ``id`` is the body's
    stable owner key — the owner-feature id for a part body, the component id for an assembly body
    (whatever the rebuild reports as the body's owner; see session._write_aicm). ``color`` is
    ``[r,g,b]`` in 0..1, or ``null`` to clear back to the default palette color. Optional
    ``metallic``/``roughness`` (0..1, clamped) select a RealView material; when either is present the
    value is stored as ``{color, metallic, roughness}``, otherwise as a legacy bare ``[r,g,b]`` array
    (both forms round-trip). Append-only vocabulary entry; mirrors set_folder/set_material's
    create-or-clear shape and round-trips through the document JSON."""
    bid = op.get("id")
    if not isinstance(bid, str) or not bid:
        raise _invalid(index, "set_appearance requires a non-empty 'id' (body owner key)")
    color = op.get("color")
    ap = doc.setdefault("appearances", {})
    if color is None:
        ap.pop(bid, None)
        if not ap:
            doc.pop("appearances", None)
        return
    if not (isinstance(color, list) and len(color) == 3
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in color)):
        raise _invalid(index, "set_appearance.color must be [r,g,b] floats (or null to clear)")
    rgb = [max(0.0, min(1.0, float(c))) for c in color]

    def _clamped(name):
        v = op.get(name)
        if v is None:
            return None
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise _invalid(index, f"set_appearance.{name} must be a number in 0..1")
        return max(0.0, min(1.0, float(v)))

    metallic = _clamped("metallic")
    roughness = _clamped("roughness")
    if metallic is None and roughness is None:
        ap[bid] = rgb  # legacy bare-array form (color-only)
    else:
        ap[bid] = {"color": rgb,
                   "metallic": metallic if metallic is not None else 0.0,
                   "roughness": roughness if roughness is not None else 0.5}


def _op_set_rollback(doc, op, index, idgen, id_map):
    """Move the rollback bar (SolidWorks semantics). ``after`` = feature id the bar sits AFTER, or
    null = bar at the end (compile everything). Validates the feature exists. Undo-able like any op
    (the whole doc is snapshotted by the session before the batch applies)."""
    if "after" not in op:
        raise _invalid(index, "set_rollback requires an 'after' field (feature id or null)")
    after = op.get("after")
    if after is None:
        doc.pop("rollback_after", None)
        return
    if not isinstance(after, str):
        raise _invalid(index, "set_rollback.after must be a feature id string or null")
    _, feat = _find_feature(doc, after)
    if feat is None:
        raise _invalid(index, f"set_rollback: feature '{after}' not found")
    doc["rollback_after"] = after


def _op_set_units(doc, op, index, idgen, id_map):
    system = op.get("system")
    if system not in ("MMGS", "IPS"):
        raise _invalid(index, "set_units.system must be MMGS or IPS")
    doc.setdefault("units", {})["system"] = system


def _op_eq_set(doc, op, index, idgen, id_map):
    name = op.get("name")
    expr = op.get("expr")
    if not isinstance(name, str) or not re.match(r"^[A-Za-z_]\w*$", name):
        raise _invalid(index, f"eq_set: invalid equation name '{name}'")
    if not isinstance(expr, str):
        raise _invalid(index, "eq_set requires a string 'expr'")
    eq = doc.setdefault("equations", {"globals": []})
    globs = eq.setdefault("globals", [])
    for g in globs:
        if isinstance(g, dict) and g.get("name") == name:
            g["expr"] = expr
            return
    globs.append({"name": name, "expr": expr})


def _op_eq_delete(doc, op, index, idgen, id_map):
    name = op.get("name")
    refs = _eq_referencing(doc, name)
    if refs:
        raise OpError("eq_in_use", f"equation '{name}' is referenced by {refs}",
                      {"referencing": refs})
    eq = doc.get("equations", {"globals": []})
    eq["globals"] = [g for g in eq.get("globals", []) if not (isinstance(g, dict) and g.get("name") == name)]


# ---- sketch ----


def _op_sketch_add_entity(doc, op, index, idgen, id_map):
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    entity = op.get("entity")
    if not isinstance(entity, dict):
        raise _invalid(index, "sketch_add_entity requires an 'entity' object")
    eid = idgen.next("e")
    ent = copy.deepcopy(entity)
    ent["id"] = eid
    ent.setdefault("construction", False)
    sk.setdefault("params", {}).setdefault("entities", []).append(ent)
    # Inferred constraints (may use "$self" to refer to this entity).
    for con in op.get("constraints", []) or []:
        c = copy.deepcopy(con)
        c["id"] = idgen.next("c")
        c["refs"] = [_sub_self(r, eid) for r in (c.get("refs", []) or [])]
        _validate_sketch_constraint(c, index)
        sk["params"].setdefault("constraints", []).append(c)
    id_map[f"${index}"] = eid


def _sub_self(handle: Any, eid: str) -> Any:
    """Replace a ``$self`` (optionally ``$self.sub``) handle with the just-created entity id."""
    if isinstance(handle, str) and handle.startswith("$self"):
        return eid + handle[len("$self"):]
    return handle


def _op_sketch_edit_entity(doc, op, index, idgen, id_map):
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    eid = op.get("id")
    for ent in sk.get("params", {}).get("entities", []):
        if ent.get("id") == eid:
            if isinstance(op.get("params"), dict):
                _patch(ent, op["params"])
            return
    raise _invalid(index, f"sketch_edit_entity: entity '{eid}' not found")


def _op_sketch_delete_entity(doc, op, index, idgen, id_map):
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    eid = op.get("id")
    params = sk.setdefault("params", {})
    ents = params.setdefault("entities", [])
    if not any(e.get("id") == eid for e in ents):
        raise _invalid(index, f"sketch_delete_entity: entity '{eid}' not found")
    params["entities"] = [e for e in ents if e.get("id") != eid]
    # Cascade: drop constraints whose handles touch this entity.
    cons = params.setdefault("constraints", [])
    params["constraints"] = [
        c for c in cons
        if not any(_handle_base(h) == eid for h in (c.get("refs", []) or []))
    ]


def _handle_base(handle: Any) -> str | None:
    if isinstance(handle, str):
        return handle.split(".", 1)[0]
    return None


def _op_sketch_add_constraint(doc, op, index, idgen, id_map):
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    con = op.get("constraint")
    if not isinstance(con, dict):
        raise _invalid(index, "sketch_add_constraint requires a 'constraint' object")
    c = copy.deepcopy(con)
    c["id"] = idgen.next("c")
    _validate_sketch_constraint(c, index)
    sk.setdefault("params", {}).setdefault("constraints", []).append(c)
    id_map[f"${index}"] = c["id"]


_CONSTRAINT_REF_COUNTS = {
    "coincident": (2, 2),
    "horizontal": (1, 2),
    "vertical": (1, 2),
    "parallel": (2, 2),
    "perpendicular": (2, 2),
    "tangent": (2, 2),
    "collinear": (2, 2),
    "equal": (2, 2),
    "concentric": (2, 2),
    "coradial": (2, 2),
    "midpoint": (2, 2),
    "symmetric": (3, 3),
    "fix": (1, 1),
    "point_on": (2, 2),
    "distance": (2, 2),
    "angle": (2, 2),
    "radius": (1, 1),
    "diameter": (1, 1),
}
_DIM_CONSTRAINT_TYPES = {"distance", "angle", "radius", "diameter"}


def _validate_sketch_constraint(con: dict, index: int) -> None:
    ctype = con.get("type")
    if not isinstance(ctype, str) or ctype not in _CONSTRAINT_REF_COUNTS:
        raise _invalid(index, f"unsupported sketch constraint type '{ctype}'")
    refs = con.get("refs")
    if not isinstance(refs, list) or not all(isinstance(r, str) and r for r in refs):
        raise _invalid(index, f"sketch constraint '{ctype}' requires string refs")
    lo, hi = _CONSTRAINT_REF_COUNTS[ctype]
    if len(refs) < lo or len(refs) > hi:
        want = str(lo) if lo == hi else f"{lo}-{hi}"
        raise _invalid(index, f"sketch constraint '{ctype}' requires {want} refs")
    if ctype in _DIM_CONSTRAINT_TYPES:
        val = con.get("value")
        if not isinstance(val, dict) or ("value" not in val and "expr" not in val):
            raise _invalid(index, f"dimension constraint '{ctype}' requires a value object")
    if "driven" in con and not isinstance(con.get("driven"), bool):
        raise _invalid(index, "sketch constraint driven flag must be a bool")


def _op_sketch_delete_constraint(doc, op, index, idgen, id_map):
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    cid = op.get("id")
    params = sk.setdefault("params", {})
    cons = params.setdefault("constraints", [])
    if not any(c.get("id") == cid for c in cons):
        raise _invalid(index, f"sketch_delete_constraint: '{cid}' not found")
    params["constraints"] = [c for c in cons if c.get("id") != cid]


def _op_sketch_set_dim(doc, op, index, idgen, id_map):
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    cid = op.get("id")
    for c in sk.get("params", {}).get("constraints", []):
        if c.get("id") == cid:
            # Any subset of {value|expr, placement, driven} may be supplied (C35-overhaul §4). At
            # least one of value/expr/placement/driven must be present.
            touched = False
            if "expr" in op and op["expr"] is not None:
                cur = c.get("value") if isinstance(c.get("value"), dict) else {"value": 0.0}
                c["value"] = {"value": cur.get("value", 0.0), "expr": op["expr"]}
                touched = True
            elif "value" in op and op["value"] is not None:
                c["value"] = {"value": float(op["value"]), "expr": None}
                touched = True
            if "placement" in op:
                if op["placement"] is None:
                    c.pop("placement", None)
                elif isinstance(op["placement"], list) and len(op["placement"]) == 2:
                    c["placement"] = [float(op["placement"][0]), float(op["placement"][1])]
                else:
                    raise _invalid(index, "sketch_set_dim.placement must be [u, v] or null")
                touched = True
            if "driven" in op and op["driven"] is not None:
                if not isinstance(op["driven"], bool):
                    raise _invalid(index, "sketch_set_dim.driven must be a bool")
                c["driven"] = op["driven"]
                touched = True
            if not touched:
                raise _invalid(index, "sketch_set_dim requires 'value', 'expr', "
                                      "'placement', or 'driven'")
            return
    raise _invalid(index, f"sketch_set_dim: constraint '{cid}' not found")


# ---- sketch geometry tools (C29-A): trim / extend / offset ----
#
# These mutate the sketch feature's entity list and flow through the normal apply_ops rebuild path
# (revision bump, solver re-run, tree/aicm re-emit) via session.apply_ops, exactly like
# sketch_add_entity. The pure 2D math lives in engine.doc.sketch_geom; this layer resolves ids,
# splices the entity list, drops dangling constraints, and records the id mapping.
#
# id_map convention (extends the flat "$<index>" -> id scheme): an op that produces several entities
# records the primary/surviving id under "$<index>" and any additional new ids under
# "$<index>#1", "$<index>#2", ... (still flat string->string, JSON-safe).


def _sketch_entities(sk: dict) -> list[dict]:
    return sk.setdefault("params", {}).setdefault("entities", [])


def _find_entity(sk: dict, eid: str) -> dict | None:
    for e in _sketch_entities(sk):
        if e.get("id") == eid:
            return e
    return None


def _geom_view(ent: dict) -> dict:
    """A plain-math entity dict (engine.doc.sketch_geom shape) for one stored sketch entity."""
    return {k: copy.deepcopy(v) for k, v in ent.items() if k not in ("id", "construction")}


def _drop_constraints_touching(sk: dict, eids: set[str]) -> None:
    """Drop constraints whose handles touch any removed entity (mirrors sketch_delete_entity)."""
    params = sk.setdefault("params", {})
    cons = params.setdefault("constraints", [])
    params["constraints"] = [
        c for c in cons
        if not any(_handle_base(h) in eids for h in (c.get("refs", []) or []))
    ]


def _stamp_entity(ent: dict, geom: dict, eid: str, construction: bool) -> dict:
    """Build a full stored entity from a sketch_geom result dict + id + construction flag."""
    out = copy.deepcopy(geom)
    out["id"] = eid
    out["construction"] = bool(construction)
    return out


def _op_sketch_trim(doc, op, index, idgen, id_map):
    from engine.doc import sketch_geom as geom

    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    eid = op.get("entity")
    target = _find_entity(sk, eid)
    if target is None:
        raise _invalid(index, f"sketch_trim: entity '{eid}' not found")
    point = op.get("point")
    if not (isinstance(point, list) and len(point) == 2):
        raise _invalid(index, "sketch_trim requires 'point': [u, v]")

    ents = _sketch_entities(sk)
    # Cut against every OTHER non-construction supported entity.
    others = [_geom_view(e) for e in ents
              if e.get("id") != eid and not e.get("construction", False)]
    try:
        spans = geom.split_entity(_geom_view(target), others, [float(point[0]), float(point[1])])
    except geom.UnsupportedEntity as exc:
        raise OpError("unsupported_entity",
                      f"op[{index}]: sketch_trim involves an unsupported entity ({exc.etype})",
                      {"index": index, "reason": f"unsupported entity '{exc.etype}'"})

    pos = next(i for i, e in enumerate(ents) if e.get("id") == eid)
    construction = bool(target.get("construction", False))

    if not spans:
        # No interior crossings -> power-trim deletes the whole entity.
        ents.pop(pos)
        _drop_constraints_touching(sk, {eid})
        id_map[f"${index}"] = ""   # nothing survives
        return

    # First surviving span keeps the original id; the rest get fresh ids.
    new_ents: list[dict] = [_stamp_entity(spans[0], spans[0], eid, construction)]
    extra_ids: list[str] = []
    for span in spans[1:]:
        neid = idgen.next("e")
        extra_ids.append(neid)
        new_ents.append(_stamp_entity(span, span, neid, construction))
    ents[pos:pos + 1] = new_ents

    # The target geometry changed shape: drop constraints that referenced the trimmed entity (its
    # handles no longer describe the same span). Mirrors entity-deletion's dangling-constraint drop.
    _drop_constraints_touching(sk, {eid})

    id_map[f"${index}"] = eid
    for k, neid in enumerate(extra_ids, start=1):
        id_map[f"${index}#{k}"] = neid


def _op_sketch_extend(doc, op, index, idgen, id_map):
    from engine.doc import sketch_geom as geom

    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    eid = op.get("entity")
    target = _find_entity(sk, eid)
    if target is None:
        raise _invalid(index, f"sketch_extend: entity '{eid}' not found")
    end = op.get("end")
    if end not in ("start", "end"):
        raise _invalid(index, "sketch_extend requires 'end': 'start' | 'end'")

    ents = _sketch_entities(sk)
    others = [_geom_view(e) for e in ents
              if e.get("id") != eid and not e.get("construction", False)]
    try:
        new_geom = geom.extend_entity(_geom_view(target), end, others)
    except geom.UnsupportedEntity as exc:
        raise OpError("unsupported_entity",
                      f"op[{index}]: sketch_extend involves an unsupported entity ({exc.etype})",
                      {"index": index, "reason": f"unsupported entity '{exc.etype}'"})

    if new_geom is None:
        raise OpError("extend_no_target",
                      f"op[{index}]: sketch_extend found nothing to extend '{eid}' to",
                      {"index": index, "reason": "no target ahead of the extended end"})

    # In-place edit of the existing entity's geometry fields (id/construction preserved).
    for k in list(target.keys()):
        if k not in ("id", "construction"):
            target.pop(k, None)
    target.update(new_geom)
    id_map[f"${index}"] = eid


def _op_sketch_offset(doc, op, index, idgen, id_map):
    from engine.doc import sketch_geom as geom

    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    eids = op.get("entities")
    if not (isinstance(eids, list) and eids):
        raise _invalid(index, "sketch_offset requires a non-empty 'entities' id list")
    distance = op.get("distance")
    if not isinstance(distance, (int, float)) or isinstance(distance, bool):
        raise _invalid(index, "sketch_offset requires a numeric 'distance'")
    side = op.get("side")
    if not (isinstance(side, list) and len(side) == 2):
        raise _invalid(index, "sketch_offset requires 'side': [u, v]")

    chain: list[dict] = []
    for e_id in eids:
        ent = _find_entity(sk, e_id)
        if ent is None:
            raise _invalid(index, f"sketch_offset: entity '{e_id}' not found")
        chain.append(_geom_view(ent))

    try:
        out = geom.offset_chain(chain, float(distance),
                                [float(side[0]), float(side[1])])
        closed = geom._chain_is_closed(chain)
    except geom.UnsupportedEntity as exc:
        raise OpError("unsupported_entity",
                      f"op[{index}]: sketch_offset involves an unsupported entity ({exc.etype})",
                      {"index": index, "reason": f"unsupported entity '{exc.etype}'"})

    ents = _sketch_entities(sk)
    cons = sk["params"].setdefault("constraints", [])
    new_ids: list[str] = []
    for span in out:
        neid = idgen.next("e")
        new_ids.append(neid)
        ents.append(_stamp_entity(span, span, neid, False))

    # Coincident constraints at the NEW joints only (consecutive offset entities meet end->start;
    # a closed chain also joins the last back to the first). Handle names are per-type: a line's end
    # is p2 / start is p1, an arc's are end / start (schema HANDLE_RE).
    def _end_handle(eid: str, which: str) -> str:
        etype = _find_entity(sk, eid).get("type")
        if etype == "line":
            return f"{eid}." + ("p1" if which == "start" else "p2")
        return f"{eid}.{which}"

    n = len(new_ids)
    joints = n if (closed and n > 1) else n - 1
    for i in range(joints):
        a = new_ids[i]
        b = new_ids[(i + 1) % n]
        cons.append({"id": idgen.next("c"), "type": "coincident",
                     "refs": [_end_handle(a, "end"), _end_handle(b, "start")]})

    id_map[f"${index}"] = new_ids[0] if new_ids else ""
    for k, neid in enumerate(new_ids[1:], start=1):
        id_map[f"${index}#{k}"] = neid


# ---- sketch fillet / chamfer (round/bevel the corner where two segments meet) ----
#
# Both mirror sketch_trim/extend/offset: resolve the two segment ids, run the pure 2D math in
# sketch_geom, edit the two lines' corner endpoints back to the tangent/setback points, and insert
# the connecting geometry (a tangent arc for fillet, a line for chamfer) with the relations that
# keep the corner closed (coincident at both joins) and — for the fillet — tangent to both edges.
# Only line-line corners are supported (the solver has no line-arc/arc-arc tangent yet); other
# entity combinations raise a clean OpError. The id_map records the new entity under "$<index>".


def _line_end_handle(eid: str, which: str) -> str:
    """Map a geom end name ('start'|'end') to a line's point handle ('p1'|'p2')."""
    return f"{eid}." + ("p1" if which == "start" else "p2")


def _set_line_end(ent: dict, which: str, pt) -> None:
    """Move a line entity's corner endpoint to *pt* ('start'->p1, 'end'->p2)."""
    ent["p1" if which == "start" else "p2"] = [float(pt[0]), float(pt[1])]


def _drop_corner_join(sk: dict, h1: str, h2: str) -> None:
    """Drop any constraint that JOINED the two corner endpoints to each other (the original
    coincident/coradial/midpoint/etc. at the sharp corner). That corner no longer exists — the
    fillet/chamfer splits it into two distinct tangent points — so leaving the stale join would
    pull the trimmed ends back together and undo the round/bevel at solve time. Constraints that
    touch only ONE of the two handles (e.g. the next edge's own corner join) are preserved."""
    params = sk.setdefault("params", {})
    cons = params.setdefault("constraints", [])
    kept = []
    for c in cons:
        refs = c.get("refs", []) or []
        if h1 in refs and h2 in refs:
            continue  # this constraint binds the two corner endpoints together -> drop it
        kept.append(c)
    params["constraints"] = kept


def _op_sketch_fillet(doc, op, index, idgen, id_map):
    from engine.doc import sketch_geom as geom

    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    e1id = op.get("e1")
    e2id = op.get("e2")
    e1 = _find_entity(sk, e1id)
    e2 = _find_entity(sk, e2id)
    if e1 is None or e2 is None:
        raise _invalid(index, "sketch_fillet requires existing 'e1' and 'e2' entity ids")
    if e1id == e2id:
        raise _invalid(index, "sketch_fillet: e1 and e2 must be different entities")
    radius = op.get("radius")
    if not isinstance(radius, (int, float)) or isinstance(radius, bool) or radius <= 0:
        raise _invalid(index, "sketch_fillet requires a positive numeric 'radius'")

    for ent in (e1, e2):
        if ent.get("type") != "line":
            raise OpError("unsupported_entity",
                          f"op[{index}]: sketch_fillet supports line-line corners only "
                          f"(got '{ent.get('type')}')",
                          {"index": index, "reason": f"unsupported entity '{ent.get('type')}'"})

    try:
        res = geom.fillet_corner(_geom_view(e1), _geom_view(e2), float(radius))
    except geom.GeomError as exc:
        raise OpError("fillet_failed", f"op[{index}]: sketch_fillet — {exc.reason}",
                      {"index": index, "reason": exc.reason})

    # The sharp corner is gone — drop the original coincident that joined the two corner ends.
    _drop_corner_join(sk, _line_end_handle(e1id, res["end_a"]),
                      _line_end_handle(e2id, res["end_b"]))

    # Trim each line back to its tangent point.
    _set_line_end(e1, res["end_a"], res["ta"])
    _set_line_end(e2, res["end_b"], res["tb"])

    # Insert the tangent arc (ta -> tb).
    aid = idgen.next("e")
    arc = copy.deepcopy(res["arc"])
    arc["id"] = aid
    arc["construction"] = False
    _sketch_entities(sk).append(arc)

    # Relations: arc endpoints coincident with the trimmed line ends + tangent to both lines.
    cons = sk["params"].setdefault("constraints", [])
    cons.append({"id": idgen.next("c"), "type": "coincident",
                 "refs": [f"{aid}.start", _line_end_handle(e1id, res["end_a"])]})
    cons.append({"id": idgen.next("c"), "type": "coincident",
                 "refs": [f"{aid}.end", _line_end_handle(e2id, res["end_b"])]})
    cons.append({"id": idgen.next("c"), "type": "tangent", "refs": [aid, e1id]})
    cons.append({"id": idgen.next("c"), "type": "tangent", "refs": [aid, e2id]})

    id_map[f"${index}"] = aid


def _op_sketch_chamfer(doc, op, index, idgen, id_map):
    from engine.doc import sketch_geom as geom

    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    e1id = op.get("e1")
    e2id = op.get("e2")
    e1 = _find_entity(sk, e1id)
    e2 = _find_entity(sk, e2id)
    if e1 is None or e2 is None:
        raise _invalid(index, "sketch_chamfer requires existing 'e1' and 'e2' entity ids")
    if e1id == e2id:
        raise _invalid(index, "sketch_chamfer: e1 and e2 must be different entities")
    distance = op.get("distance")
    if not isinstance(distance, (int, float)) or isinstance(distance, bool) or distance <= 0:
        raise _invalid(index, "sketch_chamfer requires a positive numeric 'distance'")
    # Optional unequal setback on the second edge; defaults to equal.
    distance2 = op.get("distance2")
    if distance2 is None:
        distance2 = distance
    elif not isinstance(distance2, (int, float)) or isinstance(distance2, bool) or distance2 <= 0:
        raise _invalid(index, "sketch_chamfer 'distance2' must be a positive number when given")

    for ent in (e1, e2):
        if ent.get("type") != "line":
            raise OpError("unsupported_entity",
                          f"op[{index}]: sketch_chamfer supports line-line corners only "
                          f"(got '{ent.get('type')}')",
                          {"index": index, "reason": f"unsupported entity '{ent.get('type')}'"})

    try:
        res = geom.chamfer_corner(_geom_view(e1), _geom_view(e2),
                                  float(distance), float(distance2))
    except geom.GeomError as exc:
        raise OpError("chamfer_failed", f"op[{index}]: sketch_chamfer — {exc.reason}",
                      {"index": index, "reason": exc.reason})

    # The sharp corner is gone — drop the original coincident that joined the two corner ends.
    _drop_corner_join(sk, _line_end_handle(e1id, res["end_a"]),
                      _line_end_handle(e2id, res["end_b"]))

    # Trim each line back to its setback point.
    _set_line_end(e1, res["end_a"], res["ta"])
    _set_line_end(e2, res["end_b"], res["tb"])

    # Insert the connecting bevel line (ta -> tb).
    lid = idgen.next("e")
    line = {"id": lid, "type": "line", "construction": False,
            "p1": [float(res["ta"][0]), float(res["ta"][1])],
            "p2": [float(res["tb"][0]), float(res["tb"][1])]}
    _sketch_entities(sk).append(line)

    # Relations: bevel endpoints coincident with the trimmed line ends.
    cons = sk["params"].setdefault("constraints", [])
    cons.append({"id": idgen.next("c"), "type": "coincident",
                 "refs": [f"{lid}.p1", _line_end_handle(e1id, res["end_a"])]})
    cons.append({"id": idgen.next("c"), "type": "coincident",
                 "refs": [f"{lid}.p2", _line_end_handle(e2id, res["end_b"])]})

    id_map[f"${index}"] = lid


# ---- C53: sketch mirror / pattern / convert (reserved-now, implemented here) ----
#
# All three mutate the active sketch's entity (and constraint) lists and flow through the normal
# apply_ops rebuild path, like the trim/extend/offset/fillet/chamfer tools above. The pure 2D math
# (reflection + the linear/circular step transforms) is inline here — it is small and entity-shaped,
# unlike the curve splitting in sketch_geom. id_map records the FIRST new entity under "$<index>"
# and any further new entities under "$<index>#1", "$<index>#2", ... (the flat string->string scheme
# the offset/trim ops already use).
#
# Op request schemas (host ribbon dispatches to match these — see docs/contracts/doc_ops.md):
#   sketch_mirror_entities  {sketch, entities:[id...], line:<construction-line entity id>}
#   sketch_pattern_entities {sketch, mode:"linear", entities:[id...], dir:[dx,dy], count, spacing}
#                           {sketch, mode:"circular", entities:[id...], center:[x,y], count, angle,
#                                                     equal_spacing?:bool}
#   sketch_convert          {sketch, refs:[ {datum|kind+selector}, ... ]}
#
# Supported entity types for mirror/pattern: line, circle, arc (the solver's first-class types). Any
# other type in the selection raises a clean OpError("unsupported_entity") with NO mutation.

_MP_SUPPORTED = ("line", "circle", "arc")


def _reflect_point(pt, A, B):
    """Reflect point *pt* across the infinite line through A and B (all [x, y] sequences)."""
    import math as _m
    ax, ay = float(A[0]), float(A[1])
    bx, by = float(B[0]), float(B[1])
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-18:
        raise OpError("mirror_failed", "mirror line is degenerate (zero length)",
                      {"reason": "degenerate mirror line"})
    px, py = float(pt[0]) - ax, float(pt[1]) - ay
    # projection scalar of P onto the line direction
    t = (px * dx + py * dy) / L2
    # foot of perpendicular, then reflect
    fx, fy = ax + t * dx, ay + t * dy
    return [2.0 * fx - float(pt[0]), 2.0 * fy - float(pt[1])]


def _reflect_angle_deg(ang, A, B):
    """Reflect a direction angle (degrees) across the line direction (axis through A->B)."""
    import math as _m
    axis = _m.atan2(float(B[1]) - float(A[1]), float(B[0]) - float(A[0]))
    return _m.degrees(2.0 * axis - _m.radians(float(ang)))


def _mirror_entity(ent: dict, A, B) -> dict:
    """Return the reflected geometry (no id/construction) of a line/circle/arc across line A->B."""
    etype = ent.get("type")
    if etype == "line":
        return {"type": "line",
                "p1": _reflect_point(ent["p1"], A, B),
                "p2": _reflect_point(ent["p2"], A, B)}
    if etype == "circle":
        return {"type": "circle",
                "center": _reflect_point(ent["center"], A, B),
                "radius": float(ent["radius"])}
    if etype == "arc":
        # Reflection reverses orientation: the swept direction flips, so ccw flips and the
        # start/end angles map through the axis (and swap roles to keep start->end sweep).
        a0 = _reflect_angle_deg(ent["start"], A, B)
        a1 = _reflect_angle_deg(ent["end"], A, B)
        return {"type": "arc",
                "center": _reflect_point(ent["center"], A, B),
                "radius": float(ent["radius"]),
                "start": a1, "end": a0,
                "ccw": not bool(ent.get("ccw", True))}
    raise OpError("unsupported_entity",
                  f"mirror/pattern does not support entity type '{etype}'",
                  {"reason": f"unsupported entity '{etype}'"})


def _translate_entity(geom: dict, dx: float, dy: float) -> dict:
    """Copy *geom* translated by (dx, dy). Geometry-only dict (no id/construction)."""
    out = copy.deepcopy(geom)
    etype = out.get("type")
    if etype == "line":
        out["p1"] = [out["p1"][0] + dx, out["p1"][1] + dy]
        out["p2"] = [out["p2"][0] + dx, out["p2"][1] + dy]
    else:  # circle / arc
        out["center"] = [out["center"][0] + dx, out["center"][1] + dy]
    return out


def _rotate_entity(geom: dict, cx: float, cy: float, ang_deg: float) -> dict:
    """Copy *geom* rotated by *ang_deg* about (cx, cy). Geometry-only dict (no id/construction)."""
    import math as _m
    a = _m.radians(ang_deg)
    ca, sa = _m.cos(a), _m.sin(a)

    def rot(p):
        x, y = float(p[0]) - cx, float(p[1]) - cy
        return [cx + x * ca - y * sa, cy + x * sa + y * ca]

    out = copy.deepcopy(geom)
    etype = out.get("type")
    if etype == "line":
        out["p1"] = rot(out["p1"])
        out["p2"] = rot(out["p2"])
    else:  # circle / arc
        out["center"] = rot(out["center"])
        if etype == "arc":
            out["start"] = float(out["start"]) + ang_deg
            out["end"] = float(out["end"]) + ang_deg
    return out


def _symmetric_pairs(etype: str, a: str, b: str):
    """Point-handle pairs whose symmetric relation (about the mirror axis) keeps a<->b mirrored.
    Returns a list of (handle_a, handle_b) plus a flag list of 'equal' pairs (radius equality)."""
    if etype == "line":
        return [(f"{a}.p1", f"{b}.p1"), (f"{a}.p2", f"{b}.p2")], False
    if etype == "circle":
        return [(f"{a}.center", f"{b}.center")], True
    if etype == "arc":
        return [(f"{a}.center", f"{b}.center"),
                (f"{a}.start", f"{b}.start"),
                (f"{a}.end", f"{b}.end")], True
    return [], False


def _op_sketch_mirror_entities(doc, op, index, idgen, id_map):
    """Reflect the selected entities across a mirror line and add symmetric relations so the
    mirrored copies stay tied to the originals (SolidWorks "Mirror Entities")."""
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    eids = op.get("entities")
    if not (isinstance(eids, list) and eids):
        raise _invalid(index, "sketch_mirror_entities requires a non-empty 'entities' id list")
    line_id = op.get("line")
    if not isinstance(line_id, str) or not line_id:
        raise _invalid(index, "sketch_mirror_entities requires a 'line' (mirror line entity id)")
    axis = _find_entity(sk, line_id)
    if axis is None:
        raise _invalid(index, f"sketch_mirror_entities: mirror line '{line_id}' not found")
    if axis.get("type") != "line":
        raise OpError("unsupported_entity",
                      "sketch_mirror_entities: the mirror line must be a line entity",
                      {"reason": f"mirror line is a '{axis.get('type')}'"})
    A, B = axis["p1"], axis["p2"]

    # Resolve + validate every selected entity FIRST (atomic: reject before any mutation).
    sources: list[dict] = []
    for e_id in eids:
        if e_id == line_id:
            raise _invalid(index, "sketch_mirror_entities: cannot mirror the mirror line itself")
        ent = _find_entity(sk, e_id)
        if ent is None:
            raise _invalid(index, f"sketch_mirror_entities: entity '{e_id}' not found")
        if ent.get("type") not in _MP_SUPPORTED:
            raise OpError("unsupported_entity",
                          f"op[{index}]: sketch_mirror_entities does not support entity type "
                          f"'{ent.get('type')}'",
                          {"index": index, "reason": f"unsupported entity '{ent.get('type')}'"})
        sources.append(ent)

    ents = _sketch_entities(sk)
    cons = sk["params"].setdefault("constraints", [])
    new_ids: list[str] = []
    for ent in sources:
        geom = _mirror_entity(ent, A, B)
        neid = idgen.next("e")
        new_ids.append(neid)
        ents.append(_stamp_entity(geom, geom, neid, bool(ent.get("construction", False))))
        # Symmetric relations: each original<->mirror point pair about the mirror line, plus an
        # equal relation for the radius of circles/arcs (the symmetric pairs only tie positions).
        pairs, needs_equal = _symmetric_pairs(ent["type"], ent["id"], neid)
        for ha, hb in pairs:
            cons.append({"id": idgen.next("c"), "type": "symmetric",
                         "refs": [ha, hb, line_id]})
        if needs_equal:
            cons.append({"id": idgen.next("c"), "type": "equal",
                         "refs": [ent["id"], neid]})

    id_map[f"${index}"] = new_ids[0] if new_ids else ""
    for k, neid in enumerate(new_ids[1:], start=1):
        id_map[f"${index}#{k}"] = neid


def _op_sketch_pattern_entities(doc, op, index, idgen, id_map):
    """Linear or circular pattern of the selected entities. v1 copies are plain geometry (no
    per-instance relations), like a SolidWorks geometry pattern. ``mode`` selects the kind."""
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    eids = op.get("entities")
    if not (isinstance(eids, list) and eids):
        raise _invalid(index, "sketch_pattern_entities requires a non-empty 'entities' id list")
    mode = op.get("mode")
    if mode not in ("linear", "circular"):
        raise _invalid(index, "sketch_pattern_entities requires mode 'linear' or 'circular'")
    count = op.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 2:
        raise _invalid(index, "sketch_pattern_entities requires an integer 'count' >= 2")

    # Resolve + validate the selection up front (atomic).
    sources: list[dict] = []
    for e_id in eids:
        ent = _find_entity(sk, e_id)
        if ent is None:
            raise _invalid(index, f"sketch_pattern_entities: entity '{e_id}' not found")
        if ent.get("type") not in _MP_SUPPORTED:
            raise OpError("unsupported_entity",
                          f"op[{index}]: sketch_pattern_entities does not support entity type "
                          f"'{ent.get('type')}'",
                          {"index": index, "reason": f"unsupported entity '{ent.get('type')}'"})
        sources.append(ent)

    # Build the per-step transform list (count-1 copies).
    steps: list[tuple] = []  # ("t", dx, dy) | ("r", cx, cy, ang)
    if mode == "linear":
        d = op.get("dir")
        if not (isinstance(d, list) and len(d) == 2
                and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in d)):
            raise _invalid(index, "sketch_pattern_entities (linear) requires 'dir': [dx, dy]")
        spacing = op.get("spacing")
        if not isinstance(spacing, (int, float)) or isinstance(spacing, bool):
            raise _invalid(index, "sketch_pattern_entities (linear) requires a numeric 'spacing'")
        import math as _m
        nx, ny = float(d[0]), float(d[1])
        L = _m.hypot(nx, ny)
        if L < 1e-12:
            raise _invalid(index, "sketch_pattern_entities (linear) 'dir' is zero-length")
        nx, ny = nx / L, ny / L
        for i in range(1, count):
            steps.append(("t", nx * float(spacing) * i, ny * float(spacing) * i))
    else:  # circular
        center = op.get("center")
        if not (isinstance(center, list) and len(center) == 2
                and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in center)):
            raise _invalid(index, "sketch_pattern_entities (circular) requires 'center': [x, y]")
        angle = op.get("angle")
        if not isinstance(angle, (int, float)) or isinstance(angle, bool):
            raise _invalid(index, "sketch_pattern_entities (circular) requires a numeric 'angle'")
        cx, cy = float(center[0]), float(center[1])
        # equal_spacing: 'angle' is the TOTAL span shared equally (e.g. 360 for a full ring);
        # otherwise 'angle' is the step BETWEEN consecutive instances.
        equal = bool(op.get("equal_spacing", False))
        step_ang = (float(angle) / count) if equal else float(angle)
        for i in range(1, count):
            steps.append(("r", cx, cy, step_ang * i))

    ents = _sketch_entities(sk)
    new_ids: list[str] = []
    for tf in steps:
        for ent in sources:
            geom = _geom_view(ent)
            if tf[0] == "t":
                placed = _translate_entity(geom, tf[1], tf[2])
            else:
                placed = _rotate_entity(geom, tf[1], tf[2], tf[3])
            neid = idgen.next("e")
            new_ids.append(neid)
            ents.append(_stamp_entity(placed, placed, neid,
                                      bool(ent.get("construction", False))))

    id_map[f"${index}"] = new_ids[0] if new_ids else ""
    for k, neid in enumerate(new_ids[1:], start=1):
        id_map[f"${index}#{k}"] = neid


def _op_sketch_move(doc, op, index, idgen, id_map):
    """Translate the selected entities by (dx, dy) — SolidWorks "Move/Copy Entities".

    ``copy`` false (default): move IN PLACE — each entity's own geometry shifts by (dx, dy) and its
    relations ride along (no new ids). ``copy`` true: the originals are left untouched and a single
    translated copy of each is APPENDED (like a pattern with count=2, copies only). The plain
    geometry copies carry no per-instance relations. Supported entity types: line, circle, arc."""
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    eids = op.get("entities")
    if not (isinstance(eids, list) and eids):
        raise _invalid(index, "sketch_move requires a non-empty 'entities' id list")
    dx = op.get("dx")
    dy = op.get("dy")
    if not isinstance(dx, (int, float)) or isinstance(dx, bool):
        raise _invalid(index, "sketch_move requires a numeric 'dx'")
    if not isinstance(dy, (int, float)) or isinstance(dy, bool):
        raise _invalid(index, "sketch_move requires a numeric 'dy'")
    copy_flag = op.get("copy", False)
    if not isinstance(copy_flag, bool):
        raise _invalid(index, "sketch_move 'copy' must be a bool")

    # Resolve + validate the selection up front (atomic: reject before any mutation).
    sources: list[dict] = []
    for e_id in eids:
        ent = _find_entity(sk, e_id)
        if ent is None:
            raise _invalid(index, f"sketch_move: entity '{e_id}' not found")
        if ent.get("type") not in _MP_SUPPORTED:
            raise OpError("unsupported_entity",
                          f"op[{index}]: sketch_move does not support entity type "
                          f"'{ent.get('type')}'",
                          {"index": index, "reason": f"unsupported entity '{ent.get('type')}'"})
        sources.append(ent)

    dxf, dyf = float(dx), float(dy)
    if not copy_flag:
        # In-place: shift each source entity's own geometry; constraints ride along untouched.
        for ent in sources:
            moved = _translate_entity(_geom_view(ent), dxf, dyf)
            for k in moved:
                ent[k] = moved[k]
        id_map[f"${index}"] = sources[0]["id"] if sources else ""
        return

    # Copy: append one translated copy of each source (plain geometry, no relations).
    ents = _sketch_entities(sk)
    new_ids: list[str] = []
    for ent in sources:
        geom = _translate_entity(_geom_view(ent), dxf, dyf)
        neid = idgen.next("e")
        new_ids.append(neid)
        ents.append(_stamp_entity(geom, geom, neid, bool(ent.get("construction", False))))

    id_map[f"${index}"] = new_ids[0] if new_ids else ""
    for k, neid in enumerate(new_ids[1:], start=1):
        id_map[f"${index}#{k}"] = neid


def _op_sketch_convert(doc, op, index, idgen, id_map):
    """Project referenced model geometry (datum plane / planar face / model edge) onto the sketch
    plane as real sketch entities (SolidWorks "Convert Entities"). Reuses session.sketch_project_ref
    for the projection math, then appends each projected entity to the sketch. The projected geometry
    is pinned with a whole-entity ``fix`` relation so it tracks the model (it is reference geometry,
    not free sketch geometry)."""
    from engine.doc import session as _sess

    fid = _resolve_placeholder(op.get("sketch"), id_map, index)
    sk = _find_sketch(doc, fid)
    refs = op.get("refs")
    if not (isinstance(refs, list) and refs):
        raise _invalid(index, "sketch_convert requires a non-empty 'refs' list")

    # sketch_project_ref needs a live DocSession to resolve the sketch plane + model topology. The
    # op layer mutates a working copy of the doc; wrap it in a transient session so the projection
    # sees the same geometry (it is read-only and never registered).
    tmp = _sess.DocSession(doc_id="__convert_tmp__", doc=doc)
    try:
        projected = []
        for i, ref in enumerate(refs):
            res = _sess.sketch_project_ref(tmp, fid, ref)
            projected.append(res["entity"])
    except OpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OpError("convert_failed",
                      f"op[{index}]: sketch_convert could not project a reference ({exc})",
                      {"index": index, "reason": str(exc)})

    ents = _sketch_entities(sk)
    cons = sk["params"].setdefault("constraints", [])
    new_ids: list[str] = []
    for ent in projected:
        if ent.get("type") == "point":
            # A reference edge that projects to a single point — store it as a point entity.
            geom = {"type": "point", "p": list(ent["p"])}
        else:
            geom = copy.deepcopy(ent)
        neid = idgen.next("e")
        new_ids.append(neid)
        ents.append(_stamp_entity(geom, geom, neid, False))
        # Pin the projected geometry whole (reference geometry tracks the model).
        cons.append({"id": idgen.next("c"), "type": "fix", "refs": [neid]})

    if not new_ids:
        raise _invalid(index, "sketch_convert: nothing projected")
    id_map[f"${index}"] = new_ids[0]
    for k, neid in enumerate(new_ids[1:], start=1):
        id_map[f"${index}#{k}"] = neid


# ---- assembly ----


def _op_asm_insert_component(doc, op, index, idgen, id_map):
    source = op.get("source")
    if not isinstance(source, dict) or not (source.get("path") or source.get("embedded")):
        raise _invalid(index, "asm_insert_component requires source.{path|embedded}")
    kid = idgen.next("k")
    comps = doc.setdefault("components", [])
    first = len(comps) == 0
    # Default name = source file stem (SolidWorks names components from the file).
    default_name = kid
    if source.get("path"):
        stem = str(source["path"]).replace("\\", "/").rsplit("/", 1)[-1]
        for ext in (".step+", ".step", ".stp", ".stl", ".brep", ".iges", ".igs"):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                break
        default_name = stem or kid
    comp = {
        "id": kid,
        "name": op.get("name") or default_name,
        "source": source,
        "transform": op.get("transform") or _identity16(),
        "fixed": bool(op.get("fixed", first)),
        "suppressed": False,
    }
    comps.append(comp)
    id_map[f"${index}"] = kid


def _identity16() -> list[float]:
    return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def _op_asm_edit_component(doc, op, index, idgen, id_map):
    kid = op.get("id")
    for c in doc.get("components", []):
        if c.get("id") == kid:
            for k in ("transform", "name", "suppressed", "fixed"):
                if k in op and op[k] is not None:
                    c[k] = op[k]
            return
    raise _invalid(index, f"asm_edit_component: '{kid}' not found")


def _op_asm_delete_component(doc, op, index, idgen, id_map):
    kid = op.get("id")
    comps = doc.get("components", [])
    if not any(c.get("id") == kid for c in comps):
        raise _invalid(index, f"asm_delete_component: '{kid}' not found")
    mates = doc.get("mates", [])
    referencing = [m["id"] for m in mates
                   if any(isinstance(r, dict) and r.get("component") == kid for r in m.get("refs", []))]
    if referencing and not bool(op.get("cascade", False)):
        raise OpError("dependency_exists", f"component '{kid}' used by mates {referencing}",
                      {"dependents": referencing})
    doc["components"] = [c for c in comps if c.get("id") != kid]
    doc["mates"] = [m for m in mates
                    if not any(isinstance(r, dict) and r.get("component") == kid for r in m.get("refs", []))]


def _op_asm_add_mate(doc, op, index, idgen, id_map):
    mate = op.get("mate")
    if not isinstance(mate, dict):
        raise _invalid(index, "asm_add_mate requires a 'mate' object")
    m = copy.deepcopy(mate)
    m["id"] = idgen.next("m")
    m.setdefault("suppressed", False)
    m.setdefault("align", "aligned")
    doc.setdefault("mates", []).append(m)
    id_map[f"${index}"] = m["id"]


def _op_asm_edit_mate(doc, op, index, idgen, id_map):
    mid = op.get("id")
    for m in doc.get("mates", []):
        if m.get("id") == mid:
            for k in ("value", "align", "suppressed"):
                if k in op and op[k] is not None:
                    m[k] = op[k]
            return
    raise _invalid(index, f"asm_edit_mate: '{mid}' not found")


def _op_asm_delete_mate(doc, op, index, idgen, id_map):
    mid = op.get("id")
    mates = doc.get("mates", [])
    if not any(m.get("id") == mid for m in mates):
        raise _invalid(index, f"asm_delete_mate: '{mid}' not found")
    doc["mates"] = [m for m in mates if m.get("id") != mid]


def _op_asm_set_explode(doc, op, index, idgen, id_map):
    steps = op.get("steps")
    if not isinstance(steps, list):
        raise _invalid(index, "asm_set_explode requires a 'steps' list")
    doc["explode"] = copy.deepcopy(steps)


# ---- M2 geometry helpers: schema-reserved, not implemented this card ----


def _op_unsupported_m2(doc, op, index, idgen, id_map):
    raise OpError("unsupported", f"op[{index}]: '{op.get('op')}' is reserved for M2",
                  {"index": index, "reason": f"{op.get('op')} not implemented yet"})


# ---- C42: construction toggle (host ribbon "Construction" over the sketch selection) ----


def _op_sketch_toggle_construction(doc, op, index, idgen, id_map):
    """Flip one entity's ``construction`` flag (host Sketch-tab Construction button)."""
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    eid = op.get("id")
    for ent in sk.get("params", {}).get("entities", []):
        if ent.get("id") == eid:
            ent["construction"] = not bool(ent.get("construction", False))
            return
    raise _invalid(index, f"sketch_toggle_construction: entity '{eid}' not found")


# ---- Wave 4A/4B: feature + sketch-entity copy/paste ----------------------------------------
#
# Copy/paste moves features (with their dependency closure) or loose sketch entities between
# documents via a portable, id-free "bundle" (kind ``aicad.feature_bundle/1``). The design keeps
# the op layer pure: copy_feature is READ-ONLY (no doc mutation) and surfaces its bundle through the
# id_map side-channel (``$bundle`` / ``$bundle:<op-index>``); paste_feature re-ids the bundle and
# appends through the SAME rollback-bar-aware path _op_add_feature uses.
#
# Bundle format (aicad.feature_bundle/1):
#   { "kind": "aicad.feature_bundle/1",
#     "source_doc_id":   <str|null>,        # provenance only (derived-link + cross-doc resolve)
#     "source_doc_name": <str|null>,
#     "root": "$0",                          # placeholder of the copied (root) feature
#     "feature_order": ["$0", "$1", ...],    # DEPENDENCY-FIRST build order (deps before dependents)
#     "features": { "$k": <feature> },       # id stripped; in-closure refs -> placeholders
#     "external_refs":   ["f7", ...],        # referenced SOURCE ids NOT in the closure
#     "external_datums": {"f7": "Datum1"} }  # source datum feature id -> its name (cross-doc reuse)
#
# Inside a bundled feature the id is removed; feature-id refs in the closure become ``$k``;
# sketch entity ids become ``~e0,~e1,...``; constraint ids become ``~c0,~c1,...``. Refs that point
# OUT of the closure keep their raw source id and are listed in ``external_refs`` (a datum among
# them is additionally recorded in ``external_datums`` by name, so paste can reuse a same-named
# datum in the target).

_FEATURE_PLACEHOLDER_RE = re.compile(r"^\$(\d+)$")


def _feature_refs_closure(doc: dict, root_fid: str) -> tuple[list[str], set[str]]:
    """Walk every feature *root_fid* (transitively) references — selectors' ``owner_feature``,
    datum/region refs, and ``_FEAT_LIST`` feature-id slots — restricted to real features in *doc*
    (reserved datums excluded). Returns ``(order, in_closure)`` where *order* is dependency-first
    (deps precede dependents) and ends with the root; *in_closure* is the set of included ids."""
    in_closure: set[str] = set()

    def direct_refs(fid: str) -> set[str]:
        _, feat = _find_feature(doc, fid)
        if feat is None:
            return set()
        refs = feat.get("refs", {}) or {}
        out: set[str] = set()
        _walk_selector_owners(refs, out)
        for slot_val in refs.values():
            if isinstance(slot_val, list):
                out.update(x for x in slot_val if isinstance(x, str))
            elif isinstance(slot_val, str):
                out.add(slot_val)
        # only real features in this doc count toward the closure
        out -= set(RESERVED_DATUMS)
        return {r for r in out if _find_feature(doc, r)[1] is not None}

    def visit(fid: str) -> None:
        if fid in in_closure:
            return
        in_closure.add(fid)
        for dep in direct_refs(fid):
            visit(dep)

    visit(root_fid)
    # Dependency-first order = the document's own feature order filtered to the closure (the doc is
    # already a valid topological order: a feature's refs always precede it).
    order = [f["id"] for f in _features(doc)
             if isinstance(f, dict) and f.get("id") in in_closure]
    return order, in_closure


def _stamp_bundle_refs(refs: Any, fid_to_ph: dict[str, str], external_refs: set[str]) -> Any:
    """Deep-copy *refs* with every in-closure source feature id rewritten to its placeholder.
    Out-of-closure ids are left raw and recorded in *external_refs*. Handles selectors
    (``owner_feature``), datum refs (string or ``{kind:datum,feature}``), region refs
    (``{kind:region,sketch}``), and ``_FEAT_LIST`` slots (lists of feature ids)."""
    def remap_fid(fid: str) -> str:
        if fid in fid_to_ph:
            return fid_to_ph[fid]
        if fid not in RESERVED_DATUMS and FEATURE_ID_RE.match(fid):
            external_refs.add(fid)
        return fid

    def walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict = {}
            kind = obj.get("kind")
            for k, v in obj.items():
                if kind in ("face", "edge", "vertex", "body") and k == "owner_feature" \
                        and isinstance(v, str):
                    out[k] = remap_fid(v)
                elif kind == "datum" and k == "feature" and isinstance(v, str):
                    out[k] = remap_fid(v)
                elif kind == "region" and k == "sketch" and isinstance(v, str):
                    out[k] = remap_fid(v)
                else:
                    out[k] = walk(v)
            return out
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if isinstance(obj, str):
            # bare datum id / feature-id-list entry
            if obj in RESERVED_DATUMS:
                return obj
            if FEATURE_ID_RE.match(obj):
                return remap_fid(obj)
        return obj

    return walk(copy.deepcopy(refs or {}))


def _stamp_bundle_feature(feat: dict, fid_to_ph: dict[str, str],
                          external_refs: set[str]) -> dict:
    """Return a portable copy of *feat*: id removed, refs placeholdered, and (for sketches) entity
    ids -> ``~e<n>`` and constraint ids -> ``~c<n>`` with internal handles rewritten in lockstep."""
    out = copy.deepcopy(feat)
    out.pop("id", None)
    out["refs"] = _stamp_bundle_refs(out.get("refs", {}), fid_to_ph, external_refs)
    if out.get("type") == "sketch":
        params = out.setdefault("params", {})
        ents = params.get("entities", []) or []
        cons = params.get("constraints", []) or []
        eid_map: dict[str, str] = {}
        for i, ent in enumerate(ents):
            if isinstance(ent, dict) and isinstance(ent.get("id"), str):
                eid_map[ent["id"]] = f"~e{i}"
        for i, ent in enumerate(ents):
            if isinstance(ent, dict):
                ent["id"] = f"~e{i}"
        for i, con in enumerate(cons):
            if isinstance(con, dict):
                con["id"] = f"~c{i}"
                con["refs"] = [_remap_handle(h, eid_map) for h in (con.get("refs", []) or [])]
    return out


def _remap_handle(handle: Any, eid_map: dict[str, str]) -> Any:
    """Rewrite the base of a sketch handle (``e3`` / ``e3.p1``) through *eid_map*."""
    if isinstance(handle, str):
        base, dot, sub = handle.partition(".")
        if base in eid_map:
            return eid_map[base] + (dot + sub if dot else "")
    return handle


def build_feature_bundle(doc: dict, fid: str, include_dependencies: bool = True) -> dict:
    """Package feature ``fid`` (and, by default, its full reference closure) into a portable
    ``aicad.feature_bundle/1`` bundle. Pure read — no mutation of *doc*. Raises ``OpError`` if *fid*
    is not a feature in *doc*. This is the shared bundle builder used by both copy_feature (the
    read-only op) and session.refresh_derived (the live cross-doc re-pull); keeping it in one place
    guarantees a refresh re-runs the EXACT copy path the original paste used.

    ``include_dependencies`` (default True): walk the FULL reference closure (every feature this one
    transitively references via selectors / datum / region / pattern-feature-list refs) so the paste
    is self-contained. When False, only the root is bundled and every referenced feature is flagged
    in ``external_refs`` (datums additionally in ``external_datums`` by name)."""
    _, feat = _find_feature(doc, fid)
    if feat is None:
        raise OpError("invalid_op", f"feature '{fid}' not found",
                      {"reason": f"unknown feature {fid}"})
    if include_dependencies:
        order, _in_closure = _feature_refs_closure(doc, fid)
    else:
        order = [fid]

    # placeholder per included feature, in dependency-first order
    fid_to_ph = {f: f"${i}" for i, f in enumerate(order)}
    external_refs: set[str] = set()
    features: dict[str, dict] = {}
    for src_fid in order:
        _, f = _find_feature(doc, src_fid)
        features[fid_to_ph[src_fid]] = _stamp_bundle_feature(f, fid_to_ph, external_refs)

    # Map each external datum source id -> its name so paste can reuse a same-named datum.
    external_datums: dict[str, str] = {}
    for ext in external_refs:
        _, ef = _find_feature(doc, ext)
        if ef is not None and ef.get("type") in ("plane", "axis", "point", "csys"):
            external_datums[ext] = ef.get("name") or ext

    return {
        "kind": "aicad.feature_bundle/1",
        "source_doc_id": doc.get("id"),
        "source_doc_name": doc.get("name"),
        "root": fid_to_ph[fid],
        "feature_order": list(fid_to_ph.values()),
        "features": features,
        "external_refs": sorted(external_refs),
        "external_datums": external_datums,
        # placeholder -> the LIVE source feature id it was bundled from. Provenance only (the bundle
        # is otherwise id-free): paste_feature_as_derived records it into each derived feature's
        # ``derived_from.source_feature`` so a later live refresh (session.refresh_derived) can find
        # the current source feature by id even after re-id. Append-only; paste_feature ignores it.
        "source_ids": {fid_to_ph[src]: src for src in order},
    }


def _op_copy_feature(doc, op, index, idgen, id_map):
    """READ-ONLY: package feature ``id`` (and, by default, its full reference closure) into a
    portable bundle. The bundle is returned via the id_map side-channel under ``$bundle`` (and
    ``$bundle:<index>`` when several copy_feature ops appear in one batch). No document mutation.

    ``include_dependencies`` (default true): walk the FULL reference closure (every feature this one
    transitively references via selectors / datum / region / pattern-feature-list refs) so the
    paste is self-contained. When false, only the root feature is bundled and every referenced
    feature is flagged in ``external_refs`` (datums additionally in ``external_datums`` by name) for
    cross-doc resolution at paste time."""
    fid = op.get("id")
    inc = op.get("include_dependencies", True)
    if not isinstance(inc, bool):
        raise _invalid(index, "copy_feature.include_dependencies must be a bool")
    _, feat = _find_feature(doc, fid)
    if feat is None:
        raise _invalid(index, f"copy_feature: '{fid}' not found")
    bundle = build_feature_bundle(doc, fid, include_dependencies=inc)
    id_map["$bundle"] = bundle
    id_map[f"$bundle:{index}"] = bundle


def _bundle_or_invalid(op, index) -> dict:
    """Validate the inbound bundle shape (shared by paste_feature / paste_feature_as_derived)."""
    bundle = op.get("bundle")
    if not isinstance(bundle, dict):
        raise _invalid(index, "paste requires a 'bundle' object")
    if bundle.get("kind") != "aicad.feature_bundle/1":
        raise _invalid(index, "bundle.kind must be 'aicad.feature_bundle/1'")
    order = bundle.get("feature_order")
    feats = bundle.get("features")
    if not isinstance(order, list) or not order:
        raise _invalid(index, "bundle.feature_order must be a non-empty list")
    if not isinstance(feats, dict):
        raise _invalid(index, "bundle.features must be an object")
    for ph in order:
        if ph not in feats or not isinstance(feats.get(ph), dict):
            raise _invalid(index, f"bundle.features is missing feature for '{ph}'")
    if bundle.get("root") not in order:
        raise _invalid(index, "bundle.root must appear in feature_order")
    return bundle


def _target_datum_by_name(doc: dict, name: str) -> str | None:
    """Find a same-named datum feature (plane/axis/point/csys) in *doc*; else a reserved id."""
    if name in RESERVED_DATUMS:
        return name
    for f in _features(doc):
        if isinstance(f, dict) and f.get("type") in ("plane", "axis", "point", "csys") \
                and f.get("name") == name:
            return f.get("id")
    return None


def _resolve_paste_refs(refs: Any, ph_to_fid: dict[str, str], bundle: dict,
                        doc: dict) -> Any:
    """Re-link a pasted feature's refs against the TARGET doc (selectors.md — never a silent wrong
    pick). In-closure placeholders -> the freshly-assigned target ids. Out-of-closure ids:
      * topology selector (face/edge/vertex/body) -> ``owner_feature=null`` + ``external`` marker so
        the compiler dangles for re-pick;
      * datum -> reuse a same-named target datum (Top/Front/Right/custom) else downgrade to a
        schema-valid plane selector + ``external{...,unresolved:true}``;
      * region -> keep the (dangling) source sketch id + ``external`` marker.
    ``_FEAT_LIST`` slots keep only in-closure (re-id'd) features; external feature ids are dropped
    from the list (they cannot satisfy the schema's precede-rule) and surface as build-time gaps."""
    src_doc_id = bundle.get("source_doc_id")
    src_doc_name = bundle.get("source_doc_name")
    external_datums = bundle.get("external_datums", {}) or {}

    def ext_marker(owner=None, **extra) -> dict:
        m = {"doc_id": src_doc_id, "doc_name": src_doc_name, "owner_feature": owner}
        m.update(extra)
        return m

    def walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            kind = obj.get("kind")
            if kind in ("face", "edge", "vertex", "body"):
                ow = obj.get("owner_feature")
                out = copy.deepcopy(obj)
                if isinstance(ow, str) and ow in ph_to_fid:
                    out["owner_feature"] = ph_to_fid[ow]
                elif isinstance(ow, str):
                    # cross-doc topology: detach + flag for re-pick (compiler dangles).
                    out["owner_feature"] = None
                    out["external"] = ext_marker(owner=ow)
                return out
            if kind == "datum":
                fe = obj.get("feature")
                out = copy.deepcopy(obj)
                if isinstance(fe, str) and fe in ph_to_fid:
                    out["feature"] = ph_to_fid[fe]
                elif isinstance(fe, str):
                    return _resolve_datum(fe, external_datums, doc, src_doc_id, src_doc_name)
                return out
            if kind == "region":
                out = copy.deepcopy(obj)
                sk = obj.get("sketch")
                if isinstance(sk, str) and sk in ph_to_fid:
                    out["sketch"] = ph_to_fid[sk]
                elif isinstance(sk, str):
                    out["external"] = ext_marker(owner=sk)  # dangling source sketch id kept
                return out
            return {k: walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            resolved = [walk(v) for v in obj]
            # _FEAT_LIST slots are lists of bare placeholder/feature-id strings: keep only the ones
            # that resolved to a real pasted feature (external feature ids can't pass validation).
            if obj and all(isinstance(v, str) for v in obj):
                kept = [ph_to_fid[v] for v in obj if v in ph_to_fid]
                if kept != obj:  # this was a feature-id list with external members
                    return kept
            return resolved
        if isinstance(obj, str):
            if obj in ph_to_fid:
                return ph_to_fid[obj]
            if obj in RESERVED_DATUMS:
                return obj
            if FEATURE_ID_RE.match(obj):
                # bare datum-id ref pointing out of closure -> same-named reuse or downgrade.
                return _resolve_datum(obj, external_datums, doc, src_doc_id, src_doc_name)
        return obj

    return walk(copy.deepcopy(refs or {}))


def _resolve_datum(src_id: str, external_datums: dict, doc: dict,
                   src_doc_id, src_doc_name):
    """Resolve an out-of-closure datum ref. Reuse a same-named target datum (returning a datum
    selector or reserved-id string); else downgrade to a schema-valid plane selector flagged
    unresolved (the host re-links cross-doc datums in a follow-up)."""
    name = external_datums.get(src_id)
    if name is not None:
        match = _target_datum_by_name(doc, name)
        if match in RESERVED_DATUMS:
            return match  # bare reserved id is schema-valid wherever sel-or-datum is allowed
        if match is not None:
            return {"kind": "datum", "feature": match}
    # No reuse: schema-valid fallback plane + unresolved marker for host re-link.
    return {"kind": "datum", "feature": "plane.front",
            "external": {"doc_id": src_doc_id, "doc_name": src_doc_name,
                         "owner_feature": src_id, "unresolved": True}}


def _paste_features(doc, bundle, idgen, derived: bool, *, group: str | None = None,
                    stamp_extra: dict | None = None):
    """Re-id every bundled feature, resolve refs against *doc*, and append through the same
    rollback-bar-aware path _op_add_feature uses. Returns the ordered list of new feature ids
    (dependency-first; the root is whichever placeholder bundle.root names).

    When ``derived``: every pasted feature is stamped ``derived_from`` (doc_id/doc_name + the source
    placeholder, plus the LIVE source feature id under ``source_feature`` when the bundle carries the
    ``source_ids`` map, and a shared ``group`` so a later refresh can replace the whole closure as a
    unit — see session.refresh_derived). ``stamp_extra`` (optional) merges extra keys into every
    stamp (used by refresh to re-pin the source revision)."""
    order = bundle["feature_order"]
    feats = bundle["features"]
    source_ids = bundle.get("source_ids", {}) or {}
    grp = group or ("dg-" + uuid.uuid4().hex[:10])
    ph_to_fid = {ph: idgen.next("f") for ph in order}
    new_ids: list[str] = []
    for ph in order:
        src = feats[ph]
        new_fid = ph_to_fid[ph]
        feat = _new_feature_envelope(src, new_fid)
        feat["refs"] = _resolve_paste_refs(src.get("refs", {}), ph_to_fid, bundle, doc)
        if feat.get("type") == "sketch":
            _reid_pasted_sketch(feat, idgen)
        if derived:
            stamp = {
                "doc_id": bundle.get("source_doc_id"),
                "doc_name": bundle.get("source_doc_name"),
                "feature": ph,  # source placeholder (id-free bundle provenance)
                # LIVE source feature id (when the bundle recorded it) so refresh can find the
                # current source definition by id even after the source doc re-ids around it.
                "source_feature": source_ids.get(ph),
                "group": grp,   # shared across the whole pasted closure for unit refresh
            }
            if stamp_extra:
                stamp.update(stamp_extra)
            feat["derived_from"] = stamp
        _append_feature(doc, feat)
        new_ids.append(new_fid)
    return ph_to_fid, new_ids


def _reid_pasted_sketch(feat: dict, idgen: _IdGen) -> None:
    """Assign fresh ``e<N>``/``c<N>`` ids to a pasted sketch's entities/constraints (replacing the
    bundle's ``~e``/``~c`` placeholders) and re-point every constraint handle in lockstep."""
    params = feat.setdefault("params", {})
    eid_map: dict[str, str] = {}
    for ent in params.get("entities", []) or []:
        if isinstance(ent, dict):
            neid = idgen.next("e")
            eid_map[ent.get("id")] = neid
            ent["id"] = neid
    for con in params.get("constraints", []) or []:
        if isinstance(con, dict):
            con["id"] = idgen.next("c")
            con["refs"] = [_remap_handle(h, eid_map) for h in (con.get("refs", []) or [])]


def _append_feature(doc: dict, feat: dict) -> None:
    """Append *feat* honoring the rollback bar (mirrors _op_add_feature's append branch)."""
    feats = _features(doc)
    rb = doc.get("rollback_after")
    bar_pos, bar_feat = (_find_feature(doc, rb) if rb else (None, None))
    if bar_feat is not None and bar_pos < len(feats) - 1:
        feats.insert(bar_pos + 1, feat)
        doc["rollback_after"] = feat["id"]
    else:
        feats.append(feat)


def _op_paste_feature(doc, op, index, idgen, id_map):
    """Re-id a bundle and append it (with refs resolved against this doc). The root feature's new id
    is recorded under ``$<index>``; every pasted feature is enumerated under ``$<index>#k`` in
    dependency-first order. ``target_doc_id`` is accepted (and ignored) for symmetry with the host
    API — paste always targets the doc this op batch runs against."""
    bundle = _bundle_or_invalid(op, index)
    ph_to_fid, new_ids = _paste_features(doc, bundle, idgen, derived=False)
    id_map[f"${index}"] = ph_to_fid[bundle["root"]]
    for k, nid in enumerate(new_ids):
        id_map[f"${index}#{k}"] = nid


def _op_paste_feature_as_derived(doc, op, index, idgen, id_map):
    """Like paste_feature, but stamps a ``derived_from`` link ({doc_id, doc_name, feature}) on each
    pasted feature. This is a ONE-TIME resolve+stamp: the geometry is copied independently now and
    the link is metadata only — LIVE re-link to the source document (so edits propagate) is a
    host-side follow-up (Wave 4C/4D). ``source_doc_id`` is accepted for symmetry and recorded into
    the stamp when the bundle did not carry its own provenance."""
    bundle = _bundle_or_invalid(op, index)
    if op.get("source_doc_id") is not None and bundle.get("source_doc_id") is None:
        bundle = {**bundle, "source_doc_id": op.get("source_doc_id")}
    ph_to_fid, new_ids = _paste_features(doc, bundle, idgen, derived=True)
    id_map[f"${index}"] = ph_to_fid[bundle["root"]]
    for k, nid in enumerate(new_ids):
        id_map[f"${index}#{k}"] = nid


# ---- live derived refresh (cross-document re-pull) -----------------------------------------
#
# A derived feature carries ``derived_from = {doc_id, doc_name, feature(placeholder),
# source_feature(live id), group}``. When the SOURCE feature changes, the derived copy is stale.
# session.refresh_derived fetches the live source via the session registry, re-bundles it with the
# SAME copy path (build_feature_bundle), and calls :func:`refresh_derived_group` to splice the fresh
# definition back over the existing derived features IN PLACE — preserving each derived feature's id
# and tree position. This module stays registry-blind: the session layer fetches the source bundle
# and hands it in here. The op layer only knows how to map a fresh bundle onto an existing group.


def _group_members(doc: dict, group: str) -> list[tuple[int, dict]]:
    """(position, feature) for every feature whose ``derived_from.group`` == *group*, in doc order."""
    out: list[tuple[int, dict]] = []
    for i, f in enumerate(_features(doc)):
        if isinstance(f, dict):
            df = f.get("derived_from")
            if isinstance(df, dict) and df.get("group") == group:
                out.append((i, f))
    return out


def refresh_derived_group(doc: dict, group: str, bundle: dict, idgen: _IdGen) -> dict:
    """Splice a FRESH source bundle over an existing derived *group* IN PLACE (mutates *doc*).

    Each existing group member is matched to a bundle placeholder by its recorded
    ``derived_from.source_feature`` (the live source id the placeholder was bundled from). Matched
    members are REBUILT in place — id + tree position kept, params/refs/name/type replaced from the
    re-resolved bundle, the ``derived_from`` stamp refreshed (and ``derived_stale`` cleared). Bundle
    placeholders with NO existing match (a feature added to the source closure since the first paste)
    are pasted as new derived features (appended). Existing members whose ``source_feature`` no
    longer appears in the bundle (the source feature was deleted, or dropped out of the closure) are
    marked ``derived_stale`` and LEFT in place with their last-good definition (never silently
    removed — selectors.md "never silently wrong").

    Returns ``{"refreshed": [fid...], "added": [fid...], "stale": [fid...]}``.
    """
    order = bundle["feature_order"]
    feats = bundle["features"]
    source_ids = bundle.get("source_ids", {}) or {}
    grp_doc_id = bundle.get("source_doc_id")
    grp_doc_name = bundle.get("source_doc_name")

    members = _group_members(doc, group)
    # Map live-source-id -> existing target feature (the derived copy of that source feature).
    by_source: dict[str, dict] = {}
    for _pos, f in members:
        sf = (f.get("derived_from") or {}).get("source_feature")
        if isinstance(sf, str):
            by_source.setdefault(sf, f)

    # Placeholder -> the TARGET feature id it resolves to: an existing matched member keeps its id;
    # an unmatched (new) placeholder gets a fresh id. This map drives in-closure ref re-resolution
    # so the rebuilt features cross-reference each other (and the new ones) by their target ids.
    ph_to_fid: dict[str, str] = {}
    ph_is_new: dict[str, bool] = {}
    for ph in order:
        src_fid = source_ids.get(ph)
        existing = by_source.get(src_fid) if isinstance(src_fid, str) else None
        if existing is not None:
            ph_to_fid[ph] = existing["id"]
            ph_is_new[ph] = False
        else:
            ph_to_fid[ph] = idgen.next("f")
            ph_is_new[ph] = True

    refreshed: list[str] = []
    added: list[str] = []
    for ph in order:
        src = feats[ph]
        target_fid = ph_to_fid[ph]
        stamp = {
            "doc_id": grp_doc_id,
            "doc_name": grp_doc_name,
            "feature": ph,
            "source_feature": source_ids.get(ph),
            "group": group,
        }
        if ph_is_new[ph]:
            # New source feature in the closure: paste as a fresh derived feature (append).
            feat = _new_feature_envelope(src, target_fid)
            feat["refs"] = _resolve_paste_refs(src.get("refs", {}), ph_to_fid, bundle, doc)
            if feat.get("type") == "sketch":
                _reid_pasted_sketch(feat, idgen)
            feat["derived_from"] = stamp
            _append_feature(doc, feat)
            added.append(target_fid)
        else:
            # Rebuild in place: keep id + tree position, replace the definition.
            _, existing = _find_feature(doc, target_fid)
            rebuilt = _new_feature_envelope(src, target_fid)
            rebuilt["refs"] = _resolve_paste_refs(src.get("refs", {}), ph_to_fid, bundle, doc)
            if rebuilt.get("type") == "sketch":
                _reid_pasted_sketch(rebuilt, idgen)
            # Preserve the existing feature's user-facing name (a derived copy may have been renamed
            # in the target) but otherwise swap the whole definition.
            rebuilt["name"] = existing.get("name", rebuilt.get("name"))
            existing.clear()
            existing.update(rebuilt)
            existing["derived_from"] = stamp
            existing.pop("derived_stale", None)
            refreshed.append(target_fid)

    # Existing members whose source feature is gone from the fresh bundle: mark stale, keep as-is.
    fresh_sources = {source_ids.get(ph) for ph in order}
    stale: list[str] = []
    for _pos, f in members:
        if f["id"] in refreshed:
            continue
        sf = (f.get("derived_from") or {}).get("source_feature")
        if sf not in fresh_sources:
            f["derived_stale"] = {"reason": "source_feature_deleted", "source_feature": sf,
                                  "doc_id": grp_doc_id, "doc_name": grp_doc_name}
            stale.append(f["id"])
    return {"refreshed": refreshed, "added": added, "stale": stale}


def _op_sketch_paste_entities(doc, op, index, idgen, id_map):
    """Paste a bundle of sketch entities (+ their constraints) into ``sketch``, shifted by
    (offset_x, offset_y), with FRESH ids. Constraints internal to the pasted set are kept (their
    handles re-pointed to the new entity ids); constraints that reference any entity OUTSIDE the set
    (or the origin / model geometry) are DROPPED — a pasted selection carries only its own internal
    relations (cf. sketch_move copy=true, which keeps none). Records the first new entity id under
    ``$<index>`` and the rest under ``$<index>#k``."""
    sk = _find_sketch(doc, _resolve_placeholder(op.get("sketch"), id_map, index))
    eb = op.get("entities_bundle")
    if not isinstance(eb, dict):
        raise _invalid(index, "sketch_paste_entities requires an 'entities_bundle' object")
    src_ents = eb.get("entities")
    if not isinstance(src_ents, list) or not src_ents:
        raise _invalid(index, "entities_bundle.entities must be a non-empty list")
    src_cons = eb.get("constraints", []) or []
    if not isinstance(src_cons, list):
        raise _invalid(index, "entities_bundle.constraints must be a list")
    dx = op.get("offset_x")
    dy = op.get("offset_y")
    if not isinstance(dx, (int, float)) or isinstance(dx, bool):
        raise _invalid(index, "sketch_paste_entities requires a numeric 'offset_x'")
    if not isinstance(dy, (int, float)) or isinstance(dy, bool):
        raise _invalid(index, "sketch_paste_entities requires a numeric 'offset_y'")
    dxf, dyf = float(dx), float(dy)

    # Assign fresh ids and shift geometry. The internal set = exactly the entities we paste.
    eid_map: dict[str, str] = {}
    new_entities: list[dict] = []
    for src in src_ents:
        if not isinstance(src, dict) or not isinstance(src.get("id"), str):
            raise _invalid(index, "entities_bundle.entities: each entity needs an 'id'")
        neid = idgen.next("e")
        eid_map[src["id"]] = neid
        geom = _shift_entity_any(src, dxf, dyf)
        geom["id"] = neid
        geom["construction"] = bool(src.get("construction", False))
        new_entities.append(geom)

    ents = _sketch_entities(sk)
    ents.extend(new_entities)

    # Keep only constraints whose handles ALL land inside the pasted set (drop external/origin refs).
    cons = sk["params"].setdefault("constraints", [])
    for con in src_cons:
        if not isinstance(con, dict):
            continue
        handles = con.get("refs", []) or []
        if handles and all(_handle_base(h) in eid_map for h in handles):
            nc = copy.deepcopy(con)
            nc["id"] = idgen.next("c")
            nc["refs"] = [_remap_handle(h, eid_map) for h in handles]
            cons.append(nc)

    new_ids = [e["id"] for e in new_entities]
    id_map[f"${index}"] = new_ids[0] if new_ids else ""
    for k, neid in enumerate(new_ids[1:], start=1):
        id_map[f"${index}#{k}"] = neid


def _shift_entity_any(src: dict, dx: float, dy: float) -> dict:
    """Deep-copy a sketch entity of ANY type shifted by (dx, dy) (id/construction stripped — the
    caller restamps them). Translates every point-shaped field the entity vocabulary uses."""
    out = {k: copy.deepcopy(v) for k, v in src.items() if k not in ("id", "construction")}

    def shift_pt(p):
        if isinstance(p, list) and len(p) == 2 and all(isinstance(c, (int, float)) for c in p):
            return [float(p[0]) + dx, float(p[1]) + dy]
        return p

    for key in ("p", "p1", "p2", "center", "vertex", "focus", "apex", "p0"):
        if key in out:
            out[key] = shift_pt(out[key])
    if isinstance(out.get("points"), list):
        out["points"] = [shift_pt(p) for p in out["points"]]
    return out


_OP_HANDLERS = {
    "add_feature": _op_add_feature,
    "edit_feature": _op_edit_feature,
    "delete_feature": _op_delete_feature,
    "reorder_feature": _op_reorder_feature,
    "set_suppressed": _op_set_suppressed,
    "rename": _op_rename,
    "set_folder": _op_set_folder,
    "set_material": _op_set_material,
    "set_appearance": _op_set_appearance,
    "set_units": _op_set_units,
    "set_rollback": _op_set_rollback,
    "eq_set": _op_eq_set,
    "eq_delete": _op_eq_delete,
    "sketch_add_entity": _op_sketch_add_entity,
    "sketch_edit_entity": _op_sketch_edit_entity,
    "sketch_delete_entity": _op_sketch_delete_entity,
    "sketch_add_constraint": _op_sketch_add_constraint,
    "sketch_delete_constraint": _op_sketch_delete_constraint,
    "sketch_set_dim": _op_sketch_set_dim,
    "asm_insert_component": _op_asm_insert_component,
    "asm_edit_component": _op_asm_edit_component,
    "asm_delete_component": _op_asm_delete_component,
    "asm_add_mate": _op_asm_add_mate,
    "asm_edit_mate": _op_asm_edit_mate,
    "asm_delete_mate": _op_asm_delete_mate,
    "asm_set_explode": _op_asm_set_explode,
    "sketch_trim": _op_sketch_trim,
    "sketch_extend": _op_sketch_extend,
    "sketch_offset": _op_sketch_offset,
    "sketch_fillet": _op_sketch_fillet,
    "sketch_chamfer": _op_sketch_chamfer,
    "sketch_convert": _op_sketch_convert,                  # C53
    "sketch_mirror_entities": _op_sketch_mirror_entities,  # C53
    "sketch_pattern_entities": _op_sketch_pattern_entities,  # C53
    "sketch_move": _op_sketch_move,                        # C54 (Move/Copy)
    "sketch_toggle_construction": _op_sketch_toggle_construction,  # C42
    "copy_feature": _op_copy_feature,                      # Wave 4A (read-only bundle)
    "paste_feature": _op_paste_feature,                    # Wave 4A
    "paste_feature_as_derived": _op_paste_feature_as_derived,  # Wave 4A
    "sketch_paste_entities": _op_sketch_paste_entities,    # Wave 4B
}


# Ops that NEVER mutate the document — they execute against a working copy and surface their result
# only through the id_map side-channel (e.g. copy_feature's ``$bundle``). A batch made up entirely
# of these is a pure read: the session layer can skip the undo snapshot, the revision bump, and the
# dirty flag (see engine.doc.session.apply_ops). A MIXED batch (any non-read-only op) is treated as
# mutating. Membership here is the single source of truth for "this op cannot change the doc".
READONLY_OPS: frozenset[str] = frozenset({"copy_feature"})


def is_readonly_batch(ops: list[dict]) -> bool:
    """True iff *ops* is non-empty and EVERY op is a known read-only op (``READONLY_OPS``).

    An empty batch returns False (there is nothing to read; the session treats it as an ordinary —
    mutating — apply, preserving prior behavior). An op with an unknown/missing name is NOT
    read-only, so a batch containing it is mutating (it will raise during application anyway)."""
    if not isinstance(ops, list) or not ops:
        return False
    return all(isinstance(op, dict) and op.get("op") in READONLY_OPS for op in ops)


# --------------------------------------------------------------------------- public entry


def apply_ops(doc: dict, ops: list[dict]) -> tuple[dict, dict[str, str]]:
    """Validate-then-apply *ops* against *doc*. Returns ``(new_doc, id_map)``.

    Atomic: the whole batch is applied to a deep copy; if any op fails OR the resulting document
    does not validate, the original *doc* is left untouched and an :class:`OpError` is raised.
    """
    if not isinstance(ops, list):
        raise OpError("invalid_op", "ops must be a list", {"index": -1, "reason": "ops not a list"})

    working = copy.deepcopy(doc)
    idgen = _IdGen(working)
    id_map: dict[str, str] = {}

    for index, op in enumerate(ops):
        _apply_one(working, op, index, idgen, id_map)

    # Resulting-document validation (doc_ops.md §2.1): reject batches that produce an invalid doc.
    errs = validate_document(working)
    if errs:
        raise OpError("invalid_op",
                      "resulting document failed validation: " + "; ".join(errs[:5]),
                      {"index": -1, "reason": errs[0] if errs else "invalid document",
                       "all_errors": errs})

    return working, id_map
