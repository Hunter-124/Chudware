"""Cross-document back-annotation: populate a PCB from a schematic's netlist.

This is the seam between the two ECAD documents. ``pcb_update_from_sch`` (ecad_ops.md §5) is *not*
a pure single-document op — it needs the schematic's compiled netlist AND the board at once — so the
real reconciliation lives here (called from the session/RPC layer, see
:func:`engine.ecad.rpc._ecad_update_from_sch`), not inside :mod:`engine.ecad.ops`'s single-doc apply.

:func:`update_board_from_sch` is a pure-ish function: it operates on a **deep copy** of the board
(never mutates the caller's dict) and returns the new board + a structured ``report``. The board it
returns is guaranteed to pass :func:`engine.ecad.schema.validate_board`.

Reconciliation algorithm (mirrors KiCad "Update PCB from Schematic"):

  1. For every netlist component that is *on-board* and has a non-empty ``footprint``, resolve the
     footprint def via ``library.resolve_footprint(fpid)``. Components that are ``dnp`` or have no
     footprint (sim-only parts like ``V_source``, power symbols like ``GND``/``VCC``) are skipped —
     they never get a physical footprint. An unresolved fpid is reported (no footprint added).
  2. If the board has no footprint for that component's ``ref`` yet, ADD one (auto-placed on a simple
     grid from ``place_origin``, side ``top``), linked back via ``linked_symbol`` + ``ref``.
  3. For each pad, bind ``pad['net'] = component.pins[pad.pin or pad.number]`` — the pad's
     ``pin``/``number`` maps to a schematic pin, whose net comes from the netlist. Any net referenced
     this way that is missing from ``board['nets']`` is registered (class ``default``).
  4. ref/value on existing footprints are refreshed from the netlist. Footprints whose ref is no
     longer in the netlist are *reported* as removed (report-only) unless ``prune=True``, in which
     case they are deleted.
  5. Idempotent: running the result through this function again yields an empty ``added``/``updated``
     report (and the same board).

The ``report`` shape::

    {"added": [ref, ...], "updated": [ref, ...], "removed": [ref, ...],
     "unresolved": [[ref, fpid], ...], "nets_bound": N, "linked_sch": sch_id|None}
"""

from __future__ import annotations

import copy
from typing import Any

from engine.ecad import schema


# Grid placement pitch (mm) for auto-placing newly-added footprints. Coarse, like ops.pcb_autoplace.
_GRID_PITCH = 12.7
_GRID_COLS = 8

# Default footprint by libid, so a schematic component that carries a real part but NO explicit
# footprint still lands a physical footprint on update-from-sch (Bug E6: a manually-placed R/C/LED
# has no footprint by default, so the board got 0 footprints). Mirrors the orchestrator's
# _DEFAULT_FOOTPRINTS. Sources / power symbols are intentionally absent (never on a board). Every
# fpid here resolves in the clean-room starter library.
_DEFAULT_FOOTPRINTS = {
    "Device:R": "Resistor_SMD:R_0603",
    "Device:C": "Capacitor_SMD:C_0603",
    "Device:D": "Diode_SMD:D_SMA",
    "Device:LED": "LED_SMD:LED_0805",
    "Device:Q_NPN": "Package_TO_SOT_SMD:SOT-23",
    "Device:Q_PNP": "Package_TO_SOT_SMD:SOT-23",
    "Amplifier_Operational:OPAMP_dual": "Package_SO:SOIC-8",
    "Timer:NE555": "Package_DIP:DIP-8",
}


def _effective_fpid(comp: dict) -> str:
    """The footprint fpid to use for *comp*: its explicit ``footprint``, else a per-libid default.

    Returning a non-empty default for a known passive/active part is what makes a manually-built
    schematic board-ready without the model having to assign a footprint to every part (Bug E6)."""
    fpid = comp.get("footprint")
    if isinstance(fpid, str) and fpid.strip():
        return fpid.strip()
    return _DEFAULT_FOOTPRINTS.get(str(comp.get("libid", "")), "")


def _is_power_component(comp: dict) -> bool:
    """True for power-symbol netlist components (``#PWR`` refs / ``power:`` libids).

    Power symbols carry connectivity (they name a net) but are never placed on the board. They also
    have no footprint, so this is belt-and-suspenders next to the footprint-less skip.
    """
    ref = str(comp.get("ref", ""))
    libid = str(comp.get("libid", ""))
    return ref.startswith("#PWR") or ref.startswith("#") or libid.startswith("power:")


def _on_board(comp: dict) -> bool:
    """Components default to on-board; an explicit ``on_board: False`` excludes them.

    The netlist (engine.ecad.netlist) does not currently surface ``on_board`` per component, so a
    missing key means "on board". When it is present we honor it.
    """
    return bool(comp.get("on_board", True))


def _placeable(comp: dict) -> bool:
    """A component gets a physical footprint iff: on-board, not dnp, not a power symbol, and it
    resolves to a non-empty fpid (its explicit ``footprint`` OR a per-libid default — Bug E6)."""
    if comp.get("dnp"):
        return False
    if not _on_board(comp):
        return False
    if _is_power_component(comp):
        return False
    return bool(_effective_fpid(comp))


def _resolve_footprint(library: Any, fpid: str) -> dict | None:
    """Resolve a footprint def via the supplied library (or the module-level default singleton).

    Best-effort: any resolver exception is swallowed (treated as unresolved) so a flaky library
    never aborts the whole sync.
    """
    try:
        if library is not None and hasattr(library, "resolve_footprint"):
            return library.resolve_footprint(fpid)
        from engine.ecad import library as _lib  # default singleton
        return _lib.resolve_footprint(fpid)
    except Exception:  # noqa: BLE001 - library is best-effort
        return None


def _ensure_net(board: dict, net: str) -> bool:
    """Register *net* in ``board['nets']`` if missing. Returns True if it was added."""
    if not net:
        return False
    for n in board.get("nets", []):
        if isinstance(n, dict) and n.get("name") == net:
            return False
    board.setdefault("nets", []).append({"name": net, "class": "default"})
    return True


def _fp_for_ref(board: dict, ref: str, sym_id: str | None) -> dict | None:
    """Find an existing board footprint for a schematic component.

    Prefer the explicit ``linked_symbol`` back-link (survives ref renames); fall back to matching by
    ``ref``. Returns the footprint dict (a reference into *board*) or None.
    """
    fps = board.get("footprints", [])
    if sym_id:
        for fp in fps:
            if isinstance(fp, dict) and fp.get("linked_symbol") == sym_id:
                return fp
    if ref:
        for fp in fps:
            if isinstance(fp, dict) and fp.get("ref") == ref:
                return fp
    return None


def _pad_pin_key(pad: dict) -> str:
    """The schematic pin a pad maps to: its explicit ``pin`` field, else its pad ``number``."""
    pin = pad.get("pin")
    if pin is not None and str(pin) != "":
        return str(pin)
    return str(pad.get("number", ""))


def _bind_pads(pads: list[dict], comp_pins: dict, board: dict) -> tuple[int, int]:
    """Bind each pad's net from the component's pin->net map. Mutates pads + registers nets.

    Returns ``(changed_count, bound_count)`` where ``bound_count`` is pads that ended up on a real
    net (used for the report's ``nets_bound`` tally) and ``changed_count`` is how many pad nets
    actually changed value (drives the idempotency / "updated" signal).
    """
    changed = 0
    bound = 0
    for pad in pads:
        key = _pad_pin_key(pad)
        net = comp_pins.get(key)
        # Normalize empty string to None (an unconnected pad).
        new_net = net if (isinstance(net, str) and net) else None
        if new_net is not None:
            _ensure_net(board, new_net)
            bound += 1
        if pad.get("net") != new_net:
            pad["net"] = new_net
            changed += 1
    return changed, bound


def _new_footprint(board: dict, comp: dict, fpdef: dict, place_xy: list[float],
                   fpid: str = "") -> dict:
    """Build a board Footprint instance from a netlist component + resolved footprint def."""
    fid = schema.next_id(board, "fp")
    pads = copy.deepcopy(fpdef.get("pads", []) or [])
    return {
        "id": fid,
        "fpid": fpid or comp.get("footprint", ""),
        "ref": comp.get("ref", ""),
        "value": comp.get("value", "") or fpdef.get("value", ""),
        "pos": [float(place_xy[0]), float(place_xy[1])],
        "angle": 0.0,
        "side": "top",
        "pads": pads,
        "graphics": copy.deepcopy(fpdef.get("graphics", []) or []),
        "attr": fpdef.get("attr", "smd"),
        "linked_symbol": comp.get("sym", ""),
        # Carry the symbol's user fields (MPN, tolerance, etc.) onto the footprint so
        # fab outputs (BOM) and downstream pricing can read them. The symbol is the
        # source of truth for these; the update branch below refreshes them on re-sync.
        "fields": dict(comp.get("fields", {}) or {}),
        # Back-link to the symbol libid + per-instance eng overrides (T-FIN8) so the
        # metadata resolver (engine.ecad.metadata) can find electrical seeds/overrides
        # from the board document alone.
        "libid": comp.get("libid", ""),
        "eng_overrides": copy.deepcopy(comp.get("eng_overrides"))
                         if isinstance(comp.get("eng_overrides"), dict) else None,
    }


def update_board_from_sch(board: dict, netlist: dict, *, library: Any = None,
                          place_origin: Any = None, prune: bool = False) -> dict:
    """Reconcile *board* against a schematic *netlist* (back-annotation).

    Pure-ish: works on a deep copy of *board*; the caller's dict is never mutated. The returned
    board passes :func:`schema.validate_board`.

    Args:
        board: an ``aicad.pcb/1`` document dict.
        netlist: output of :func:`engine.ecad.netlist.build_netlist` (``components`` + ``nets``).
        library: an object with ``resolve_footprint(fpid)`` (e.g. a ``library.Library`` instance);
            ``None`` uses the module-level default singleton.
        place_origin: ``[x, y]`` grid origin for newly-placed footprints; defaults to the board
            ``origin``.
        prune: if True, delete board footprints whose ref is no longer in the netlist (they are
            always reported under ``removed`` regardless).

    Returns ``{"mutated_board": dict, "report": dict}``.
    """
    work = copy.deepcopy(board)
    components = list(netlist.get("components", []) or [])

    report: dict[str, Any] = {
        "added": [], "updated": [], "removed": [],
        "unresolved": [], "nets_bound": 0, "linked_sch": None,
    }

    # Record the back-link to the schematic if the netlist carries a doc id (informational).
    sch_id = netlist.get("id") or netlist.get("doc_id")
    if isinstance(sch_id, str) and sch_id:
        work["linked_sch"] = sch_id
        report["linked_sch"] = sch_id

    # Refs the schematic still owns (placeable components) — drives the removed-set computation.
    sch_refs = {str(c.get("ref", "")) for c in components if _placeable(c)}

    # Existing footprints already placed (count toward the grid offset so new parts don't overlap).
    placed_count = len(work.get("footprints", []))
    if place_origin is not None and len(place_origin) == 2:
        ox, oy = float(place_origin[0]), float(place_origin[1])
    else:
        origin = work.get("origin", [0.0, 0.0]) or [0.0, 0.0]
        ox, oy = float(origin[0]), float(origin[1])

    grid_i = placed_count  # next free grid slot index

    # ── pass 1: add/update footprints + bind pads from the netlist ──
    # Deterministic order: sort components by ref so ids/placement are stable across runs.
    for comp in sorted(components, key=lambda c: (str(c.get("ref", "")), str(c.get("sym", "")))):
        if not _placeable(comp):
            continue
        ref = str(comp.get("ref", ""))
        sym_id = comp.get("sym")
        fpid = _effective_fpid(comp)   # explicit footprint, else a per-libid default (Bug E6)
        comp_pins = comp.get("pins", {}) or {}

        fp = _fp_for_ref(work, ref, sym_id)
        if fp is None:
            # ADD: resolve the footprint def, then place on the grid.
            fpdef = _resolve_footprint(library, fpid)
            if not isinstance(fpdef, dict):
                report["unresolved"].append([ref, fpid])
                continue
            place_xy = [ox + (grid_i % _GRID_COLS) * _GRID_PITCH,
                        oy + (grid_i // _GRID_COLS) * _GRID_PITCH]
            grid_i += 1
            fp = _new_footprint(work, comp, fpdef, place_xy, fpid=fpid)
            work.setdefault("footprints", []).append(fp)
            changed, bound = _bind_pads(fp["pads"], comp_pins, work)
            report["nets_bound"] += bound
            report["added"].append(ref)
        else:
            # UPDATE: refresh ref/value + the back-link, then rebind pads.
            dirty = False
            if fp.get("ref") != ref:
                fp["ref"] = ref
                dirty = True
            new_val = comp.get("value", "")
            if new_val and fp.get("value") != new_val:
                fp["value"] = new_val
                dirty = True
            if sym_id and fp.get("linked_symbol") != sym_id:
                fp["linked_symbol"] = sym_id
                dirty = True
            # Refresh symbol-derived user fields (MPN, tolerance, ...) — symbol wins.
            # Merge (not replace) so any footprint-only field is preserved.
            comp_fields = dict(comp.get("fields", {}) or {})
            if comp_fields:
                merged = dict(fp.get("fields", {}) or {})
                merged.update(comp_fields)
                if merged != (fp.get("fields") or {}):
                    fp["fields"] = merged
                    dirty = True
            # Refresh the eng metadata back-links (T-FIN8) — symbol wins.
            if comp.get("libid") and fp.get("libid") != comp.get("libid"):
                fp["libid"] = comp.get("libid")
                dirty = True
            new_ov = comp.get("eng_overrides") if isinstance(comp.get("eng_overrides"), dict) else None
            if new_ov is not None and fp.get("eng_overrides") != new_ov:
                fp["eng_overrides"] = copy.deepcopy(new_ov)
                dirty = True
            changed, bound = _bind_pads(fp.get("pads", []), comp_pins, work)
            report["nets_bound"] += bound
            if changed or dirty:
                report["updated"].append(ref)

    # ── pass 2: footprints on the board whose ref is no longer in the schematic ──
    survivors = []
    for fp in work.get("footprints", []):
        ref = str(fp.get("ref", ""))
        # Only consider footprints that carry a ref and look schematic-linked. A footprint with no
        # ref (a mechanical/decorative part) is left alone.
        if ref and ref not in sch_refs:
            report["removed"].append(ref)
            if prune:
                continue  # drop it
        survivors.append(fp)
    work["footprints"] = survivors

    report["added"] = sorted(set(report["added"]))
    report["updated"] = sorted(set(report["updated"]))
    report["removed"] = sorted(set(report["removed"]))
    report["unresolved"] = sorted(report["unresolved"])

    return {"mutated_board": work, "report": report}


if __name__ == "__main__":  # pragma: no cover - smoke
    import json
    import sys
    from engine.ecad import netlist as _netlist

    if len(sys.argv) > 2:
        sch = json.load(open(sys.argv[1]))
        brd = json.load(open(sys.argv[2]))
    else:
        sch = schema.new_schematic()
        brd = schema.new_board()
    nl = _netlist.build_netlist(sch)
    out = update_board_from_sch(brd, nl)
    print(json.dumps(out["report"], indent=2, sort_keys=True))
