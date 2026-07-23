"""ECAD schematic MCP tools (T-RF8 split of ecad_tools.py): ecad_new_schematic, find_part, place_symbol, wire, add_power, label, status, run_erc, update_from_sch."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
import json

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
    ecad_session,
    lib_search,
    netlist_mod,
)

def register_ecad_sch_tools(mcp: FastMCP) -> None:
    """ECAD schematic MCP tools (T-RF8 split of ecad_tools.py): ecad_new_schematic, find_part, place_symbol, wire, add_power, label, status, run_erc, update_from_sch."""


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

    @mcp.tool()
    def ecad_find_part(query: str, limit: int = 12) -> str:
        """Search the component library and return RANKED real parts so you place an actual part
        instead of inventing a libid.

        For each hit: its ``libid`` (symbols) or ``fpid`` (footprints), name, description, and
        PIN COUNT (symbols) so you know how many pins you'll have to wire. Hits are sorted best
        first. Pass the exact ``libid``/``fpid`` of a hit to ecad_place_symbol / a footprint op.
        """
        from engine.ecad.library import resolve as lib_resolve  # STAYS (T-RF20): no ecad_library RPC; pin-count lookup
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
                          angle: float = 0.0, footprint: str = "", doc_id: str = "",
                          sim_params: str = "") -> str:
        """Place a symbol on the active schematic and return its assigned REF plus its full PIN
        LIST (name/number/type/world-xy) and the count of pins still unconnected.

        The pin list is the context you need to wire: each pin's world ``xy`` is where a wire must
        reach, and ``net`` tells you what it already touches. ``value`` matters for simulation —
        a resistor needs e.g. '1k', a voltage source '5' — set it now or with ecad_set later.
        ``ref`` auto-assigns (R1, R2, ...) if omitted. ``footprint`` (an fpid like
        'Resistor_SMD:R_0603') makes the part board-ready for ecad_update_from_sch; omit it and a
        sane default for the part's libid is used automatically when you sync to a board.

        ``sim_params`` (JSON object string) attaches per-instance simulation/engineering data.
        Its ``"eng"`` sub-object overrides the library engineering metadata for THIS instance
        (partial dict — e.g. '{"eng": {"power_w": {"typ": 0.02}}}'); see
        contracts/ecad/boardphys.md. Overrides flow onto the board footprint at sync and into
        the aicad.boardphys/1 sidecar + ecad_status power totals.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        # E7: Validate libid resolves BEFORE dispatching the op. An unresolvable libid would
        # silently produce a 2-pin stub with ok:true, which then corrupts the netlist. Fail fast
        # with a clear error and direct the user to ecad_find_part.
        from engine.ecad import library as _lib  # STAYS (T-RF20): no ecad_library RPC
        if _lib.resolve(libid) is None:
            keyword = libid.split(":")[-1] if ":" in libid else libid
            return _err(
                f"unknown library part {libid!r} — use ecad_find_part to find a real libid",
                hint=(f"'{libid}' is not in the component library. "
                      f"Run ecad_find_part('{keyword}') to discover the correct libid."),
                next_actions=[
                    f"ecad_find_part('{keyword}')  # search for the correct libid",
                    "ecad_find_part('resistor')  # browse common parts",
                ],
                libid=libid,
            )
        op: dict = {"op": "sch_add_symbol", "libid": libid, "pos": [float(x), float(y)],
                    "angle": float(angle)}
        if ref:
            op["ref"] = ref
        if value:
            op["value"] = value
        if footprint:
            op["footprint"] = footprint
        if sim_params:
            try:
                sp = json.loads(sim_params)
            except (TypeError, ValueError) as e:
                return _err(f"sim_params is not valid JSON: {e}",
                            hint='Pass a JSON object string, e.g. '
                                 '\'{"eng": {"power_w": {"typ": 0.02}}}\'.',
                            next_actions=["ecad_place_symbol(...)  # retry with valid JSON"])
            if not isinstance(sp, dict):
                return _err("sim_params must be a JSON object",
                            hint='e.g. \'{"eng": {"power_w": {"typ": 0.02}}}\'.')
            op["sim_params"] = sp
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
        payload = {
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
            ) + ["ecad_status()  # see the whole design + what's still unconnected",
                 "render_schematic_ascii()  # full sheet ASCII + grid for edits"],
        }
        _attach_schematic_ascii_readback(payload, doc_id=session.doc_id,
                                         focus=list(sym.get("pos") or []))
        return _dumps(payload)



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
            resp = _rpc("ecad_update_from_sch",
                        {"board_doc_id": pcb_id, "sch_doc_id": sch_id})
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
