"""ECAD schematic MCP tools (T-RF8 split of ecad_tools.py): ecad_new_schematic, find_part, place_symbol, wire, add_power, label, status, run_erc, update_from_sch."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature

from .ecad_common import (
    OpError,
    _ECAD,
    _apply,
    _attach_board_ascii_readback,
    _attach_schematic_ascii_readback,
    _compile_of,
    _component_eng,
    _dumps,
    _erc_lines,
    _erc_summary,
    _err,
    _fp_overrides,
    _get_sch,
    _placed_pins,
    _power_totals,
    _pretty_pin,
    _resolve_ref_or_id,
    _rpc,
    _rs,
    _unconnected_pins,
    _update_from_sch,
    ecad_session,
    lib_search,
    netlist_mod,
)
from typing import Any, Union

from .cad_common import coerce_json_or_repr, require_list

# ------------------------------------------------------- model-dialect normalizers (boundary)
# ecad_place_symbol/ecad_wire used to accept exactly ONE rigid argument shape while their
# ecad_pcb_tools.py siblings (ecad_set_net_class:844, ecad_autoroute:341, ecad_add_zone:476)
# already take Union[dict, str] for the same concepts. Live models write that sibling dialect,
# a batch ops:[...] list, or a nested placement/connection dict — and got a hard rejection.
# Everything below normalizes at the TOOL BOUNDARY only: the ops handed to the core RPC are
# identical to the strict path, and a genuinely malformed call still raises a teaching
# ValueError naming every accepted shape (never a silent ok:true no-op). The coercion itself
# is the SHARED helper the rest of services/mcp already uses —
# cad_common.require_list / coerce_json_or_repr (services/mcp/cad_common.py:130 / :91).

# Keys a model uses to wrap a batch of placements/connections in one object.
_BATCH_KEYS = ("ops", "batch", "items", "symbols", "placements", "parts",
               "wires", "connections")
# Wire-endpoint aliases for "which symbol" — the vocabulary the sch_connect op itself uses
# ("sym") plus the ref-des words the docstrings/guidance name.
_REF_KEYS = ("ref", "reference", "refdes", "ref_des", "designator", "sym", "symbol",
             "component")
# ...and for "which pin" (ecad_place_symbol's pin list exposes number + name).
_PIN_KEYS = ("pin", "pin_number", "pin_num", "pin_name", "number", "name")
# Placement aliases. libid first so {"part": "Device:R", "ref": "R5"} reads correctly, and
# "fpid" because ecad_find_part returns libid OR fpid and models paste whichever they got.
_LIBID_KEYS = ("libid", "lib_id", "lib", "part", "part_id", "symbol", "component",
               "fpid", "name")
_REFDES_KEYS = ("ref", "reference", "refdes", "ref_des", "designator")
_POS_KEYS = ("pos", "position", "at", "xy", "location")
_ANGLE_KEYS = ("angle", "rotation", "rot", "orientation")
_FOOTPRINT_KEYS = ("footprint", "fpid", "fp", "package")
_SIMPARAM_KEYS = ("sim_params", "simparams", "sim", "spice")
_VALUE_KEYS = ("value", "val")
_FROM_KEYS = ("from", "from_ref", "a", "source", "src", "start")
_TO_KEYS = ("to", "to_ref", "b", "target", "dst", "end")
_FROM_PIN_KEYS = ("from_pin", "fromPin", "a_pin", "source_pin", "src_pin", "start_pin")
_TO_PIN_KEYS = ("to_pin", "toPin", "b_pin", "target_pin", "dst_pin", "end_pin")

# ------------------------------------------------- refdes collision + sim_params dialect
# `ecad_place_symbol` is ADD-ONLY: no MCP tool edits an existing symbol (`sch_edit_symbol` is
# a core op with no tool surface), so a duplicate refdes can only mean the model lost count of
# its own designators — never "modify that part". Measured live 3x across 2 passes:
#   ecad_place_symbol('Device:C_pol', ref='C1', value='10u')
#   -> could not place: resulting document failed validation: symbols[4].ref: duplicate 'C1'
# Renumber into the next free slot of the SAME series and SAY SO: the caller wires to that ref
# on its very next call and needs the real name. A silent renumber would be worse than the
# error it replaces.

def _next_free_ref(doc: dict, requested: str) -> str:
    """Next unused refdes in the same prefix series as `requested` (C1 taken -> C2)."""
    taken = {str(s.get("ref")) for s in doc.get("symbols", []) if s.get("ref")}
    req = str(requested)
    cut = len(req)
    while cut > 0 and req[cut - 1].isdigit():
        cut -= 1
    prefix, digits = req[:cut], req[cut:]
    if not prefix:
        prefix = "U"  # an all-numeric "refdes" has no series; park it on the IC series
    n = int(digits) if digits else 0
    while True:
        n += 1
        cand = f"{prefix}{n}"
        if cand not in taken:
            return cand


# sim_params keys the pipeline ACTUALLY reads: ac_mag/ac_phase/eng (ecad_common
# _NetlistFacade.build_netlist) and waveform + its flat fields (_apply_sim_source_params).
# Anything else is dropped silently downstream, so it is rejected at this boundary instead.
_SIMPARAM_WAVEFORM_FIELDS = ("amplitude", "freq", "offset", "td", "theta",
                             "v1", "v2", "tr", "tf", "pw", "per")
_SIMPARAM_NUMERIC = ("ac_mag", "ac_phase") + _SIMPARAM_WAVEFORM_FIELDS
_SIMPARAM_ALLOWED = ("waveform", "eng", "points") + _SIMPARAM_NUMERIC
# Device MAGNITUDE written SPICE-style. These have NO sim_params slot at all — a passive's
# magnitude lives in the symbol's `value` field, and inventing a nested slot for it would
# silently corrupt the simulation it was meant to configure.
_SIMPARAM_VALUE_FIELD_KEYS = ("capacitance", "resistance", "inductance", "value",
                              "cap", "res", "ind", "c", "r", "l")


def _parse_kv_sim_params(text: str) -> tuple[dict | None, str]:
    """Parse the SPICE-style ``key=value[,key=value]`` sim_params dialect.

    Returns ``(params, "")`` when every key has a real sim_params slot, else
    ``(None, reason)`` — a key we cannot place is NEVER guessed into a nested slot.
    """
    s = str(text).strip().strip(";,")
    if "=" not in s:
        return None, ""
    out: dict = {}
    for part in s.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            return None, f"{part!r} is not a key=value pair"
        raw_key, raw_val = part.split("=", 1)
        key, val = raw_key.strip().strip("'\"").lower(), raw_val.strip().strip("'\"")
        if key in _SIMPARAM_VALUE_FIELD_KEYS:
            return None, (f"{raw_key.strip()!r} is the part's MAGNITUDE, not a sim_params "
                          f"field — pass it as value={val!r} instead")
        if key not in _SIMPARAM_ALLOWED:
            return None, f"{raw_key.strip()!r} is not a sim_params field"
        if key == "waveform":
            out[key] = val.upper()
        elif key in _SIMPARAM_NUMERIC:
            try:
                out[key] = float(val)
            except ValueError:
                return None, f"{raw_key.strip()}={val!r} must be a number"
        else:
            return None, f"{raw_key.strip()!r} needs a JSON object value, not key=value"
    return (out, "") if out else (None, "")


def _pick(d: dict, keys: tuple[str, ...]) -> Any:
    """First non-empty value among `keys` in `d` (alias resolution); None when none present."""
    for k in keys:
        if k in d:
            v = d[k]
            if v is not None and v != "":
                return v
    return None


def _batch_of(d: dict) -> list | None:
    """The wrapped batch list inside e.g. {"ops": [...]} / {"symbols": [...]}, else None."""
    for k in _BATCH_KEYS:
        v = d.get(k)
        if isinstance(v, (list, tuple)):
            return list(v)
    return None


def _split_dotted(token: str) -> tuple[str, str]:
    """'R1.2' / 'R1:2' -> ('R1', '2') — the exact ref.pin spelling THESE TOOLS PRINT BACK in
    net_members / unconnected_pins, so a model echoing our own output isn't a syntax error.
    Also tolerates the _pretty_pin 'R1.2(passive)' decoration."""
    for sep in (".", ":"):
        if sep in token:
            ref, _, pin = token.rpartition(sep)
            pin = pin.split("(")[0].strip()
            if ref and pin:
                return ref.strip(), pin
    return token, ""


def _endpoint(value: Any, pin: Any, side: str) -> tuple[str, str]:
    """Normalize ONE wire endpoint to (ref, pin). Accepts the documented 'R1' + separate pin,
    the sibling Union[dict, str] endpoint {'ref': 'R1', 'pin': '1'} (ref/sym/designator +
    pin/number/name aliases), a ['R1', '1'] pair, and the dotted 'R1.1'."""
    v = coerce_json_or_repr(value)
    if isinstance(v, (list, tuple)) and len(v) == 2:
        if pin is None or pin == "":
            pin = v[1]
        v = v[0]
    if isinstance(v, dict):
        pin_v = _pick(v, _PIN_KEYS)
        v = _pick(v, _REF_KEYS)
        if pin_v is not None:
            pin = pin_v
    if v is None or not str(v).strip():
        raise ValueError(
            f"the {side} endpoint names no symbol — pass a ref like 'R1' with its pin, "
            f"{{'ref': 'R1', 'pin': '1'}}, or the dotted 'R1.1'")
    ref = str(v).strip()
    if pin is None or str(pin).strip() == "":
        ref, pin = _split_dotted(ref)
    if pin is None or str(pin).strip() == "":
        raise ValueError(
            f"the {side} endpoint {ref!r} carries no pin — pass the pin number/name "
            f"separately, as {{'ref': {ref!r}, 'pin': '1'}}, or dotted as '{ref}.1'")
    return ref, str(pin).strip()


def _wire_specs(from_ref: Any, from_pin: Any, to_ref: Any, to_pin: Any) -> list[dict]:
    """Normalize ecad_wire's four rigid args into a LIST of
    {from_ref, from_pin, to_ref, to_pin} — one entry for the plain call, N for a batch."""
    first = coerce_json_or_repr(from_ref)
    conns: list | None = None
    if isinstance(first, dict):
        conns = _batch_of(first)
        if conns is None and (_pick(first, _FROM_KEYS) is not None
                              or _pick(first, _TO_KEYS) is not None):
            conns = [first]  # ONE whole-connection dict {'from': ..., 'to': ...}
    elif isinstance(first, (list, tuple)) and first and all(
            isinstance(e, dict) for e in first):
        conns = list(first)  # a batch of connection dicts
    if conns is None:
        fr0, fp0 = _endpoint(from_ref, from_pin, "from")
        to0, tp0 = _endpoint(to_ref, to_pin, "to")
        return [{"from_ref": fr0, "from_pin": fp0, "to_ref": to0, "to_pin": tp0}]
    conns = require_list(conns, "from_ref",
                         "a list of connections, e.g. "
                         "[{'from': 'R1.1', 'to': 'R2.2'}]")
    if not conns:
        raise ValueError("no connections to make — the wire batch is empty")
    out: list[dict] = []
    for entry in conns:
        e = coerce_json_or_repr(entry)
        if not isinstance(e, dict):
            raise ValueError(
                "each wire in a batch must be an object like "
                "{'from': 'R1.1', 'to': 'R2.2'} or "
                "{'from': {'ref': 'R1', 'pin': '1'}, 'to': {'ref': 'R2', 'pin': '2'}}, "
                f"got {type(entry).__name__}")
        a, b = _pick(e, _FROM_KEYS), _pick(e, _TO_KEYS)
        if a is None or b is None:
            raise ValueError(
                "each wire needs BOTH a 'from' and a 'to' endpoint, e.g. "
                "{'from': 'R1.1', 'to': 'R2.2'}")
        fr, fp = _endpoint(a, _pick(e, _FROM_PIN_KEYS), "from")
        to, tp2 = _endpoint(b, _pick(e, _TO_PIN_KEYS), "to")
        out.append({"from_ref": fr, "from_pin": fp, "to_ref": to, "to_pin": tp2})
    return out


# --------------------------------------------------- wire endpoint-name dialect (a/b)
# LIVE gap measured 2026-07-29 (claude-sonnet-4-5, ecad turn):
#   ecad_wire(ref_a="U1", pin_a="3", ref_b="C1", pin_b="1", doc_id="ecadcpp0000000b")
#   -> unknown_argument for ALL FOUR names, so the whole wiring turn connected nothing.
# a/b is a completely standard naming for the two ends of a connection and the mapping is
# unambiguous: `a` is the FIRST endpoint (-> from_*), `b` the SECOND (-> to_*). ORDER IS
# PRESERVED — a never becomes `to`.
#
# Both word orders (`ref_a` and `a_ref`) are declared because neither is reachable from the
# other through the shared affix reconciler: norm_arg gives "refa" vs "aref", and
# arg_reconciliation.resolve_unknown only relates names by prefix/suffix
# (services/agent/arg_reconciliation.py:392-400). Adding them cannot shadow a spelling that
# already worked either — every supplied name that affix-matches one of the new parameters
# ("ref", "pin") already matched >=2 of from_ref/from_pin/to_ref/to_pin and was therefore
# already ambiguous.
#
# They are REAL DECLARED PARAMETERS, never a **kwargs bag: a var-keyword parameter turns the
# shared reconciler OFF for this tool on both dispatch paths (agent_turn._signature_spec /
# server._arg_meta read VAR_KEYWORD as "accepts anything") and FastMCP then DROPS the unknown
# key silently — a loud unknown_argument would become a silent wrong call.
#
# start_/end_/source_/target_ are deliberately NOT declared as parameters: those words are
# already accepted at the endpoint-DICT layer (_FROM_KEYS/_TO_KEYS above), and each extra
# top-level parameter is schema noise a model must read past on every wire call.

def _blank(v: Any) -> bool:
    """True when an argument slot was left at its empty default."""
    return v is None or (isinstance(v, str) and not v.strip())


def _fold_wire_aliases(canon: dict, folds: list, notes: list) -> dict | None:
    """Fold a/b endpoint aliases onto the canonical from_*/to_* slots.

    Returns None on success (``canon`` updated in place, every rewrite echoed in ``notes``),
    or a refusal payload when a canonical name and one of its synonyms DISAGREE — two
    spellings of the same endpoint that carry different values is a real ambiguity, and
    picking one silently would wire the wrong pin and report success."""
    filled: dict[str, str] = {}
    for slot, name, value in folds:
        if _blank(value):
            continue
        cur = canon[slot]
        if _blank(cur):
            canon[slot] = value
            filled[slot] = name
            notes.append(f"interpreted {name}={value!r} as {slot} "
                         f"({'a' if slot.startswith('from') else 'b'} is the "
                         f"{'first' if slot.startswith('from') else 'second'} endpoint)")
            continue
        if str(cur) == str(value):
            continue
        held = filled.get(slot, slot)
        return {
            "ok": False,
            "error": f"{held}={cur!r} and {name}={value!r} are two spellings of the same "
                     f"wire endpoint but disagree. {name} is accepted only as a synonym of "
                     f"{slot} — pass one of them, not both; NO wire was made.",
            "hint": "ecad_wire connects endpoint a (from_ref/from_pin, also spelled "
                    "ref_a/pin_a or a_ref/a_pin) to endpoint b (to_ref/to_pin, also "
                    "ref_b/pin_b or b_ref/b_pin).",
            "next_actions": ["ecad_wire('U1', '3', 'C1', '1')",
                             "ecad_status()  # list the placed refs and their pins"],
            "warnings": [],
        }
    return None


def _merge_place_spec(base: dict, e: dict) -> dict:
    """Fold ONE model-written placement dict onto the top-level defaults."""
    lib = _pick(e, _LIBID_KEYS)
    if lib is None or not str(lib).strip():
        raise ValueError(
            "each placement needs a libid from ecad_find_part, e.g. "
            "{'libid': 'Device:R', 'x': 0, 'y': 0, 'value': '1k'} "
            "(libid/lib_id/part/symbol are all accepted)")
    spec = dict(base, libid=str(lib).strip())
    pos = _pick(e, _POS_KEYS)
    if pos is not None:
        pos = coerce_json_or_repr(pos)
        if isinstance(pos, dict):
            spec["x"] = pos.get("x", spec["x"])
            spec["y"] = pos.get("y", spec["y"])
        else:
            pt = require_list(pos, "pos", "an [x, y] point in mm")
            if len(pt) < 2:
                raise ValueError(f"pos must be an [x, y] point in mm, got {pos!r}")
            spec["x"], spec["y"] = pt[0], pt[1]
    for slot, keys in (("x", ("x", "pos_x")), ("y", ("y", "pos_y")),
                       ("ref", _REFDES_KEYS), ("value", _VALUE_KEYS),
                       ("angle", _ANGLE_KEYS), ("footprint", _FOOTPRINT_KEYS),
                       ("sim_params", _SIMPARAM_KEYS)):
        got = _pick(e, keys)
        if got is not None:
            spec[slot] = got
    for slot in ("x", "y", "angle"):
        raw = spec[slot]
        try:
            spec[slot] = float(raw) if raw not in (None, "") else 0.0
        except (TypeError, ValueError):
            raise ValueError(
                f"{slot} must be a number ({'degrees' if slot == 'angle' else 'mm'}), "
                f"got {raw!r}") from None
    return spec


def _place_specs(libid: Any, x: Any, y: Any, ref: Any, value: Any, angle: Any,
                 footprint: Any, sim_params: Any) -> list[dict]:
    """Normalize ecad_place_symbol's args into a LIST of placement specs. Accepts the
    documented libid string, ONE placement dict, a {'symbols': [...]} / {'ops': [...]} wrapper,
    or a bare list of placement dicts/libid strings (batch)."""
    base = {"libid": "", "x": x, "y": y, "ref": ref, "value": value, "angle": angle,
            "footprint": footprint, "sim_params": sim_params}
    v = coerce_json_or_repr(libid)
    if isinstance(v, str):
        if not v.strip():
            raise ValueError(
                "libid is required — find one with ecad_find_part, then pass e.g. "
                "'Device:R' (or a placement dict / list of placement dicts)")
        return [_merge_place_spec(base, {"libid": v})]
    if isinstance(v, dict):
        entries = _batch_of(v) or [v]
    elif isinstance(v, (list, tuple)):
        entries = list(v)
    else:
        raise ValueError(
            "libid must be a library id like 'Device:R', ONE placement dict "
            "{'libid': 'Device:R', 'x': 0, 'y': 0}, or a list of those, got "
            f"{type(libid).__name__}")
    if not entries:
        raise ValueError("no symbols to place — the placement batch is empty")
    out: list[dict] = []
    for entry in entries:
        en = coerce_json_or_repr(entry)
        if isinstance(en, str):
            if not en.strip():
                raise ValueError("a placement entry is an empty libid string")
            out.append(_merge_place_spec(base, {"libid": en}))
        elif isinstance(en, dict):
            out.append(_merge_place_spec(base, en))
        else:
            raise ValueError(
                "each placement must be a libid string or an object like "
                "{'libid': 'Device:R', 'x': 0, 'y': 0, 'value': '1k'}, got "
                f"{type(entry).__name__}")
    return out

# ------------------------------------------------- library-miss policy (substitute vs generate)
# The pin count above which ecad_find_part REFUSES to substitute a generic and routes to
# ecad_make_part instead. 3, deliberately:
#   * 2 pins — a resistor/capacitor/diode/crystal/bead symbol has no pinout to get wrong;
#     the part-specific information is `value`, which the caller passes.
#   * 3 pins — a SOT-23 discrete (MOSFET/BJT). 1=G/2=S/3=D (and 1=B/2=E/3=C) is the JEDEC
#     package convention, not this library's invention, so the generic is the real symbol.
#   * 4+ pins — the substitute's pin NUMBERS are its own. MCU:Generic_QFP48 numbers 1..48
#     sequentially; no real 48-pin MCU does. Wiring against it produces a netlist that
#     looks complete and is wrong on every net, which is worse than any refusal — and since
#     ecad_make_part can now build the real part from a caller-supplied pinout, the refusal
#     is actionable rather than a dead end.
_SUBSTITUTABLE_MAX_PINS = 3

def register_ecad_sch_tools(mcp: FastMCP) -> None:
    """ECAD schematic MCP tools (T-RF8 split of ecad_tools.py): ecad_new_schematic, find_part, place_symbol, wire, add_power, label, status, run_erc, update_from_sch."""

    # Make parts created by ecad_make_part in EARLIER sessions resolvable. store_part
    # registers the native dir as a side effect of writing, which only covers the process
    # that created the part — so without this call a part generated yesterday sits on disk
    # unresolvable and ecad_make_part's "it resolves from now on" becomes false after a
    # restart. One idempotent path append, no I/O; every consumer in this process shares
    # the native._default singleton, so the parts-catalog adapter sees it too.
    from services.ecad.library.generate import register_library_dir
    register_library_dir()


    # ---------------------------------------------------------------- new doc

    @mcp.tool()
    def ecad_new_schematic(name: str = "Schematic1") -> str:
        """Start a NEW schematic document and make it the active one.

        Returns the doc id and tells you the next move: find a real part with
        ecad_find_part, then place it with ecad_place_symbol. The doc starts empty (no
        components, no nets) — every later tool defaults to this active schematic, so you can
        omit doc_id.
        """
        s = ecad_session.new_session("sch", name)
        _ECAD["current_sch"] = s.doc_id
        _ECAD["current"] = s.doc_id
        return _dumps({
            "ok": True, "doc_id": s.doc_id, "kind": "sch", "name": name,
            "revision": s.revision, "components": 0, "nets": 0,
            "warnings": [],
            "hint": "Empty schematic. Find a real part before inventing one: "
                    "ecad_find_part('resistor') returns library libids you can place.",
            "next_actions": [
                "ecad_find_part('resistor')  # discover real libids (e.g. Device:R)",
                "ecad_place_symbol('Device:R', x=0, y=0, value='1k')  # place your first part",
            ],
        })



    # ---------------------------------------------------------------- find part

    def _symbol_record(libid: str, lib_resolve, *, score: int = 0,
                       name: str = "", description: str = "") -> dict:
        """Build one symbol hit record (pins, ref prefix, sim flag) from a libid."""
        sdef = lib_resolve(libid) or {}
        pins = sdef.get("pins", [])
        rec = {"kind": "symbol", "libid": libid,
               "name": name or sdef.get("name", ""),
               "description": description or sdef.get("description", ""),
               "score": score,
               "pin_count": len(pins),
               "pins": [f"{p.get('number')}:{p.get('name')}({p.get('type')})"
                        for p in pins[:12]],
               "reference_prefix": sdef.get("reference_prefix", ""),
               "default_value": sdef.get("value", "")}
        if sdef.get("sim"):
            rec["simulatable"] = True
        return rec

    @mcp.tool()
    def ecad_find_part(query: str, limit: int = 12, category: str = "") -> str:
        """Search the component library and return RANKED real parts so you place an actual part
        instead of inventing a libid.

        For each hit: its ``libid`` (symbols) or ``fpid`` (footprints), name, description, and
        PIN COUNT (symbols) so you know how many pins you'll have to wire. Hits are sorted best
        first. Pass the exact ``libid``/``fpid`` of a hit to ecad_place_symbol / a footprint op.

        The library is a clean-room GENERIC set — it holds no manufacturer part numbers. Ask for
        a real one and what happens depends on the PIN COUNT of the closest generic:
        a 2- or 3-pin passive/discrete (a resistor, a diode, a SOT-23 MOSFET) is substituted and
        flagged ``substituted: true``, because a generic ``Device:R`` genuinely IS the right
        symbol for any resistor. Anything with MORE pins is REFUSED and routed to
        ``ecad_make_part`` — a generic 48-pin QFP is not an ESP32's pinout, and a schematic
        wired against the wrong pinout is a fabricated design, not an approximation.

        ``ecad_find_part('list')`` browses every library part grouped by family.
        ``category`` restricts the search to one family prefix (e.g. 'Device', 'MCU',
        'Regulator_Switching'); an unknown category errors with the valid list.
        """
        from services.ecad.library import resolve as lib_resolve  # STAYS (T-RF20): no ecad_library RPC; pin-count lookup
        from services.ecad.library import substitute as _sub

        q = (query or "").strip()
        families = _sub.family_names()

        # Browse mode. The old miss hint named a fixed example list that happened to omit the
        # MCU and regulator families a model most often wants; this is the real index.
        if q.lower() in _sub.LIST_QUERIES:
            index = _sub.family_index()
            total = sum(f["count"] for f in index)
            return _dumps({
                "ok": True, "query": q, "count": total, "families": index,
                "categories": families, "warnings": [],
                "hint": f"{total} generic library symbols in {len(index)} families. "
                        "These are GENERIC parts — search by function ('buck regulator', "
                        "'mcu', 'imu') or by a real part number to get the closest generic.",
                "next_actions": ["ecad_place_symbol('<libid>', x=0, y=0, value='<value>')"],
            })

        cat = (category or "").strip()
        if cat:
            resolved_cat = next((f for f in families if f.lower() == cat.lower()), None)
            if resolved_cat is None:
                return _err(f"unknown category {category!r}",
                            hint="category filters on the library prefix of a libid (the part "
                                 f"before the colon). Valid: {', '.join(families)}.",
                            next_actions=["ecad_find_part('list')  # browse every family",
                                          f"ecad_find_part({q!r})  # search without a category"],
                            query=q, hits=[], categories=families)
            cat = resolved_cat

        if not q:
            return _err("query is empty",
                        hint="Pass a function or a part number, e.g. ecad_find_part('buck "
                             "regulator') or ecad_find_part('TPS62840').",
                        next_actions=["ecad_find_part('list')  # browse every library part"],
                        query=query, hits=[], categories=families)

        hits = lib_search(q) or []
        if cat:
            hits = [h for h in hits
                    if (h.get("libid") or h.get("fpid") or "").split(":", 1)[0] == cat]
        out_hits: list[dict] = []
        for h in hits[:max(1, int(limit))]:
            if h.get("kind") == "symbol":
                out_hits.append(_symbol_record(h.get("libid", ""), lib_resolve,
                                               score=h.get("score", 0),
                                               name=h.get("name", ""),
                                               description=h.get("description", "")))
            else:
                out_hits.append({"kind": h.get("kind"), "name": h.get("name", ""),
                                 "description": h.get("description", ""),
                                 "score": h.get("score", 0), "fpid": h.get("fpid", "")})

        if not out_hits:
            # No literal match. Two very different situations, and conflating them is what
            # used to produce confidently wrong schematics:
            #
            #  * PASSIVES/DISCRETES (<= 3 pins). Device:R IS the resistor symbol; the only
            #    thing that distinguishes a 10k from a 4k7 is `value`, which the caller
            #    supplies. A 3-pin SOT-23 discrete's 1=G/2=S/2=B numbering is the JEDEC
            #    standard, not a guess. Substituting here loses nothing, so it stays.
            #
            #  * MULTI-PIN ICs AND MODULES (> 3 pins). Here the substitute's pin map is
            #    essentially guaranteed to be wrong: MCU:Generic_QFP48 numbers its pins
            #    1..48 sequentially and an ESP32-S3 does not. Before ecad_make_part existed
            #    the choice was "wrong pinout" vs "dead end", and wrong-pinout-with-a-warning
            #    won. It no longer is: ecad_make_part builds the REAL part from a
            #    caller-supplied pinout, so an honest refusal that names it is strictly
            #    better than an ok:true answer the caller will wire up.
            sug = _sub.suggest(q)
            if sug and cat and sug["libid"].split(":", 1)[0] != cat:
                sug = None
            if sug:
                rec = _symbol_record(sug["libid"], lib_resolve)
                if rec["pin_count"] > _SUBSTITUTABLE_MAX_PINS:
                    return _err(
                        f"no library part matches {q!r}, and the closest generic "
                        f"{sug['libid']} is a {rec['pin_count']}-pin "
                        f"{sug['family']} whose pin map is NOT {q}'s — refusing to "
                        "substitute it",
                        hint=f"A {rec['pin_count']}-pin substitute would place a symbol "
                             f"whose pins are numbered for {sug['libid']}, so every wire "
                             f"you then drew would be on the wrong {q} pin. Build the "
                             "real part instead: ecad_make_part takes the pinout FROM "
                             "YOU (one entry per pin: number, name, type) plus the "
                             "package geometry, and generates a symbol + footprint that "
                             "resolve immediately. Read the pinout off the datasheet — "
                             "nothing here will invent it. (Passives and 2-3 pin "
                             "discretes are still substituted, because a generic "
                             "Device:R really is the right resistor symbol.)",
                        next_actions=[
                            f"ecad_make_part(name={q!r}, libid='Generated:{q}', "
                            "pins=[{'number':'1','name':'GND','type':'power_in',"
                            "'side':'left'}, ...], package='module_castellated', "
                            "pitch=1.27, body_w=18.0, body_h=25.5, pad_len=2.0, "
                            "pad_width=0.9)  # build the real part from its datasheet",
                            f"ecad_find_part({sug['libid'].split(':')[-1]!r})  "
                            f"# place {sug['libid']} deliberately, pinout and all",
                            "ecad_find_part('list')  # browse every library part",
                        ],
                        query=q, requested=q, hits=[], categories=families,
                        substituted=False, exact_match=False,
                        closest_generic=sug["libid"],
                        closest_generic_pin_count=rec["pin_count"],
                        closest_generic_family=sug["family"],
                        make_part_tool="ecad_make_part")
                rec["substituted"] = True
                rec["requested"] = q
                warning = (f"SUBSTITUTED: the library has no symbol for {q!r}. Offering the "
                           f"closest generic {sug['family']}: {sug['libid']} "
                           f"({rec['pin_count']} pins). {sug['caveat']} If that pin map is "
                           f"not {q}'s, build the real part with ecad_make_part instead of "
                           "wiring this one.")
                return _dumps({
                    "ok": True, "query": q, "requested": q, "substituted": True,
                    "exact_match": False, "substitute_libid": sug["libid"],
                    "substitute_family": sug["family"],
                    "count": len(out_hits) + 1, "hits": [rec],
                    "warnings": [warning],
                    "hint": f"No exact part — {sug['libid']} is the generic "
                            f"{sug['family']} standing in for {q!r}. Placeable as-is; its "
                            "pin map is NOT the requested part's. "
                            + sug["caveat"]
                            + " Need the real pinout? ecad_make_part builds it from a "
                              "pinout you supply.",
                    "next_actions": [
                        f"ecad_place_symbol('{sug['libid']}', x=0, y=0, value={q!r})",
                        f"ecad_make_part(name={q!r}, libid='Generated:{q}', pins=[...])  "
                        "# the real part, from its datasheet pinout",
                        "ecad_find_part('list')  # browse every library part",
                    ],
                })
            scope = f" in category {cat!r}" if cat else ""
            return _err(f"no library parts match {q!r}{scope}, and no generic equivalent "
                        "covers that function",
                        hint="This is a clean-room GENERIC library — no manufacturer part "
                             f"numbers. Families: {', '.join(families)}. Search by FUNCTION "
                             "('buck regulator', 'mcu', 'opamp', 'imu', 'header', 'crystal') "
                             "or call ecad_find_part('list') for every part with its pin "
                             "count. If the part genuinely is not here, ecad_make_part "
                             "generates it from a pinout you supply.",
                        next_actions=["ecad_find_part('list')  # every part, grouped by family",
                                      f"ecad_make_part(name={q!r}, libid='Generated:{q}', "
                                      "pins=[{'number':'1','name':'GND',"
                                      "'type':'power_in'}, ...])",
                                      "ecad_find_part('resistor')"],
                        query=q, hits=[], categories=families, substituted=False,
                        make_part_tool="ecad_make_part")

        first = out_hits[0]
        first_id = first.get("libid") or first.get("fpid")
        ex = (f"ecad_place_symbol('{first.get('libid')}', x=0, y=0)"
              if first.get("kind") == "symbol"
              else f"use fpid '{first.get('fpid')}' on the board")
        return _dumps({
            "ok": True, "query": q, "count": len(out_hits), "hits": out_hits,
            "substituted": False, "exact_match": True, "warnings": [],
            "hint": f"Best match: {first_id} ({first.get('description') or first.get('name')}). "
                    "Place it with its EXACT libid/fpid — do not invent ids.",
            "next_actions": [ex],
        })


    # ---------------------------------------------------------------- make part

    @mcp.tool()
    def ecad_make_part(name: str, pins: Any = None, package: str = "",
                       libid: str = "", fpid: str = "",
                       pitch: float = 0.0, body_w: float = 0.0, body_h: float = 0.0,
                       pad_len: float = 0.0, pad_width: float = 0.0,
                       pad_offset: float = 0.0, row_span: float = 0.0,
                       drill: float = 0.0, exposed_pad: float = 0.0,
                       numbering: str = "", reference_prefix: str = "U",
                       value: str = "", description: str = "", keywords: str = "",
                       overwrite: bool = False) -> str:
        """CREATE a real library part — symbol + footprint — from a pinout YOU supply.

        This is the honest answer to "the library has no ESP32-WROOM-32". It never invents
        an electrical fact: the pin numbers, names and electrical types come from you (read
        them off the datasheet), and the pad geometry is computed from the pitch / body /
        pad dimensions you pass. Calling it with a part name and NO pin list is refused —
        a part number is not a pinout.

        ``pins`` — a list (or JSON string) of one object per pin::

            [{"number": "1", "name": "GND", "type": "power_in", "side": "left"},
             {"number": "2", "name": "3V3", "type": "power_in", "side": "left"}, ...]

        ``type`` takes the full KiCad electrical enum — input, output, bidirectional,
        tri_state, passive, free, unspecified, power_in, power_out, open_collector,
        open_emitter, no_connect. An UNKNOWN type is an error and a bare ``"power"`` is an
        error (power_in and power_out are opposite ERC roles), because a wrong pin type
        silences the ERC check that would have caught your mistake. Optional ``shape``
        takes the KiCad graphic vocabulary (line/inverted/clock/inverted_clock/input_low/
        clock_low/output_low/falling_edge_clock/non_logic); it is cosmetic, so an
        unrenderable value is noted rather than fatal. ``side`` (left/right/top/bottom) is
        optional for the symbol — omitted, pins split left/right in number order — but
        REQUIRED on every pin for a castellated module, which has no fixed side layout.

        ``package`` (optional; omit it for a symbol-only part): module_castellated, soic,
        sop, ssop, tssop, qfn, dfn, lga, qfp, lqfp, sot, dip, header, 0402, 0603, 0805,
        1206. ``pad_len`` is the pad dimension PERPENDICULAR to the body edge, ``pad_width``
        the one ALONG it, ``pitch`` the pad-to-pad centre spacing, ``row_span`` the
        centre-to-centre spacing of two opposing rows, ``pad_offset`` an explicit
        origin-to-pad-centre distance. Standard families (soic/sot/dip/header/chip) fall
        back to the IPC-7351 nominals the starter library documents; a QFN/QFP/module pad
        ring is NOT guessed — the missing number is named in the error.

        Returns the created ``libid``, ``pin_count`` and ``pad_count`` so you can verify the
        part before wiring it, then place it with ``ecad_place_symbol(libid=...)``.
        ``overwrite=False`` (the default) makes a name collision a clear error and leaves
        the stored part untouched; pass ``overwrite=True`` to replace it.

        Give the footprint its OWN id (``fpid='RF_Module:ESP32-WROOM-32_Module'``). Passing
        ``fpid`` equal to ``libid`` works and both resolve, but the symbol and the footprint
        are then two different records under one string, which a catalog keyed on a flat
        part id (``part_get``/``part_place``) collapses into whichever it saw last — so the
        call succeeds with a loud ID COLLISION warning. Omit ``fpid`` and a distinct one is
        derived for you.
        """
        from services.ecad.library import generate as _gen  # STAYS: no ecad_library RPC

        nm = str(name or "").strip()
        if not nm:
            return _err("name is empty",
                        hint="Pass the part name you are building, e.g. "
                             "name='ESP32-WROOM-32'.",
                        next_actions=["ecad_make_part(name='ESP32-WROOM-32', "
                                      "pins=[...], package='module_castellated')"],
                        created=False)
        raw_pins = coerce_json_or_repr(pins) if pins is not None else None
        if isinstance(raw_pins, dict):
            # A model wrapping the list: {"pins": [...]} / {"items": [...]}.
            raw_pins = _batch_of(raw_pins) or raw_pins.get("pins") or raw_pins
        if isinstance(raw_pins, str):
            return _err(f"pins {raw_pins!r} is not a pin list",
                        hint="pins must be a list of objects, one per pin: "
                             "[{'number':'1','name':'GND','type':'power_in'}, ...]. A "
                             "part name alone cannot produce a part — read the pinout off "
                             "the datasheet.",
                        next_actions=[f"ecad_make_part(name={nm!r}, "
                                      "pins=[{'number':'1','name':'GND',"
                                      "'type':'power_in'}, ...])"],
                        name=nm, created=False)
        geometry = {"pitch": pitch, "body_w": body_w, "body_h": body_h,
                    "pad_len": pad_len, "pad_width": pad_width,
                    "pad_offset": pad_offset, "row_span": row_span, "drill": drill,
                    "exposed_pad": exposed_pad, "numbering": numbering}
        try:
            made = _gen.make_part(nm, raw_pins, libid=libid, package=package, fpid=fpid,
                                  overwrite=bool(overwrite),
                                  reference_prefix=reference_prefix, value=value,
                                  description=description, keywords=keywords,
                                  **geometry)
        except ValueError as e:
            # Every refusal from generate.py already names what is missing and the next
            # call; surface it verbatim rather than flattening it into "invalid args".
            return _err(str(e),
                        hint="Nothing was written. This generator will not guess an "
                             "electrical fact or a pad coordinate — supply the value it "
                             "named and call again.",
                        next_actions=[
                            f"ecad_make_part(name={nm!r}, libid='Generated:{nm}', "
                            "pins=[{'number':'1','name':'GND','type':'power_in',"
                            "'side':'left'}, ...], package='module_castellated', "
                            "pitch=1.27, body_w=18.0, body_h=25.5, pad_len=2.0, "
                            "pad_width=0.9)",
                            "ecad_find_part('list')  # what the library already has",
                        ],
                        name=nm, created=False)
        except OSError as e:
            return _err(f"could not write the generated part: {e}",
                        hint="The native library directory is not writable. Set "
                             "CHUDWARE_ECAD_LIB_DIR to a writable path and retry.",
                        next_actions=["ecad_find_part('list')"], name=nm, created=False)

        libid_out, fpid_out = made["libid"], made["fpid"]
        pin_count, pad_count = made["pin_count"], made["pad_count"]
        warnings: list[str] = []
        if fpid_out and pad_count != pin_count:
            warnings.append(
                f"PAD/PIN MISMATCH: the symbol has {pin_count} pins but the footprint has "
                f"{pad_count} pads. That is legitimate for an exposed thermal pad or a "
                "mechanical mounting pad, and a MISTAKE otherwise — check it before you "
                "route the board.")
        if made.get("id_collision"):
            # Legal at the library tier (symbols/ and footprints/ are separate namespaces,
            # exactly like KiCad's symbol libs vs .pretty dirs) and both resolve — but the
            # symbol and the footprint are then two DIFFERENT records under ONE string, and
            # a consumer keyed on a flat part id keeps only one. Measured 2026-07-29: the
            # parts index kept the FOOTPRINT record, whose geometry_ref carries no libid, so
            # part_place reported "geometry_ref missing 'libid'" for a part that had just
            # been created successfully. Named loudly instead of silently auto-suffixed.
            warnings.append(
                f"ID COLLISION: the symbol and the footprint were both stored as "
                f"{libid_out!r}. Both resolve here, but they are two different records "
                "under one id, so a catalog keyed on a flat part id (part_get / "
                "part_place) can end up serving the footprint where the symbol was meant. "
                f"Prefer a distinct footprint id, e.g. fpid='{fpid_out}_"
                f"{(package or 'FP').strip().upper()}'.")
        return _dumps({
            "ok": True, "created": True, "name": nm,
            "libid": libid_out, "fpid": fpid_out,
            "pin_count": pin_count, "pad_count": pad_count,
            "overwrote": bool(overwrite),
            "pins": [f"{p['number']}:{p['name']}({p['type']})"
                     for p in (_lib_resolve_pins(libid_out))],
            "paths": made["paths"], "notes": made["notes"], "warnings": warnings,
            "hint": (f"Created {libid_out} — {pin_count} pins"
                     + (f", footprint {fpid_out} with {pad_count} pads" if fpid_out
                        else " (symbol only — pass package= to add a footprint)")
                     + ". It resolves from now on, so ecad_find_part finds it without "
                       "substituting. The pinout is exactly what you supplied: nothing "
                       "was inferred from the part name."),
            "next_actions": [
                f"ecad_place_symbol('{libid_out}', x=0, y=0, value={nm!r}"
                + (f", footprint='{fpid_out}')" if fpid_out else ")"),
                f"ecad_find_part({nm!r})  # confirm it resolves without a substitution",
            ],
        })

    def _lib_resolve_pins(libid: str) -> list[dict]:
        """The stored pin list of a just-generated part (read back, not echoed)."""
        from services.ecad.library import resolve as _resolve
        return list((_resolve(libid) or {}).get("pins", []))


    # ---------------------------------------------------------------- place symbol

    def _place_one(session, spec: dict, attach_readback: bool) -> dict:
        """Place ONE normalized spec and build its payload dict (ok or teaching error)."""
        libid = spec["libid"]
        # E7: Validate libid resolves BEFORE dispatching the op. An unresolvable libid would
        # silently produce a 2-pin stub with ok:true, which then corrupts the netlist. Fail fast
        # with a clear error and direct the user to ecad_find_part.
        from services.ecad import library as _lib  # STAYS (T-RF20): no ecad_library RPC
        _resolved_def = _lib.resolve(libid)
        if _resolved_def is None:
            keyword = libid.split(":")[-1] if ":" in libid else libid
            return _rs.err_envelope(
                f"unknown library part {libid!r} — use ecad_find_part to find a real libid",
                hint=(f"'{libid}' is not in the component library. "
                      f"Run ecad_find_part('{keyword}') to discover the correct libid."),
                next_actions=[
                    f"ecad_find_part('{keyword}')  # search for the correct libid",
                    "ecad_find_part('resistor')  # browse common parts",
                ],
                libid=libid,
            )
        # The C++ core has no symbol library server-side (core/ecad/ops.cpp ensure_lib_def
        # stubs when no def rides the op) — pass the resolved def inline so lib_cache,
        # ref-prefix annotation (R1, not U1) and pin geometry are identical to the old path.
        op: dict = {"op": "sch_add_symbol", "libid": libid,
                    "pos": [float(spec["x"]), float(spec["y"])],
                    "angle": float(spec["angle"]), "def": _resolved_def}
        ref_warnings: list[str] = []
        if spec["ref"]:
            want_ref = str(spec["ref"])
            existing = {str(s.get("ref")) for s in session.doc.get("symbols", [])}
            if want_ref in existing:
                # Add-only tool: a taken ref means the model lost count, not "edit that one".
                assigned = _next_free_ref(session.doc, want_ref)
                ref_warnings.append(
                    f"REFDES REASSIGNED: {want_ref} is already used on this schematic — "
                    f"{libid} was placed as {assigned} instead. Wire to {assigned}, not "
                    f"{want_ref}. (ecad_place_symbol only ADDS symbols; to change an existing "
                    f"part, delete it and place it again.)")
                want_ref = assigned
            op["ref"] = want_ref
        if spec["value"]:
            op["value"] = str(spec["value"])
        if spec["footprint"]:
            op["footprint"] = str(spec["footprint"])
        sim_params = spec["sim_params"]
        if sim_params:
            # Widened with the rest of the boundary: a real dict (the sibling
            # ecad_set_net_class params dialect), a JSON / python-repr object string, or the
            # SPICE-style 'key=value,key=value' form. The kv form is accepted ONLY for keys
            # with a real slot — 'Capacitance=10u' has none (a passive's magnitude lives in
            # `value`), and guessing a nested slot for it would silently corrupt the sim.
            sp = coerce_json_or_repr(sim_params)
            if isinstance(sp, str):
                kv, why = _parse_kv_sim_params(sp)
                if kv is not None:
                    sp = kv
                else:
                    detail = f" — {why}" if why else ""
                    return _rs.err_envelope(
                        f"sim_params {sim_params!r} is neither a JSON object nor a usable "
                        f"key=value list{detail}",
                        hint="sim_params carries SIMULATION STIMULUS and ENGINEERING "
                             "metadata, not the part's magnitude. Accepted fields: "
                             f"{', '.join(_SIMPARAM_ALLOWED)}. A passive's magnitude goes in "
                             "value= (e.g. value='10u'). Shapes: "
                             '{"eng": {"power_w": {"typ": 0.02}}} or '
                             "'waveform=SIN,amplitude=5,freq=1000'.",
                        next_actions=[
                            "ecad_place_symbol('Device:C', x=0, y=0, value='10u')",
                            "ecad_place_symbol('Simulation_SPICE:V_source', x=0, y=0, "
                            "value='5', sim_params='waveform=SIN,amplitude=5,freq=1000')",
                        ],
                        accepted_sim_params=list(_SIMPARAM_ALLOWED))
            if not isinstance(sp, dict):
                return _rs.err_envelope(
                    "sim_params must be a JSON object",
                    hint='e.g. {"eng": {"power_w": {"typ": 0.02}}} or '
                         "'waveform=SIN,amplitude=5,freq=1000'.",
                    accepted_sim_params=list(_SIMPARAM_ALLOWED))
            op["sim_params"] = sp
        try:
            resp = _apply(session, [op])
        except OpError as e:
            return _rs.err_envelope(
                f"could not place {libid!r}: {e.message}",
                hint="Check the libid with ecad_find_part; pass an exact match.",
                next_actions=[f"ecad_find_part('{libid.split(':')[-1]}')"],
                detail=e.detail)
        sid = resp.get("id_map", {}).get("$0")
        doc = session.doc
        sym = next((s for s in doc.get("symbols", []) if s.get("id") == sid), None)
        if sym is None:
            return _rs.err_envelope("placed symbol but could not locate it (internal)",
                                    hint="Call ecad_status() to inspect the document.")
        pins = _placed_pins(doc, sym)
        nl = netlist_mod.build_netlist(doc)
        unconnected = _unconnected_pins(doc, nl)
        this_unconnected = [p for p in unconnected if p["ref"] == sym.get("ref")]
        warnings = ref_warnings + list(resp.get("warnings", []))
        pin_labels = ", ".join(f"{p['number']}({p['name']}/{p['type']})" for p in pins)
        payload = {
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "ref": sym.get("ref"), "id": sid, "libid": libid,
            "requested_ref": str(spec["ref"]) if spec["ref"] else "",
            "ref_reassigned": bool(ref_warnings),
            "value": sym.get("value", ""), "pos": sym.get("pos"),
            "pins": pins,
            "unconnected_pins_this_symbol": [p["pinref"] for p in this_unconnected],
            "total_unconnected": len(unconnected),
            "warnings": warnings,
            "hint": ((f"{ref_warnings[0]} " if ref_warnings else "")
                     + (f"Placed {sym.get('ref')} with pins: {pin_labels}. "
                        f"All {len(this_unconnected)} of its pins are floating — wire each pin "
                        "to another pin (ecad_wire) or to a power/ground port (ecad_add_power)."
                        if this_unconnected else
                        f"Placed {sym.get('ref')}; its pins are already on existing nets.")),
            "next_actions": (
                [f"ecad_wire('{sym.get('ref')}', '{pins[0]['number']}', '<otherRef>', "
                 f"'<otherPin>')  # connect pin {pins[0]['number']}"]
                if pins else []
            ) + ["ecad_status()  # see the whole design + what's still unconnected",
                 "render_schematic_ascii()  # full sheet ASCII + grid for edits"],
        }
        if attach_readback:
            _attach_schematic_ascii_readback(payload, doc_id=session.doc_id,
                                             focus=list(sym.get("pos") or []))
        return payload

    @mcp.tool()
    def ecad_place_symbol(libid: Union[str, dict, list] = "", x: float = 0.0, y: float = 0.0,
                          ref: str = "", value: str = "",
                          angle: float = 0.0, footprint: str = "", doc_id: str = "",
                          sim_params: Union[str, dict] = "") -> str:
        """Place a symbol on the active schematic and return its assigned REF plus its full PIN
        LIST (name/number/type/world-xy) and the count of pins still unconnected.

        The pin list is the context you need to wire: each pin's world ``xy`` is where a wire must
        reach, and ``net`` tells you what it already touches. ``value`` matters for simulation —
        a resistor needs e.g. '1k', a voltage source '5' — set it now or with ecad_set later.
        ``ref`` auto-assigns (R1, R2, ...) if omitted. ``footprint`` (an fpid like
        'Resistor_SMD:R_0603') makes the part board-ready for ecad_update_from_sch; omit it and a
        sane default for the part's libid is used automatically when you sync to a board.

        ``libid`` also accepts the shapes the PCB-side tools accept, so one call can place many
        parts:
          - 'Device:R' with x/y/value/angle/footprint (the plain form)
          - ONE placement dict: {'libid': 'Device:R', 'pos': [20, 0], 'value': '1k',
            'angle': 90}  (pos/position/at, ref/designator, value/val, angle/rotation,
            footprint/fpid are all accepted)
          - a BATCH: [{'libid': 'Device:R', ...}, {'libid': 'Device:C', ...}] or
            {'symbols': [...]} / {'ops': [...]}. The response then carries ``placed`` (one
            per-symbol payload each) plus ``refs``.

        ``sim_params`` (a JSON object, or an object string) attaches per-instance simulation/
        engineering data. Its ``"eng"`` sub-object overrides the library engineering metadata for
        THIS instance (partial dict — e.g. '{"eng": {"power_w": {"typ": 0.02}}}'); see
        contracts/ecad/boardphys.md. Overrides flow onto the board footprint at sync and into
        the aicad.boardphys/1 sidecar + ecad_status power totals.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        try:
            specs = _place_specs(libid, x, y, ref, value, angle, footprint, sim_params)
        except ValueError as e:
            return _err(
                str(e),
                hint="ecad_place_symbol takes a libid string (with x/y/value/ref/angle/"
                     "footprint), ONE placement dict, or a list of placement dicts, e.g. "
                     "[{'libid': 'Device:R', 'pos': [20, 0], 'value': '1k'}].",
                next_actions=["ecad_find_part('resistor')  # get a real libid",
                              "ecad_place_symbol('Device:R', x=0, y=0, value='1k')"])
        single = len(specs) == 1
        results = [_place_one(session, s, attach_readback=single) for s in specs]
        if single:
            return _dumps(results[0])
        placed = [r for r in results if r.get("ok")]
        failed = [{"libid": s["libid"], "error": r.get("error")}
                  for s, r in zip(specs, results) if not r.get("ok")]
        refs = [r.get("ref") for r in placed]
        payload = {
            "ok": not failed, "doc_id": session.doc_id, "revision": session.revision,
            "count": len(placed), "refs": refs, "placed": results, "failed": failed,
            "total_unconnected": (placed[-1].get("total_unconnected") if placed else 0),
            "warnings": [w for r in results for w in (r.get("warnings") or [])],
            "hint": (f"Placed {len(placed)}/{len(specs)} symbol(s): "
                     f"{', '.join(str(r) for r in refs) or '(none)'}."
                     + (f" {len(failed)} failed: "
                        + "; ".join(f"{f['libid']}: {f['error']}" for f in failed)
                        if failed else "")),
            "next_actions": ["ecad_wire('<ref>', '<pin>', '<otherRef>', '<otherPin>')",
                             "ecad_status()  # see the whole design + what's unconnected"],
        }
        _attach_schematic_ascii_readback(payload, doc_id=session.doc_id)
        return _dumps(payload)



    # ---------------------------------------------------------------- wire

    def _wire_one(session, spec: dict, attach_readback: bool) -> dict:
        """Connect ONE normalized {from_ref, from_pin, to_ref, to_pin} spec."""
        from_ref, from_pin = spec["from_ref"], spec["from_pin"]
        to_ref, to_pin = spec["to_ref"], spec["to_pin"]
        doc = session.doc
        a = _resolve_ref_or_id(doc, from_ref)
        b = _resolve_ref_or_id(doc, to_ref)
        if a is None or b is None:
            missing = from_ref if a is None else to_ref
            refs = [s.get("ref") for s in doc.get("symbols", [])]
            return _rs.err_envelope(f"no symbol {missing!r} on the schematic",
                                    hint=f"Placed refs are: {', '.join(refs) or '(none)'}. "
                                         "Place the part first or fix the ref.",
                                    next_actions=["ecad_status()"],
                                    placed_refs=refs)
        op = {"op": "sch_connect",
              "from": {"sym": a.get("id"), "pin": str(from_pin)},
              "to": {"sym": b.get("id"), "pin": str(to_pin)}}
        try:
            resp = _apply(session, [op])
        except OpError as e:
            # Most common cause: a bad pin name/number — list the real pins to recover.
            a_pins = [p["number"] for p in _placed_pins(doc, a)]
            b_pins = [p["number"] for p in _placed_pins(doc, b)]
            return _rs.err_envelope(
                f"could not wire {from_ref}.{from_pin} -> {to_ref}.{to_pin}: {e.message}",
                hint=f"{from_ref} pins: {a_pins}; {to_ref} pins: {b_pins}. "
                     "Use a real pin number/name.",
                next_actions=[f"ecad_wire('{from_ref}', '{a_pins[0] if a_pins else '1'}', "
                              f"'{to_ref}', '{b_pins[0] if b_pins else '1'}')"],
                detail=e.detail)
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        # which net did this pin land on?
        by_pin = nl.get("by_pin", {})
        net = by_pin.get(f"{a.get('id')}.{from_pin}") or by_pin.get(f"{b.get('id')}.{to_pin}") or ""
        members = nl.get("nets", {}).get(net, [])
        member_refs = [f"{m.get('ref')}.{m.get('pin')}" for m in members]
        unconnected = _unconnected_pins(doc, nl)
        payload = {
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "net": net or None, "net_members": member_refs, "net_size": len(members),
            "total_unconnected": len(unconnected),
            "unconnected_pins": [_pretty_pin(p) for p in unconnected[:12]],
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Connected {from_ref}.{from_pin} <-> {to_ref}.{to_pin} on net "
                     f"{net or 'N$?'} ({len(members)} pins). "
                     + (f"{len(unconnected)} pin(s) still unconnected: "
                        f"{', '.join(_pretty_pin(p) for p in unconnected[:6])}"
                        f"{'...' if len(unconnected) > 6 else ''} — wire them or mark no-connect."
                        if unconnected else "Every pin is now connected.")),
            "next_actions": (
                [f"ecad_wire('{unconnected[0]['ref']}', '{unconnected[0]['pin']}', "
                 "'<ref>', '<pin>')"] if unconnected else
                ["ecad_run_erc()  # verify the design electrically",
                 "ecad_simulate('op')  # solve DC node voltages",
                 "render_schematic_ascii()  # full sheet ASCII + grid"]
            ),
        }
        if attach_readback:
            _attach_schematic_ascii_readback(payload, doc_id=session.doc_id)
        return payload

    @mcp.tool()
    def ecad_wire(from_ref: Union[str, dict, list] = "", from_pin: Union[str, int] = "",
                  to_ref: Union[str, dict] = "", to_pin: Union[str, int] = "",
                  doc_id: str = "",
                  ref_a: Union[str, dict] = "", pin_a: Union[str, int] = "",
                  ref_b: Union[str, dict] = "", pin_b: Union[str, int] = "",
                  a_ref: Union[str, dict] = "", a_pin: Union[str, int] = "",
                  b_ref: Union[str, dict] = "", b_pin: Union[str, int] = "") -> str:
        """Auto-route an orthogonal wire from one pin to another (emits sch_connect) and return
        the RESULTING net plus the updated list of what is still unconnected.

        ``from_ref``/``to_ref`` are ref-des ('R1') or internal ids; pins are pin numbers ('1') or
        names. The engine names the net automatically (power/label name wins, else N$n). The
        returned ``net`` + ``net_members`` confirm the two pins really joined.

        Endpoints also accept the shapes the PCB-side tools accept:
          - 'R1', '1', 'R2', '2' (the plain form)
          - the dotted spelling this tool PRINTS BACK: ecad_wire('R1.1', to_ref='R2.2')
          - endpoint dicts: ecad_wire({'ref': 'R1', 'pin': '1'}, to_ref={'ref': 'R2',
            'pin': '2'})  (ref/sym/designator + pin/number/name aliases)
          - ONE connection dict: ecad_wire({'from': 'R1.1', 'to': 'R2.2'})
          - a BATCH: ecad_wire([{'from': 'R1.1', 'to': 'R2.2'}, {'from': 'R2.2',
            'to': 'V1.-'}]) or {'ops': [...]}. The response then carries ``wires`` (one
            per-connection payload each).

        The two ends may also be named a/b instead of from/to — ``ref_a``/``pin_a``/
        ``ref_b``/``pin_b`` (or ``a_ref``/``a_pin``/``b_ref``/``b_pin``) are exact synonyms of
        ``from_ref``/``from_pin``/``to_ref``/``to_pin``: **a is the first endpoint, b the
        second**. Passing both spellings of one endpoint with DIFFERENT values is refused
        rather than guessed.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        # LIVE-DIALECT gap (2026-07-29): ref_a/pin_a/ref_b/pin_b all died as
        # unknown_argument — see the endpoint-name dialect block above. a -> from, b -> to.
        notes: list = []
        canon = {"from_ref": from_ref, "from_pin": from_pin,
                 "to_ref": to_ref, "to_pin": to_pin}
        bad = _fold_wire_aliases(
            canon,
            [("from_ref", "ref_a", ref_a), ("from_ref", "a_ref", a_ref),
             ("from_pin", "pin_a", pin_a), ("from_pin", "a_pin", a_pin),
             ("to_ref", "ref_b", ref_b), ("to_ref", "b_ref", b_ref),
             ("to_pin", "pin_b", pin_b), ("to_pin", "b_pin", b_pin)],
            notes)
        if bad:
            return _dumps(bad)
        try:
            specs = _wire_specs(canon["from_ref"], canon["from_pin"],
                                canon["to_ref"], canon["to_pin"])
        except ValueError as e:
            return _err(
                str(e),
                hint="ecad_wire takes ('R1', '1', 'R2', '2'), the dotted 'R1.1'/'R2.2', "
                     "endpoint dicts {'ref': 'R1', 'pin': '1'}, ONE connection dict "
                     "{'from': 'R1.1', 'to': 'R2.2'}, or a list of connection dicts.",
                next_actions=["ecad_status()  # list the placed refs and their pins",
                              "ecad_wire('R1', '1', 'R2', '2')"])
        single = len(specs) == 1
        results = [_wire_one(session, s, attach_readback=single) for s in specs]
        if single:
            if notes:
                results[0]["interpreted"] = notes
            return _dumps(results[0])
        made = [r for r in results if r.get("ok")]
        failed = [{"from": f"{s['from_ref']}.{s['from_pin']}",
                   "to": f"{s['to_ref']}.{s['to_pin']}", "error": r.get("error")}
                  for s, r in zip(specs, results) if not r.get("ok")]
        last = made[-1] if made else {}
        payload = {
            "ok": not failed, "doc_id": session.doc_id, "revision": session.revision,
            "count": len(made), "wires": results, "failed": failed,
            "nets": [r.get("net") for r in made],
            "total_unconnected": last.get("total_unconnected", 0),
            "unconnected_pins": last.get("unconnected_pins", []),
            "warnings": [w for r in results for w in (r.get("warnings") or [])],
            "hint": (f"Connected {len(made)}/{len(specs)} wire(s); "
                     f"{last.get('total_unconnected', 0)} pin(s) still unconnected."
                     + (" Failed: " + "; ".join(f"{f['from']}->{f['to']}: {f['error']}"
                                                for f in failed) if failed else "")),
            "next_actions": ["ecad_run_erc()  # verify the design electrically",
                             "ecad_status()  # see what's still unconnected"],
        }
        if notes:
            payload["interpreted"] = notes
        _attach_schematic_ascii_readback(payload, doc_id=session.doc_id)
        return _dumps(payload)



    # ---------------------------------------------------------------- power / label

    @mcp.tool()
    def ecad_add_power(net: str, x: float = 0.0, y: float = 0.0, doc_id: str = "") -> str:
        """Drop a power/ground port (e.g. net='GND', 'VCC', '+5V') at ``(x, y)``.

        A power port both NAMES the net it sits on and acts as its source for ERC (a 'GND' port
        means power_in pins on that net are driven). Place it at a pin's world xy (from
        ecad_place_symbol's pin list) to bond a pin straight to that rail without a wire.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        op = {"op": "sch_add_power", "net": net, "pos": [float(x), float(y)]}
        try:
            resp = _apply(session, [op])
        except OpError as e:
            return _err(f"could not add power port {net!r}: {e.message}", hint=e.message)
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        members = nl.get("nets", {}).get(net, [])
        member_refs = [f"{m.get('ref')}.{m.get('pin')}" for m in members]
        unconnected = _unconnected_pins(doc, nl)
        payload = {
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "net": net, "pos": [float(x), float(y)],
            "net_size": len(members),
            "net_members": member_refs,
            "total_unconnected": len(unconnected),
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Power port '{net}' placed. "
                     + (f"It bonded to {len(members)} pin(s): "
                        f"{', '.join(member_refs[:6])}."
                        if members else
                        "No pin is at this location yet — place it on a pin's world xy "
                        "(see a symbol's pin list) or run a wire to it.")),
            "next_actions": ["ecad_status()",
                             "ecad_run_erc()  # power_not_driven errors disappear once a rail "
                             "is sourced",
                             "render_schematic_ascii()"],
        }
        _attach_schematic_ascii_readback(payload, doc_id=session.doc_id,
                                         focus=[float(x), float(y)])
        return _dumps(payload)



    @mcp.tool()
    def ecad_label(text: str, x: float = 0.0, y: float = 0.0, scope: str = "local",
                   doc_id: str = "") -> str:
        """Place a net label ``text`` at ``(x, y)``. Two labels of the same text+scope on a sheet
        merge their nets; ``scope='global'`` merges across sheets. Drop a label on a wire/pin to
        give that net a readable name (used by ERC, the netlist, and SPICE probes).
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        op = {"op": "sch_add_label", "text": text, "pos": [float(x), float(y)], "scope": scope}
        try:
            resp = _apply(session, [op])
        except OpError as e:
            return _err(f"could not add label {text!r}: {e.message}", hint=e.message)
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        members = nl.get("nets", {}).get(text, [])
        payload = {
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "label": text, "scope": scope, "pos": [float(x), float(y)],
            "net_size": len(members),
            "net_members": [f"{m.get('ref')}.{m.get('pin')}" for m in members],
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Label '{text}' placed. "
                     + (f"It names a net with {len(members)} pin(s)."
                        if members else
                        "It is not on any wire/pin yet — place it ON a wire or a pin's world xy "
                        "to name that net.")),
            "next_actions": ["ecad_status()", "render_schematic_ascii()"],
        }
        _attach_schematic_ascii_readback(payload, doc_id=session.doc_id,
                                         focus=[float(x), float(y)])
        return _dumps(payload)



    # ---------------------------------------------------------------- status (THE context tool)

    @mcp.tool()
    def ecad_status(doc_id: str = "", detail: str = "summary",
                    page: int = 0, page_size: int = 50,
                    include: str = "") -> str:
        """THE situational-awareness call — summary-first, token-bounded.

        Default ``detail='summary'`` returns a COMPACT design summary sized for a
        small context window:
          - counts (components/nets, ERC error+warning counts, unconnected count)
          - board ``power`` totals (contracts/ecad/boardphys.md)
          - components as ``{ref, value, libid}`` (+ power_w; NO pins)
          - nets as ``{name, size}`` (NO members)
          - unconnected sample (≤6) + prioritized ``next_actions``

        Drill-down:
          - ``detail='list'|'full'`` + ``page``/``page_size`` for components/nets
          - ``include='pins,members'`` (CSV) opts into pin lists / net members
          - ``detail='full'`` retains the SAME component refs + net names as the
            full internal payload (set equality with pre-projection data)

        Call this whenever you are unsure what to do next — it tells you what
        exists, what is wrong, and the single best next op.
        """
        did = doc_id or _ECAD["current"] or _ECAD["current_sch"] or _ECAD["current_pcb"]
        if did is None:
            return _err("no active document",
                        hint="Start one first.",
                        next_actions=["ecad_new_schematic('mydesign')",
                                      "ecad_new_pcb('myboard')"])
        try:
            session = ecad_session.get_session(did)
        except OpError as e:
            return _err(e.message, hint="That doc id is not open.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        doc = session.doc
        inc_list = [p.strip() for p in str(include or "").split(",") if p.strip()]
        opts = _rs.ServeOpts(
            detail=detail or "summary",
            page=int(page or 0),
            page_size=int(page_size or 50),
            include=inc_list or None,
            budget_tokens=800 if (detail or "summary").strip().lower() == "summary" else None,
        )
        if session.kind == "pcb":
            return _pcb_status(session, opts)
        # ---- schematic status (full internal payload, then project) ----
        nl = netlist_mod.build_netlist(doc)
        components = []
        comp_engs = []
        for c in nl.get("components", []):
            eng = _component_eng(libid=c.get("libid") or "", fpid=c.get("footprint") or "",
                                 overrides=c.get("eng_overrides"))
            comp_engs.append(eng)
            components.append({"ref": c.get("ref"), "libid": c.get("libid"),
                               "value": c.get("value"), "footprint": c.get("footprint") or None,
                               "pins": c.get("pins"),
                               # T-FIN8: per-component power (honest nulls when unknown —
                               # contracts/ecad/boardphys.md)
                               "power_w": eng["power_w"] if eng else {"typ": None, "max": None}})
        power = _power_totals(comp_engs)
        nets = []
        for name, members in sorted(nl.get("nets", {}).items()):
            nets.append({"name": name, "size": len(members),
                         "members": [f"{m.get('ref')}.{m.get('pin')}" for m in members]})
        # ERC (from the C++ session compile — engine.ecad.erc runs inside compile_field)
        violations: list[dict] = []
        try:
            violations = _compile_of(session).get("erc", []) or []
        except Exception:  # noqa: BLE001 — ERC is advisory; never break status
            violations = []
        erc = _erc_summary(violations)
        unconnected = _unconnected_pins(doc, nl)

        # Build a prioritized next_actions list — errors first, then unconnected pins.
        next_actions: list[str] = []
        for g in erc["groups"]:
            if g["severity"] == "error":
                tgt = (g["pins"][:3] or g["nets"][:3])
                next_actions.append(
                    f"FIX ERROR {g['rule']} ({g['count']}x): {', '.join(map(str, tgt))} — "
                    f"{g['messages'][0] if g['messages'] else ''}")
        if unconnected:
            names = ", ".join(_pretty_pin(p) for p in unconnected[:6])
            more = "..." if len(unconnected) > 6 else ""
            next_actions.append(
                f"{len(unconnected)} pin(s) unconnected: {names}{more} — wire them "
                "(ecad_wire) or mark no-connect.")
        if not components:
            next_actions.append("Empty schematic — ecad_find_part then ecad_place_symbol.")
        elif not violations and not unconnected:
            next_actions.append("Design is clean — ecad_simulate('op') or ecad_run_drc on a board.")

        # one-line headline
        head = (f"{len(components)} component(s), {len(nets)} net(s), "
                f"{erc['errors']} ERC error(s), {erc['warnings']} warning(s), "
                f"{len(unconnected)} unconnected pin(s).")
        if power and power.get("power_w_typ") is not None:
            head += f" Board power ~{power['power_w_typ'] * 1000:.1f} mW typ."
        full = {
            "ok": True, "doc_id": session.doc_id, "kind": "sch",
            "name": doc.get("name"), "revision": session.revision,
            "summary": head,
            "component_count": len(components), "components": components,
            "power": power,
            "net_count": len(nets), "nets": nets,
            "erc": erc,
            "unconnected_count": len(unconnected),
            "unconnected_pins": [_pretty_pin(p) for p in unconnected],
            "warnings": nl.get("warnings", []),
            "hint": head + " Fix ERC errors first, then connect floating pins, then simulate.",
            "next_actions": next_actions or ["ecad_simulate('op')"],
        }
        return _rs.serve(full, opts, projector=_rs.project_ecad_status)



    def _pcb_status(session, opts: "_rs.ServeOpts | None" = None) -> str:
        doc = session.doc
        fps = doc.get("footprints", [])
        nets = doc.get("nets", [])
        tracks = doc.get("tracks", [])
        vias = doc.get("vias", [])
        violations: list[dict] = []
        try:
            res = _compile_of(session).get("drc", [])  # C++ compile runs engine.ecad.drc
            violations = res if isinstance(res, list) else res.get("violations", [])
        except Exception:  # noqa: BLE001
            violations = []
        erc = _erc_summary(violations)  # same grouping shape works for DRC
        next_actions: list[str] = []
        for g in erc["groups"]:
            if g["severity"] == "error":
                next_actions.append(f"FIX DRC {g['rule']} ({g['count']}x): "
                                    f"{g['messages'][0] if g['messages'] else ''}")
        if not fps:
            next_actions.append("No footprints — place parts (ecad_find_part finds fpids).")
        if not doc.get("outline", {}).get("shapes"):
            next_actions.append("No board outline yet — set one with a pcb_set_outline op.")
        # T-FIN8: per-footprint power + board total (honest nulls when unknown).
        fp_engs = [_component_eng(libid=f.get("libid") or "", fpid=f.get("fpid") or "",
                                  overrides=_fp_overrides(f)) for f in fps]
        power = _power_totals(fp_engs)
        head = (f"{len(fps)} footprint(s), {len(nets)} net(s), {len(tracks)} track(s), "
                f"{len(vias)} via(s), {erc['errors']} DRC error(s), {erc['warnings']} warning(s).")
        if power and power.get("power_w_typ") is not None:
            head += f" Board power ~{power['power_w_typ'] * 1000:.1f} mW typ."
        full = {
            "ok": True, "doc_id": session.doc_id, "kind": "pcb",
            "name": doc.get("name"), "revision": session.revision,
            "summary": head,
            "footprint_count": len(fps),
            "footprints": [{"ref": f.get("ref"), "fpid": f.get("fpid"),
                            "pos": f.get("pos"), "side": f.get("side"),
                            "power_w": (e or {}).get("power_w", {"typ": None, "max": None})}
                           for f, e in zip(fps, fp_engs)],
            "power": power,
            "net_count": len(nets),
            "nets": [{"name": n.get("name"), "size": len(n.get("pads") or n.get("nodes") or [])}
                     if isinstance(n, dict) else {"name": n}
                     for n in nets],
            "track_count": len(tracks), "via_count": len(vias),
            "drc": erc,
            "warnings": [],
            "hint": head + " Fix DRC errors first.",
            "next_actions": next_actions or ["ecad_run_drc()", "ecad_export('gerber')"],
        }
        o = opts or _rs.ServeOpts(detail="summary", budget_tokens=800)
        return _rs.serve(full, o, projector=_rs.project_ecad_status)



    # ---------------------------------------------------------------- ERC

    @mcp.tool()
    def ecad_run_erc(doc_id: str = "") -> str:
        """Run the Electrical Rules Check on the active schematic and return the violations as
        readable lines plus a one-line verdict ('N errors, M warnings; fix errors first: ...').

        ERC is advisory — it never blocks edits. Errors (output-output shorts, undriven power,
        no-connect-connected) are real bugs; warnings (floating pins, missing footprint) are
        usually wiring you haven't done yet.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        violations = _compile_of(session).get("erc", []) or []  # C++ compile runs engine.ecad.erc
        summary = _erc_summary(violations)
        lines = _erc_lines(violations)
        first_errors = [v for v in violations if v.get("severity") == "error"][:3]
        verdict = (f"{summary['errors']} error(s), {summary['warnings']} warning(s)."
                   + (" fix errors first: "
                      + "; ".join(v.get("message", "") for v in first_errors)
                      if first_errors else
                      (" No errors." if summary['warnings'] else " Clean.")))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "errors": summary["errors"], "warnings": summary["warnings"],
            "verdict": verdict,
            "violations": lines,
            "by_rule": summary["groups"],
            "hint": verdict,
            "next_actions": (
                [f"Address {first_errors[0].get('rule')}: "
                 f"{first_errors[0].get('message')}"] if first_errors else
                ["ecad_simulate('op')  # ERC is clean enough to solve the circuit"]
            ),
        })



    # ---------------------------------------------------------------- sync sch -> pcb

    @mcp.tool()
    def ecad_update_from_sch(sch_doc_id: str = "", pcb_doc_id: str = "") -> str:
        """Populate the active PCB from the active schematic (KiCad 'Update PCB from Schematic').

        Adds a footprint for every schematic component that carries one, binds each pad to its
        schematic net, registers missing nets, and reports parts that no longer exist on the
        schematic. Sim-only parts (V/I sources) and power symbols are skipped (they have no
        footprint). Run this AFTER the schematic is wired and BEFORE routing: the typical flow is
        update_from_sch -> ecad_autoroute -> ecad_fill_zones.

        AUTO-READBACK: response includes compact layer ASCII + net_delta + findings. READ it
        before the next place/route. Call render_board_ascii before declaring the board done.
        """
        sch_id = sch_doc_id or _ECAD["current_sch"]
        pcb_id = pcb_doc_id or _ECAD["current_pcb"]
        if sch_id is None:
            return _err("no active schematic to sync FROM",
                        hint="Build a schematic first (ecad_new_schematic, place + wire parts).",
                        next_actions=["ecad_new_schematic('mydesign')"])
        if pcb_id is None:
            return _err("no active board to sync INTO",
                        hint="Create a board first; it will be populated from the schematic.",
                        next_actions=["ecad_new_pcb('myboard')"])
        try:
            resp = _update_from_sch(ecad_session.get_session(pcb_id),
                                    ecad_session.get_session(sch_id))
        except OpError as e:
            return _err(f"update_from_sch failed: {e.message}", hint=e.message,
                        next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
        report = resp.get("report", {})
        board = resp.get("tree", {})
        compile_info = resp.get("compile", {})
        ratsnest = compile_info.get("ratsnest", []) or []
        added = report.get("added", [])
        updated = report.get("updated", [])
        unresolved = report.get("unresolved", [])
        warnings = list(resp.get("warnings", []))
        head = (f"{len(added)} footprint(s) added, {len(updated)} updated, "
                f"{report.get('nets_bound', 0)} pad-net binding(s); "
                f"{len(ratsnest)} airwire(s) now need routing.")
        next_actions: list[str] = []
        if unresolved:
            next_actions.append(
                "Resolve missing footprints (assign a real fpid in the schematic): "
                + ", ".join(f"{ref}({fpid})" for ref, fpid in unresolved))
        if ratsnest:
            next_actions.append("ecad_autoroute()  # connect the ratsnest, then ecad_fill_zones()")
        else:
            next_actions.append("ecad_run_drc()  # board populated; check spacing")
        payload = {
            "ok": True, "doc_id": pcb_id, "sch_doc_id": sch_id, "kind": "pcb",
            "revision": resp.get("revision"),
            "footprints_added": added, "footprints_updated": updated,
            "nets_bound": report.get("nets_bound", 0),
            "unresolved": unresolved,
            "footprint_count": len(board.get("footprints", [])),
            "ratsnest_remaining": len(ratsnest),
            "warnings": warnings,
            "hint": head + " Next: ecad_autoroute then ecad_fill_zones. "
                    "READ the ascii_readback before the next board edit.",
            "next_actions": next_actions,
        }
        _attach_board_ascii_readback(payload, doc_id=pcb_id)
        return _dumps(payload)
