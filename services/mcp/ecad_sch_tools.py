"""ECAD schematic authoring, edit, bus, readback, plot, and interchange MCP tools."""
from __future__ import annotations

import json
import os
from typing import Any, Union

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature

from .ecad_common import (
    OpError,
    _ECAD,
    _apply,
    _attach_board_ascii_readback,
    _attach_schematic_ascii_readback,
    _compile_of,
    _component_eng,
    _default_out_dir,
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

from .cad_common import coerce_json_or_repr, require_list, require_mapping

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

_INLINE_TEXT_LIMIT = 65536


def _net_signature(netlist: dict) -> dict[str, tuple[str, ...]]:
    """Stable net membership signature used for compact mutation deltas."""
    out: dict[str, tuple[str, ...]] = {}
    for name, members in (netlist.get("nets") or {}).items():
        pins = sorted(f"{m.get('ref', '?')}.{m.get('pin', '?')}"
                      for m in members if isinstance(m, dict))
        out[str(name)] = tuple(pins)
    return out


def _erc_violations(compile_field: Any) -> list[dict] | None:
    """ERC list from an ecad compile field, or None when the field is unavailable."""
    if not isinstance(compile_field, dict) or "erc" not in compile_field:
        return None
    value = compile_field.get("erc")
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("violations"), list):
        return value["violations"]
    return []


def _apply_schematic_tool(session, op: dict, *, action: str,
                          focus: list[float] | None = None,
                          extra: dict | None = None,
                          next_actions: list[str] | None = None) -> dict:
    """Apply one schematic op and attach deterministic net/ERC deltas plus ASCII readback."""
    readback_warnings: list[str] = []
    try:
        before_nl = netlist_mod.build_netlist(session.doc)
        before_sig = _net_signature(before_nl)
    except Exception as e:  # noqa: BLE001 — readback must never block the requested mutation
        before_sig = {}
        readback_warnings.append(
            f"pre-mutation net readback unavailable: {type(e).__name__}: {e}")
    before_erc = _erc_violations(session.last_compile)
    resp = _apply(session, [op])
    readback_warnings.extend(resp.get("warnings") or [])
    try:
        after_nl = netlist_mod.build_netlist(session.doc)
        after_sig = _net_signature(after_nl)
        unconnected = _unconnected_pins(session.doc, after_nl)
    except Exception as e:  # noqa: BLE001 — mutation succeeded; surface degraded readback
        after_nl = {}
        after_sig = {}
        unconnected = []
        readback_warnings.append(
            f"post-mutation net readback unavailable: {type(e).__name__}: {e}")
    after_erc = _erc_violations(session.last_compile)
    erc_error = ""
    if after_erc is None:
        try:
            after_erc = _erc_violations(_compile_of(session))
        except Exception as e:  # noqa: BLE001 — mutation succeeded; readback is best-effort
            erc_error = f"ERC readback unavailable: {type(e).__name__}: {e}"
            readback_warnings.append(erc_error)

    added = sorted(set(after_sig) - set(before_sig))
    removed = sorted(set(before_sig) - set(after_sig))
    changed = sorted(name for name in set(before_sig) & set(after_sig)
                     if before_sig[name] != after_sig[name])
    before_erc_summary = _erc_summary(before_erc or [])
    after_erc_summary = _erc_summary(after_erc or [])
    payload: dict = {
        "ok": True,
        "doc_id": session.doc_id,
        "revision": session.revision,
        "action": action,
        "net_readback": {
            "net_count": len(after_sig),
            "unconnected_count": len(unconnected),
            "unconnected_pins": [_pretty_pin(p) for p in unconnected[:12]],
            "delta": {
                "added": added[:20],
                "removed": removed[:20],
                "changed": changed[:20],
                "truncated": any(len(v) > 20 for v in (added, removed, changed)),
            },
        },
        "erc_available": after_erc is not None,
        "erc": after_erc_summary,
        "erc_delta": {
            "errors": (after_erc_summary["errors"] - before_erc_summary["errors"]
                       if after_erc is not None else None),
            "warnings": (after_erc_summary["warnings"] - before_erc_summary["warnings"]
                         if after_erc is not None else None),
        },
        "warnings": readback_warnings,
        "hint": (f"{action} applied. Read net_readback and erc_delta before the next edit."
                 + (f" {erc_error}" if erc_error else "")),
        "next_actions": next_actions or [
            "render_schematic_ascii()  # inspect the changed sheet",
            "ecad_lint_schematic()  # check drawing geometry",
            "ecad_run_erc()  # check electrical rules",
        ],
    }
    created_id = (resp.get("id_map") or {}).get("$0")
    if created_id:
        payload["id"] = created_id
    if extra:
        payload.update(extra)
    _attach_schematic_ascii_readback(payload, doc_id=session.doc_id, focus=focus)
    return payload


def _export_text(session, kind: str, *, key: str | None = None,
                 opts: dict | None = None) -> str:
    """Fetch one text artifact from the C++ ecad_export ``files`` envelope."""
    request: dict = {"kind": kind, "doc_id": session.doc_id}
    if opts:
        request["opts"] = opts
    resp = _rpc("ecad_export", request)
    files = resp.get("files") or {}
    if not isinstance(files, dict):
        raise ValueError(f"ecad_export({kind!r}) returned a non-object files field")
    if key and isinstance(files.get(key), str):
        return files[key]
    strings = [(str(k), v) for k, v in sorted(files.items()) if isinstance(v, str)]
    if len(strings) == 1:
        return strings[0][1]
    raise ValueError(
        f"ecad_export({kind!r}) returned no {key or 'single text'} artifact "
        f"(keys: {', '.join(str(k) for k in sorted(files)) or '(none)'})")


def _artifact_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _write_artifact(path: str, content: str, *, binary: bool = False) -> tuple[str, int]:
    """Write returned C++ text client-side and confirm the bytes landed."""
    target = _artifact_path(path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    if binary:
        data = content.encode("latin-1")
        with open(target, "wb") as fh:
            fh.write(data)
    else:
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
    return target, os.path.getsize(target) if os.path.exists(target) else 0


def _inline_text(text: str) -> tuple[str, bool]:
    if len(text) <= _INLINE_TEXT_LIMIT:
        return text, False
    return text[:_INLINE_TEXT_LIMIT], True

def register_ecad_sch_tools(mcp: FastMCP) -> None:
    """Register the complete schematic authoring/readback/export surface."""

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
        from services.ecad.library import resolve as lib_resolve  # STAYS: no ecad_library RPC; pin-count lookup
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
        # Agent placement is strict even though document import/paste is resilient: accept a
        # Python-library definition (the richer source) or an exact native-catalog libid, and
        # refuse a model guess before the core's visible generic-body recovery path can run.
        from services.ecad import library as _lib  # Python project/vendor libraries remain valid
        resolved_def = _lib.resolve(libid)
        native_match = None
        if resolved_def is None:
            try:
                catalog = _rpc("ecad_symbols", {"query": libid, "limit": 100})
                native_match = next(
                    (row for row in (catalog.get("symbols") or [])
                     if isinstance(row, dict) and row.get("libid") == libid),
                    None,
                )
            except Exception as e:  # noqa: BLE001
                return _rs.err_envelope(
                    f"native symbol catalog unavailable: {type(e).__name__}: {e}",
                    reason="symbol_catalog_unavailable",
                    hint="Do not place a guessed symbol while the native catalog is unavailable.",
                    next_actions=[f"ecad_symbols(query='{libid.split(':')[-1]}')"],
                    libid=libid,
                )
        if resolved_def is None and native_match is None:
            keyword = libid.split(":")[-1] if ":" in libid else libid
            return _rs.err_envelope(
                f"unknown library symbol {libid!r}",
                reason="unknown_libid",
                hint=(f"'{libid}' is in neither the project/Python library nor the native "
                      f"catalog. Run ecad_symbols(query='{keyword}') and use an exact hit."),
                next_actions=[
                    f"ecad_symbols(query='{keyword}')",
                    f"ecad_find_part('{keyword}')",
                ],
                libid=libid,
            )
        op: dict = {"op": "sch_add_symbol", "libid": libid,
                    "pos": [float(spec["x"]), float(spec["y"])],
                    "angle": float(spec["angle"])}
        if resolved_def is not None:
            op["def"] = resolved_def
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



    # ---------------------------------------------------------------- edit / annotate / primitives

    @mcp.tool()
    def ecad_edit_symbol(ref_or_id: str, value: str | None = None,
                         ref: str | None = None, footprint: str | None = None,
                         dnp: bool | None = None, in_bom: bool | None = None,
                         on_board: bool | None = None, datasheet: str | None = None,
                         unit: int | None = None, angle: float | None = None,
                         mirror: str | None = None, x: float | None = None,
                         y: float | None = None, fields: Any = None,
                         doc_id: str = "") -> str:
        """Edit a placed symbol by reference designator (``R1``) or internal id (``s1``).

        Omitted fields are preserved. ``x``/``y`` may be supplied independently; ``fields`` is
        the complete user-fields object. Returns the edited symbol plus net/ERC and ASCII readback.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, reason=e.code, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        sym = _resolve_ref_or_id(session.doc, str(ref_or_id))
        if sym is None:
            refs = sorted(str(s.get("ref")) for s in session.doc.get("symbols", [])
                          if s.get("ref"))
            return _err(f"no symbol {ref_or_id!r} on the schematic",
                        reason="symbol_not_found",
                        hint=f"Placed refs are: {', '.join(refs) or '(none)'}.",
                        next_actions=["ecad_status(detail='list', include='pins')"],
                        placed_refs=refs[:100])
        op: dict = {"op": "sch_edit_symbol", "id": sym.get("id")}
        for key, supplied in (
            ("value", value), ("ref", ref), ("footprint", footprint),
            ("dnp", dnp), ("in_bom", in_bom), ("on_board", on_board),
            ("datasheet", datasheet), ("unit", unit), ("angle", angle),
            ("mirror", mirror),
        ):
            if supplied is not None:
                op[key] = supplied
        if x is not None or y is not None:
            old = sym.get("pos") or [0.0, 0.0]
            try:
                op["pos"] = [float(old[0] if x is None else x),
                             float(old[1] if y is None else y)]
            except (TypeError, ValueError) as e:
                return _err(f"invalid symbol coordinates: {e}", reason="invalid_coordinates",
                            hint="x and y are finite schematic millimetres.",
                            next_actions=[f"ecad_edit_symbol('{ref_or_id}', x=0, y=0)"])
        if fields is not None:
            try:
                op["fields"] = require_mapping(fields, "fields", "a JSON object of user fields")
            except ValueError as e:
                return _err(str(e), reason="invalid_fields",
                            hint="Pass fields={'Manufacturer': '...', 'MPN': '...'} or omit it.",
                            next_actions=[f"ecad_edit_symbol('{ref_or_id}', value='new value')"])
        if len(op) == 2:
            return _err("no symbol edits were supplied", reason="no_changes",
                        hint="Set at least one of value/ref/footprint/dnp/in_bom/on_board/"
                             "datasheet/unit/angle/mirror/x/y/fields.",
                        next_actions=[f"ecad_edit_symbol('{ref_or_id}', value='new value')"])
        try:
            payload = _apply_schematic_tool(
                session, op, action=f"edited symbol {sym.get('ref') or sym.get('id')}",
                focus=op.get("pos") or list(sym.get("pos") or []),
                extra={"id": sym.get("id")},
            )
        except OpError as e:
            return _err(f"could not edit symbol {ref_or_id!r}: {e.message}",
                        reason=e.code, hint=e.message, detail=e.detail,
                        next_actions=["ecad_status(detail='list', include='pins')"])
        edited = _resolve_ref_or_id(session.doc, str(sym.get("id")))
        if edited:
            payload.update({
                "ref": edited.get("ref"),
                "value": edited.get("value"),
                "footprint": edited.get("footprint") or "",
                "pos": edited.get("pos"),
                "angle": edited.get("angle", 0.0),
                "mirror": edited.get("mirror", "none"),
                "pins": _placed_pins(session.doc, edited),
            })
        return _dumps(payload)

    @mcp.tool()
    def ecad_delete(id_or_ref: str, doc_id: str = "") -> str:
        """Delete any schematic entity by id, or delete a symbol by reference designator."""
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, reason=e.code, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        token = str(id_or_ref or "").strip()
        if not token:
            return _err("id_or_ref is required", reason="missing_entity",
                        hint="Pass an id from render_schematic_ascii's index or a symbol ref.",
                        next_actions=["render_schematic_ascii()"])
        collections = (
            ("symbols", "symbol", "sch_delete_symbol"),
            ("wires", "wire", "sch_delete_wire"),
            ("buses", "bus", "sch_delete_bus"),
            ("bus_entries", "bus_entry", "sch_delete_bus_entry"),
            ("junctions", "junction", "sch_delete_junction"),
            ("labels", "label", "sch_delete_label"),
            ("power", "power", "sch_delete_power"),
            ("no_connect", "no_connect", "sch_delete_no_connect"),
            ("texts", "text", "sch_delete_text"),
            ("graphics", "graphic", "sch_delete_graphic"),
            ("sheet_symbols", "sheet_symbol", "sch_delete_sheet_symbol"),
            ("sheets", "sheet", "sch_delete_sheet"),
        )
        found: dict | None = _resolve_ref_or_id(session.doc, token)
        found_kind = "symbol" if found is not None else ""
        delete_op = "sch_delete_symbol" if found is not None else ""
        if found is None:
            for key, kind, opname in collections[1:]:
                entity = next((v for v in session.doc.get(key, []) or []
                               if isinstance(v, dict) and str(v.get("id")) == token), None)
                if entity is not None:
                    found, found_kind, delete_op = entity, kind, opname
                    break
        if found is None:
            for sheet_symbol in session.doc.get("sheet_symbols", []) or []:
                for pin in sheet_symbol.get("pins", []) or []:
                    if isinstance(pin, dict) and str(pin.get("id")) == token:
                        found, found_kind, delete_op = pin, "sheet_pin", "sch_delete_sheet_pin"
                        break
                if found is not None:
                    break
        if found is None:
            known = sorted(
                str(entity.get("id"))
                for key, _, _ in collections
                for entity in (session.doc.get(key, []) or [])
                if isinstance(entity, dict) and entity.get("id")
            )
            return _err(f"no schematic entity {token!r}", reason="entity_not_found",
                        hint="Use render_schematic_ascii() for the bounded entity index.",
                        next_actions=["render_schematic_ascii()", "ecad_status(detail='list')"],
                        known_ids=known[:100], known_ids_truncated=len(known) > 100)
        try:
            payload = _apply_schematic_tool(
                session, {"op": delete_op, "id": found.get("id")},
                action=f"deleted {found_kind} {found.get('ref') or found.get('id')}",
                focus=list(found.get("pos") or []),
                extra={"deleted_id": found.get("id"), "deleted_kind": found_kind,
                       "deleted_ref": found.get("ref")},
            )
        except OpError as e:
            return _err(f"could not delete {found_kind} {token!r}: {e.message}",
                        reason=e.code, hint=e.message, detail=e.detail,
                        next_actions=["render_schematic_ascii()"])
        return _dumps(payload)

    @mcp.tool()
    def ecad_annotate(scope: str = "all", mode: str = "keep",
                      sheet: str | None = None, doc_id: str = "") -> str:
        """Assign reference designators with ``sch_annotate``.

        ``scope`` is ``'all'`` or ``'sheet'``; ``mode='keep'`` preserves existing refs while
        ``mode='reset'`` renumbers the selected scope. Supply ``sheet`` for sheet scope.
        """
        scope = str(scope or "all").lower()
        mode = str(mode or "keep").lower()
        if scope not in ("all", "sheet") or mode not in ("keep", "reset"):
            return _err("invalid annotation scope or mode", reason="invalid_annotation",
                        hint="Use scope='all'|'sheet' and mode='keep'|'reset'.",
                        next_actions=["ecad_annotate(scope='all', mode='keep')"])
        if scope == "sheet" and not sheet:
            return _err("sheet scope requires sheet=<sheet id>", reason="missing_sheet",
                        hint="Use the sheet id shown by ecad_status/render_schematic_ascii.",
                        next_actions=["ecad_annotate(scope='all', mode='keep')"])
        try:
            session = _get_sch(doc_id or None)
            op: dict = {"op": "sch_annotate", "scope": scope, "mode": mode}
            if sheet:
                op["sheet"] = str(sheet)
            payload = _apply_schematic_tool(
                session, op, action=f"annotated {scope} ({mode})",
                extra={"scope": scope, "mode": mode, "sheet": sheet},
            )
            refs = sorted(str(s.get("ref")) for s in session.doc.get("symbols", [])
                          if s.get("ref"))
            payload.update({"refs": refs[:200], "refs_truncated": len(refs) > 200})
        except OpError as e:
            return _err(f"could not annotate schematic: {e.message}",
                        reason=e.code, hint=e.message, detail=e.detail,
                        next_actions=["ecad_status(detail='list')"])
        return _dumps(payload)

    def _point_mutation(opname: str, noun: str, x: float, y: float,
                        doc_id: str) -> str:
        try:
            session = _get_sch(doc_id or None)
            pos = [float(x), float(y)]
            payload = _apply_schematic_tool(
                session, {"op": opname, "pos": pos}, action=f"added {noun}",
                focus=pos, extra={"pos": pos},
            )
        except OpError as e:
            return _err(f"could not add {noun}: {e.message}", reason=e.code,
                        hint=e.message, detail=e.detail,
                        next_actions=["render_schematic_ascii()"])
        except (TypeError, ValueError) as e:
            return _err(f"invalid {noun} coordinates: {e}", reason="invalid_coordinates",
                        hint="x and y are finite schematic millimetres.",
                        next_actions=["render_schematic_ascii()"])
        return _dumps(payload)

    @mcp.tool()
    def ecad_no_connect(x: float, y: float, doc_id: str = "") -> str:
        """Mark the pin at exact world ``(x, y)`` intentionally unconnected."""
        return _point_mutation("sch_add_no_connect", "no-connect marker", x, y, doc_id)

    @mcp.tool()
    def ecad_junction(x: float, y: float, doc_id: str = "") -> str:
        """Add an electrical junction at exact world ``(x, y)``."""
        return _point_mutation("sch_add_junction", "junction", x, y, doc_id)

    @mcp.tool()
    def ecad_bus(name: str, points: Any, doc_id: str = "") -> str:
        """Add a named bus polyline. ``points`` is ``[[x,y], ...]`` with at least two points."""
        try:
            pts_in = require_list(points, "points", "an array of at least two [x,y] points")
            pts = [[float(p[0]), float(p[1])] for p in pts_in
                   if isinstance(p, (list, tuple)) and len(p) == 2]
        except (ValueError, TypeError, IndexError) as e:
            return _err(f"invalid bus points: {e}", reason="invalid_points",
                        hint="Pass points=[[0,0],[25.4,0]] in schematic mm.",
                        next_actions=["ecad_bus('D[0..7]', [[0,0],[25.4,0]])"])
        if len(pts) != len(pts_in) or len(pts) < 2 or not str(name or "").strip():
            return _err("bus needs a non-empty name and at least two valid [x,y] points",
                        reason="invalid_bus",
                        hint="Example: ecad_bus('D[0..7]', [[0,0],[25.4,0]]).",
                        next_actions=["ecad_bus('D[0..7]', [[0,0],[25.4,0]])"])
        try:
            session = _get_sch(doc_id or None)
            op = {"op": "sch_add_bus", "name": str(name).strip(), "pts": pts}
            payload = _apply_schematic_tool(
                session, op, action=f"added bus {str(name).strip()}",
                focus=pts[0], extra={"name": str(name).strip(), "points": pts},
            )
        except OpError as e:
            return _err(f"could not add bus {name!r}: {e.message}", reason=e.code,
                        hint=e.message, detail=e.detail,
                        next_actions=["ecad_lint_schematic()"])
        return _dumps(payload)

    @mcp.tool()
    def ecad_bus_entry(x: float, y: float, bus: str | None = None,
                       member: str | None = None, doc_id: str = "") -> str:
        """Add a bus entry at ``(x,y)``; ``bus`` names its bus and ``member`` one expanded net."""
        try:
            session = _get_sch(doc_id or None)
            pos = [float(x), float(y)]
            op: dict = {"op": "sch_add_bus_entry", "pos": pos}
            if bus is not None:
                op["bus"] = str(bus)
            if member is not None:
                op["member"] = str(member)
            payload = _apply_schematic_tool(
                session, op, action="added bus entry", focus=pos,
                extra={"pos": pos, "bus": bus, "member": member},
                next_actions=["ecad_lint_schematic()  # catches unresolved entries",
                              "ecad_netlist()  # confirm member net binding",
                              "render_schematic_ascii()"],
            )
        except OpError as e:
            return _err(f"could not add bus entry: {e.message}", reason=e.code,
                        hint=e.message, detail=e.detail,
                        next_actions=["ecad_lint_schematic()"])
        except (TypeError, ValueError) as e:
            return _err(f"invalid bus entry coordinates: {e}", reason="invalid_coordinates",
                        hint="x and y are finite schematic millimetres.",
                        next_actions=["render_schematic_ascii()"])
        return _dumps(payload)

    @mcp.tool()
    def ecad_note(x: float, y: float, text: str, size: float = 1.27,
                  doc_id: str = "") -> str:
        """Add non-electrical schematic text at ``(x,y)`` in mm."""
        if not str(text or ""):
            return _err("note text is required", reason="missing_text",
                        hint="Pass text='Decoupling: place close to U1'.",
                        next_actions=["ecad_note(0, 0, 'Design note')"])
        try:
            session = _get_sch(doc_id or None)
            pos = [float(x), float(y)]
            payload = _apply_schematic_tool(
                session, {"op": "sch_add_text", "pos": pos,
                          "text": str(text), "size": float(size)},
                action="added schematic note", focus=pos,
                extra={"pos": pos, "text": str(text), "size": float(size)},
            )
        except OpError as e:
            return _err(f"could not add schematic note: {e.message}", reason=e.code,
                        hint=e.message, detail=e.detail,
                        next_actions=["render_schematic_ascii()"])
        except (TypeError, ValueError) as e:
            return _err(f"invalid note position or size: {e}", reason="invalid_coordinates",
                        hint="x, y, and size are numeric schematic millimetres.",
                        next_actions=["ecad_note(0, 0, 'Design note')"])
        return _dumps(payload)

    # ---------------------------------------------------------------- compiled outputs / interchange

    @mcp.tool()
    def ecad_netlist(doc_id: str = "", fmt: str = "summary",
                     path: str | None = None) -> str:
        """Read the compiled schematic netlist.

        ``fmt='summary'`` returns bounded net names and pin counts. ``fmt='json'`` returns the
        full compiled JSON netlist (and writes it when ``path`` is supplied). ``fmt='kicad'``
        requests the KiCad-style netlist writer and optionally writes its returned text to
        ``path`` client-side.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, reason=e.code, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        dialect = str(fmt or "summary").lower()
        nl: dict | None = None
        if dialect in ("summary", "json", "full"):
            try:
                nl = netlist_mod.build_netlist(session.doc)
            except Exception as e:  # noqa: BLE001
                return _err(f"netlist compile failed: {type(e).__name__}: {e}",
                            reason="netlist_compile_failed",
                            hint="Run ecad_status() and fix the reported document/compile error.",
                            next_actions=["ecad_status()", "ecad_run_erc()"])
        if dialect == "summary":
            assert nl is not None
            rows = [{"name": str(name), "pin_count": len(members)}
                    for name, members in sorted((nl.get("nets") or {}).items())]
            cap = 100
            all_buses = sorted((nl.get("buses") or {}).items())
            buses = {str(name): list(members)[:100] for name, members in all_buses[:50]}
            buses_truncated = (len(all_buses) > 50
                               or any(len(members) > 100 for _, members in all_buses[:50]))
            return _dumps({
                "ok": True, "doc_id": session.doc_id, "revision": session.revision,
                "fmt": "summary", "component_count": len(nl.get("components") or []),
                "net_count": len(rows), "nets": rows[:cap],
                "truncated": len(rows) > cap or buses_truncated,
                "omitted_nets": max(0, len(rows) - cap),
                "buses": buses, "buses_truncated": buses_truncated,
                "warnings": list(nl.get("warnings") or []),
                "hint": (f"Compiled {len(rows)} net(s). Use fmt='json' for full pin/component "
                         "records or fmt='kicad' for an interchange netlist."),
                "next_actions": ["ecad_run_erc()", "ecad_bom()", "ecad_plot(fmt='pdf')"],
            })
        if dialect in ("json", "full"):
            assert nl is not None
            try:
                text = _dumps(nl)
                artifact = None
                size = len(text.encode("utf-8"))
                spilled = False
                if path:
                    artifact, size = _write_artifact(str(path), text)
                elif len(text) > _INLINE_TEXT_LIMIT:
                    out_dir = str(_default_out_dir(session.doc.get("name", "schematic")))
                    artifact, size = _write_artifact(
                        os.path.join(out_dir, "netlist.json"), text)
                    spilled = True
            except Exception as e:  # noqa: BLE001
                return _err(f"netlist output failed: {type(e).__name__}: {e}",
                            reason="netlist_output_failed",
                            hint="Check the output path or omit path to return JSON inline.",
                            next_actions=["ecad_netlist(fmt='summary')"])
            return _dumps({
                "ok": True, "doc_id": session.doc_id, "revision": session.revision,
                "fmt": "json", "netlist": nl if artifact is None else None,
                "path": artifact, "bytes": size, "auto_spilled": spilled,
                "warnings": (list(nl.get("warnings") or [])
                             + (["full netlist exceeded the inline bound and was written to path"]
                                if spilled else [])),
                "hint": (f"Full compiled netlist {'written to ' + artifact if artifact else 'returned'} "
                         f"({len(nl.get('nets') or {})} nets, {len(nl.get('components') or [])} "
                         "components)."),
                "next_actions": ["ecad_run_erc()", "ecad_bom()", "ecad_plot(fmt='pdf')"],
            })
        if dialect != "kicad":
            return _err(f"unknown netlist format {fmt!r}", reason="invalid_format",
                        hint="Use fmt='summary', 'json', or 'kicad'.",
                        next_actions=["ecad_netlist(fmt='summary')"])
        try:
            text = _export_text(session, "netlist", key="netlist")
            artifact = None
            size = len(text.encode("utf-8"))
            if path:
                artifact, size = _write_artifact(str(path), text)
            inline, truncated = _inline_text(text)
        except Exception as e:  # noqa: BLE001
            return _err(f"schematic netlist export failed: {type(e).__name__}: {e}",
                        reason="export_failed",
                        hint="Run ecad_status() and ecad_run_erc(), then retry.",
                        next_actions=["ecad_status()", "ecad_run_erc()"])
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "fmt": "kicad",
            "path": artifact, "text": None if artifact else inline,
            "text_truncated": False if artifact else truncated, "bytes": size,
            "warnings": (["inline netlist truncated; pass path= to write the full artifact"]
                         if truncated and not artifact else []),
            "hint": (f"KiCad-style netlist {'written to ' + artifact if artifact else 'returned'} "
                     f"({size} byte(s))."),
            "next_actions": ["ecad_bom()", "ecad_export_kicad('design.kicad_sch')"],
        })

    @mcp.tool()
    def ecad_bom(doc_id: str = "", fmt: str = "csv", exclude_dnp: bool = True,
                 path: str | None = None) -> str:
        """Generate a schematic BOM as ``csv`` or ``json``; optionally write it to ``path``."""
        dialect = str(fmt or "csv").lower()
        if dialect not in ("csv", "json"):
            return _err(f"unknown BOM format {fmt!r}", reason="invalid_format",
                        hint="Use fmt='csv' or fmt='json'.",
                        next_actions=["ecad_bom(fmt='csv')"])
        try:
            session = _get_sch(doc_id or None)
            text = _export_text(session, "bom", key="bom",
                                opts={"fmt": dialect, "exclude_dnp": bool(exclude_dnp)})
            artifact = None
            size = len(text.encode("utf-8"))
            if path:
                artifact, size = _write_artifact(str(path), text)
            inline, truncated = _inline_text(text)
        except OpError as e:
            return _err(e.message, reason=e.code, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        except Exception as e:  # noqa: BLE001
            return _err(f"schematic BOM export failed: {type(e).__name__}: {e}",
                        reason="export_failed",
                        hint="Confirm the schematic has annotated components, then retry.",
                        next_actions=["ecad_annotate()", "ecad_status()"])
        if dialect == "csv":
            rows = max(0, text.count("\n") - 1)
        else:
            try:
                parsed_bom = json.loads(text)
                if isinstance(parsed_bom, list):
                    rows = len(parsed_bom)
                elif isinstance(parsed_bom, dict):
                    groups = parsed_bom.get("groups", parsed_bom.get("items", []))
                    rows = len(groups) if isinstance(groups, list) else 0
                else:
                    rows = 0
            except (TypeError, ValueError):
                rows = 0
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "fmt": dialect,
            "exclude_dnp": bool(exclude_dnp), "path": artifact,
            "text": None if artifact else inline,
            "text_truncated": False if artifact else truncated,
            "rows": rows, "bytes": size,
            "warnings": (["inline BOM truncated; pass path= to write the full artifact"]
                         if truncated and not artifact else []),
            "hint": (f"Schematic BOM {'written to ' + artifact if artifact else 'returned'} "
                     f"({rows} row/group(s), {size} byte(s))."),
            "next_actions": ["ecad_plot(fmt='pdf')", "ecad_export_kicad('design.kicad_sch')"],
        })

    @mcp.tool()
    def ecad_plot(doc_id: str = "", fmt: str = "svg", path: str | None = None,
                  sheet: str | None = None, title_block: bool = True) -> str:
        """Plot the schematic to vector SVG or PDF and return confirmed artifact path(s)."""
        dialect = str(fmt or "svg").lower()
        if dialect not in ("svg", "pdf"):
            return _err(f"unknown plot format {fmt!r}", reason="invalid_format",
                        hint="Use fmt='svg' or fmt='pdf'.",
                        next_actions=["ecad_plot(fmt='pdf')"])
        try:
            session = _get_sch(doc_id or None)
            resp = _rpc("ecad_export", {
                "kind": dialect, "doc_id": session.doc_id,
                "opts": {"sheet": str(sheet or ""), "title_block": bool(title_block)},
            })
            files = resp.get("files") or {}
            items = [(str(k), value) for k, value in sorted(files.items())
                     if isinstance(value, str)]
            if not items:
                raise ValueError(f"ecad_export({dialect!r}) returned no text artifacts")
            default_dir = str(_default_out_dir(session.doc.get("name", "schematic")))
            name = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                           for ch in str(session.doc.get("name") or "schematic"))
            requested = _artifact_path(str(path)) if path else ""
            written: list[str] = []
            sizes: dict[str, int] = {}
            for index, (key, content) in enumerate(items):
                if len(items) == 1:
                    target = requested or os.path.join(default_dir, f"{name}.{dialect}")
                    if (target.endswith(os.sep) or os.path.isdir(target)
                            or (requested and not os.path.splitext(target)[1])):
                        target = os.path.join(target, f"{name}.{dialect}")
                elif requested and os.path.splitext(requested)[1].lower() == f".{dialect}":
                    stem, ext = os.path.splitext(requested)
                    target = requested if index == 0 else f"{stem}-{key}{ext}"
                else:
                    out_dir = requested or default_dir
                    target = os.path.join(out_dir, f"{name}-{key}.{dialect}")
                written_path, byte_count = _write_artifact(
                    target, content, binary=dialect == "pdf")
                written.append(written_path)
                sizes[written_path] = byte_count
            _ECAD["out_dir"] = os.path.dirname(written[0])
        except OpError as e:
            return _err(e.message, reason=e.code, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        except Exception as e:  # noqa: BLE001
            return _err(f"schematic plot failed: {type(e).__name__}: {e}",
                        reason="export_failed",
                        hint="Run ecad_lint_schematic() and confirm the selected sheet id.",
                        next_actions=["ecad_lint_schematic()", "render_schematic_ascii()"])
        total = sum(sizes.values())
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "fmt": dialect,
            "sheet": sheet, "title_block": bool(title_block),
            "path": written[0], "paths": written, "page_count": len(written),
            "bytes": total, "file_bytes": sizes, "exists": all(n > 0 for n in sizes.values()),
            "warnings": [],
            "hint": (f"Wrote {len(written)} vector {dialect.upper()} page(s) to "
                     f"{written[0]} ({total} byte(s) total), confirmed on disk."),
            "next_actions": ["ecad_bom()", "ecad_export_kicad('design.kicad_sch')"],
        })

    @mcp.tool()
    def ecad_import_kicad(path: str, mode: str = "merge", doc_id: str = "") -> str:
        """Import a KiCad ``.kicad_sch`` file into the active schematic."""
        import_mode = str(mode or "merge").lower()
        if import_mode not in ("merge", "replace"):
            return _err(f"unknown import mode {mode!r}", reason="invalid_mode",
                        hint="Use mode='merge' or mode='replace'.",
                        next_actions=["ecad_import_kicad('design.kicad_sch', mode='merge')"])
        source = _artifact_path(str(path))
        if not os.path.isfile(source):
            return _err(f"KiCad schematic does not exist: {source}",
                        reason="file_not_found", hint="Pass an existing .kicad_sch path.",
                        next_actions=["ecad_import_kicad('/absolute/path/design.kicad_sch')"])
        try:
            session = _get_sch(doc_id or None)
            payload = _apply_schematic_tool(
                session, {"op": "sch_import_kicad", "path": source, "mode": import_mode},
                action=f"imported KiCad schematic ({import_mode})",
                extra={"source_path": source, "mode": import_mode},
            )
        except OpError as e:
            return _err(f"KiCad schematic import failed: {e.message}", reason=e.code,
                        hint=e.message, detail=e.detail,
                        next_actions=["ecad_status()", "ecad_import_kicad(path, mode='replace')"])
        return _dumps(payload)

    @mcp.tool()
    def ecad_export_kicad(path: str, doc_id: str = "") -> str:
        """Export the active schematic as a KiCad 7 ``.kicad_sch`` file."""
        if not str(path or "").strip():
            return _err("path is required", reason="missing_path",
                        hint="Pass a .kicad_sch output path.",
                        next_actions=["ecad_export_kicad('design.kicad_sch')"])
        try:
            session = _get_sch(doc_id or None)
            text = _export_text(session, "kicad", key="kicad")
            artifact, size = _write_artifact(str(path), text)
        except OpError as e:
            return _err(e.message, reason=e.code, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        except Exception as e:  # noqa: BLE001
            return _err(f"KiCad schematic export failed: {type(e).__name__}: {e}",
                        reason="export_failed",
                        hint="Run ecad_status() and ecad_annotate(), then retry.",
                        next_actions=["ecad_status()", "ecad_annotate()"])
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "kind": "kicad",
            "path": artifact, "bytes": size, "exists": size > 0, "warnings": [],
            "hint": f"Wrote KiCad schematic to {artifact} ({size} byte(s)), confirmed on disk.",
            "next_actions": ["ecad_plot(fmt='pdf')", "ecad_bom()"],
        })

    @mcp.tool()
    def ecad_symbols(query: str = "", limit: int = 20) -> str:
        """Discover native schematic symbols instead of guessing libids."""
        try:
            bounded_limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            return _err(f"invalid symbol limit {limit!r}", reason="invalid_limit",
                        hint="Pass an integer from 1 to 100.",
                        next_actions=["ecad_symbols('resistor', limit=20)"])
        try:
            resp = _rpc("ecad_symbols", {"query": str(query or ""),
                                         "limit": bounded_limit})
        except Exception as e:  # noqa: BLE001
            return _err(f"symbol catalog unavailable: {type(e).__name__}: {e}",
                        reason="rpc_failed",
                        hint="The native symbol-library RPC must be available.",
                        next_actions=["ecad_find_part('resistor')"])
        symbols = resp.get("symbols") or []
        if not isinstance(symbols, list):
            return _err("symbol catalog returned an invalid payload",
                        reason="invalid_rpc_response",
                        hint="Retry ecad_symbols or use ecad_find_part.",
                        next_actions=["ecad_find_part('resistor')"])
        symbols = symbols[:bounded_limit]
        return _dumps({
            "ok": True, "query": str(query or ""), "limit": bounded_limit,
            "count": len(symbols), "symbols": symbols,
            "warnings": list(resp.get("warnings") or []),
            "hint": (f"Found {len(symbols)} native symbol(s). Feed an exact libid to "
                     "ecad_place_symbol; never guess one."),
            "next_actions": ([f"ecad_place_symbol('{symbols[0].get('libid')}', x=0, y=0)"
                              if symbols and isinstance(symbols[0], dict)
                              and symbols[0].get("libid") else
                              "ecad_symbols('resistor')"]),
        })

    @mcp.tool()
    def ecad_load_symbol_library(path: str) -> str:
        """Load every symbol from a KiCad ``.kicad_sym`` library into the native catalog.

        Reach for this when you were given a vendor .kicad_sym and need a symbol the native
        catalog lacks. After loading it, use ecad_symbols to discover the exact imported libid
        before placing the symbol.
        """
        requested = str(path or "").strip()
        if not requested:
            return _err("path is required", reason="missing_path",
                        hint="Pass the existing vendor .kicad_sym file you were given.",
                        next_actions=[
                            "ecad_load_symbol_library('/absolute/path/vendor.kicad_sym')"])
        source = _artifact_path(requested)
        if os.path.splitext(source)[1].lower() != ".kicad_sym":
            return _err(f"not a KiCad symbol-library path: {source}",
                        reason="invalid_file_type",
                        hint="Pass a file ending in .kicad_sym; schematic files use "
                             "ecad_import_kicad instead.",
                        next_actions=[
                            "ecad_load_symbol_library('/absolute/path/vendor.kicad_sym')"])
        if not os.path.isfile(source):
            return _err(f"KiCad symbol library does not exist: {source}",
                        reason="file_not_found",
                        hint="Pass an existing .kicad_sym path.",
                        next_actions=[
                            "ecad_load_symbol_library('/absolute/path/vendor.kicad_sym')"])
        try:
            resp = _rpc("ecad_load_symbol_library", {"path": source})
        except OpError as e:
            return _err(f"KiCad symbol-library import failed: {e.message}",
                        reason=e.code, detail=e.detail,
                        hint="Confirm this is a readable KiCad .kicad_sym library.",
                        next_actions=["ecad_symbols()"])
        except Exception as e:  # noqa: BLE001
            return _err(f"symbol-library RPC unavailable: {type(e).__name__}: {e}",
                        reason="rpc_failed",
                        hint="The native symbol-library RPC must be available.",
                        next_actions=["ecad_symbols()"])
        if not isinstance(resp, dict):
            return _err("symbol-library import returned an invalid payload",
                        reason="invalid_rpc_response",
                        hint="Retry the import, then inspect the native catalog.",
                        next_actions=["ecad_symbols()"])
        if resp.get("ok") is False:
            return _err(str(resp.get("error") or "KiCad symbol-library import failed"),
                        reason=str(resp.get("reason") or "import_failed"),
                        hint="Confirm this is a readable KiCad .kicad_sym library.",
                        warnings=list(resp.get("warnings") or []),
                        next_actions=["ecad_symbols()"])
        added = resp.get("added") or []
        if not isinstance(added, list):
            return _err("symbol-library import returned an invalid added-symbol list",
                        reason="invalid_rpc_response",
                        hint="Retry the import, then inspect the native catalog.",
                        next_actions=["ecad_symbols()"])
        warnings = list(resp.get("warnings") or [])
        return _dumps({
            "ok": True, "path": source, "added_count": len(added), "added": added,
            "warnings": warnings,
            "hint": (f"Loaded {len(added)} symbol(s) from {source}. "
                     "Use ecad_symbols to discover an exact imported libid."),
            "next_actions": ["ecad_symbols()"],
        })


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
                               # Per-component power (honest nulls when unknown —
                               # contracts/ecad/boardphys.md)
                               "power_w": eng["power_w"] if eng else {"typ": None, "max": None}})
        power = _power_totals(comp_engs)
        nets = []
        for name, members in sorted(nl.get("nets", {}).items()):
            nets.append({"name": name, "size": len(members),
                         "members": [f"{m.get('ref')}.{m.get('pin')}" for m in members]})
        # ERC (from the C++ session compile — failure is visible and can never mean "clean")
        violations: list[dict] = []
        erc_available = True
        status_warnings = list(nl.get("warnings", []))
        try:
            violations = _compile_of(session).get("erc", []) or []
        except Exception as e:  # noqa: BLE001 — status survives, but its verdict is unavailable
            erc_available = False
            status_warnings.append(
                f"ERC unavailable: {type(e).__name__}: {e}")
        erc = _erc_summary(violations)
        unconnected = _unconnected_pins(doc, nl)

        # Build a prioritized next_actions list — errors first, then unconnected pins.
        next_actions: list[str] = []
        if not erc_available:
            next_actions.append(
                "ERC unavailable — retry ecad_run_erc(); do not treat this status as clean.")
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
        elif erc_available and not violations and not unconnected:
            next_actions.append("Design is clean — ecad_simulate('op') or ecad_run_drc on a board.")

        # one-line headline
        erc_head = (f"{erc['errors']} ERC error(s), {erc['warnings']} warning(s)"
                    if erc_available else "ERC unavailable")
        head = (f"{len(components)} component(s), {len(nets)} net(s), "
                f"{erc_head}, {len(unconnected)} unconnected pin(s).")
        if power and power.get("power_w_typ") is not None:
            head += f" Board power ~{power['power_w_typ'] * 1000:.1f} mW typ."
        full = {
            "ok": True, "doc_id": session.doc_id, "kind": "sch",
            "name": doc.get("name"), "revision": session.revision,
            "summary": head,
            "component_count": len(components), "components": components,
            "power": power,
            "net_count": len(nets), "nets": nets,
            "erc_available": erc_available,
            "erc": erc,
            "unconnected_count": len(unconnected),
            "unconnected_pins": [_pretty_pin(p) for p in unconnected],
            "warnings": status_warnings,
            "hint": (head + (" Fix ERC errors first, then connect floating pins, then simulate."
                             if erc_available else
                             " ERC DID NOT RUN; retry ecad_run_erc before any clean claim.")),
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
        drc_available = True
        status_warnings: list[str] = []
        try:
            res = _compile_of(session).get("drc", [])  # C++ compile runs engine.ecad.drc
            violations = res if isinstance(res, list) else res.get("violations", [])
        except Exception as e:  # noqa: BLE001 — status survives, but its verdict is unavailable
            drc_available = False
            status_warnings.append(
                f"DRC unavailable: {type(e).__name__}: {e}")
        erc = _erc_summary(violations)  # same grouping shape works for DRC
        next_actions: list[str] = []
        if not drc_available:
            next_actions.append(
                "DRC unavailable — retry ecad_run_drc(); do not treat this status as clean.")
        for g in erc["groups"]:
            if g["severity"] == "error":
                next_actions.append(f"FIX DRC {g['rule']} ({g['count']}x): "
                                    f"{g['messages'][0] if g['messages'] else ''}")
        if not fps:
            next_actions.append("No footprints — place parts (ecad_find_part finds fpids).")
        if not doc.get("outline", {}).get("shapes"):
            next_actions.append("No board outline yet — set one with a pcb_set_outline op.")
        # Per-footprint power + board total (honest nulls when unknown).
        fp_engs = [_component_eng(libid=f.get("libid") or "", fpid=f.get("fpid") or "",
                                  overrides=_fp_overrides(f)) for f in fps]
        power = _power_totals(fp_engs)
        drc_head = (f"{erc['errors']} DRC error(s), {erc['warnings']} warning(s)"
                    if drc_available else "DRC unavailable")
        head = (f"{len(fps)} footprint(s), {len(nets)} net(s), {len(tracks)} track(s), "
                f"{len(vias)} via(s), {drc_head}.")
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
            "drc_available": drc_available,
            "drc": erc,
            "warnings": status_warnings,
            "hint": (head + (" Fix DRC errors first." if drc_available else
                             " DRC DID NOT RUN; retry ecad_run_drc before any clean claim.")),
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
