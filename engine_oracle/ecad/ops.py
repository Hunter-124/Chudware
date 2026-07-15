"""ECAD document ops — validate-then-apply the closed mutation vocabulary (ecad_ops.md).

The GUI and the AI ECAD tools emit exactly these ops. :func:`apply_ops` is the single entry point:
it takes a document dict (schematic OR board) + an op batch and returns ``{"id_map": {...}}`` or
raises :class:`OpError` with a structured ``{code, message, detail}`` payload (ecad_ops.md §7).

Transactionality (ecad_ops.md §2 — identical discipline to engine.doc.ops):
  1. **Validation atomic** — the WHOLE batch is applied to a deep copy; if any op raises OR the
     resulting document fails ``schema.validate`` the original *doc* is left untouched and an
     :class:`OpError` is raised. No partial mutation ever escapes.
  2. **Application atomic** — all ops apply to the working copy; the caller (session) swaps it in and
     bumps the revision by exactly 1.
  3. Compile (netlist/ERC/DRC) failures are NOT op errors — they surface in the session's
     ``compile`` field, never here.

Id placeholders (ecad_ops.md §3): an op that creates an object may be referenced by a LATER op in
the same batch as ``"$<op-index>"`` (e.g. add a symbol in op 0, wire to it with
``{"sym":"$0","pin":"1"}`` in op 1). Forward references (``$N`` for the current/a later op) are an
error. Same semantics as engine.doc.ops.

Ids are allocated via :func:`schema.next_id` (``<prefix><n>``, never reused) seeded from the
working document so multiple creates in one batch get distinct ids.
"""

from __future__ import annotations

import copy
from typing import Any

from engine.ecad import schema


# --------------------------------------------------------------------------- error envelope


class OpError(Exception):
    """A structured op error (ecad_ops.md §7). Carries ``code`` + ``detail`` for the RPC envelope.

    Mirrors :class:`engine.doc.ops.OpError` exactly so the worker/MCP bridge can treat both modules'
    errors identically.
    """

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_envelope(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def _invalid(index: int, reason: str) -> OpError:
    return OpError("invalid_op", f"op[{index}]: {reason}", {"index": index, "reason": reason})


# --------------------------------------------------------------------------- id allocation


class _IdGen:
    """Monotonic id generator seeded from the working document.

    Wraps :func:`schema.next_id`, which scans the document's CURRENT ids. We track which ids we've
    already handed out this batch (the objects may not yet be appended when the next allocation
    happens, e.g. when an op stages a list it inserts at the end) so two creates in one batch never
    collide.
    """

    def __init__(self, doc: dict) -> None:
        self._doc = doc

    def next(self, prefix: str) -> str:
        return schema.next_id(self._doc, prefix)


# --------------------------------------------------------------------------- placeholders


def _resolve_placeholder(value: Any, id_map: dict[str, str], index: int) -> Any:
    """Resolve a ``"$N"`` placeholder string to its assigned id. Non-placeholders pass through.

    A ``"$N"`` referring to an op that produced no id (or to the current/a later op) is an error.
    """
    if isinstance(value, str) and value.startswith("$"):
        if value not in id_map:
            raise _invalid(index,
                           f"placeholder '{value}' does not refer to an earlier created object")
        return id_map[value]
    return value


def _resolve_deep(obj: Any, id_map: dict[str, str], index: int) -> Any:
    """Recursively resolve every ``$N`` placeholder string inside *obj* (dict/list/scalar)."""
    if isinstance(obj, dict):
        return {k: _resolve_deep(v, id_map, index) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_deep(v, id_map, index) for v in obj]
    return _resolve_placeholder(obj, id_map, index)


# --------------------------------------------------------------------------- generic helpers


def _patch(target: dict, patch: dict) -> None:
    """Patch semantics (ecad_ops.md §4/§5): present keys replace; a ``None`` value clears the key."""
    for k, v in patch.items():
        if v is None:
            target.pop(k, None)
        else:
            target[k] = v


def _find(items: list[dict], oid: str) -> dict | None:
    for it in items:
        if isinstance(it, dict) and it.get("id") == oid:
            return it
    return None


def _require_xy(op: dict, key: str, index: int) -> list[float]:
    v = op.get(key)
    if not (isinstance(v, (list, tuple)) and len(v) == 2
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v)):
        raise _invalid(index, f"'{key}' must be [x, y]")
    return [float(v[0]), float(v[1])]


def _require_sch(doc: dict, index: int) -> None:
    if not schema.is_schematic(doc):
        raise _invalid(index, "this op is only valid on a schematic document")


def _require_pcb(doc: dict, index: int) -> None:
    if not schema.is_board(doc):
        raise _invalid(index, "this op is only valid on a board document")


def _orthogonalize(pts: list[list[float]]) -> list[list[float]]:
    """Drop consecutive duplicate points and merge colinear runs (ecad_ops.md sch_add_wire note).

    Keeps endpoints; only removes interior vertices that lie on the straight segment between their
    neighbours (so a manhattan polyline stays manhattan but redundant vertices are dropped). Pure
    geometry — does not force segments onto axes (the host already submits orthogonal points; the
    schema is warn-only on diagonals).
    """
    clean: list[list[float]] = []
    for p in pts:
        q = [float(p[0]), float(p[1])]
        if not clean or clean[-1] != q:
            clean.append(q)
    if len(clean) <= 2:
        return clean
    out = [clean[0]]
    for i in range(1, len(clean) - 1):
        a, b, c = out[-1], clean[i], clean[i + 1]
        # colinear if cross product of (b-a) x (c-a) ~ 0
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(cross) < 1e-9:
            continue  # b is redundant
        out.append(b)
    out.append(clean[-1])
    return out


# --------------------------------------------------------------------------- library coupling


def _ensure_lib_def(doc: dict, libid: str, caller_def: Any,
                    warnings: list[str], index: int, kind: str) -> None:
    """Make sure ``doc['lib_cache'][libid]`` holds a usable def for *libid* (ecad_ops.md §4/§5).

    Resolution order:
      1. already in ``lib_cache`` -> nothing to do.
      2. a caller-provided def on the op (``def`` field) -> store it verbatim.
      3. ``engine.ecad.library.resolve(libid)`` (the library module is built in parallel; a missing
         module is NOT a hard error) -> store the resolved def.
      4. fall back to a minimal stub + record a warning.

    *kind* is "symbol" or "footprint" — only affects the stub's default shape and warning text.
    """
    cache = doc.setdefault("lib_cache", {})
    if libid in cache:
        return
    if isinstance(caller_def, dict):
        cache[libid] = copy.deepcopy(caller_def)
        return
    try:  # pragma: no cover - exercised only when the parallel library module exists
        from engine.ecad import library  # type: ignore
        resolved = library.resolve(libid)
        if isinstance(resolved, dict):
            cache[libid] = copy.deepcopy(resolved)
            return
        warnings.append(f"op[{index}]: library.resolve({libid!r}) returned no def; using a stub")
    except ImportError:
        warnings.append(f"op[{index}]: engine.ecad.library not available; "
                        f"using a stub for {libid!r}")
    except Exception as exc:  # noqa: BLE001 - library is best-effort, never hard-fail an op
        warnings.append(f"op[{index}]: library.resolve({libid!r}) failed ({exc}); using a stub")
    cache[libid] = _stub_def(libid, kind)


def _stub_def(libid: str, kind: str) -> dict:
    """A minimal, schema-valid def so a placed object referencing an unresolved lib still validates.

    Schematic symbols MUST resolve in ``lib_cache`` (validate_schematic), so the schematic stub is a
    SymbolDef with two passive pins. Footprints embed their pads on the placed instance, so the PCB
    footprint stub only needs a plausible reference_prefix/value pair.
    """
    name = libid.split(":", 1)[-1] or libid
    if kind == "footprint":
        return {"fpid": libid, "name": name, "pads": []}
    return {
        "libid": libid, "name": name, "description": f"stub for {libid}",
        "reference_prefix": "U", "value": name, "units_count": 1, "power": False,
        "pins": [
            {"number": "1", "name": "1", "pos": [-2.54, 0.0], "angle": 0.0,
             "length": 2.54, "type": "passive", "unit": 1},
            {"number": "2", "name": "2", "pos": [2.54, 0.0], "angle": 180.0,
             "length": 2.54, "type": "passive", "unit": 1},
        ],
    }


def _ref_prefix(doc: dict, libid: str) -> str:
    sdef = doc.get("lib_cache", {}).get(libid) or {}
    prefix = sdef.get("reference_prefix")
    if isinstance(prefix, str) and prefix:
        return prefix
    # heuristic fallback from the lib name (Device:R -> "R")
    name = libid.split(":", 1)[-1]
    return (name[:1].upper() if name else "U") or "U"


def _next_ref(doc: dict, prefix: str) -> str:
    """Next free ``<prefix><n>`` ref-des across all placed symbols (ecad_ops.md sch_add_symbol)."""
    import re
    pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    used = set()
    for s in doc.get("symbols", []):
        if not isinstance(s, dict):
            continue
        m = pat.match(s.get("ref", "") or "")
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"{prefix}{n}"


# --------------------------------------------------------------------------- schematic ops


def _op_sch_add_symbol(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    libid = op.get("libid")
    if not isinstance(libid, str) or not libid:
        raise _invalid(index, "sch_add_symbol requires a 'libid' string")
    _ensure_lib_def(doc, libid, op.get("def"), warnings, index, "symbol")
    sid = idgen.next("s")
    ref = op.get("ref")
    if not isinstance(ref, str) or not ref:
        ref = _next_ref(doc, _ref_prefix(doc, libid))
    sym = {
        "id": sid,
        "libid": libid,
        "sheet": op.get("sheet", "sh1"),
        "ref": ref,
        "value": op.get("value", (doc["lib_cache"].get(libid, {}) or {}).get("value", "")),
        "pos": _require_xy(op, "pos", index),
        "angle": float(op.get("angle", 0.0)),
        "mirror": op.get("mirror", "none"),
        "unit": int(op.get("unit", 1)),
        "fields": copy.deepcopy(op.get("fields", {})),
        "in_bom": True, "on_board": True, "dnp": bool(op.get("dnp", False)),
    }
    if op.get("footprint") is not None:
        sym["footprint"] = op["footprint"]
    if op.get("sim_params") is not None:
        sym["sim_params"] = copy.deepcopy(op["sim_params"])
    if sym["mirror"] not in schema.SYM_MIRROR:
        raise _invalid(index, f"mirror must be one of {sorted(schema.SYM_MIRROR)}")
    doc.setdefault("symbols", []).append(sym)
    id_map[f"${index}"] = sid


def _op_sch_edit_symbol(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    sid = _resolve_placeholder(op.get("id"), id_map, index)
    sym = _find(doc.get("symbols", []), sid)
    if sym is None:
        raise OpError("unknown_object", f"op[{index}]: no symbol '{sid}'",
                      {"index": index, "reason": f"unknown symbol {sid}"})
    patch = {}
    for k in ("pos", "angle", "mirror", "ref", "value", "footprint", "fields", "dnp", "sim_params"):
        if k in op:
            patch[k] = op[k]
    if "mirror" in patch and patch["mirror"] is not None and patch["mirror"] not in schema.SYM_MIRROR:
        raise _invalid(index, f"mirror must be one of {sorted(schema.SYM_MIRROR)}")
    _patch(sym, patch)


def _op_sch_delete_symbol(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    sid = _resolve_placeholder(op.get("id"), id_map, index)
    syms = doc.get("symbols", [])
    if _find(syms, sid) is None:
        raise OpError("unknown_object", f"op[{index}]: no symbol '{sid}'",
                      {"index": index, "reason": f"unknown symbol {sid}"})
    # Cascades pins (they live in the SymbolDef, so removing the instance drops them). Wires are
    # left dangling and re-netlisted (ecad_ops.md sch_delete_symbol note) — no wire surgery here.
    doc["symbols"] = [s for s in syms if s.get("id") != sid]


def _op_sch_add_wire(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    pts = op.get("pts")
    if not (isinstance(pts, list) and len(pts) >= 2
            and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in pts)):
        raise _invalid(index, "sch_add_wire requires 'pts': >=2 [x,y] points")
    clean = _orthogonalize([[float(p[0]), float(p[1])] for p in pts])
    if len(clean) < 2:
        raise _invalid(index, "sch_add_wire: degenerate (all points coincident)")
    wid = idgen.next("w")
    wire = {"id": wid, "sheet": op.get("sheet", "sh1"), "pts": clean, "bus": False}
    doc.setdefault("wires", []).append(wire)
    id_map[f"${index}"] = wid


def _op_sch_edit_wire(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    wid = _resolve_placeholder(op.get("id"), id_map, index)
    wire = _find(doc.get("wires", []), wid)
    if wire is None:
        raise OpError("unknown_object", f"op[{index}]: no wire '{wid}'",
                      {"index": index, "reason": f"unknown wire {wid}"})
    pts = op.get("pts")
    if not (isinstance(pts, list) and len(pts) >= 2
            and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in pts)):
        raise _invalid(index, "sch_edit_wire requires 'pts': >=2 [x,y] points")
    clean = _orthogonalize([[float(p[0]), float(p[1])] for p in pts])
    if len(clean) < 2:
        raise _invalid(index, "sch_edit_wire: degenerate (all points coincident)")
    wire["pts"] = clean


def _op_sch_delete_wire(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    wid = _resolve_placeholder(op.get("id"), id_map, index)
    wires = doc.get("wires", [])
    if _find(wires, wid) is None:
        raise OpError("unknown_object", f"op[{index}]: no wire '{wid}'",
                      {"index": index, "reason": f"unknown wire {wid}"})
    doc["wires"] = [w for w in wires if w.get("id") != wid]


def _op_sch_add_junction(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    jid = idgen.next("j")
    j = {"id": jid, "sheet": op.get("sheet", "sh1"), "pos": _require_xy(op, "pos", index)}
    doc.setdefault("junctions", []).append(j)
    id_map[f"${index}"] = jid


def _op_sch_delete_junction(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    jid = _resolve_placeholder(op.get("id"), id_map, index)
    js = doc.get("junctions", [])
    if _find(js, jid) is None:
        raise OpError("unknown_object", f"op[{index}]: no junction '{jid}'",
                      {"index": index, "reason": f"unknown junction {jid}"})
    doc["junctions"] = [x for x in js if x.get("id") != jid]


def _op_sch_add_label(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    text = op.get("text")
    if not isinstance(text, str) or not text:
        raise _invalid(index, "sch_add_label requires a non-empty 'text'")
    scope = op.get("scope", "local")
    if scope not in schema.LABEL_SCOPES:
        raise _invalid(index, f"scope must be one of {sorted(schema.LABEL_SCOPES)}")
    lid = idgen.next("l")
    lab = {"id": lid, "sheet": op.get("sheet", "sh1"), "pos": _require_xy(op, "pos", index),
           "text": text, "scope": scope, "angle": float(op.get("angle", 0.0))}
    doc.setdefault("labels", []).append(lab)
    id_map[f"${index}"] = lid


def _op_sch_edit_label(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    lid = _resolve_placeholder(op.get("id"), id_map, index)
    lab = _find(doc.get("labels", []), lid)
    if lab is None:
        raise OpError("unknown_object", f"op[{index}]: no label '{lid}'",
                      {"index": index, "reason": f"unknown label {lid}"})
    if "scope" in op and op["scope"] is not None and op["scope"] not in schema.LABEL_SCOPES:
        raise _invalid(index, f"scope must be one of {sorted(schema.LABEL_SCOPES)}")
    if "text" in op and (not isinstance(op["text"], str) or not op["text"]):
        raise _invalid(index, "sch_edit_label.text must be a non-empty string")
    _patch(lab, {k: op[k] for k in ("pos", "text", "scope", "angle") if k in op})


def _op_sch_delete_label(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    lid = _resolve_placeholder(op.get("id"), id_map, index)
    labs = doc.get("labels", [])
    if _find(labs, lid) is None:
        raise OpError("unknown_object", f"op[{index}]: no label '{lid}'",
                      {"index": index, "reason": f"unknown label {lid}"})
    doc["labels"] = [x for x in labs if x.get("id") != lid]


def _op_sch_add_power(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    net = op.get("net")
    if not isinstance(net, str) or not net:
        raise _invalid(index, "sch_add_power requires a non-empty 'net'")
    pid = idgen.next("pw")
    pw = {"id": pid, "sheet": op.get("sheet", "sh1"), "pos": _require_xy(op, "pos", index),
          "net": net, "symbol": op.get("symbol", net)}
    doc.setdefault("power", []).append(pw)
    id_map[f"${index}"] = pid


def _op_sch_add_no_connect(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    nid = idgen.next("nc")
    nc = {"id": nid, "sheet": op.get("sheet", "sh1"), "pos": _require_xy(op, "pos", index)}
    doc.setdefault("no_connect", []).append(nc)
    id_map[f"${index}"] = nid


def _pin_world_for(doc: dict, sym: dict, pin_no: str, index: int) -> tuple[float, float]:
    """World endpoint of pin *pin_no* on placed symbol *sym* (uses schema.pin_world)."""
    sdef = doc.get("lib_cache", {}).get(sym.get("libid")) or {}
    unit = sym.get("unit", 1)
    for pin in sdef.get("pins", []):
        if pin.get("unit", 1) not in (unit, 0):
            continue
        if str(pin.get("number")) == str(pin_no) or str(pin.get("name")) == str(pin_no):
            return schema.pin_world(sym, pin)
    raise OpError("bad_ref", f"op[{index}]: symbol '{sym.get('id')}' has no pin '{pin_no}'",
                  {"index": index, "reason": f"no pin {pin_no} on {sym.get('id')}"})


def _op_sch_connect(doc, op, index, idgen, id_map, warnings):
    """Convenience: auto-route an orthogonal wire pin->pin (ecad_ops.md §4 sch_connect).

    Endpoints are the two pins' world coordinates (via :func:`schema.pin_world`). The route is a
    manhattan L (or straight line when already axis-aligned): pin_a -> elbow -> pin_b. Refs are
    placeholder-eligible (``{"sym":"$0","pin":"1"}``).
    """
    _require_sch(doc, index)
    frm, to = op.get("from"), op.get("to")
    if not (isinstance(frm, dict) and isinstance(to, dict)):
        raise _invalid(index, "sch_connect requires 'from' and 'to' = {sym, pin}")

    def _endpoint(ref: dict) -> tuple[float, float]:
        sid = _resolve_placeholder(ref.get("sym"), id_map, index)
        sym = _find(doc.get("symbols", []), sid)
        if sym is None:
            raise OpError("bad_ref", f"op[{index}]: sch_connect references unknown symbol '{sid}'",
                          {"index": index, "reason": f"unknown symbol {sid}"})
        return _pin_world_for(doc, sym, str(ref.get("pin")), index)

    ax, ay = _endpoint(frm)
    bx, by = _endpoint(to)

    # Other pins to route AROUND: the auto-route must not lay a wire vertex (or segment) onto a
    # THIRD pin, or the netlist union-find silently shorts that pin into the net. A bare manhattan
    # L elbow at (bx, ay) lands exactly on a stacked-aligned neighbour pin (e.g. R1.1 above R2.1 in
    # a divider), so wiring R1.2->R2.1 would also short R1.1. Collect every other placed pin and
    # pick the first candidate route whose segments touch none of them.
    TOL = 1e-6
    others: list[tuple[float, float]] = []
    for _sym, _pin, (wx, wy) in schema.iter_placed_pins(doc):
        if (abs(wx - ax) < TOL and abs(wy - ay) < TOL) or \
           (abs(wx - bx) < TOL and abs(wy - by) < TOL):
            continue  # the two endpoints themselves
        others.append((wx, wy))

    def _on_seg(p, s, e) -> bool:
        px, py = p
        sx, sy = s
        ex, ey = e
        if abs(sx - ex) < TOL:          # vertical segment
            return abs(px - sx) < TOL and (min(sy, ey) - TOL) <= py <= (max(sy, ey) + TOL)
        if abs(sy - ey) < TOL:          # horizontal segment
            return abs(py - sy) < TOL and (min(sx, ex) - TOL) <= px <= (max(sx, ex) + TOL)
        return False

    def _clear(route) -> bool:
        for s, e in zip(route, route[1:]):
            for q in others:
                if _on_seg(q, s, e):
                    return False
        return True

    mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
    if abs(ax - bx) < TOL or abs(ay - by) < TOL:
        # Already axis-aligned: a straight segment, unless a pin sits ON it -> jog around it.
        straight = [[ax, ay], [bx, by]]
        if abs(ax - bx) < TOL:  # vertical line -> jog in x
            d = 2.54
            detour = [[ax, ay], [ax + d, ay], [ax + d, by], [bx, by]]
        else:                   # horizontal line -> jog in y
            d = 2.54
            detour = [[ax, ay], [ax, ay + d], [bx, ay + d], [bx, by]]
        candidates = [straight, detour]
    else:
        candidates = [
            [[ax, ay], [bx, ay], [bx, by]],            # L: horizontal then vertical (legacy default)
            [[ax, ay], [ax, by], [bx, by]],            # L: vertical then horizontal
            [[ax, ay], [mx, ay], [mx, by], [bx, by]],  # Z through the x-midpoint (clears stacked pins)
            [[ax, ay], [ax, my], [bx, my], [bx, by]],  # Z through the y-midpoint
        ]
    pts = next((c for c in candidates if _clear(c)), candidates[0])
    clean = _orthogonalize(pts)
    wid = idgen.next("w")
    doc.setdefault("wires", []).append(
        {"id": wid, "sheet": op.get("sheet", frm.get("sheet", "sh1")), "pts": clean, "bus": False})
    id_map[f"${index}"] = wid


def _op_sch_annotate(doc, op, index, idgen, id_map, warnings):
    """Assign ref-des to all ``?`` symbols deterministically (ecad_ops.md §4 sch_annotate).

    ``mode``=="reset" first strips every non-``?`` ref to its prefix+``?`` so the whole sheet is
    renumbered; ``mode``=="keep" (default) only fills symbols whose ref is missing or ends in ``?``.
    Numbering is deterministic: symbols sorted by (x, y, id), assigned the next free index per prefix.
    """
    _require_sch(doc, index)
    scope = op.get("scope", "all")
    if scope not in ("all", "sheet"):
        raise _invalid(index, "sch_annotate.scope must be 'all' or 'sheet'")
    mode = op.get("mode", "keep")
    if mode not in ("keep", "reset"):
        raise _invalid(index, "sch_annotate.mode must be 'keep' or 'reset'")
    syms = doc.get("symbols", [])
    sheet = op.get("sheet", "sh1")

    def _prefix_of(sym: dict) -> str:
        ref = sym.get("ref", "") or ""
        base = ref.rstrip("?")
        import re
        m = re.match(r"^([A-Za-z_]+)", base)
        if m:
            return m.group(1)
        return _ref_prefix(doc, sym.get("libid", ""))

    in_scope = [s for s in syms if scope == "all" or s.get("sheet") == sheet]
    if mode == "reset":
        for s in in_scope:
            s["ref"] = _prefix_of(s) + "?"
    # used counters per prefix (from refs we keep fixed)
    used: dict[str, set[int]] = {}
    import re
    for s in syms:
        ref = s.get("ref", "") or ""
        m = re.match(r"^([A-Za-z_]+)(\d+)$", ref)
        if m:
            used.setdefault(m.group(1), set()).add(int(m.group(2)))
    needs = sorted((s for s in in_scope if (s.get("ref", "") or "").endswith("?")
                    or not s.get("ref")),
                   key=lambda s: (s.get("pos", [0, 0])[0], s.get("pos", [0, 0])[1], s.get("id", "")))
    for s in needs:
        prefix = _prefix_of(s)
        u = used.setdefault(prefix, set())
        n = 1
        while n in u:
            n += 1
        u.add(n)
        s["ref"] = f"{prefix}{n}"


def _op_sch_set_grid(doc, op, index, idgen, id_map, warnings):
    _require_sch(doc, index)
    grid = op.get("grid")
    if not isinstance(grid, (int, float)) or isinstance(grid, bool) or grid <= 0:
        raise _invalid(index, "sch_set_grid requires a positive numeric 'grid' (mm)")
    doc.setdefault("units", {})["grid"] = float(grid)


def _op_sch_add_sheet(doc, op, index, idgen, id_map, warnings):
    """Create a child sheet (hierarchy). ``parent`` defaults to the root sheet ``sh1``.

    Additive: a flat single-sheet doc is unaffected. The new sheet id is recorded under ``$<index>``
    so a later ``sch_add_sheet_symbol`` can reference it via ``"child":"$<index>"``.
    """
    _require_sch(doc, index)
    sheets = doc.setdefault("sheets", [])
    parent = op.get("parent", "sh1")
    if parent is not None and _find(sheets, parent) is None:
        raise OpError("unknown_object", f"op[{index}]: no parent sheet '{parent}'",
                      {"index": index, "reason": f"unknown sheet {parent}"})
    shid = idgen.next("sh")
    sheet = {
        "id": shid, "name": op.get("name", shid), "parent": parent,
        "size": list(op.get("size", [297, 210])), "ports": [],
    }
    sheets.append(sheet)
    id_map[f"${index}"] = shid


def _op_sch_add_sheet_symbol(doc, op, index, idgen, id_map, warnings):
    """Place a hierarchical sheet symbol that instantiates a child sheet, exposing sheet pins.

    ``sheet`` is the parent sheet the symbol is drawn on; ``child`` is the instantiated sheet id
    (placeholder-eligible). ``pins`` is an optional list of ``{name, pos, shape}`` connection points;
    each bridges to the equal-named ``hier`` label in the child sheet (netlist.py). The new sheet
    symbol id is recorded under ``$<index>``.
    """
    _require_sch(doc, index)
    child = _resolve_placeholder(op.get("child"), id_map, index)
    if not isinstance(child, str) or not child:
        raise _invalid(index, "sch_add_sheet_symbol requires a 'child' sheet id")
    if _find(doc.get("sheets", []), child) is None:
        raise OpError("unknown_object", f"op[{index}]: no child sheet '{child}'",
                      {"index": index, "reason": f"unknown sheet {child}"})
    hid = idgen.next("hs")
    pins_out: list[dict] = []
    seen: set[str] = set()
    for k, pn in enumerate(op.get("pins", []) or []):
        if not isinstance(pn, dict):
            raise _invalid(index, f"pins[{k}] must be an object")
        name = pn.get("name")
        if not isinstance(name, str) or not name:
            raise _invalid(index, f"pins[{k}].name must be a non-empty string")
        if name in seen:
            raise _invalid(index, f"pins[{k}].name duplicate {name!r}")
        seen.add(name)
        shape = pn.get("shape", "passive")
        if shape not in schema.SHEET_PIN_SHAPES:
            raise _invalid(index, f"pins[{k}].shape must be one of {sorted(schema.SHEET_PIN_SHAPES)}")
        pos = pn.get("pos")
        if not (isinstance(pos, (list, tuple)) and len(pos) == 2
                and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in pos)):
            raise _invalid(index, f"pins[{k}].pos must be [x, y]")
        pins_out.append({"name": name, "pos": [float(pos[0]), float(pos[1])], "shape": shape})
    hs = {
        "id": hid, "sheet": op.get("sheet", "sh1"), "child": child,
        "pos": _require_xy(op, "pos", index), "pins": pins_out,
    }
    doc.setdefault("sheet_symbols", []).append(hs)
    id_map[f"${index}"] = hid


def _op_sch_add_sheet_pin(doc, op, index, idgen, id_map, warnings):
    """Add a sheet pin to an existing sheet symbol (placeholder-eligible ``id``).

    The pin name should match a ``hier`` label in the instantiated child sheet for the net to bridge.
    """
    _require_sch(doc, index)
    hid = _resolve_placeholder(op.get("id"), id_map, index)
    hs = _find(doc.get("sheet_symbols", []), hid)
    if hs is None:
        raise OpError("unknown_object", f"op[{index}]: no sheet symbol '{hid}'",
                      {"index": index, "reason": f"unknown sheet symbol {hid}"})
    name = op.get("name")
    if not isinstance(name, str) or not name:
        raise _invalid(index, "sch_add_sheet_pin requires a non-empty 'name'")
    pins = hs.setdefault("pins", [])
    if any(isinstance(p, dict) and p.get("name") == name for p in pins):
        raise _invalid(index, f"sheet pin {name!r} already exists on {hid}")
    shape = op.get("shape", "passive")
    if shape not in schema.SHEET_PIN_SHAPES:
        raise _invalid(index, f"shape must be one of {sorted(schema.SHEET_PIN_SHAPES)}")
    pins.append({"name": name, "pos": _require_xy(op, "pos", index), "shape": shape})


def _op_sch_import_kicad(doc, op, index, idgen, id_map, warnings):
    """Best-effort .kicad_sch import (ecad_ops.md §4). The KiCad importer lives in the library
    module (built in parallel). Until it lands this is a documented best-effort stub: it never
    mutates the doc and records a warning. (The integrator wires the real importer here.)"""
    _require_sch(doc, index)
    warnings.append(f"op[{index}]: sch_import_kicad is a best-effort stub "
                    "(library/kicad_sch importer not wired yet); no objects imported")


# --------------------------------------------------------------------------- PCB ops


def _net_names(doc: dict) -> set[str]:
    return {n.get("name") for n in doc.get("nets", []) if isinstance(n, dict)}


def _require_net(doc: dict, net: Any, index: int) -> None:
    if net is not None and net not in _net_names(doc):
        raise OpError("net_not_found", f"op[{index}]: net {net!r} not in board nets",
                      {"index": index, "reason": f"net {net!r} not found"})


def _ensure_net(doc: dict, net: Any) -> None:
    """Register *net* in the board net table if missing (manual track/zone may introduce a net)."""
    if net is None:
        return
    if net not in _net_names(doc):
        doc.setdefault("nets", []).append({"name": net, "class": "default"})


def _op_pcb_set_stackup(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    stack = doc.setdefault("stackup", {})
    if "copper_layers" in op:
        n = op["copper_layers"]
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > schema.COPPER_LAYERS_MAX:
            raise _invalid(index, "copper_layers must be 1..32")
        if n != 1 and n % 2 == 1:
            raise _invalid(index, "copper_layers must be 1 or an even count")
        stack["copper_layers"] = n
    if "thickness" in op and op["thickness"] is not None:
        stack["thickness"] = float(op["thickness"])
    if "layers" in op and op["layers"] is not None:
        stack["layers"] = copy.deepcopy(op["layers"])


def _op_pcb_set_outline(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    shapes = op.get("shapes")
    if not isinstance(shapes, list):
        raise _invalid(index, "pcb_set_outline requires a 'shapes' list")
    closed = op.get("closed", True)
    if closed and not _outline_closes(shapes):
        raise OpError("outline_not_closed",
                      f"op[{index}]: outline shapes do not form a closed loop",
                      {"index": index, "reason": "outline not closed"})
    doc["outline"] = {"shapes": copy.deepcopy(shapes), "closed": bool(closed)}


def _outline_closes(shapes: list, tol: float = 1e-3) -> bool:
    """True if the line/arc/circle shapes chain end-to-end into at least one closed loop.

    A single ``circle`` is inherently closed. Otherwise we collect each shape's two endpoints and
    require every endpoint to be matched (each vertex used an even number of times within *tol*).
    """
    if not shapes:
        return False
    pts: list[tuple[float, float]] = []
    for s in shapes:
        t = s.get("type")
        if t == "circle":
            continue  # self-closing
        if t == "line":
            pts.append((float(s["p1"][0]), float(s["p1"][1])))
            pts.append((float(s["p2"][0]), float(s["p2"][1])))
        elif t == "arc":
            import math
            c = s["center"]; r = float(s["radius"])
            a0 = math.radians(float(s["a0"])); a1 = math.radians(float(s["a1"]))
            pts.append((c[0] + r * math.cos(a0), c[1] + r * math.sin(a0)))
            pts.append((c[0] + r * math.cos(a1), c[1] + r * math.sin(a1)))
        else:
            return False
    if not pts:
        return True  # only circles -> closed
    # every endpoint must pair with another endpoint within tol (degree even at every vertex)
    matched = [False] * len(pts)
    for i in range(len(pts)):
        if matched[i]:
            continue
        for j in range(len(pts)):
            if i == j or matched[j]:
                continue
            if abs(pts[i][0] - pts[j][0]) < tol and abs(pts[i][1] - pts[j][1]) < tol:
                matched[i] = matched[j] = True
                break
    return all(matched)


def _op_pcb_add_footprint(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    fpid = op.get("fpid")
    if not isinstance(fpid, str) or not fpid:
        raise _invalid(index, "pcb_add_footprint requires an 'fpid' string")
    side = op.get("side", "top")
    if side not in schema.FP_SIDES:
        raise _invalid(index, f"side must be one of {sorted(schema.FP_SIDES)}")
    # Resolve the footprint def for its pads (best-effort; library built in parallel).
    fpdef = _resolve_fp_def(doc, fpid, op.get("def"), warnings, index)
    fid = idgen.next("fp")
    pads = copy.deepcopy(op.get("pads") if op.get("pads") is not None else fpdef.get("pads", []))
    linked = op.get("linked_symbol")
    if linked is not None:
        linked = _resolve_placeholder(linked, id_map, index)
    fp = {
        "id": fid, "fpid": fpid,
        "ref": op.get("ref", ""), "value": op.get("value", fpdef.get("value", "")),
        "pos": _require_xy(op, "pos", index), "angle": float(op.get("angle", 0.0)), "side": side,
        "pads": pads, "graphics": copy.deepcopy(fpdef.get("graphics", [])),
        "attr": fpdef.get("attr", "smd"),
    }
    if linked is not None:
        fp["linked_symbol"] = linked
    doc.setdefault("footprints", []).append(fp)
    id_map[f"${index}"] = fid


def _resolve_fp_def(doc: dict, fpid: str, caller_def: Any,
                    warnings: list[str], index: int) -> dict:
    """Resolve a footprint def (pads/graphics). Footprints are NOT cached in lib_cache (they embed
    their pads on the instance), so this returns a def dict without storing it. Best-effort via the
    parallel library module; a caller-provided def wins; otherwise an empty stub."""
    if isinstance(caller_def, dict):
        return caller_def
    try:  # pragma: no cover - exercised only when the library module exists
        from engine.ecad import library  # type: ignore
        resolved = library.resolve_footprint(fpid) if hasattr(library, "resolve_footprint") \
            else library.resolve(fpid)
        if isinstance(resolved, dict):
            return resolved
    except ImportError:
        warnings.append(f"op[{index}]: engine.ecad.library not available; "
                        f"footprint {fpid!r} placed with caller-provided/empty pads")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"op[{index}]: footprint resolve {fpid!r} failed ({exc})")
    return {"fpid": fpid, "pads": []}


def _op_pcb_edit_footprint(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    fid = _resolve_placeholder(op.get("id"), id_map, index)
    fp = _find(doc.get("footprints", []), fid)
    if fp is None:
        raise OpError("unknown_object", f"op[{index}]: no footprint '{fid}'",
                      {"index": index, "reason": f"unknown footprint {fid}"})
    if "side" in op and op["side"] is not None and op["side"] not in schema.FP_SIDES:
        raise _invalid(index, f"side must be one of {sorted(schema.FP_SIDES)}")
    _patch(fp, {k: op[k] for k in ("pos", "angle", "side", "ref", "value") if k in op})


def _op_pcb_delete_footprint(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    fid = _resolve_placeholder(op.get("id"), id_map, index)
    fps = doc.get("footprints", [])
    if _find(fps, fid) is None:
        raise OpError("unknown_object", f"op[{index}]: no footprint '{fid}'",
                      {"index": index, "reason": f"unknown footprint {fid}"})
    doc["footprints"] = [f for f in fps if f.get("id") != fid]


def _op_pcb_add_track(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    layer = op.get("layer")
    if layer not in set(schema.copper_layer_ids(int(doc.get("stackup", {}).get("copper_layers", 2)))):
        raise OpError("layer_not_in_stackup",
                      f"op[{index}]: track layer {layer!r} is not a copper layer in the stackup",
                      {"index": index, "reason": f"layer {layer!r} not a copper layer"})
    net = op.get("net")
    _require_net(doc, net, index)
    kind = op.get("kind", "segment")
    if kind not in schema.TRACK_KINDS:
        raise _invalid(index, f"kind must be one of {sorted(schema.TRACK_KINDS)}")
    width = op.get("width")
    if width is None:
        width = _default_track_width(doc, net)
    tid = idgen.next("t")
    track = {"id": tid, "net": net, "layer": layer, "width": float(width), "kind": kind,
             "p1": _require_xy(op, "p1", index), "p2": _require_xy(op, "p2", index)}
    if kind == "arc":
        track["center"] = _require_xy(op, "center", index)
        track["a0"] = float(op.get("a0", 0.0))
        track["a1"] = float(op.get("a1", 0.0))
    doc.setdefault("tracks", []).append(track)
    id_map[f"${index}"] = tid


def _default_track_width(doc: dict, net: Any) -> float:
    classes = doc.get("net_classes", {}) or {}
    cls_name = "default"
    for n in doc.get("nets", []):
        if isinstance(n, dict) and n.get("name") == net:
            cls_name = n.get("class", "default")
            break
    cls = classes.get(cls_name) or classes.get("default") or {}
    return float(cls.get("track_width", 0.25))


def _op_pcb_add_tracks(doc, op, index, idgen, id_map, warnings):
    """Polyline -> consecutive segment tracks (route convenience, ecad_ops.md §5 pcb_add_tracks).

    Records the FIRST track id under ``$<index>`` and any further ids under ``$<index>#k``
    (the flat string->string id_map convention engine.doc.ops uses for multi-object ops)."""
    _require_pcb(doc, index)
    layer = op.get("layer")
    if layer not in set(schema.copper_layer_ids(int(doc.get("stackup", {}).get("copper_layers", 2)))):
        raise OpError("layer_not_in_stackup",
                      f"op[{index}]: track layer {layer!r} is not a copper layer in the stackup",
                      {"index": index, "reason": f"layer {layer!r} not a copper layer"})
    net = op.get("net")
    _require_net(doc, net, index)
    pts = op.get("pts")
    if not (isinstance(pts, list) and len(pts) >= 2
            and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in pts)):
        raise _invalid(index, "pcb_add_tracks requires 'pts': >=2 [x,y] points")
    width = op.get("width")
    if width is None:
        width = _default_track_width(doc, net)
    clean = _orthogonalize([[float(p[0]), float(p[1])] for p in pts])
    if len(clean) < 2:
        raise _invalid(index, "pcb_add_tracks: degenerate (all points coincident)")
    tracks = doc.setdefault("tracks", [])
    new_ids: list[str] = []
    for a, b in zip(clean, clean[1:]):
        tid = idgen.next("t")
        new_ids.append(tid)
        tracks.append({"id": tid, "net": net, "layer": layer, "width": float(width),
                       "kind": "segment", "p1": a, "p2": b})
    if new_ids:
        id_map[f"${index}"] = new_ids[0]
        for k, tid in enumerate(new_ids[1:], start=1):
            id_map[f"${index}#{k}"] = tid


def _op_pcb_delete_track(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    tid = _resolve_placeholder(op.get("id"), id_map, index)
    tracks = doc.get("tracks", [])
    if _find(tracks, tid) is None:
        raise OpError("unknown_object", f"op[{index}]: no track '{tid}'",
                      {"index": index, "reason": f"unknown track {tid}"})
    doc["tracks"] = [t for t in tracks if t.get("id") != tid]


def _op_pcb_add_via(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    net = op.get("net")
    _require_net(doc, net, index)
    vtype = op.get("type", "through")
    if vtype not in schema.VIA_TYPES:
        raise _invalid(index, f"via type must be one of {sorted(schema.VIA_TYPES)}")
    cu = schema.copper_layer_ids(int(doc.get("stackup", {}).get("copper_layers", 2)))
    layers = op.get("layers") or [cu[0], cu[-1]]
    for lyr in layers:
        if lyr not in cu:
            raise OpError("layer_not_in_stackup",
                          f"op[{index}]: via layer {lyr!r} is not a copper layer",
                          {"index": index, "reason": f"layer {lyr!r} not a copper layer"})
    vid = idgen.next("v")
    cls = (doc.get("net_classes", {}) or {}).get("default", {})
    via = {"id": vid, "net": net, "at": _require_xy(op, "at", index),
           "drill": float(op.get("drill", cls.get("via_drill", 0.4))),
           "diameter": float(op.get("diameter", cls.get("via_diameter", 0.8))),
           "layers": list(layers), "type": vtype}
    doc.setdefault("vias", []).append(via)
    id_map[f"${index}"] = vid


def _op_pcb_delete_via(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    vid = _resolve_placeholder(op.get("id"), id_map, index)
    vias = doc.get("vias", [])
    if _find(vias, vid) is None:
        raise OpError("unknown_object", f"op[{index}]: no via '{vid}'",
                      {"index": index, "reason": f"unknown via {vid}"})
    doc["vias"] = [v for v in vias if v.get("id") != vid]


def _op_pcb_add_zone(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    net = op.get("net")
    if not isinstance(net, str) or not net:
        raise _invalid(index, "pcb_add_zone requires a 'net' name")
    _require_net(doc, net, index)
    layer = op.get("layer")
    if layer not in set(schema.copper_layer_ids(int(doc.get("stackup", {}).get("copper_layers", 2)))):
        raise OpError("layer_not_in_stackup",
                      f"op[{index}]: zone layer {layer!r} is not a copper layer",
                      {"index": index, "reason": f"layer {layer!r} not a copper layer"})
    poly = op.get("polygon")
    if not (isinstance(poly, list) and len(poly) >= 3
            and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in poly)):
        raise _invalid(index, "pcb_add_zone requires 'polygon': >=3 [x,y] points")
    zid = idgen.next("z")
    zone = {"id": zid, "net": net, "layer": layer,
            "polygon": [[float(p[0]), float(p[1])] for p in poly],
            "clearance": float(op.get("clearance", 0.3)),
            "fill": op.get("fill", "solid")}
    if zone["fill"] not in schema.ZONE_FILLS:
        raise _invalid(index, f"zone fill must be one of {sorted(schema.ZONE_FILLS)}")
    if op.get("thermal") is not None:
        zone["thermal"] = copy.deepcopy(op["thermal"])
    doc.setdefault("zones", []).append(zone)
    id_map[f"${index}"] = zid


def _op_pcb_edit_zone(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    zid = _resolve_placeholder(op.get("id"), id_map, index)
    zone = _find(doc.get("zones", []), zid)
    if zone is None:
        raise OpError("unknown_object", f"op[{index}]: no zone '{zid}'",
                      {"index": index, "reason": f"unknown zone {zid}"})
    patch = op.get("patch") if isinstance(op.get("patch"), dict) else \
        {k: op[k] for k in ("net", "layer", "polygon", "clearance", "fill", "thermal") if k in op}
    if "net" in patch and patch["net"] is not None:
        _require_net(doc, patch["net"], index)
    if "fill" in patch and patch["fill"] is not None and patch["fill"] not in schema.ZONE_FILLS:
        raise _invalid(index, f"zone fill must be one of {sorted(schema.ZONE_FILLS)}")
    zone.pop("filled_polys", None)  # invalidate the cached fill (compile re-fills)
    _patch(zone, patch)


def _op_pcb_delete_zone(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    zid = _resolve_placeholder(op.get("id"), id_map, index)
    zones = doc.get("zones", [])
    if _find(zones, zid) is None:
        raise OpError("unknown_object", f"op[{index}]: no zone '{zid}'",
                      {"index": index, "reason": f"unknown zone {zid}"})
    doc["zones"] = [z for z in zones if z.get("id") != zid]


def _op_pcb_set_rules(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    if "net_classes" in op and isinstance(op["net_classes"], dict):
        nc = doc.setdefault("net_classes", {})
        for name, cls in op["net_classes"].items():
            if cls is None:
                nc.pop(name, None)
            else:
                nc[name] = copy.deepcopy(cls)
    patch = op.get("patch") if isinstance(op.get("patch"), dict) else \
        {k: v for k, v in op.items() if k not in ("op", "net_classes", "patch")}
    if patch:
        _patch(doc.setdefault("rules", {}), patch)


def _op_pcb_assign_net(doc, op, index, idgen, id_map, warnings):
    _require_pcb(doc, index)
    pad_ref = op.get("pad")
    if not (isinstance(pad_ref, dict) and "fp" in pad_ref and "pad" in pad_ref):
        raise _invalid(index, "pcb_assign_net requires 'pad': {fp, pad}")
    net = op.get("net")
    _require_net(doc, net, index)
    fid = _resolve_placeholder(pad_ref.get("fp"), id_map, index)
    fp = _find(doc.get("footprints", []), fid)
    if fp is None:
        raise OpError("unknown_object", f"op[{index}]: no footprint '{fid}'",
                      {"index": index, "reason": f"unknown footprint {fid}"})
    pad_no = str(pad_ref.get("pad"))
    for pad in fp.get("pads", []):
        if str(pad.get("number")) == pad_no or str(pad.get("id")) == pad_no:
            pad["net"] = net
            return
    raise OpError("bad_ref", f"op[{index}]: footprint '{fid}' has no pad '{pad_no}'",
                  {"index": index, "reason": f"no pad {pad_no} on {fid}"})


def _op_pcb_update_from_sch(doc, op, index, idgen, id_map, warnings):
    """Back-annotation from a linked schematic's netlist (ecad_ops.md §5 pcb_update_from_sch).

    DOCUMENTED BEST-EFFORT STUB. The real op pulls components/nets/footprints from the linked
    schematic's compiled netlist (engine.ecad.netlist) and the open schematic session — neither is
    available to a pure ops-on-one-doc function. So here we only (a) record the link
    (``doc['linked_sch'] = sch_doc_id``) and (b) emit a warning telling the integrator to run the
    real reconciliation at the session/worker layer (where both docs + the netlist live). It is
    idempotent and never hard-fails. See the report's "library/integrator coupling" note."""
    _require_pcb(doc, index)
    sch_id = op.get("sch_doc_id")
    if not isinstance(sch_id, str) or not sch_id:
        raise _invalid(index, "pcb_update_from_sch requires a 'sch_doc_id'")
    doc["linked_sch"] = sch_id
    warnings.append(f"op[{index}]: pcb_update_from_sch recorded linked_sch={sch_id!r} but is a "
                    "best-effort stub — the netlist-driven reconciliation runs at the integrator "
                    "(session/worker) layer where the schematic doc + netlist are available")


def _op_pcb_autoplace(doc, op, index, idgen, id_map, warnings):
    """Best-effort initial placement of unplaced footprints (ecad_ops.md §5 pcb_autoplace).

    A footprint is "unplaced" iff its pos is the origin [0,0] (the spawn default). We lay those out
    on a simple grid inside ``area`` (default a 100x100 region from the board origin), stepping by a
    coarse pitch. Deterministic (sorted by id) and idempotent-ish (already-placed parts untouched)."""
    _require_pcb(doc, index)
    area = op.get("area") or [doc.get("origin", [0.0, 0.0])[0],
                              doc.get("origin", [0.0, 0.0])[1], 100.0, 100.0]
    x0, y0, w, _h = (float(area[0]), float(area[1]), float(area[2]), float(area[3]))
    pitch = 12.7
    cols = max(1, int(w // pitch))
    unplaced = sorted((f for f in doc.get("footprints", [])
                       if f.get("pos", [0, 0]) == [0.0, 0.0] or f.get("pos") == [0, 0]),
                      key=lambda f: f.get("id", ""))
    for i, fp in enumerate(unplaced):
        fp["pos"] = [x0 + (i % cols) * pitch, y0 + (i // cols) * pitch]
    if unplaced:
        warnings.append(f"op[{index}]: pcb_autoplace placed {len(unplaced)} footprint(s) on a grid "
                        "(heuristic; connectivity-aware placement is a follow-up)")


# --------------------------------------------------------------------------- shared ops


def _op_rename_doc(doc, op, index, idgen, id_map, warnings):
    name = op.get("name")
    if not isinstance(name, str) or not name:
        raise _invalid(index, "rename_doc requires a non-empty 'name'")
    doc["name"] = name


def _op_set_field(doc, op, index, idgen, id_map, warnings):
    """Set an arbitrary user field on a target object (ecad_ops.md §6). ``target`` selects the object
    by ``{kind, id}`` (kind = symbols|footprints|... matching the doc list); the field is written
    into that object's ``fields`` map. A ``null`` value clears the key."""
    target = op.get("target")
    key = op.get("key")
    if not (isinstance(target, dict) and isinstance(key, str) and key):
        raise _invalid(index, "set_field requires 'target':{kind,id} and a string 'key'")
    list_key = target.get("kind")
    oid = _resolve_placeholder(target.get("id"), id_map, index)
    obj = _find(doc.get(list_key, []), oid) if isinstance(list_key, str) else None
    if obj is None:
        raise OpError("unknown_object",
                      f"op[{index}]: target {list_key!r}/{oid!r} not found",
                      {"index": index, "reason": f"unknown target {list_key}/{oid}"})
    fields = obj.setdefault("fields", {})
    if op.get("value") is None:
        fields.pop(key, None)
    else:
        fields[key] = op["value"]


def _op_nop(doc, op, index, idgen, id_map, warnings):
    """Transactional grouping no-op (ecad_ops.md §6)."""
    return


# --------------------------------------------------------------------------- dispatch


_OP_HANDLERS = {
    # schematic
    "sch_add_symbol": _op_sch_add_symbol,
    "sch_edit_symbol": _op_sch_edit_symbol,
    "sch_delete_symbol": _op_sch_delete_symbol,
    "sch_add_wire": _op_sch_add_wire,
    "sch_edit_wire": _op_sch_edit_wire,
    "sch_delete_wire": _op_sch_delete_wire,
    "sch_add_junction": _op_sch_add_junction,
    "sch_delete_junction": _op_sch_delete_junction,
    "sch_add_label": _op_sch_add_label,
    "sch_edit_label": _op_sch_edit_label,
    "sch_delete_label": _op_sch_delete_label,
    "sch_add_power": _op_sch_add_power,
    "sch_add_no_connect": _op_sch_add_no_connect,
    "sch_connect": _op_sch_connect,
    "sch_annotate": _op_sch_annotate,
    "sch_set_grid": _op_sch_set_grid,
    "sch_add_sheet": _op_sch_add_sheet,
    "sch_add_sheet_symbol": _op_sch_add_sheet_symbol,
    "sch_add_sheet_pin": _op_sch_add_sheet_pin,
    "sch_import_kicad": _op_sch_import_kicad,
    # pcb
    "pcb_set_stackup": _op_pcb_set_stackup,
    "pcb_set_outline": _op_pcb_set_outline,
    "pcb_add_footprint": _op_pcb_add_footprint,
    "pcb_edit_footprint": _op_pcb_edit_footprint,
    "pcb_delete_footprint": _op_pcb_delete_footprint,
    "pcb_add_track": _op_pcb_add_track,
    "pcb_add_tracks": _op_pcb_add_tracks,
    "pcb_delete_track": _op_pcb_delete_track,
    "pcb_add_via": _op_pcb_add_via,
    "pcb_delete_via": _op_pcb_delete_via,
    "pcb_add_zone": _op_pcb_add_zone,
    "pcb_edit_zone": _op_pcb_edit_zone,
    "pcb_delete_zone": _op_pcb_delete_zone,
    "pcb_set_rules": _op_pcb_set_rules,
    "pcb_assign_net": _op_pcb_assign_net,
    "pcb_update_from_sch": _op_pcb_update_from_sch,
    "pcb_autoplace": _op_pcb_autoplace,
    # shared
    "rename_doc": _op_rename_doc,
    "set_field": _op_set_field,
    "nop": _op_nop,
}

def _family(name: str) -> str:
    i = name.find("_")
    return name[:i] if i > 0 else name


def _levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _score_match(q: str, cand: str) -> int:
    q = q.lower()
    cand_l = cand.lower()
    if not q:
        return 0
    if cand_l == q:
        return 100000
    if cand_l.startswith(q):
        return 50000 + len(q) * 10
    if q.startswith(cand_l):
        return 45000 + len(cand_l) * 10
    qf, cf = _family(q), _family(cand_l)
    if len(qf) >= 2 and qf == cf:
        qrest = q[len(qf) + 1:] if len(q) > len(qf) + 1 else ""
        crest = cand_l[len(cf) + 1:] if len(cand_l) > len(cf) + 1 else ""
        bonus = 0
        if qrest and crest.startswith(qrest):
            bonus = 2000 + len(qrest) * 10
        elif qrest:
            bonus = 1000 - _levenshtein(qrest, crest) * 20
        return 30000 + bonus
    if q in cand_l:
        return 20000 + len(q)
    if cand_l in q:
        return 15000 + len(cand_l)
    return 10000 - _levenshtein(q, cand_l) * 100


def list_ecad_ops() -> list[str]:
    """Closed ECAD op vocabulary (single source: ``_OP_HANDLERS``)."""
    return sorted(_OP_HANDLERS.keys())


def suggest_ecad_ops(unknown: str, n: int = 5) -> list[str]:
    """Closest N op kinds — prefix/token-family preferred over pure Levenshtein."""
    scored = [(_score_match(unknown or "", k), k) for k in _OP_HANDLERS]
    scored.sort(key=lambda p: (-p[0], p[1]))
    return [k for _, k in scored[: max(0, n)]]


def unknown_ecad_op_message(index: int, kind: str) -> str:
    closest = suggest_ecad_ops(kind, 5)
    msg = f"op[{index}]: unknown op '{kind}'"
    if closest:
        msg += ". closest: " + ", ".join(closest)
    msg += "; see cad_guide()"
    return msg



def _apply_one(doc, op, index, idgen, id_map, warnings) -> None:
    if not isinstance(op, dict):
        raise _invalid(index, "op must be an object")
    kind = op.get("op")
    if not isinstance(kind, str):
        raise _invalid(index, "op.op (the op name) is required")
    handler = _OP_HANDLERS.get(kind)
    if handler is None:
        raise OpError("invalid_op", unknown_ecad_op_message(index, kind),
                      {"index": index, "reason": f"unsupported op '{kind}'",
                       "suggestions": suggest_ecad_ops(kind, 5)})
    handler(doc, op, index, idgen, id_map, warnings)


# --------------------------------------------------------------------------- public entry


def apply_ops(doc: dict, ops: list[dict]) -> dict:
    """Validate-then-apply *ops* against *doc*. Returns the ops-response payload.

    Returns ``{"id_map": {"$0": "s1", ...}, "warnings": [...]}``. The session layer merges this into
    the full ops-response (revision/kind/tree/compile). Atomic: the whole batch applies to a deep
    copy and the RESULTING document must pass ``schema.validate`` (empty error list); any op failure
    OR a validation failure leaves *doc* untouched and raises :class:`OpError` (ecad_ops.md §2).

    The mutated working document is returned under ``"doc"`` so the session can swap it in without a
    second apply pass.
    """
    if not isinstance(ops, list):
        raise OpError("invalid_op", "ops must be a list", {"index": -1, "reason": "ops not a list"})
    if not (schema.is_schematic(doc) or schema.is_board(doc)):
        raise OpError("invalid_op", f"document is neither sch nor pcb ({doc.get('schema')!r})",
                      {"index": -1, "reason": "unknown doc kind"})

    working = copy.deepcopy(doc)
    idgen = _IdGen(working)
    id_map: dict[str, str] = {}
    warnings: list[str] = []

    for index, op in enumerate(ops):
        _apply_one(working, op, index, idgen, id_map, warnings)

    errs = schema.validate(working)
    if errs:
        raise OpError("invalid_op",
                      "resulting document failed validation: " + "; ".join(errs[:5]),
                      {"index": -1, "reason": errs[0] if errs else "invalid document",
                       "all_errors": errs})

    return {"id_map": id_map, "warnings": warnings, "doc": working}
