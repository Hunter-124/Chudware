"""AI-ECAD MCP tools — schematic capture, ERC, SPICE simulation, PCB DRC, fab/3D export.

THE DESIGN GOAL (project owner): "the AI is extremely limited; its tools must have plenty of
automatic features/helpers to give it the context it needs to ask the right questions and do the
right things." So every tool here does the BOOKKEEPING and returns RICH, ACTIONABLE context — the
current state, what's wrong, and an imperative ``next_actions`` list — exactly the way the CAD
agent loop's feedback functions (``agent/loop.py``: ``_feedback_ok`` / ``_repeat_nudge`` /
``NOOP_CHANGE_NOTE``) guide a weak local model. A tool that just returns ``{"ok": true}`` is a
failure: it must name the unconnected pins, summarize the nets, group the ERC violations, and tell
the model the single best next op.

Architecture mirrors ``mcp_server/server.py`` exactly: ``@mcp.tool()`` (FastMCP) functions that
return JSON strings and call the engine helpers DIRECTLY (engine.ecad / engine.sim), holding their
own session state in a module-level dict (``_ECAD``). Registration is deferred behind
:func:`register_ecad_tools` so ``server.py`` can wire us in with a tiny fenced block.

Tools (each returns ``{ok, ...result, warnings:[], hint:"...", next_actions:[...]}``):
  ecad_new_schematic / ecad_new_pcb   start a doc
  ecad_find_part                      library.search -> ranked real libids/fpids + pin counts
  ecad_place_symbol                   place a symbol -> its ref + full pin list + unconnected count
  ecad_wire                           auto-route pin->pin -> resulting net + remaining unconnected
  ecad_add_power / ecad_label         power port / net label
  ecad_status                         THE context tool: components, nets, ERC summary, next_actions
  ecad_run_erc                        readable ERC violation lines + "N errors, M warnings"
  ecad_simulate                       netlist -> SPICE circuit -> simulate; key results + plain English
  ecad_run_drc                        PCB DRC summary with the worst offenders
  ecad_export / ecad_to_3d            wrap fab writers / 3D export (lazy import, clean if unavailable)
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Optional

from engine.ecad import netlist as netlist_mod
from engine.ecad import schema
from engine.ecad import session as ecad_session
from engine.ecad.library import search as lib_search
from engine.ecad.ops import OpError


# --------------------------------------------------------------------------- module state
#
# We keep our OWN session bookkeeping (the FastMCP-direct pattern). ``docs`` maps each opened
# doc_id -> its EcadSession; ``current_sch`` / ``current_pcb`` track the "active" document so the
# weak model can omit the doc_id on every call (it usually works on one doc at a time).

_ECAD: dict = {
    "current_sch": None,   # doc_id of the active schematic
    "current_pcb": None,   # doc_id of the active board
    "current": None,       # doc_id of the MOST-RECENTLY active doc (either kind) — what status defaults to
    "out_dir": None,       # last export directory used
}


# --------------------------------------------------------------------------- small helpers


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(message: str, *, hint: str = "", next_actions: list[str] | None = None,
         **extra) -> str:
    """A structured error payload that still TEACHES — every error carries a hint + next steps."""
    out = {"ok": False, "error": message, "warnings": [],
           "hint": hint or message,
           "next_actions": next_actions or []}
    out.update(extra)
    return _dumps(out)


def _active_sch_id(doc_id: str | None) -> str | None:
    return doc_id or _ECAD["current_sch"]


def _active_pcb_id(doc_id: str | None) -> str | None:
    return doc_id or _ECAD["current_pcb"]


def _get_sch(doc_id: str | None):
    """Resolve the active schematic session, or raise OpError with a friendly message."""
    did = _active_sch_id(doc_id)
    if did is None:
        raise OpError("no_doc", "no active schematic — call ecad_new_schematic(name) first",
                      {})
    s = ecad_session.get_session(did)
    if s.kind != "sch":
        raise OpError("wrong_kind", f"doc '{did}' is a {s.kind}, not a schematic", {})
    return s


def _get_pcb(doc_id: str | None):
    did = _active_pcb_id(doc_id)
    if did is None:
        raise OpError("no_doc", "no active board — call ecad_new_pcb(name) first", {})
    s = ecad_session.get_session(did)
    if s.kind != "pcb":
        raise OpError("wrong_kind", f"doc '{did}' is a {s.kind}, not a board", {})
    return s


def _sym_by_ref(doc: dict, ref: str) -> dict | None:
    """Find a placed symbol by its ref-des (e.g. 'R1'). Refs are how the model thinks; ids are
    internal. Returns the symbol instance dict or None."""
    for s in doc.get("symbols", []):
        if s.get("ref") == ref:
            return s
    return None


def _resolve_ref_or_id(doc: dict, token: str) -> dict | None:
    """Accept either a ref-des ('R1') or an internal symbol id ('s1')."""
    sym = _sym_by_ref(doc, token)
    if sym is not None:
        return sym
    for s in doc.get("symbols", []):
        if s.get("id") == token:
            return s
    return None


def _placed_pins(doc: dict, sym: dict) -> list[dict]:
    """The pin list of a placed symbol with world coords + the net each pin currently sits on.

    This is the single most important context for a weak model: "here is what you can wire, by
    name/number, and what it's already connected to."
    """
    sdef = schema.symbol_def(doc, sym.get("libid")) or {}
    unit = sym.get("unit", 1)
    nl = netlist_mod.build_netlist(doc)
    by_pin = nl.get("by_pin", {})
    out: list[dict] = []
    for pin in sdef.get("pins", []):
        if pin.get("unit", 1) not in (unit, 0):
            continue
        world = schema.pin_world(sym, pin)
        pinref = f"{sym.get('id', '')}.{pin.get('number', '')}"
        net = by_pin.get(pinref, "")
        out.append({
            "number": str(pin.get("number", "")),
            "name": str(pin.get("name", "")),
            "type": str(pin.get("type", "passive")),
            "xy": [round(world[0], 4), round(world[1], 4)],
            "net": net or None,
            "connected": bool(net) and _net_size(nl, net) > 1,
        })
    return out


def _net_size(nl: dict, net: str) -> int:
    return len(nl.get("nets", {}).get(net, []))


def _unconnected_pins(doc: dict, nl: dict | None = None) -> list[dict]:
    """Every pin that is floating (no net) or alone on its net (single-pin net = a wiring slip).

    Returns ``[{"pinref","ref","pin","name","type","xy","net"}]`` sorted by ref then pin — this is
    what feeds the "3 pins unconnected: U1.4, R2.1 — wire them or mark no-connect" next-action.
    """
    nl = nl or netlist_mod.build_netlist(doc)
    by_pin = nl.get("by_pin", {})
    sym_by_id = {s.get("id"): s for s in doc.get("symbols", [])}
    out: list[dict] = []
    for sym, pin, world in schema.iter_placed_pins(doc):
        pinref = f"{sym.get('id', '')}.{pin.get('number', '')}"
        net = by_pin.get(pinref, "")
        size = _net_size(nl, net) if net else 0
        ptype = str(pin.get("type", "passive"))
        if ptype == "no_connect":
            continue
        if net == "" or size <= 1:
            s = sym_by_id.get(sym.get("id"), sym)
            out.append({
                "pinref": pinref,
                "ref": s.get("ref", ""),
                "pin": str(pin.get("number", "")),
                "name": str(pin.get("name", "")),
                "type": ptype,
                "xy": [round(world[0], 4), round(world[1], 4)],
                "net": net or None,
            })
    out.sort(key=lambda d: (d["ref"] or "~", d["pin"]))
    return out


def _pretty_pin(p: dict) -> str:
    """'R1.2(passive)' style label for a pin record from _unconnected_pins / _placed_pins."""
    ref = p.get("ref") or "?"
    num = p.get("pin") or p.get("number") or "?"
    return f"{ref}.{num}"


def _apply(session, ops: list[dict]) -> dict:
    """Apply an op batch at the session's current revision and return the ops-response."""
    return ecad_session.apply_ops(session, session.revision, ops)


# --------------------------------------------------------------------------- ERC formatting


def _erc_summary(violations: list[dict]) -> dict:
    """Group ERC violations by rule with offending refs/pins, and split error/warning counts."""
    errors = [v for v in violations if v.get("severity") == "error"]
    warnings = [v for v in violations if v.get("severity") != "error"]
    by_rule: dict[str, dict] = {}
    for v in violations:
        rule = v.get("rule", "?")
        slot = by_rule.setdefault(rule, {"rule": rule, "severity": v.get("severity"),
                                         "count": 0, "pins": [], "nets": [], "messages": []})
        slot["count"] += 1
        for pr in v.get("pins", []) or []:
            if pr not in slot["pins"]:
                slot["pins"].append(pr)
        n = v.get("net")
        if n and n not in slot["nets"]:
            slot["nets"].append(n)
        if len(slot["messages"]) < 3:
            slot["messages"].append(v.get("message", ""))
        # if any instance is an error, the rule group is an error
        if v.get("severity") == "error":
            slot["severity"] = "error"
    groups = sorted(by_rule.values(), key=lambda g: (g["severity"] != "error", g["rule"]))
    return {"errors": len(errors), "warnings": len(warnings), "groups": groups}


def _erc_lines(violations: list[dict]) -> list[str]:
    """One readable line per violation, errors first."""
    ordered = sorted(violations, key=lambda v: (v.get("severity") != "error", v.get("rule", "")))
    lines = []
    for v in ordered:
        sev = (v.get("severity") or "warning").upper()
        lines.append(f"[{sev}] {v.get('rule')}: {v.get('message')}")
    return lines


# --------------------------------------------------------------------------- the tools


def register_ecad_tools(mcp) -> None:
    """Register every AI-ECAD tool on the given FastMCP instance.

    Called from ``mcp_server/server.py`` after ``mcp`` is created. Kept as a function (not
    import-time side effects) so the tools can be driven against a stub ``mcp`` in tests without
    importing the whole stdio server.
    """

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

    @mcp.tool()
    def ecad_new_pcb(name: str = "Board1") -> str:
        """Start a NEW PCB (board) document and make it the active one.

        Returns the doc id + stackup summary. A fresh board has a 2-layer copper stackup, a
        default net class, and a GND net but no footprints or outline yet. Use ecad_find_part to
        discover footprint fpids, place them, route, then ecad_run_drc.
        """
        s = ecad_session.new_session("pcb", name)
        _ECAD["current_pcb"] = s.doc_id
        _ECAD["current"] = s.doc_id
        n_cu = s.doc.get("stackup", {}).get("copper_layers", 2)
        return _dumps({
            "ok": True, "doc_id": s.doc_id, "kind": "pcb", "name": name,
            "revision": s.revision, "copper_layers": n_cu, "footprints": 0,
            "nets": [n.get("name") for n in s.doc.get("nets", [])],
            "warnings": [],
            "hint": f"Empty {n_cu}-layer board. Place footprints (ecad_find_part finds fpids), "
                    "route tracks, then ecad_run_drc to check spacing.",
            "next_actions": [
                "ecad_find_part('0603')  # discover footprint fpids",
                "ecad_run_drc()  # at any time, see clearance/width violations",
            ],
        })

    # ---------------------------------------------------------------- find part

    @mcp.tool()
    def ecad_find_part(query: str, limit: int = 12) -> str:
        """Search the component library and return RANKED real parts so you place an actual part
        instead of inventing a libid.

        For each hit: its ``libid`` (symbols) or ``fpid`` (footprints), name, description, and
        PIN COUNT (symbols) so you know how many pins you'll have to wire. Hits are sorted best
        first. Pass the exact ``libid``/``fpid`` of a hit to ecad_place_symbol / a footprint op.
        """
        from engine.ecad.library import resolve as lib_resolve  # local: pin-count lookup
        hits = lib_search(query) or []
        out_hits: list[dict] = []
        for h in hits[:max(1, int(limit))]:
            rec = {"kind": h.get("kind"), "name": h.get("name", ""),
                   "description": h.get("description", ""), "score": h.get("score", 0)}
            if h.get("kind") == "symbol":
                libid = h.get("libid", "")
                rec["libid"] = libid
                sdef = lib_resolve(libid) or {}
                pins = sdef.get("pins", [])
                rec["pin_count"] = len(pins)
                rec["pins"] = [f"{p.get('number')}:{p.get('name')}({p.get('type')})"
                               for p in pins[:12]]
                rec["reference_prefix"] = sdef.get("reference_prefix", "")
                rec["default_value"] = sdef.get("value", "")
                if sdef.get("sim"):
                    rec["simulatable"] = True
            else:
                rec["fpid"] = h.get("fpid", "")
            out_hits.append(rec)
        if not out_hits:
            return _err(f"no library parts match {query!r}",
                        hint="Try a broader keyword (e.g. 'resistor', 'cap', 'led', 'opamp', "
                             "'connector', 'voltage', 'gnd', '0603').",
                        next_actions=["ecad_find_part('resistor')",
                                      "ecad_find_part('voltage')"],
                        query=query, hits=[])
        first = out_hits[0]
        first_id = first.get("libid") or first.get("fpid")
        ex = (f"ecad_place_symbol('{first.get('libid')}', x=0, y=0)"
              if first.get("kind") == "symbol"
              else f"use fpid '{first.get('fpid')}' on the board")
        return _dumps({
            "ok": True, "query": query, "count": len(out_hits), "hits": out_hits,
            "warnings": [],
            "hint": f"Best match: {first_id} ({first.get('description') or first.get('name')}). "
                    "Place it with its EXACT libid/fpid — do not invent ids.",
            "next_actions": [ex],
        })

    # ---------------------------------------------------------------- place symbol

    @mcp.tool()
    def ecad_place_symbol(libid: str, x: float = 0.0, y: float = 0.0,
                          ref: str = "", value: str = "",
                          angle: float = 0.0, doc_id: str = "") -> str:
        """Place a symbol on the active schematic and return its assigned REF plus its full PIN
        LIST (name/number/type/world-xy) and the count of pins still unconnected.

        The pin list is the context you need to wire: each pin's world ``xy`` is where a wire must
        reach, and ``net`` tells you what it already touches. ``value`` matters for simulation —
        a resistor needs e.g. '1k', a voltage source '5' — set it now or with ecad_set later.
        ``ref`` auto-assigns (R1, R2, ...) if omitted.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        op: dict = {"op": "sch_add_symbol", "libid": libid, "pos": [float(x), float(y)],
                    "angle": float(angle)}
        if ref:
            op["ref"] = ref
        if value:
            op["value"] = value
        try:
            resp = _apply(session, [op])
        except OpError as e:
            return _err(f"could not place {libid!r}: {e.message}",
                        hint="Check the libid with ecad_find_part; pass an exact match.",
                        next_actions=[f"ecad_find_part('{libid.split(':')[-1]}')"],
                        detail=e.detail)
        sid = resp.get("id_map", {}).get("$0")
        doc = session.doc
        sym = next((s for s in doc.get("symbols", []) if s.get("id") == sid), None)
        if sym is None:
            return _err("placed symbol but could not locate it (internal)",
                        hint="Call ecad_status() to inspect the document.")
        pins = _placed_pins(doc, sym)
        nl = netlist_mod.build_netlist(doc)
        unconnected = _unconnected_pins(doc, nl)
        this_unconnected = [p for p in unconnected if p["ref"] == sym.get("ref")]
        warnings = list(resp.get("warnings", []))
        pin_labels = ", ".join(f"{p['number']}({p['name']}/{p['type']})" for p in pins)
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "ref": sym.get("ref"), "id": sid, "libid": libid,
            "value": sym.get("value", ""), "pos": sym.get("pos"),
            "pins": pins,
            "unconnected_pins_this_symbol": [p["pinref"] for p in this_unconnected],
            "total_unconnected": len(unconnected),
            "warnings": warnings,
            "hint": (f"Placed {sym.get('ref')} with pins: {pin_labels}. "
                     f"All {len(this_unconnected)} of its pins are floating — wire each pin to "
                     "another pin (ecad_wire) or to a power/ground port (ecad_add_power)."
                     if this_unconnected else
                     f"Placed {sym.get('ref')}; its pins are already on existing nets."),
            "next_actions": (
                [f"ecad_wire('{sym.get('ref')}', '{pins[0]['number']}', '<otherRef>', "
                 f"'<otherPin>')  # connect pin {pins[0]['number']}"]
                if pins else []
            ) + ["ecad_status()  # see the whole design + what's still unconnected"],
        })

    # ---------------------------------------------------------------- wire

    @mcp.tool()
    def ecad_wire(from_ref: str, from_pin: str, to_ref: str, to_pin: str,
                  doc_id: str = "") -> str:
        """Auto-route an orthogonal wire from one pin to another (emits sch_connect) and return
        the RESULTING net plus the updated list of what is still unconnected.

        ``from_ref``/``to_ref`` are ref-des ('R1') or internal ids; pins are pin numbers ('1') or
        names. The engine names the net automatically (power/label name wins, else N$n). The
        returned ``net`` + ``net_members`` confirm the two pins really joined.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        doc = session.doc
        a = _resolve_ref_or_id(doc, from_ref)
        b = _resolve_ref_or_id(doc, to_ref)
        if a is None or b is None:
            missing = from_ref if a is None else to_ref
            refs = [s.get("ref") for s in doc.get("symbols", [])]
            return _err(f"no symbol {missing!r} on the schematic",
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
            return _err(f"could not wire {from_ref}.{from_pin} -> {to_ref}.{to_pin}: {e.message}",
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
        return _dumps({
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
                 "ecad_simulate('op')  # solve DC node voltages"]
            ),
        })

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
        return _dumps({
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
                             "is sourced"],
        })

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
        return _dumps({
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
            "next_actions": ["ecad_status()"],
        })

    # ---------------------------------------------------------------- status (THE context tool)

    @mcp.tool()
    def ecad_status(doc_id: str = "") -> str:
        """THE situational-awareness call. Returns a COMPACT design summary sized for a small
        context window: component count + list, every net with its size and members, the ERC
        violation summary grouped by rule, and a PRIORITIZED ``next_actions`` list naming the
        exact pins to fix.

        Call this whenever you are unsure what to do next — it tells you what exists, what is
        wrong, and the single best next op.
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
        if session.kind == "pcb":
            return _pcb_status(session)
        # ---- schematic status ----
        nl = netlist_mod.build_netlist(doc)
        components = [{"ref": c.get("ref"), "libid": c.get("libid"),
                       "value": c.get("value"), "footprint": c.get("footprint") or None,
                       "pins": c.get("pins")}
                      for c in nl.get("components", [])]
        nets = []
        for name, members in sorted(nl.get("nets", {}).items()):
            nets.append({"name": name, "size": len(members),
                         "members": [f"{m.get('ref')}.{m.get('pin')}" for m in members]})
        # ERC
        violations: list[dict] = []
        try:
            from engine.ecad import erc as erc_mod
            violations = erc_mod.run_erc(doc, nl)
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
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "kind": "sch",
            "name": doc.get("name"), "revision": session.revision,
            "summary": head,
            "component_count": len(components), "components": components,
            "net_count": len(nets), "nets": nets,
            "erc": erc,
            "unconnected_pins": [_pretty_pin(p) for p in unconnected],
            "warnings": nl.get("warnings", []),
            "hint": head + " Fix ERC errors first, then connect floating pins, then simulate.",
            "next_actions": next_actions or ["ecad_simulate('op')"],
        })

    def _pcb_status(session) -> str:
        doc = session.doc
        fps = doc.get("footprints", [])
        nets = doc.get("nets", [])
        tracks = doc.get("tracks", [])
        vias = doc.get("vias", [])
        violations: list[dict] = []
        try:
            from engine.ecad import drc as drc_mod
            res = drc_mod.run_drc(doc)
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
        head = (f"{len(fps)} footprint(s), {len(nets)} net(s), {len(tracks)} track(s), "
                f"{len(vias)} via(s), {erc['errors']} DRC error(s), {erc['warnings']} warning(s).")
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "kind": "pcb",
            "name": doc.get("name"), "revision": session.revision,
            "summary": head,
            "footprint_count": len(fps),
            "footprints": [{"ref": f.get("ref"), "fpid": f.get("fpid"),
                            "pos": f.get("pos"), "side": f.get("side")} for f in fps],
            "nets": [n.get("name") for n in nets],
            "track_count": len(tracks), "via_count": len(vias),
            "drc": erc,
            "warnings": [],
            "hint": head + " Fix DRC errors first.",
            "next_actions": next_actions or ["ecad_run_drc()", "ecad_export('gerber')"],
        })

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
        try:
            from engine.ecad import erc as erc_mod
            violations = erc_mod.run_erc(doc, nl)
        except Exception as e:  # noqa: BLE001
            return _err(f"ERC engine error: {e}", hint="Inspect with ecad_status().")
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

    # ---------------------------------------------------------------- simulate

    @mcp.tool()
    def ecad_simulate(analysis: str = "op", params: Optional[dict] = None,
                      probes: Optional[list] = None, doc_id: str = "") -> str:
        """Build the netlist -> SPICE circuit -> run the native simulator, and return the KEY
        results with a plain-English reading.

        ``analysis`` is one of: 'op' (DC operating point), 'dc' (source sweep), 'tran' (transient),
        'ac' (frequency response). ``params`` carries analysis args:
          op   -> {}                  (none)
          dc   -> {"source":"V1","start":0,"stop":5,"step":0.05}
          tran -> {"tstep":1e-6,"tstop":1e-3,"uic":false}
          ac   -> {"sweep":"dec","points":20,"fstart":1,"fstop":1e6}
        If ``probes`` is omitted, DEFAULTS TO ALL NODE VOLTAGES (you won't always know the node
        names — this surfaces them all). Returns op node voltages, or for tran/ac a compact
        summary (final value / -3 dB point) plus a sentence like 'V(OUT) settles to 2.49 V'.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        try:
            from engine.sim.spice import netlist_to_spice_circuit
            from engine.sim.circuit import simulate as sim_run
        except Exception as e:  # noqa: BLE001
            return _err(f"simulator unavailable ({type(e).__name__}: {e})",
                        hint="The sim engine (numpy/scipy) is not importable here.")
        circuit, warns = netlist_to_spice_circuit(nl)
        if not circuit.get("devices"):
            return _err("nothing to simulate — the netlist has no simulatable devices",
                        hint="Place parts with sim models (R/C/L/V/I/D/Q) and give them VALUES "
                             "(a resistor needs '1k', a source '5'); wire them into a circuit.",
                        next_actions=["ecad_find_part('voltage')  # add a V source",
                                      "ecad_status()"],
                        warnings=warns)

        # default probes = every non-ground node voltage (the weak model rarely names probes).
        nodes = [n for n in circuit.get("nodes", []) if str(n) not in ("0", "gnd", "GND")]
        if probes:
            probe_list = list(probes)
        else:
            probe_list = [f"V({n})" for n in nodes]
            # also add the current through every voltage source — usually the interesting datum.
            probe_list += [f"I({d['name']})" for d in circuit["devices"] if d.get("kind") == "V"]

        an = _build_analysis(analysis, params or {}, circuit)
        if an is None:
            return _err(f"unknown analysis {analysis!r}",
                        hint="Use one of: 'op', 'dc', 'tran', 'ac'.",
                        next_actions=["ecad_simulate('op')"])
        study = {"analyses": [an], "probes": probe_list}
        result = sim_run(circuit, study)
        conv = result.get("convergence", {})
        converged = bool(conv.get(an["type"], False))

        payload: dict = {
            "ok": True, "doc_id": session.doc_id, "analysis": an["type"],
            "converged": converged,
            "device_count": len(circuit["devices"]),
            "nodes": nodes, "probes": probe_list,
            "warnings": warns + ([] if converged else
                                 [f"{an['type']} did not converge — see sim_log"]),
            "sim_log": result.get("log", [])[-6:],
        }

        reading_lines: list[str] = []
        if an["type"] == "op":
            op = result.get("op", {})
            payload["op"] = {k: _round(v) for k, v in op.items()}
            for k, v in sorted(op.items()):
                if k.startswith("V(") and _finite(v):
                    reading_lines.append(f"{k} = {_round(v)} V")
            for k, v in sorted(op.items()):
                if k.startswith("I(") and _finite(v):
                    reading_lines.append(f"{k} = {_eng(v)}A")
        elif an["type"] == "dc":
            payload["dc"] = _summarize_sweep(result.get("dc", {}), reading_lines, unit="V")
        elif an["type"] == "tran":
            payload["tran"] = _summarize_tran(result.get("tran", {}), reading_lines)
        elif an["type"] == "ac":
            payload["ac"] = _summarize_ac(result.get("ac", {}), reading_lines)

        reading = ("; ".join(reading_lines) if reading_lines else
                   "no finite probe values — check connectivity / source values")
        payload["reading"] = reading
        payload["hint"] = (
            (f"{an['type'].upper()} solved: {reading}." if converged else
             f"{an['type'].upper()} did NOT converge: {reading}. Check that every node has a DC "
             "path to ground and that source/part values are sane (run ecad_run_erc).")
        )
        payload["next_actions"] = (
            ["ecad_status()  # the result matches the topology you built"] if converged else
            ["ecad_run_erc()  # find the connectivity/driver problem",
             "ecad_status()  # confirm wiring"]
        )
        return _dumps(payload)

    # ---------------------------------------------------------------- DRC

    @mcp.tool()
    def ecad_run_drc(doc_id: str = "") -> str:
        """Run the Design Rule Check on the active board and return a summary with the worst
        offenders (clearance, track width, via size, annular ring, edge clearance, etc.).

        DRC is advisory. The grouped ``by_rule`` view + the readable lines tell you which physical
        spacing/size rule each track/pad/via breaks so you can widen, move, or re-space it.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a board first.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        try:
            from engine.ecad import drc as drc_mod
            res = drc_mod.run_drc(doc)
            violations = res if isinstance(res, list) else res.get("violations", [])
        except Exception as e:  # noqa: BLE001
            return _err(f"DRC engine error: {e}", hint="Inspect with ecad_status().")
        summary = _erc_summary(violations)
        lines = _erc_lines(violations)
        worst = lines[:5]
        verdict = (f"{summary['errors']} error(s), {summary['warnings']} warning(s)."
                   + (" worst: " + " | ".join(worst) if worst else " Clean board."))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "errors": summary["errors"], "warnings": summary["warnings"],
            "verdict": verdict,
            "violations": lines, "by_rule": summary["groups"],
            "warnings": [],
            "hint": verdict,
            "next_actions": (
                ["Address the worst DRC offenders above (widen tracks / increase spacing / "
                 "move parts), then re-run ecad_run_drc()."] if violations else
                ["ecad_export('gerber')  # board passes DRC — generate fab outputs"]
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
        from engine.ecad import rpc as ecad_rpc
        try:
            resp = ecad_rpc._ecad_update_from_sch(
                {"board_doc_id": pcb_id, "sch_doc_id": sch_id}, lambda _d: None)
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
        return _dumps({
            "ok": True, "doc_id": pcb_id, "sch_doc_id": sch_id, "kind": "pcb",
            "revision": resp.get("revision"),
            "footprints_added": added, "footprints_updated": updated,
            "nets_bound": report.get("nets_bound", 0),
            "unresolved": unresolved,
            "footprint_count": len(board.get("footprints", [])),
            "ratsnest_remaining": len(ratsnest),
            "warnings": warnings,
            "hint": head + " Next: ecad_autoroute then ecad_fill_zones.",
            "next_actions": next_actions,
        })

    # ---------------------------------------------------------------- autoroute

    @mcp.tool()
    def ecad_autoroute(pitch: float = 0.0, doc_id: str = "") -> str:
        """Auto-route the active board's ratsnest with a basic 2-layer grid (Lee/A*) router.

        Connects the unrouted airwires with orthogonal tracks + through-vias, respecting clearance.
        Honest about its limits: it is single-pass (no rip-up/reroute), so dense boards leave some
        nets failed — those are reported by name and need manual routing or more space. ``pitch``
        (mm) overrides the grid resolution (default 0.25); finer threads tighter gaps but is slower.
        Run ecad_update_from_sch first so there is a ratsnest to route.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Routing needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        from engine.ecad import rpc as ecad_rpc
        opts = {"pitch": float(pitch)} if pitch and pitch > 0 else {}
        try:
            resp = ecad_rpc._ecad_autoroute(
                {"doc_id": session.doc_id, "opts": opts}, lambda _d: None)
        except OpError as e:
            return _err(f"autoroute failed: {e.message}", hint=e.message,
                        next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
        report = resp.get("report", {})
        routed = report.get("routed", [])
        failed = report.get("failed", [])
        before = report.get("airwires_before", 0)
        after = report.get("airwires_after", 0)
        warnings = list(resp.get("warnings", []))
        head = (f"routed {len(routed)} net(s), {len(failed)} failed; ratsnest "
                f"{before} -> {after} airwire(s); {report.get('vias', 0)} via(s), "
                f"{report.get('length_mm', 0.0)} mm of track.")
        next_actions: list[str] = []
        if failed:
            next_actions.append(
                f"{len(failed)} net(s) failed ({', '.join(map(str, failed[:6]))}) — "
                "they need manual routing or more space (move parts apart / finer pitch).")
            next_actions.append("ecad_autoroute(pitch=0.15)  # retry with a finer grid")
        else:
            next_actions.append("ecad_fill_zones()  # pour copper, then ecad_run_drc()")
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": resp.get("revision"),
            "routed_nets": routed, "failed_nets": failed,
            "airwires_before": before, "airwires_after": after,
            "ratsnest_remaining": after,
            "vias": report.get("vias", 0), "length_mm": report.get("length_mm", 0.0),
            "grid": report.get("grid", {}),
            "warnings": warnings,
            "hint": head + (" All nets routed." if not failed else
                            f" {len(failed)} net(s) failed — manual routing or more space needed."),
            "next_actions": next_actions,
        })

    # ---------------------------------------------------------------- zones

    @mcp.tool()
    def ecad_add_zone(net: str, layer: str = "F.Cu",
                      rect_or_polygon: Optional[list] = None, doc_id: str = "") -> str:
        """Add a copper pour (zone) bound to ``net`` on ``layer`` — usually a ground/power plane.

        ``rect_or_polygon`` is either a rectangle ``[x0, y0, x1, y1]`` (auto-expanded to 4 corners)
        or an explicit polygon ``[[x, y], ...]`` (>=3 points). Omit it to flood the whole board
        outline bounding box (a full-board GND pour, the common case). The pour is not filled until
        ecad_fill_zones runs (compile fills a coarse preview automatically after this edit).
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Zones live on a board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        polygon = _zone_polygon(doc, rect_or_polygon)
        if polygon is None:
            return _err("could not derive a zone polygon",
                        hint="Pass rect_or_polygon as [x0,y0,x1,y1] or [[x,y],...] (>=3 pts), "
                             "or set a board outline first so the whole-board default works.",
                        next_actions=["ecad_status()"])
        # The zone op requires the net to already exist on the board (there is no add-net op). A
        # fresh board has GND; other nets arrive via ecad_update_from_sch. Guide the model rather
        # than emitting an op that would fail.
        if not any(n.get("name") == net for n in doc.get("nets", [])):
            existing = [n.get("name") for n in doc.get("nets", [])]
            return _err(f"net {net!r} is not on the board yet",
                        hint=f"Board nets are: {', '.join(existing) or '(none)'}. A fresh board "
                             "has GND; populate other nets with ecad_update_from_sch first, or "
                             "pour GND.",
                        next_actions=["ecad_add_zone('GND')  # the default ground net",
                                      "ecad_update_from_sch()  # bind schematic nets to the board"],
                        board_nets=existing)
        try:
            resp = _apply(session, [{"op": "pcb_add_zone", "net": net, "layer": layer,
                                     "polygon": polygon}])
        except OpError as e:
            return _err(f"could not add zone on {net!r}/{layer}: {e.message}", hint=e.message,
                        next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
        zones = session.doc.get("zones", [])
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "net": net, "layer": layer, "polygon": polygon,
            "zone_count": len(zones),
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Added a {net} copper pour on {layer} ({len(polygon)} vertices). "
                     "Fill it (ecad_fill_zones) to compute the actual copper, then ecad_run_drc."),
            "next_actions": ["ecad_fill_zones()  # compute the poured copper",
                             "ecad_run_drc()  # confirm clearance to other-net copper"],
        })

    @mcp.tool()
    def ecad_fill_zones(doc_id: str = "") -> str:
        """Fill every copper pour on the active board at FULL resolution and report the result.

        Computes each zone's copper polygons (outline clipped to the board, minus clearance around
        other-net copper, with thermal relief on same-net through-hole pads) and persists them — this
        is the copper that gets exported to Gerber. Reports zones filled + total copper area. The
        interactive rebuild already fills a coarse preview after each edit; call this before export
        for the precise fill.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Pours live on a board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        if not session.doc.get("zones"):
            return _err("the board has no copper pours to fill",
                        hint="Add a ground/power pour first.",
                        next_actions=["ecad_add_zone('GND')  # whole-board ground pour"])
        from engine.ecad import rpc as ecad_rpc
        try:
            resp = ecad_rpc._ecad_fill_zones({"doc_id": session.doc_id}, lambda _d: None)
        except OpError as e:
            return _err(f"zone fill failed: {e.message}", hint=e.message,
                        next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
        summary = resp.get("fill_summary", {})
        filled = summary.get("zones_filled", 0)
        total = summary.get("zones", 0)
        area = summary.get("copper_area_mm2", 0.0)
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": resp.get("revision"),
            "zones": total, "zones_filled": filled,
            "polygons": summary.get("polygons", 0),
            "copper_area_mm2": area,
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Filled {filled}/{total} pour(s): {area} mm^2 of copper across "
                     f"{summary.get('polygons', 0)} polygon(s). This is the copper Gerber exports."),
            "next_actions": ["ecad_run_drc()  # check the pour respects clearance",
                             "ecad_export('gerber')  # the fill is baked into the copper layers"],
        })

    # ---------------------------------------------------------------- parameter sweep

    @mcp.tool()
    def ecad_sweep(param: str, values: Optional[list] = None,
                   analysis: str = "op", probe: str = "", doc_id: str = "") -> str:
        """Parameter sweep: re-simulate the active schematic once per value of ``param`` and report
        how the output moves (a SPICE ``.step``).

        ``param`` is a device name (e.g. 'R1', 'V1' — sets its value) or a circuit .param. ``values``
        is the list to sweep (e.g. [1000, 2000, 5000]); start/stop/step is not exposed here — pass an
        explicit list. ``analysis`` is 'op'/'dc'/'tran'/'ac' (default 'op'). ``probe`` picks the
        output trace to summarize (default: the first non-ground node voltage). Returns the swept
        trend in plain English ('V(OUT) rises 2.5 V -> 4.0 V as R1 goes 1k -> 5k').
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Sweeps run on a schematic.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        if not values or not isinstance(values, list):
            return _err("ecad_sweep needs a non-empty 'values' list",
                        hint="Pass the values to sweep, e.g. ecad_sweep('R1', [1000, 2000, 5000]).",
                        next_actions=["ecad_sweep('R1', [1000, 2000, 5000])"])
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        try:
            from engine.sim.spice import netlist_to_spice_circuit
            from engine.sim.circuit import simulate as sim_run
        except Exception as e:  # noqa: BLE001
            return _err(f"simulator unavailable ({type(e).__name__}: {e})",
                        hint="The sim engine (numpy/scipy) is not importable here.")
        circuit, warns = netlist_to_spice_circuit(nl)
        if not circuit.get("devices"):
            return _err("nothing to sweep — the netlist has no simulatable devices",
                        hint="Place R/C/L/V/I/D parts with VALUES and wire them up first.",
                        next_actions=["ecad_status()"], warnings=warns)

        nodes = [n for n in circuit.get("nodes", []) if str(n) not in ("0", "gnd", "GND")]
        probe_list = [probe] if probe else [f"V({n})" for n in nodes]
        an = _build_analysis(analysis, {}, circuit)
        if an is None:
            return _err(f"unknown analysis {analysis!r}",
                        hint="Use one of: 'op', 'dc', 'tran', 'ac'.",
                        next_actions=["ecad_sweep('R1', [1000, 2000])"])
        fvals = [float(v) for v in values]
        study = {"analyses": [an], "probes": probe_list,
                 "step": {"param": param, "values": fvals}}
        result = sim_run(circuit, study)
        step = result.get("step", {})
        runs = step.get("runs", [])
        trend, sweep_table, chosen = _summarize_sweep_steps(runs, an["type"], probe or None)
        converged = bool(result.get("convergence", {}).get("step", False))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "analysis": an["type"],
            "param": param, "values": fvals, "converged": converged,
            "probe": chosen, "sweep": sweep_table,
            "trend": trend,
            "warnings": warns + ([] if converged else
                                 ["one or more sweep points did not converge"]),
            "hint": (f"Swept {param} over {len(fvals)} value(s): {trend}" if trend else
                     f"Swept {param} but found no finite {chosen or 'probe'} value — "
                     "check connectivity / values (ecad_run_erc)."),
            "next_actions": ["ecad_simulate('op')  # inspect one operating point in detail",
                             "ecad_status()"],
        })

    # ---------------------------------------------------------------- export

    @mcp.tool()
    def ecad_export(kind: str, path: str = "", doc_id: str = "") -> str:
        """Export a fabrication output from the active board.

        ``kind`` is one of: 'gerber' (copper/mask/silk layer set), 'drill' (Excellon PTH/NPTH),
        'bom' (CSV bill of materials), 'placement' (pick-and-place CSV). ``path`` is the output
        directory (gerber/drill) or file (bom/placement); defaults under the AI-CAD cache. Returns
        the written path(s).
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Export needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        kind = (kind or "").lower()
        try:
            from engine.ecad import fab
        except Exception as e:  # noqa: BLE001
            return _err(f"fab writers unavailable ({type(e).__name__}: {e})",
                        hint="The engine.ecad.fab package failed to import.")
        out_dir = path or str(_default_out_dir(doc.get("name", "board")))
        try:
            if kind == "gerber":
                os.makedirs(out_dir, exist_ok=True)
                files = fab.write_gerbers(doc, out_dir)
                result = {"kind": "gerber", "dir": out_dir, "files": files}
            elif kind == "drill":
                os.makedirs(out_dir, exist_ok=True)
                files = fab.write_drill(doc, out_dir)
                result = {"kind": "drill", "dir": out_dir, "files": files}
            elif kind == "bom":
                text = fab.write_bom(doc, fmt="csv")
                fp = path or os.path.join(out_dir, "bom.csv")
                os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                result = {"kind": "bom", "path": fp, "rows": text.count("\n")}
            elif kind == "placement":
                text = fab.write_placement(doc)
                fp = path or os.path.join(out_dir, "placement.csv")
                os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                result = {"kind": "placement", "path": fp, "rows": text.count("\n")}
            else:
                return _err(f"unknown export kind {kind!r}",
                            hint="Use 'gerber', 'drill', 'bom', or 'placement'.",
                            next_actions=["ecad_export('gerber')"])
        except Exception as e:  # noqa: BLE001
            return _err(f"export {kind} failed: {type(e).__name__}: {e}",
                        hint="Check ecad_run_drc() and that the board has footprints/copper.")
        _ECAD["out_dir"] = out_dir
        result.update({"ok": True, "doc_id": session.doc_id, "warnings": [],
                       "hint": f"Wrote {kind} to {result.get('path') or result.get('dir')}.",
                       "next_actions": ["ecad_export('drill')  # the other fab outputs",
                                        "ecad_export('bom')"]})
        return _dumps(result)

    @mcp.tool()
    def ecad_to_3d(path: str = "", doc_id: str = "") -> str:
        """Export a 3D model (STEP) of the active board.

        Calls ``engine.ecad.threed.board_to_step`` (now available), assembles the board substrate
        and proxy-boxes for every on-board footprint, and writes a STEP file. Returns the file
        path + volume summary and a hint telling the user to open the STEP as an import feature
        in the AI-CAD app so they can design an enclosure around the board.

        Falls back to a clean, actionable error (never raises) when the board has no outline or
        when there is no active PCB session.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="3D export needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc

        # Build the output path (default: AI-CAD cache dir named after the board).
        out_path = path or str(_default_out_dir(doc.get("name", "board")) / "board.step")

        try:
            from engine.ecad.threed import board_to_step  # lazy import; no OCP at module load
        except ImportError as e:
            return _err(f"3D exporter unavailable (ImportError: {e})",
                        hint="engine.ecad.threed could not be imported — "
                             "OCP/build123d may not be installed in this environment. "
                             "Use ecad_export('gerber') for fab output.",
                        next_actions=["ecad_export('gerber')", "ecad_export('drill')"],
                        available=False)

        try:
            result = board_to_step(doc, out_path)
        except Exception as e:  # noqa: BLE001 — BoardGeometryError or any OCP error
            return _err(f"3D export failed: {type(e).__name__}: {e}",
                        hint="Check that the board has a closed Edge.Cuts outline "
                             "(pcb_set_outline op, closed=True). "
                             "An empty outline or open chain raises BoardGeometryError.",
                        next_actions=["ecad_run_drc()  # confirms outline is present",
                                      "ecad_status()"])

        step_path = result.get("step_path", out_path)
        summary   = result.get("summary", {})
        _ECAD["out_dir"] = os.path.dirname(step_path)
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "kind": "step",
            "step_path": step_path,
            "board_volume_mm3": summary.get("board_volume", 0.0),
            "n_components": summary.get("n_components", 0),
            "n_proxies": summary.get("n_proxies", 0),
            "summary": summary,
            "warnings": summary.get("warnings", []),
            "hint": (f"Wrote STEP to {step_path} "
                     f"(board volume {summary.get('board_volume', 0):.1f} mm³, "
                     f"{summary.get('n_components', 0)} component body/bodies). "
                     "Open this STEP in the CAD app as an import feature to design "
                     "the enclosure."),
            "next_actions": [
                f"# In the CAD app: File > Import > {step_path}",
                "# Or drag the STEP onto the 3D canvas to start the enclosure design.",
            ],
        })

    # tools registered; nothing to return.


# --------------------------------------------------------------------------- sim helpers (module level)


def _build_analysis(analysis: str, params: dict, circuit: dict) -> dict | None:
    """Map a friendly analysis name + params dict to a sim.md §3 analysis dict, filling sane
    defaults (a weak model usually under-specifies)."""
    a = (analysis or "op").lower()
    if a == "op":
        return {"type": "op"}
    if a == "dc":
        src = params.get("source")
        if not src:  # default to the first voltage source in the circuit
            src = next((d["name"] for d in circuit.get("devices", []) if d.get("kind") == "V"),
                       "V1")
        return {"type": "dc", "source": src,
                "start": float(params.get("start", 0.0)),
                "stop": float(params.get("stop", 5.0)),
                "step": float(params.get("step", (float(params.get("stop", 5.0)) -
                                                  float(params.get("start", 0.0))) / 50 or 0.1))}
    if a == "tran":
        tstop = float(params.get("tstop", 1e-3))
        return {"type": "tran",
                "tstep": float(params.get("tstep", tstop / 200)),
                "tstop": tstop,
                "tstart": float(params.get("tstart", 0.0)),
                "uic": bool(params.get("uic", False))}
    if a == "ac":
        return {"type": "ac",
                "sweep": params.get("sweep", "dec"),
                "points": int(params.get("points", 20)),
                "fstart": float(params.get("fstart", 1.0)),
                "fstop": float(params.get("fstop", 1e6))}
    return None


def _finite(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except Exception:  # noqa: BLE001
        return False


def _round(v: Any, n: int = 4) -> Any:
    try:
        f = float(v)
        if not math.isfinite(f):
            return None
        return round(f, n)
    except Exception:  # noqa: BLE001
        return v


def _eng(v: float) -> str:
    """Engineering-notation magnitude for a current/value, e.g. 3.1m, 1.5u, 12.0 ."""
    try:
        f = float(v)
    except Exception:  # noqa: BLE001
        return str(v)
    if f == 0 or not math.isfinite(f):
        return f"{f:g} "
    sign = "-" if f < 0 else ""
    a = abs(f)
    for suf, scale in (("M", 1e6), ("k", 1e3), ("", 1.0), ("m", 1e-3),
                       ("u", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        q = a / scale
        if 1.0 <= q < 1000.0:
            return f"{sign}{q:.3g}{suf}"
    return f"{f:.3g} "


def _summarize_sweep(dc: dict, reading_lines: list[str], unit: str = "V") -> dict:
    """Compact a DC sweep: keep endpoints + min/max per trace."""
    out: dict = {"sweep": dc.get("sweep"), "points": len(dc.get("x", [])),
                 "x_range": [dc.get("x", [None])[0], dc.get("x", [None])[-1]]
                 if dc.get("x") else [], "traces": {}}
    for p, vals in dc.get("traces", {}).items():
        fin = [v for v in vals if _finite(v)]
        if not fin:
            continue
        out["traces"][p] = {"first": _round(vals[0]), "last": _round(vals[-1]),
                            "min": _round(min(fin)), "max": _round(max(fin))}
        reading_lines.append(f"{p}: {_round(fin[0])}->{_round(fin[-1])} {unit} over sweep")
    return out


def _summarize_tran(tran: dict, reading_lines: list[str]) -> dict:
    """Compact a transient run: final value + peak per trace, and the time span."""
    times = tran.get("x_t", [])
    out: dict = {"points": len(times),
                 "t_end": _round(times[-1], 9) if times else None, "traces": {}}
    for p, vals in tran.get("traces", {}).items():
        fin = [v for v in vals if _finite(v)]
        if not fin:
            continue
        final = vals[-1] if _finite(vals[-1]) else fin[-1]
        out["traces"][p] = {"final": _round(final), "min": _round(min(fin)),
                            "max": _round(max(fin))}
        reading_lines.append(f"{p} settles to {_round(final)} V "
                             f"(peak {_round(max(fin, key=abs))} V)")
    return out


def _summarize_ac(ac: dict, reading_lines: list[str]) -> dict:
    """Compact an AC sweep: peak gain + the -3 dB corner frequency per trace."""
    freqs = ac.get("x_f", [])
    out: dict = {"points": len(freqs),
                 "f_range": [_round(freqs[0], 3), _round(freqs[-1], 3)] if freqs else [],
                 "traces": {}}
    for p, mags in ac.get("mag", {}).items():
        fin = [(f, m) for f, m in zip(freqs, mags) if _finite(m)]
        if not fin:
            continue
        peak = max(m for _, m in fin)
        # -3 dB point: first frequency where |H| drops below peak/sqrt(2).
        thresh = peak / math.sqrt(2.0)
        corner = None
        for f, m in fin:
            if m < thresh:
                corner = f
                break
        out["traces"][p] = {"peak_mag": _round(peak),
                            "f_-3dB": _round(corner, 3) if corner else None}
        if corner:
            reading_lines.append(f"{p}: -3 dB corner ~{_round(corner, 3)} Hz "
                                 f"(peak |H|={_round(peak)})")
        else:
            reading_lines.append(f"{p}: peak |H|={_round(peak)} (no -3 dB rolloff in band)")
    return out


def _zone_polygon(doc: dict, rect_or_polygon: Optional[list]) -> list | None:
    """Resolve the ecad_add_zone shape arg into a polygon ``[[x, y], ...]`` (>=3 vertices).

    Accepts a rect ``[x0, y0, x1, y1]`` (4 scalars -> 4 corners), an explicit polygon
    ``[[x, y], ...]``, or ``None`` (flood the board outline's bounding box — the whole-board pour).
    Returns None if no usable polygon can be derived (e.g. None + no outline)."""
    if rect_or_polygon is None:
        bbox = _outline_bbox(doc)
        if bbox is None:
            return None
        x0, y0, x1, y1 = bbox
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    # explicit polygon: list of [x, y] points
    if (isinstance(rect_or_polygon, list) and len(rect_or_polygon) >= 3
            and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in rect_or_polygon)):
        return [[float(p[0]), float(p[1])] for p in rect_or_polygon]
    # rect: [x0, y0, x1, y1]
    if (isinstance(rect_or_polygon, list) and len(rect_or_polygon) == 4
            and all(isinstance(v, (int, float)) for v in rect_or_polygon)):
        x0, y0, x1, y1 = (float(v) for v in rect_or_polygon)
        lo_x, hi_x = sorted((x0, x1))
        lo_y, hi_y = sorted((y0, y1))
        return [[lo_x, lo_y], [hi_x, lo_y], [hi_x, hi_y], [lo_x, hi_y]]
    return None


def _outline_bbox(doc: dict) -> tuple[float, float, float, float] | None:
    """Bounding box (minx, miny, maxx, maxy) of the board outline shapes, or None if empty."""
    xs: list[float] = []
    ys: list[float] = []
    for sh in doc.get("outline", {}).get("shapes", []) or []:
        for key in ("p1", "p2", "center"):
            p = sh.get(key)
            if isinstance(p, (list, tuple)) and len(p) == 2:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _summarize_sweep_steps(runs: list, atype: str, probe: Optional[str]):
    """Compact a `.step` sweep into (trend_sentence, sweep_table, chosen_probe).

    For each swept value we pull a single scalar for the chosen probe (the op node voltage, or the
    final transient value, or the peak AC magnitude) and build a plain-English 'rises/falls/holds'
    sentence so a weak model can read the trend without parsing the raw runs."""
    # Pick the probe to report: the explicit one, else the first probe that has finite values.
    def _scalar(run: dict, p: str):
        if atype == "op":
            return run.get("op", {}).get(p)
        if atype == "tran":
            tr = run.get("tran", {}).get("traces", {}).get(p, [])
            return tr[-1] if tr else None
        if atype == "ac":
            mags = run.get("ac", {}).get("mag", {}).get(p, [])
            fin = [m for m in mags if _finite(m)]
            return max(fin) if fin else None
        if atype == "dc":
            tr = run.get("dc", {}).get("traces", {}).get(p, [])
            fin = [v for v in tr if _finite(v)]
            return fin[-1] if fin else None
        return None

    chosen = probe
    if chosen is None:
        # find the probe name present across runs with the most finite values
        names: list[str] = []
        for run in runs:
            for key in ("op",):
                names += list(run.get(key, {}).keys())
            for key in ("tran", "dc"):
                names += list(run.get(key, {}).get("traces", {}).keys())
            for key in ("ac",):
                names += list(run.get(key, {}).get("mag", {}).keys())
        # prefer a node voltage
        v_names = [n for n in names if str(n).startswith("V(")]
        ordered = v_names or names
        for cand in ordered:
            if any(_finite(_scalar(run, cand)) for run in runs):
                chosen = cand
                break

    table: list[dict] = []
    series: list[tuple[float, float]] = []
    for run in runs:
        val = run.get("value")
        scal = _scalar(run, chosen) if chosen else None
        row = {"value": _round(val), chosen or "probe": _round(scal)}
        table.append(row)
        if _finite(val) and _finite(scal):
            series.append((float(val), float(scal)))

    if not series or chosen is None:
        return "", table, chosen
    first_v, first_y = series[0]
    last_v, last_y = series[-1]
    delta = last_y - first_y
    unit = "" if atype == "ac" else " V"
    if abs(delta) < 1e-9:
        verb = "holds at"
        trend = f"{chosen} {verb} {_round(first_y)}{unit} as {_round(first_v)} -> {_round(last_v)}"
    else:
        verb = "rises" if delta > 0 else "falls"
        trend = (f"{chosen} {verb} {_round(first_y)}{unit} -> {_round(last_y)}{unit} "
                 f"as the swept value goes {_round(first_v)} -> {_round(last_v)}")
    return trend, table, chosen


def _default_out_dir(name: str):
    from engine import paths
    return paths.model_dir(f"ecad_{name}")
